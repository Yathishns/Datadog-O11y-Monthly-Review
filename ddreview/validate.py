"""Self-checks on the notebook, the facts and any LLM-polished text.

Errors stop the build (the notebook is still written as <name>.INVALID.md); warnings go to validation.txt and the build
summary; only aggregation caveats appear in the notebook's appendix.
- rates: every {num, den, pct} in facts recomputes within 0.005 and failed <= calls
- reconciliation: each domain sums its counted services; the domains on the review's volume tier sum to the totals;
  noise + business = all
- markdown: one H1, no H4+, every table has a separator and consistent column counts, no unresolved placeholders,
  no None / nan / TODO tokens, every mermaid fence closed and lint-clean, no raw HTML outside Mermaid
- tier guard: per-day, per-week and per-hour figures only when the window allows them
- provenance: every number in the Markdown outside code is in the ledger (a warning on the generated notebook; an error
  when checking LLM output with --text). --text only flags a figure that no facts.json value prints to in any format:
  single digits, the round numbers in _EXEMPT, numbers glued to letters ('2.1x') and any value another fact happens to
  format to all pass. --against ORIGINAL.md is the strong check: per section (matched by heading), the numbers in the
  polished text must be exactly the numbers of the original section — none added, changed or dropped.
"""
import re
from collections import Counter

from . import mermaid
from .recipes import tag_value
from .narrative import WORDS, LEDGER

_FENCE = re.compile(r"^```(\w*)\n(.*?)^```", re.S | re.M)
_NUM = re.compile(r"(?<![\w/.\-@#:])\d[\d,]*(?:\.\d+)?(?![\w/\-:])")
_DATEISH = re.compile(r"^\d{1,2}$")
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_SCALED = re.compile(r"\d[\d,.]*\s+(?:\w+\s+){0,2}(a day|per day|a week|per week|daily|an hour|per hour|hourly)\b")
_PLACEHOLDER = re.compile(r"\{[a-z_][a-z0-9_]*\}|\{\{|\}\}")
_EXEMPT = {"10", "28", "30", "100", "95", "99", "99.5", "50", "20", "15", "5", "25", "75", "12"}


def check_facts(F):
    errors, warnings = [], []

    def walk(obj, path):
        if isinstance(obj, dict):
            if set(obj) >= {"num", "den", "pct"}:
                num, den, pct = obj["num"], obj["den"], obj["pct"]
                expect = round(100.0 * num / den, 4) if den else 0.0
                if abs(expect - pct) > 0.005:
                    errors.append(f"rate at {path} does not recompute: {num}/{den} -> {expect} but pct={pct}")
                if num > den:
                    errors.append(f"rate at {path} has num > den ({num} > {den})")
            for k, v in obj.items():
                walk(v, f"{path}.{k}")
        elif isinstance(obj, list):
            for i, v in enumerate(obj[:200]):
                walk(v, f"{path}[{i}]")
    walk({k: v for k, v in F.items() if k not in ("tables", "mom")}, "facts")
    services = F["services"]
    for key, d in F["domains"].items():
        counted = d.get("counted_services", d.get("services", []))
        want = sum(services[n]["requests"]["all"] for n in counted if n in services)
        if abs(want - d["requests"]["all"]) > 0.01 * max(want, 1):
            errors.append(f"domain {key} ({d['requests']['all']:,}) does not reconcile with its counted services ({want:,})")
    t = F["totals"]
    on_tier = sum(d["requests"]["all"] for d in F["domains"].values() if d.get("volume_tier", t.get("tier")) == t.get("tier"))
    if abs(on_tier - t["requests"]) > 0.01 * max(on_tier, 1):
        errors.append(f"domains on the {t.get('tier')} tier ({on_tier:,}) do not reconcile with the totals ({t['requests']:,})")
    if t["business_requests"] + t["noise_requests"] != t["requests"]:
        errors.append("noise + business requests do not equal all requests")
    for name, s in F["services"].items():
        if s["p95_s"] is not None and s["p95_s"] < 0:
            errors.append(f"negative p95 on {name}")
    return errors, warnings


def _blank_known_names(text, F):
    """Replace service and display names with spaces (same length, so line numbers hold) before token checks, so a
    service literally named 'None' or 'brace{svc}' is not mistaken for a formatting slip."""
    names = set(F.get("services", {})) | {s.get("display") for s in F.get("services", {}).values()}
    for nm in sorted((x for x in names if x and len(x) >= 3), key=len, reverse=True):
        text = text.replace(nm, " " * len(nm))
    return text


def check_markdown(md, F=None, strict_numbers=False, fragment=False):
    """``fragment``: an LLM answer that replaces part of a notebook (checked with --against), so it need not carry the H1."""
    errors, warnings = [], []
    if md.startswith("﻿"):
        errors.append("file starts with a BOM")
    if "\r\n" in md:
        warnings.append("CRLF line endings (LF expected)")
        md = md.replace("\r\n", "\n")
    h1 = re.findall(r"^# ", md, re.M)
    if len(h1) > 1 or (len(h1) == 0 and not fragment):
        errors.append(f"{len(h1)} H1 headings (exactly one expected" + (", or none in a fragment)" if fragment else ")"))
    if re.search(r"^#{4,} ", md, re.M):
        errors.append("H4 or deeper heading (Datadog's TOC supports H1-H3)")
    fences = list(_FENCE.finditer(md))
    if md.count("```") % 2:
        errors.append("an unclosed code fence")
    if re.search(r"[^\n]```", md):
        errors.append("a code fence that does not start a line (Datadog shows it as text)")
    ledger = set(LEDGER.numbers)
    if F is not None:
        ledger |= set((F.get("meta") or {}).get("ledger") or [])
    for m in fences:
        if m.group(1) == "mermaid":
            errors.extend(f"mermaid: {p}" for p in mermaid.lint(m.group(2)))
            if F is not None and m.group(2).startswith("pie"):
                for v in re.findall(r'^\s*"[^"]*"\s*:\s*(\d+)$', m.group(2), re.M):
                    if v not in ledger and v.replace(",", "") not in ledger:
                        (errors if strict_numbers else warnings).append(f"pie value {v} is not in the ledger")
    body = _FENCE.sub(lambda m: "\n" * m.group(0).count("\n"), md)          # keep line numbers true to the file
    body_no_code = re.sub(r"`[^`\n]*`", "", body)
    scan = _blank_known_names(body_no_code, F) if F is not None else body_no_code
    for m in _PLACEHOLDER.finditer(scan):
        line = scan[: m.start()].count("\n") + 1
        errors.append(f"unresolved placeholder {m.group(0)} at line {line}")
    for tok in ("nan", "inf", "TODO", "_todo", "REVIEW"):
        for m in re.finditer(r"(?<![\w.])" + re.escape(tok) + r"(?![\w])", scan):
            line = scan[: m.start()].count("\n") + 1
            errors.append(f"token '{tok}' at line {line}")
    for m in re.finditer(r"(\| None \||[:=] None\b|\bNone (s|ms|%|days|minutes)\b|\bNone,|\(None\)|\bNone\s*[–→-]|[–→-]\s*None\b)", scan):
        line = scan[: m.start()].count("\n") + 1
        errors.append(f"a None value was printed at line {line}")
    if re.search(r"<(?!br/?>)[a-zA-Z]+[^>]*>", body_no_code):
        warnings.append("raw HTML outside Mermaid labels (Datadog's Markdown may not render it)")
    lines = body.split("\n")
    i = 0
    while i < len(lines):
        if lines[i].startswith("|"):
            block = []
            while i < len(lines) and lines[i].startswith("|"):
                block.append(lines[i])
                i += 1
            cols = block[0].count("|") - 1
            if len(block) < 2 or not re.match(r"^\|(---\|)+$", block[1]):
                errors.append(f"table without a separator row near: {block[0][:60]}")
            for row in block[1:]:
                if row.count("|") - 1 != cols:
                    errors.append(f"table row with {row.count('|') - 1} cells instead of {cols}: {row[:70]}")
            continue
        i += 1
    size = len(md.encode("utf-8"))
    if size > 60 * 1024:
        warnings.append(f"notebook is {size // 1024} KB; Datadog imported 26 KB without trouble, larger files are untested")
    if F is not None:
        if not F["window"].get("per_day_allowed"):
            for m in _SCALED.finditer(body_no_code):
                line = body_no_code[: m.start()].count("\n") + 1
                errors.append(f"a figure per day/week/hour at line {line} although the window is not proved: '{m.group(0)}'")
        unknown = []
        numbers_text = body_no_code
        for s in sorted((F.get("meta") or {}).get("config_text") or [], key=len, reverse=True):
            numbers_text = re.sub(re.escape(s), lambda m: " " * len(m.group(0)), numbers_text, flags=re.I)
        for line_no, line in enumerate(numbers_text.split("\n"), 1):
            if line.startswith("|") and re.match(r"^\|\s*\d+\s*\|", line):
                line = re.sub(r"^\|\s*\d+\s*\|", "| |", line)          # table index column
            for m in _NUM.finditer(line):
                tok = m.group(0)
                if tok in ledger or tok.replace(",", "") in ledger:
                    continue
                if re.match(r"^(19|20)\d\d,?$", tok) or len(tok) == 1 or tok in _EXEMPT:
                    continue
                after = line[m.end():m.end() + 5].strip()
                if _DATEISH.match(tok) and any(after.startswith(mo) for mo in _MONTHS):
                    continue
                if re.match(r"^\d+\.\s", line.strip()) and line.strip().startswith(tok + "."):
                    continue                                           # numbered list
                unknown.append(f"line {line_no}: {tok} — {line.strip()[:80]}")
        if unknown:
            (errors if strict_numbers else warnings).extend(f"number not in the ledger: {u}" for u in unknown[:40])
    return errors, warnings


def check_findings(body, F):
    errors = []
    ids = [f["id"] for f in body]
    if len(ids) != len(set(ids)):
        errors.append("duplicate finding ids")
    for f in body:
        if f["severity"] not in ("red", "amber", "info"):
            errors.append(f"{f['id']}: bad severity {f['severity']}")
        for a in f["actions"]:
            if not a.get("text") or a.get("horizon") not in ("now", "next", "later"):
                errors.append(f"{f['id']}: action without text or horizon")
        for w, q, note in f["live_graph"]:
            if w != "note" and "service:" in q:
                m = re.search(r"service:(\"[^\"]+\"|[^,}*]+)(\*?)", q)
                svc = m.group(1).strip('"') if m else None
                tags = {s_.get("metric_service") for s_ in F["services"].values()} | {r.get("metric_service") for rows in F["resources"].values() for r in rows}
                tags.discard(None)
                known = set(F["services"]) | {tag_value(s) for s in F["services"]} | tags | {tag_value(s) for s in tags}
                if svc and m.group(2):
                    ok = any(k.startswith(svc) for k in known)
                else:
                    ok = not svc or svc in known
                if not ok:
                    errors.append(f"{f['id']}: live graph references unknown service {svc}")
    return errors



# ---------------------------------------------------------------- polished text against the original
_ANY_NUM = re.compile(r"(?<![\w.])-?\d[\d,]*(?:\.\d+)?")


def _sections(md):
    """{key: text} per H2/H3 section, with fences, code spans and numbered-list markers removed. 'Finding 3' headings key
    on the finding number so a polished copy may drop the severity icon or re-word the title."""
    md = _FENCE.sub(lambda m: "" if m.group(1) == "mermaid" else m.group(2), md)      # other fenced text is still text
    out, key, buf = {}, "_preamble", []

    def flush():
        if buf:
            out[key] = out.get(key, "") + "\n".join(buf)
    for line in md.split("\n"):
        if not line.startswith("#"):
            line = re.sub(r"\bFindings?\s+\d+(\s*(,|and)\s*\d+)*", "Finding", line)      # a cross-reference, not a figure
        m = re.match(r"^#{2,3}\s+(.*)$", line)
        if m:
            flush()
            h = m.group(1)
            fm = re.search(r"Finding\s+(\d+)", h)
            key = f"finding {fm.group(1)}" if fm else re.sub(r"[^a-z0-9 ]+", "", h.lower()).strip()
            # the heading's own figures ('refunds fail 33.0 %') belong to the section; its 'Finding N' does not
            buf = [re.sub(r"`[^`\n]*`", "", re.sub(r"Finding\s+\d+", "", h))]
            continue
        if line.startswith("# "):
            continue
        line = re.sub(r"`[^`\n]*`", "", line)
        line = re.sub(r"^\s*\d+\.\s", "", line)
        buf.append(line)
    flush()
    return out


_WORD_NUM = {w: k for k, w in WORDS.items() if k}
_WORD_ALT = "|".join(sorted((re.escape(w) for w in _WORD_NUM), key=len, reverse=True))
_ONE_IN = re.compile(rf"(?i)\bone in ({_WORD_ALT})\b")
_NUM_WORD = re.compile(rf"(?i)(?<![\w-])({_WORD_ALT})(?![\w-])")
# 'one' is a count only before what the kit counts (findings, services, ... or the verbs of the month-over-month lead)
_ONE_COUNT = re.compile(r"(?i)\s+(?:[a-z-]+\s+)?(dependenc|operation|finding|service|host|gap|endpoint|node|domain|feature|request|call)|\s+(is|was|persists|escalated|eased|closed|could|fell|had|now)\b")


def _tokens(text):
    """Numbers as written, plus every count the kit writes as a word ('seven persist', 'two of its features', 'One is new'),
    normalised to digits so 'six' and '6' compare equal; 'one in twelve' is one rate token."""
    toks = Counter(t.replace(",", "") for t in _ANY_NUM.findall(text))
    toks.update(f"one in {_WORD_NUM[m.group(1).lower()]}" for m in _ONE_IN.finditer(text))
    rate_spans = [m.span() for m in _ONE_IN.finditer(text)]
    for m in _NUM_WORD.finditer(text):
        w = m.group(1).lower()
        if any(a <= m.start() < b for a, b in rate_spans):
            continue                      # part of 'one in twelve'
        if w == "one" and (not _ONE_COUNT.match(text, m.end()) or re.search(r"(?i)\b(each|every|no|any|this|that|the|which|some|another|only)\s+$", text[max(0, m.start() - 12):m.start()])):
            continue
        toks[str(_WORD_NUM[w])] += 1
    return toks


def check_against(polished, original):
    """No number in a polished section may be absent from the same section of the original (an error: added or changed);
    a number of the original missing from the polished copy is a warning (a summary may leave figures out)."""
    errors, warnings = [], []
    ps, orig = _sections(polished.replace("\r\n", "\n")), _sections(original.replace("\r\n", "\n"))
    matched = 0
    for key, text in ps.items():
        if key == "_preamble" and not _tokens(text):
            continue
        if key not in orig:
            if _tokens(text):
                errors.append(f"section '{key}' is not in the original, so its numbers cannot be checked")
            continue
        matched += 1
        got, want = _tokens(text), _tokens(orig[key])
        for tok, k in sorted((got - want).items()):
            errors.append(f"section '{key}': {tok} appears {k} more time{'s' if k != 1 else ''} than in the original (added or changed)")
        for tok, k in sorted((want - got).items()):
            warnings.append(f"section '{key}': {tok} appears {k} fewer time{'s' if k != 1 else ''} than in the original (dropped or changed)")
    if not matched:
        errors.append("no section of the polished text matches a section of the original by heading")
    return errors, warnings
