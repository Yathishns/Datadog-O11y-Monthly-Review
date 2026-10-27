"""ddreview command line.

    python -m ddreview inspect  EXPORTS_DIR [--config org.json]
    python -m ddreview init     EXPORTS_DIR --org "Client name" [--config org.json] [--force]
    python -m ddreview build    EXPORTS_DIR --config org.json [--month "November 2026"] [--as-of 2026-11-19] [--out out] [--previous auto|PATH|none] [--window-days 7] [--max-findings 8] [--diagrams both|code|prompt] [--no-prompts] [--strict] [--force]
    python -m ddreview diff     PREVIOUS_FACTS.json CURRENT_FACTS.json [--config org.json]
    python -m ddreview validate NOTEBOOK.md --facts facts.json [--text] [--against ORIGINAL.md]

Exit codes: 0 ok · 1 validation errors, or any warning under --strict (the notebook is written as .INVALID.md) · 2 input error ·
3 config error.
"""
import argparse
import glob
import hashlib
import json
import os
import re
import sys
from datetime import datetime

from . import KIT_VERSION, aggregate, compare, config, detectors, mermaid, prompts, readers, render, validate
from .config import ConfigError
from .narrative import LEDGER


def _utf8_stdout():
    for stream in ("stdout", "stderr"):
        s = getattr(sys, stream)
        if hasattr(s, "reconfigure"):
            try:
                s.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


def write_text(path, text):
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


def dump_json(path, obj):
    """Write to a temporary file and rename, so a failure mid-write never leaves a truncated facts.json for next month."""
    text = json.dumps(obj, indent=1, sort_keys=True, ensure_ascii=False, default=str) + "\n"
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    os.replace(tmp, path)


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _iso_date(s):
    try:
        datetime.strptime(s, "%Y-%m-%d")
    except ValueError:
        raise argparse.ArgumentTypeError(f"--as-of must be YYYY-MM-DD, got {s!r}")
    return s


def _positive_int(s):
    try:
        v = int(s)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected a whole number, got {s!r}")
    if v <= 0:
        raise argparse.ArgumentTypeError("must be a positive number")
    return v


# ---------------------------------------------------------------- inspect
def cmd_inspect(a):
    cfg = _load_cfg(a.config)
    inv = readers.discover(a.exports, cfg["columns"].get("aliases"))
    if not inv:
        print(f"no .csv/.tsv/.txt files in {a.exports}")
        return 2
    print(f"ddreview {KIT_VERSION} — {len(inv)} file(s) in {a.exports}\n")
    for f in inv:
        print(f"{f['file']}")
        print(f"  kind: {f['kind']}" + ("  (duplicate of an earlier file — skipped)" if f["duplicate"] else "") + f"   delimiter: {f['delimiter']}   encoding: {f.get('encoding', 'utf-8')}")
        if f["kind"] == "unknown":
            if f.get("reason"):
                print(f"  -> {f['reason']}")
            else:
                print(f"  headers: {', '.join(f['headers'][:8])}")
                print("  -> not recognised. Expected one of: spans (Date, Service, Resource, Duration ...), endpoints (Name, Service, Request Count, Error Rate ...),")
                print("     services (Name, Requests, Monitors ...), inferred services (Intg ...), resource catalog (account, cloud_provider, resource_type ...)")
            continue
        for canon, idx in sorted(f["columns"].items()):
            print(f"  {canon:14} <- {f['headers'][idx]}")
        extras = [h for i, h in enumerate(f["headers"]) if i not in f["columns"].values()]
        if extras:
            print(f"  extras (unused): {', '.join(extras[:10])}")
    warnings = []
    try:
        F = _facts_without_month(inv, cfg, warnings)
    except ConfigError as e:
        print(f"config error: {e}")
        return 3
    except SystemExit as e:
        for w_ in warnings:
            print(f"warning: {w_}")
        print(f"\n{e}")
        return 2
    w = F["window"]
    print("\nwindow")
    if F["coverage"]["inputs"]["spans"]:
        s = w["spans"]
        if s.get("start_local"):
            print(f"  span sample: {s['rows']:,} rows, {s['start_local']} -> {s['end_local']} {s['tz']} ({aggregate.sample_length_text(s['minutes_text'])})" + ("  CAPPED export" if s["capped"] else ""))
        else:
            print(f"  span sample: {s['rows']:,} rows, no timestamps (pass --as-of or --month when building)")
        print(f"  entry rule: {F['coverage']['entry_rule']}  (entry spans: {sum(x['spans']['entry'] for x in F['services'].values()):,})")
    if F["coverage"]["inputs"]["endpoints"]:
        ag = w["aggregate"]
        print(f"  endpoints list: {ag['days']} days ({ag['source']}, confidence {ag['confidence']})" + ("; probes: " + "; ".join(f"{p['service']} {p['resource']} every {p['implied_interval_s']} s" for p in ag["proof"][:3]) if ag["proof"] else "")
              + (f"; the cadence also fits {', '.join(str(d) for d in ag['tied'] if d != ag['days'])} days" if len(ag.get("tied", [])) > 1 else ""))
    for i in F["meta"]["inputs"]:
        for col, u in i.get("units", {}).items():
            print(f"  {i['file']}: {col} unit = {u['unit']} ({u['evidence']})")
        skipped = {k: v for k, v in (i.get("skipped") or {}).items() if v}
        if skipped:
            print(f"  {i['file']}: skipped rows — " + ", ".join(f"{v:,} {k.replace('_', ' ')}" for k, v in skipped.items()))
    print("\nservices")
    for name, s in sorted(F["services"].items(), key=lambda kv: -kv[1]["requests"]["all"]):
        p95 = f"{s['p95_s']:.3f}" if s["p95_s"] is not None else (f"{s['p95_worst_s']:.3f}†" if s["p95_worst_s"] is not None else "-")
        fail = f"{s['err']['business']['pct']:>7.3f} %" if (F["coverage"]["failure_signal"] and s.get("failure_known", True)) else "not recorded"
        print(f"  {name:36} {s['role']:11} requests {s['requests']['all']:>12,}  fail {fail:>9}  p95 {p95:>9}  op {s['operation'] or '-'} ({s['operation_source']})")
    print("\ncapabilities unlocked: " + ", ".join(k for k, v in F["coverage"]["capabilities"].items() if v and v != "none"))
    skipped_dets = []
    avail = {"failure_signal": F["coverage"]["failure_signal"], "dependencies": F["coverage"]["capabilities"]["dependencies"] != "none",
             "request_anatomy": bool(F["coverage"]["capabilities"].get("request_anatomy")), "status_codes": F["coverage"]["capabilities"]["status_codes"], "previous": True}
    for d in detectors.DETECTORS:
        missing = [k for k in d["needs"] if not avail.get(k)]
        if missing:
            skipped_dets.append(f"{detectors.DISPLAY.get(d['name'], d['name'])} ({'; '.join(detectors.NEEDS_TEXT[m] for m in missing)})")
    skipped_dets.append("regressions since last month (runs once an earlier month's facts.json is in the output folder)")
    if skipped_dets:
        print("detectors that would be skipped: " + "; ".join(skipped_dets))
    if F["coverage"]["unlock_hints"]:
        print("to unlock more:")
        for h in F["coverage"]["unlock_hints"]:
            print(f"  - {h}")
    for w_ in warnings:
        print(f"warning: {w_}")
    if F["caveats"]:
        print("\ncaveats:")
        for c in F["caveats"]:
            print(f"  - {c}")
    return 0


# ---------------------------------------------------------------- init
def cmd_init(a):
    path = a.config or "org.json"
    existing = None
    if os.path.exists(path) and not a.force:
        _load_cfg(path)          # a broken existing config is reported as a config error, not a traceback
        existing = json.loads(config.read_text_file(path))
        print(f"{path} exists — merging (new services are listed under _new_since_last_init; use --force to overwrite)")
    cfg = _load_cfg(path if existing is not None else None)
    inv = readers.discover(a.exports, cfg["columns"].get("aliases"))
    warnings = []
    try:
        F = _facts_without_month(inv, cfg, warnings)
    except ConfigError as e:
        print(f"config error: {e}")
        return 3
    except SystemExit as e:
        for w_ in warnings:
            print(f"warning: {w_}")
        print(e)
        return 2
    draft, todos = config.draft_config(F, a.org, existing)
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    dump_json(path, draft)
    pdir = os.path.join(os.path.dirname(os.path.abspath(path)), "prompts")
    os.makedirs(pdir, exist_ok=True)
    write_text(os.path.join(pdir, "20-name-domains.md"), _name_domains_prompt(F, a.org))
    print(f"wrote {path}")
    print(f"wrote {os.path.join(pdir, '20-name-domains.md')} (beside the config; paste it into an LLM to get domain names, audiences and tiers as JSON)")
    print("\nto do before the first build:")
    for t in todos[:30]:
        print(f"  - {t}")
    if len(todos) > 30:
        print(f"  ... and {len(todos) - 30} more (search the file for _todo)")
    return 0


def _facts_without_month(inv, cfg, warnings):
    """inspect and init have no --month: date the facts from the data, and only when the exports carry no timestamps at all
    use a placeholder month, dropping the warnings and caveats that would mention it."""
    try:
        return aggregate.build_facts(inv, cfg, warnings=warnings)
    except SystemExit as e:
        if "no readable timestamps" not in str(e):
            raise
    del warnings[:]
    F = aggregate.build_facts(inv, cfg, month="January 2000", warnings=warnings)
    warnings[:] = [w for w in warnings if "month" not in w.lower()]
    F["caveats"] = [c for c in F["caveats"] if "reporting month" not in c]
    return F


def _name_domains_prompt(F, org):
    from .narrative import table
    rows = []
    has_stack = any(s["tech"] or s["operation"] for s in F["services"].values() if s["role"] == "entry")
    for name, s in sorted(F["services"].items(), key=lambda kv: -kv[1]["requests"]["all"]):
        if s["role"] != "entry":
            continue
        top = [r["redacted"] for r in F["resources"].get(name, [])[:5] if r["class"] == "business"]
        row = [name, f"{s['requests']['all']:,}", ", ".join(top)]
        if has_stack:
            row.insert(1, s["tech"] or s["operation"] or "?")
        rows.append(row)
    cols = ["Service", "Requests", "Busiest operations"] if not has_stack else ["Service", "Stack", "Requests", "Busiest operations"]
    return (f"# Name the domains for {org}\n\nBelow are the traced services of {org} with their request volume" + (", stack" if has_stack else "") + " and busiest operations (identifiers masked). "
            "Group them into business domains a leadership reader would recognise (for example \"Members — mobile app\", \"Clinics\", \"Payments\"), name who feels it when each domain breaks "
            "(the audience), and classify each domain's tier: customer (end users or revenue), clinical (patients), staff (internal users), internal (plumbing). "
            "Return only a JSON array matching this shape, one object per domain:\n\n"
            "```json\n[{\"key\": \"members\", \"label\": \"Members — mobile app\", \"icon\": \"👥\", \"audience\": \"Members\", \"tier\": \"customer\", \"services\": [\"^service-name$\"], \"description\": \"the member app's API\"}]\n```\n\n"
            + table(cols, rows) + "\n")


# ---------------------------------------------------------------- build
def cmd_build(a):
    cfg = _load_cfg(a.config)
    if a.org:
        cfg["org"]["name"] = a.org
        if not a.config:
            cfg["org"]["short"] = config.slug(a.org) or "org"
    elif not a.config:
        cfg["org"]["name"] = os.path.basename(os.path.abspath(os.path.join(a.exports, os.pardir))) or "org"
        cfg["org"]["short"] = config.slug(cfg["org"]["name"]) or "org"
    if a.max_findings is not None:
        cfg["report"]["max_findings"] = a.max_findings
    if a.diagrams:
        cfg["report"]["diagrams"] = a.diagrams
    if cfg["report"]["diagrams"] == "prompt" and a.no_prompts:
        print("--diagrams prompt needs the prompt files: drop --no-prompts, or use --diagrams code")
        return 2
    inv = readers.discover(a.exports, cfg["columns"].get("aliases"))
    unknown = [f["file"] + (f" ({f['reason']})" if f.get("reason") else "") for f in inv if f["kind"] == "unknown"]
    if unknown:
        print("not recognised (run `inspect` for the headers): " + "; ".join(unknown))
    warnings = []
    LEDGER.numbers.clear()            # one ledger per build, so builds in one process stay byte-identical
    try:
        F = aggregate.build_facts(inv, cfg, month=a.month, as_of=a.as_of, window_days=a.window_days, warnings=warnings)
    except ConfigError as e:
        print(f"config error: {e}")
        return 3
    except SystemExit as e:
        for w_ in warnings:
            print(f"warning: {w_}")
        print(e)
        return 2
    short = cfg["org"]["short"]
    out_dir = os.path.join(a.out, short, F["meta"]["month_key"])
    if os.path.exists(os.path.join(out_dir, "facts.json")) and not a.force:
        print(f"{out_dir} already holds a build — pass --force to overwrite it")
        return 2
    # previous month: explicit --previous > config 'previous' > auto
    prev = None
    choice = a.previous or cfg.get("previous") or "auto"
    if choice.strip().lower() in ("auto", "none"):
        choice = choice.strip().lower()
    if choice == "auto":
        candidates = sorted(p for p in glob.glob(os.path.join(a.out, short, "*", "facts.json")) if os.path.basename(os.path.dirname(p)) < F["meta"]["month_key"])
        prev_path = candidates[-1] if candidates else None
    elif choice == "none":
        prev_path = None
    else:
        prev_path = choice
        if not os.path.exists(prev_path):
            print(f"previous facts not found: {prev_path}")
            return 2
    if choice == "none":
        F["meta"]["previous_reason"] = "none"
    elif not prev_path:
        F["meta"]["previous_reason"] = "not_found"
        F["meta"]["analyst_notes"].append(f"no earlier month's facts.json was found under <--out>/{short}/ — if this is not the first review, pass --previous PATH")
    if prev_path:
        try:
            prev = compare.load_previous(prev_path, org_short=short, before_month_key=F["meta"]["month_key"])
        except SystemExit as e:
            print(e)
            return 2
        prev["meta"]["_path"] = prev_path
        prev = compare.apply_aliases(prev, cfg["services"].get("aliases"), F["node_groups"])
        compare.compare_facts(F, prev, cfg)
    body, also, healthy, estate, extra_also = detectors.run_all(F, cfg, prev)
    if prev:
        compare.compare_findings(F, prev, body, also, cfg)
    F["meta"]["findings_count"] = len(body)
    # diagrams
    diagrams, mermaid_problems, mmd_files = {}, [], []
    if cfg["report"]["diagrams"] not in ("both", "code"):
        mermaid.landscape(F, body, cfg)              # not drawn, but the landscape prompt folds the hosts it would leave out
    if cfg["report"]["diagrams"] in ("both", "code"):
        cand = {"landscape": mermaid.landscape(F, body, cfg), "pie": mermaid.request_share(F), "dependencies": mermaid.dependencies(F, body)}
        for f in body:
            if f["diagram"] and f["diagram"].startswith("inside:"):
                cand[f["diagram"]] = mermaid.inside(F, f["diagram"].split(":", 1)[1])
        for name, text in cand.items():
            if not text:
                continue
            problems = mermaid.lint(text)
            fname = {"landscape": "01-landscape", "pie": "02-request-share", "dependencies": "03-dependencies"}.get(name) or \
                "04-" + prompts.inside_prompt_name(name.split(":", 1)[1])[len("04-diagram-"):-len(".md")]
            mmd_files.append((fname, text))
            if problems:
                mermaid_problems.append(f"{fname}: " + "; ".join(problems))
                continue
            diagrams[name] = text
    # notebook
    md = render.render(F, body, also, healthy, estate, cfg, diagrams, extra_also)
    # only now, with the new notebook in hand, replace what an earlier build of this month wrote
    _clear_generated(out_dir, short, F["meta"]["month_key"])
    os.makedirs(out_dir, exist_ok=True)
    if mmd_files:
        os.makedirs(os.path.join(out_dir, "mermaid"), exist_ok=True)
        for fname, text in mmd_files:
            write_text(os.path.join(out_dir, "mermaid", fname + ".mmd"), text)
    F["findings"] = body
    F["also_noted"] = [{"id": f["id"], "severity": f["severity"], "line": f["summary"]["finding"], "detector": f["detector"], "subject": f["subject"], "metrics": f["metrics"], "title": f["title"],
                        "merged": f.get("merged", [])} for f in also]
    F["healthy"] = healthy
    F["meta"]["ledger"] = sorted(LEDGER.numbers)
    facts_path = os.path.join(out_dir, "facts.json")
    dump_json(facts_path, F)
    F["meta"]["facts_sha256"] = sha256_file(facts_path)
    # validation
    errors, warns = validate.check_facts(F)
    e2, w2 = validate.check_markdown(md, F)
    errors += e2 + validate.check_findings(body, F)
    warns += w2 + warnings + [f"mermaid diagram dropped: {p}" for p in mermaid_problems]
    invalid = bool(errors or (a.strict and warns))
    strict_failed = bool(a.strict and warns and not errors)
    name = f"{short}-observability-review-{F['meta']['month_key']}"
    md_path = os.path.join(out_dir, name + (".INVALID.md" if invalid else ".md"))
    write_text(md_path, md)
    if not a.no_prompts:
        prompts.write_all(F, body, cfg, os.path.join(out_dir, "prompts"), mask=cfg["prompts"].get("mask_resources", True))
    strict_line = [f"STRICT: warnings fail the build ({len(warns)} warning{'s' if len(warns) != 1 else ''})", ""] if a.strict else []
    write_text(os.path.join(out_dir, "validation.txt"), "\n".join(strict_line + ["ERRORS:"] + (errors or ["none"]) + ["", "WARNINGS:"] + (warns or ["none"])) + "\n")
    _write_summary(out_dir, F, body, also, healthy, md_path, facts_path, errors, warns, inv, strict=a.strict)
    _write_mapping_report(out_dir, F, inv)
    tables = len(re.findall(r"^\|(?:---\|)+$", md, re.M))
    print(f"\n{('INVALID (strict: ' + str(len(warns)) + ' warnings) — ') if strict_failed else ('INVALID — ' if invalid else '')}{md_path}")
    print(f"  {len(md.split()):,} words · {md.count('```mermaid')} diagrams · {tables} tables · {len(body)} findings ({sum(1 for f in body if f['severity'] == 'red')} red, {sum(1 for f in body if f['severity'] == 'amber')} amber) · {sum(1 for h in healthy if h.get('status', 'healthy') == 'healthy')} healthy domains")
    print(f"  facts: {facts_path}")
    if errors:
        print("  errors:")
        for e in errors[:20]:
            print(f"    - {e}")
    if warns:
        print(f"  warnings ({len(warns)}): see validation.txt")
        for w in warns[:6]:
            print(f"    - {w[:160]}")
    notes = F["meta"].get("analyst_notes") or []
    if notes:
        print("  for the analyst (not in the notebook):")
        for x in notes[:8]:
            print(f"    - {x}")
    return 1 if invalid else 0


def _clear_generated(out_dir, short, month_key):
    """Remove only what an earlier build of this month wrote (both notebook names, mermaid/*.mmd, prompts/*.md), so a
    --force rebuild never leaves a stale notebook or prompt behind; files a consultant added elsewhere survive."""
    for suffix in (".md", ".INVALID.md"):
        pth = os.path.join(out_dir, f"{short}-observability-review-{month_key}{suffix}")
        if os.path.isfile(pth):
            os.remove(pth)
    for sub, pattern in (("mermaid", "*.mmd"), ("prompts", "*.md")):
        for pth in glob.glob(os.path.join(out_dir, sub, pattern)):
            os.remove(pth)
        try:
            os.rmdir(os.path.join(out_dir, sub))          # only when empty: a folder holding the consultant's own files stays
        except OSError:
            pass


def _write_summary(out_dir, F, body, also, healthy, md_path, facts_path, errors, warns, inv, strict=False):
    lines = [f"ddreview {KIT_VERSION} build summary — {F['meta']['org'] or F['meta']['org_short']} · {F['meta']['month']} · as of {F['meta']['as_of']}", ""]
    lines.append("inputs:")
    for f in inv:
        skipped = sum((f.get("skipped") or {}).values()) if f.get("skipped") else 0
        lines.append(f"  {f['file']}: {f['kind']}" + (" (duplicate, skipped)" if f["duplicate"] else "") + f" · {f.get('rows') or 0:,} rows" + (f" kept of {f.get('raw_rows'):,} ({skipped:,} skipped)" if f.get("raw_rows") and skipped else "") + f" · sha256 {f['sha256'][:12]}")
    ag = F["window"]["aggregate"]
    ep = f"endpoints window {ag['days']} days ({ag['source']})" if ag.get("days") else "no Endpoints list"
    spans_w = F["window"]["spans"]
    if spans_w.get("minutes_text"):
        sp = f"span sample {aggregate.sample_length_text(spans_w['minutes_text'])}"
    elif spans_w.get("rows"):
        sp = f"span sample of {spans_w['rows']:,} rows, no timestamps"
    else:
        sp = "no span sample"
    lines.append(f"window: volumes tier {F['window']['volumes_tier']}; {ep}; {sp}")
    lines.append("capabilities: " + ", ".join(k for k, v in F["coverage"]["capabilities"].items() if v and v != "none"))
    lines.append("findings: " + ", ".join(f"{f['severity']} {f['id']}" for f in body))
    lines.append("also noted: " + ", ".join(f["id"] for f in also))
    lines.append("healthy: " + ", ".join(f"{h['domain']} ({h.get('status', 'healthy')})" for h in healthy))
    if F.get("mom"):
        m = F["mom"]["findings"]
        lines.append(f"since {F['mom']['previous']['month']}: {len(m['closed'])} closed/superseded/below threshold, {len(m.get('unmeasured', []))} not measured, {len(m['new'])} new, "
                     f"{len(m.get('first_measured', []))} first measured, {len(m['persisting'])} persisting, {len(m.get('demoted', []))} demoted, {len(m.get('promoted', []))} promoted, {len(F['mom']['moved'])} metrics moved")
    lines.append(f"validation: {len(errors)} errors, {len(warns)} warnings" + (" (STRICT: warnings fail the build)" if strict else ""))
    for x in F["meta"].get("analyst_notes") or []:
        lines.append(f"analyst note: {x}")
    lines.append(f"notebook sha256 {sha256_file(md_path)}")
    lines.append(f"facts sha256 {sha256_file(facts_path)}")
    write_text(os.path.join(out_dir, "build-summary.txt"), "\n".join(lines) + "\n")


def _write_mapping_report(out_dir, F, inv):
    lines = ["# How each export was read", ""]
    for f in inv:
        lines.append(f"## {f['file']} — {f['kind']}" + (" (duplicate, skipped)" if f["duplicate"] else "") + (f" ({f['reason']})" if f.get("reason") else ""))
        lines.append("")
        if f.get("encoding") and f["encoding"] != "utf-8":
            lines.append(f"- decoded as {f['encoding']} (not valid UTF-8)")
        for canon, idx in sorted(f.get("columns", {}).items()):
            lines.append(f"- `{canon}` <- `{f['headers'][idx]}`")
        for col, u in f.get("units", {}).items():
            lines.append(f"- unit of `{col}`: {u['unit']} ({u['evidence']})" + (" — inferred, set columns.units to override" if u.get("inferred") else ""))
        extras = [h for i, h in enumerate(f["headers"]) if i not in f.get("columns", {}).values()]
        if extras:
            lines.append(f"- unused columns: {', '.join(extras)}")
        skipped = {k: v for k, v in (f.get("skipped") or {}).items() if v}
        if skipped:
            lines.append("- skipped rows: " + ", ".join(f"{v:,} {k.replace('_', ' ')}" for k, v in skipped.items()))
        lines.append("")
    lines.append(f"Entry-span rule: {F['coverage']['entry_rule']}. Dependency graph method: {F['coverage']['graph_method']}.")
    write_text(os.path.join(out_dir, "mapping-report.md"), "\n".join(lines) + "\n")


# ---------------------------------------------------------------- diff
def cmd_diff(a):
    cfg = _load_cfg(a.config)
    F = _load_facts(a.current)
    if F is None:
        return 2
    if not a.config:
        cfg["thresholds"] = dict(cfg["thresholds"], **((F.get("meta") or {}).get("thresholds") or {}))    # the build's own thresholds
    try:
        prev = compare.load_previous(a.previous, org_short=F.get("meta", {}).get("org_short"), before_month_key=F.get("meta", {}).get("month_key"))
    except SystemExit as e:
        print(e)
        return 2
    prev["meta"]["_path"] = a.previous
    aliases = cfg["services"].get("aliases") if a.config else ((F.get("meta") or {}).get("aliases") or {})
    prev = compare.apply_aliases(prev, aliases, F.get("node_groups", {}))
    F.setdefault("caveats", [])
    mom = compare.compare_facts(F, prev, cfg)
    body = F.get("findings", [])
    compare.compare_findings(F, prev, body, F.get("also_noted", []), cfg)
    print(f"{prev['meta'].get('month')} -> {F['meta'].get('month')}   ({mom['comparable']['note'] or 'windows comparable'})")
    for status in ("closed", "unmeasured", "new", "first_measured", "persisting", "promoted", "demoted"):
        for f in mom["findings"].get(status, []):
            print(f"  {f.get('status', status):15} {f['title']}" + (f"  {f.get('then_text', '')} -> {f.get('now_text', '')}" if status not in ("new", "first_measured") else ""))
    print("\nwhat moved:")
    for r in mom["moved"][:20]:
        print(f"  {r['where']:40} {r['metric']:14} {r['then_text']:>12} -> {r['now_text']:<12} {r['change_text']}")
    return 0


# ---------------------------------------------------------------- validate
def _load_facts(path):
    """facts.json, or None after printing why it cannot be used."""
    try:
        with open(path, encoding="utf-8") as fh:
            F = json.load(fh)
    except (OSError, ValueError) as e:
        print(f"cannot read {path}: {e}")
        return None
    need = ("meta", "window", "services", "domains", "totals", "coverage")
    if not isinstance(F, dict) or any(k not in F for k in need):
        print(f"{path} is not a facts.json written by ddreview (missing {', '.join(k for k in need if not isinstance(F, dict) or k not in F)})")
        return None
    return F


def _read_md(path):
    """A Markdown file as text, or None after saying why it cannot be read."""
    try:
        return config.read_text_file(path)
    except UnicodeDecodeError:
        print(f"cannot read {path}: it is not UTF-8 text — save it as UTF-8")
    except OSError as e:
        print(f"cannot read {path}: {e}")
    return None


def cmd_validate(a):
    md = _read_md(a.notebook)
    if md is None:
        return 2
    F = None
    LEDGER.numbers.clear()
    if a.facts:
        F = _load_facts(a.facts)
        if F is None:
            return 2
        _rebuild_ledger(F)
    wrapped = re.match(r"^\s*```(?:markdown|md)?\r?\n(.*)\r?\n```\s*$", md, re.S)
    inner = None if wrapped else re.search(r"^```(?:markdown|md)[ \t]*\r?\n(.*?)\r?\n```[ \t]*$", md, re.S | re.M)
    pre = []
    if wrapped and "```mermaid" not in wrapped.group(1).split("\n", 1)[0]:
        md = wrapped.group(1) + "\n"                 # an answer an LLM wrapped in a code fence is checked as the text it is
        pre.append("the file is wrapped in a code fence; it was checked as the text inside it — remove the fence before pasting")
    elif inner:
        md = inner.group(1) + "\n"
        pre.append("the answer sits in a ```markdown fence with other text around it; it was checked as the text inside the fence — paste only that")
    errors, warns = validate.check_markdown(md, F, strict_numbers=a.text, fragment=bool(a.against))
    warns = pre + warns
    for m_ in re.finditer(r"paste the Mermaid from `prompts/([^`]+)` here", md):
        warns.append(f"a diagram marker is still in the notebook: paste the answer to prompts/{m_.group(1)} in its place")
    if F:
        try:
            e2, w2 = validate.check_facts(F)
        except (KeyError, TypeError) as e:
            e2, w2 = [f"facts.json is incomplete: {e}"], []
        errors += e2
        warns += w2
    else:
        warns.append("numbers not checked: no --facts given (structure-only check)")
    if a.against:
        original = _read_md(a.against)
        if original is None:
            return 2
        e3, w3 = validate.check_against(md, original)
        errors += e3
        warns += w3
    for e in errors:
        print(f"ERROR   {e}")
    for w in warns:
        print(f"warning {w}")
    print(f"{len(errors)} errors, {len(warns)} warnings")
    return 1 if errors else 0


def _rebuild_ledger(F):
    """Load the ledger the build persisted, and register every number printable from facts.json so a standalone validate
    can check a notebook or an LLM-polished copy."""
    from .narrative import big, n, pct, sec
    for tok in (F.get("meta") or {}).get("ledger") or []:
        LEDGER.numbers.add(tok)
    days = (F.get("window", {}).get("aggregate") or {}).get("days")
    per_day = bool(F.get("window", {}).get("per_day_allowed")) and days
    for k in (F.get("window", {}).get("aggregate") or {}).get("candidates", {}):
        LEDGER.add(k)

    def walk(obj):
        if isinstance(obj, dict):
            if set(obj) >= {"num", "den", "pct"}:
                n(obj["num"]); n(obj["den"]); pct(obj); pct(obj["pct"], 0); pct(obj["pct"], 1); pct(obj["pct"], 2); pct(obj["pct"], 3); pct(obj["pct"], 4)
                if per_day:
                    n(obj["num"] / days)
            for v in obj.values():
                walk(v)
        elif isinstance(obj, list):
            for v in obj:
                walk(v)
        elif isinstance(obj, bool):
            return
        elif isinstance(obj, (int, float)):
            n(obj); big(obj); sec(obj); sec(obj / 1000.0); LEDGER.add(f"{obj:.1f}"); LEDGER.add(f"{obj:.2f}"); LEDGER.add(f"{obj:.0f}")
            if per_day:
                n(obj / days)
            if 0 <= obj <= 100:
                pct(obj, 0); pct(obj, 1); pct(obj, 2); pct(obj, 3); pct(obj, 4)
    walk(F)


# ---------------------------------------------------------------- main
def _load_cfg(path):
    try:
        return config.load_config(path)
    except ConfigError as e:
        print(f"config error: {e}")
        sys.exit(3)


def main(argv=None):
    _utf8_stdout()
    ap = argparse.ArgumentParser(prog="ddreview", description="Datadog exports -> monthly Observability Review notebook (Markdown + Mermaid).")
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("inspect", help="say how each export is recognised and what it unlocks")
    p.add_argument("exports")
    p.add_argument("--config")
    p = sub.add_parser("init", help="draft the per-org config from the exports")
    p.add_argument("exports")
    p.add_argument("--org", required=True)
    p.add_argument("--config", default="org.json")
    p.add_argument("--force", action="store_true")
    p = sub.add_parser("build", help="build the notebook, facts.json, diagrams and prompt files")
    p.add_argument("exports")
    p.add_argument("--config")
    p.add_argument("--org", help="org name when there is no config yet (default: the exports folder's parent name); with --config it changes the display name only")
    p.add_argument("--month", help='e.g. "November 2026" (default: from the data)')
    p.add_argument("--as-of", type=_iso_date, help="YYYY-MM-DD (default: latest timestamp in the data)")
    p.add_argument("--out", default="out")
    p.add_argument("--previous", default=None, help="auto | path to last month's facts.json | none (default: the config's 'previous', else auto)")
    p.add_argument("--window-days", type=_positive_int, help="assert the Endpoints-list range when no health check proves it uniquely")
    p.add_argument("--max-findings", type=_positive_int)
    p.add_argument("--diagrams", choices=["both", "code", "prompt"])
    p.add_argument("--no-prompts", action="store_true")
    p.add_argument("--strict", action="store_true", help="warnings also fail the build")
    p.add_argument("--force", action="store_true", help="overwrite an existing month folder")
    p = sub.add_parser("diff", help="month-over-month tables from two facts.json files")
    p.add_argument("previous")
    p.add_argument("current")
    p.add_argument("--config")
    p = sub.add_parser("validate", help="check a notebook (or LLM output) against facts.json")
    p.add_argument("notebook")
    p.add_argument("--facts")
    p.add_argument("--text", action="store_true", help="treat unknown numbers as errors (for LLM-polished text); needs --facts")
    p.add_argument("--against", help="the original notebook: each section of the checked file must carry the same numbers as the same section there")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 0
    if a.cmd == "init" and a.config and os.path.exists(a.config) and not os.path.isfile(a.config):
        print(f"config path is a folder, not a file: {a.config}")
        return 3
    if a.cmd in ("inspect", "init", "build") and not os.path.isdir(a.exports):
        print(f"exports folder not found (or not a folder): {a.exports}")
        return 2
    if a.cmd == "validate":
        for pth in (a.notebook, a.facts, a.against):
            if pth and not os.path.isfile(pth):
                print(f"file not found: {pth}")
                return 2
        if a.text and not a.facts:
            print("validate --text needs --facts facts.json")
            return 2
    if a.cmd == "diff":
        for pth in (a.previous, a.current):
            if not os.path.isfile(pth):
                print(f"file not found: {pth}")
                return 2
    if getattr(a, "config", None) and not os.path.isfile(a.config) and a.cmd != "init":
        print(f"config not found: {a.config}")
        return 3
    return {"inspect": cmd_inspect, "init": cmd_init, "build": cmd_build, "diff": cmd_diff, "validate": cmd_validate}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
