"""The notebook: Markdown that imports into a Datadog notebook (Notebooks → ⋮ → Import markdown file).

Frozen section order so months and clients compare: title and thesis · how to read · executive summary · since last month ·
what Datadog sees today (landscape diagram, domain table, pie) · one section per finding in one shape · also noted ·
recommendations Now / Next / Later · appendix. One H1, H2 for sections, H3 at most; GFM pipe tables with simple separators;
no raw HTML outside Mermaid labels. Every number printed goes through a narrative helper so the ledger sees it. Failure
figures appear only when the exports carry a failure signal. Analyst instructions (config keys, CLI flags) never appear
here: they go to build-summary.txt.
"""
from .aggregate import sample_length_text
from .narrative import LEDGER, range_words, big, cap, code, join_and, n, pct, sec, sec_range, table, trend_arrow, words
from .prompts import inside_prompt_name

SEV = {"red": "🔴", "amber": "🟠", "info": "🔵"}
ROW = {"healthy": ("✅", "Keep watching"), "quiet": ("⚪", "Too little traffic to judge"), "review": ("🟠", "Review the small operations"),
       "unmeasured": ("⚪", "Add Status Code to the export to judge failures"),
       "review_list": ("🟠", "Export its spans or the Endpoints list to name the failing operations"),
       "review_apart": ("🟠", "Review the services counted apart")}
SKIP_LABELS = {"unreadable_timestamp": ("row with an unreadable date", "rows with an unreadable date"),
               "unreadable_duration": ("row with an unreadable duration", "rows with an unreadable duration"),
               "empty_service": ("row with no service", "rows with no service"),
               "repeated_span_id": ("repeated span id", "repeated span ids"),
               "overlap_duplicate": ("row repeated from an earlier file", "rows repeated from an earlier file"),
               "unreadable_count": ("row with an unreadable request count", "rows with an unreadable request count")}
NOT_RECORDED = "— (not recorded)"


def short_title(t, maxlen=70):
    """Cut a title at a word boundary, never inside a code span."""
    if len(t) <= maxlen:
        return t
    cut = t.rfind(" ", 0, maxlen)
    head = t[: cut if cut > 0 else maxlen]
    if head.count("`") % 2:
        head = head[: head.rfind("`")]
    head = head.rstrip(" ,;:—-")
    return head + "…" if head else ""


def _minutes(text):
    return sample_length_text(text)


def render(F, body, also, healthy, estate_lines, cfg, diagrams, extra_also=()):
    for c in F["caveats"]:
        LEDGER.add(c)
    LEDGER.add(F["meta"]["kit_version"])
    LEDGER.add(F["window"]["spans"].get("minutes_text", ""))
    for d in F["window"].get("aggregate", {}).get("candidates", {}):
        LEDGER.add(d)
    dep_refs = [f["number"] for f in body if f.get("diagram") == "dependencies"]
    parts = [head(F, cfg, body), summary(F, body, healthy, cfg)]
    if F.get("mom"):
        parts.append(since_last_month(F))
    parts.append(landscape(F, diagrams, cfg))
    shown = {}
    for f in body:
        parts.append(finding_section(F, f, diagrams, cfg, shown, dep_refs))
    if also or estate_lines or extra_also:
        parts.append(also_noted(also, estate_lines, extra_also))
    parts.append(recommendations(F, body))
    parts.append(appendix(F, cfg, bool(diagrams)))
    if cfg["report"].get("closing_note"):
        parts.append(cfg["report"]["closing_note"].strip() + "\n")
    return "\n".join(parts)


def _lc(a):
    """Lower-case an audience's first word only when it is an ordinary capitalised word: 'Members' -> 'members', 'IT team' stays."""
    w, _, rest = a.partition(" ")
    if len(w) > 1 and w[0].isupper() and w[1:].islower():
        w = w[0].lower() + w[1:]
    return w + (" " + rest if rest else "")


def _join_audiences(auds):
    if len(auds) > 2 and any(" and " in a for a in auds):
        return ", ".join(auds[:-1]) + ", and " + auds[-1]
    if len(auds) == 2 and any(" and " in a for a in auds):
        return f"{auds[0]}, and {auds[1]}"
    return join_and(auds)


def _contains_words(small, big_):
    s, b = small.lower().split(), big_.lower().split()
    return len(s) < len(b) and any(b[i:i + len(s)] == s for i in range(len(b) - len(s) + 1))


def head(F, cfg, body):
    org = cfg["org"].get("name") or F["meta"]["org"] or "Observability"
    title = cfg["report"].get("title_template", "{org} — Observability Review, {month}").format(org=org, month=F["meta"]["month"])
    doms = [d["label"] for d in sorted(F["domains"].values(), key=lambda d: -d["requests"]["all"]) if not d.get("empty")]
    auds = []
    for d in F["domains"].values():
        a = d.get("audience", "")
        if a and not a.startswith("_todo") and a not in auds and not d.get("empty"):
            auds.append(a)
    auds = [a for a in auds if not any(a != b and _contains_words(a, b) for b in auds)]
    wp = F["window"]["phrase"]["volumes"]
    whose = f"{org}'s" if cfg["org"].get("name") else "the"
    extra = len(doms) - 6
    if not doms:
        dom_text = "services"
    elif extra > 0:
        dom_text = join_and(doms[:6] + [f"{words(extra)} other system{'s' if extra != 1 else ''}"])
    else:
        dom_text = join_and(doms) + " systems"
    who = _join_audiences([_lc(a) for a in auds]) if auds else "the people who use them"
    thesis = cfg["report"].get("thesis") or f"What {whose} {dom_text} did {wp}, what it means for {who}, and what to do next."
    prepared = " · ".join(x for x in [f"Prepared for {cfg['org'].get('prepared_for')}" if cfg["org"].get("prepared_for") else "", cfg["org"].get("prepared_by", ""), F["meta"]["as_of_label"]]
                          if x and not str(x).startswith("_todo"))
    sources = []
    if F["coverage"]["inputs"]["endpoints"]:
        sources.append(f"Datadog APM's Endpoints list ({F['window']['phrase']['aggregate'].replace('in the ', 'the ', 1)})")
    if F["coverage"]["inputs"]["services"]:
        sources.append("the APM Services list")
    if F["coverage"]["inputs"]["spans"]:
        s = F["window"]["spans"]
        sources.append(f"a trace sample of {_minutes(s['minutes_text'])} ({s['when']})" if s["when"] else "a trace sample")
    if F["coverage"]["inputs"]["catalog"]:
        sources.append(f"the resource catalog ({n(F['catalog']['rows'])} resources)")
    nf = len(body)
    intro = ((f"{cap(words(nf))} findings, each with the evidence, the impact and the actions." if nf > 1 else "One finding, with its evidence, impact and actions.") if nf
             else "No finding reached the threshold this month; the executive summary says what was checked and how it stood.")
    return (f"# {title}\n\n**{thesis}**\n\n{prepared}\n\n"
            f"> 📖 **How to read this notebook.** {intro} "
            f"Graphs marked *➕ live graph* can be added in a minute with `/timeseries` so the page stays current. Numbers come from {join_and(sources)}. "
            f"Use **⋮ → Download as PDF** to circulate.\n\n---\n")


def _trend_cell(m):
    d = m.get("direction", "new")
    if m.get("status") == "first_measured":
        return "🆕 first measured"
    if m.get("status") == "new" or not m:
        return "🆕 new"
    if d == "new":
        cell = "no comparable figure"
    else:
        cell = trend_arrow(d) + " " + d
    if m.get("status") == "promoted":
        cell += " (was noted)"
    return cell


def summary(F, body, healthy, cfg):
    has_mom = bool(F.get("mom"))
    header = ["#", "Finding", "Who feels it", "Severity", "Action"] + (["Trend"] if has_mom else [])
    rows = []
    for f in body:
        row = [str(f["number"]), f["summary"]["finding"], f["summary"].get("who", f["audience"]), SEV[f["severity"]], f["summary"]["action"]]
        if has_mom:
            row.append(_trend_cell(f.get("mom") or {}))
        rows.append(row)
    for h in healthy:
        icon, action = ROW.get(h.get("status", "healthy"), ROW["healthy"])
        action = h.get("action") or action
        row = ["", f"{h['icon']} {h['sentence']}".strip(), h["audience"], icon, action]
        if has_mom:
            row.append("")
        rows.append(row)
    out = "## 📌 Executive summary\n\n" + table(header, rows) + "\n"
    if cfg["report"].get("delivered_alongside"):
        out += f"\n**Delivered alongside this review:** {cfg['report']['delivered_alongside']}\n"
    return out + "\n---\n"


LABEL_PREFIXES = ("failure rate of ", "p95 of ", "4xx share on ", "calls to ", "scanner probe requests on ", "traffic gap of ",
                  "database statements per request on ", "token requests per business call on ")


def label_text(lbl):
    """'failure rate of GET /orders/{id}' -> 'failure rate of `GET /orders/{id}`' — the subject reads as a name, never as a placeholder."""
    lbl = lbl or ""
    for pre in LABEL_PREFIXES:
        if lbl.startswith(pre) and len(lbl) > len(pre):
            return pre + code(lbl[len(pre):])
    return lbl


def _mom_text(entry):
    """'3 → 3' means nothing without its metric: always prefix the label when there is one."""
    lbl = label_text(entry.get("label") or "")
    then, now = entry.get("then_text", "—"), entry.get("now_text", "—")
    return f"{lbl}: {then} → {now}" if lbl else f"{then} → {now}"


def _status_with_severity(base, row):
    was, now = row.get("then_severity"), row.get("severity")
    if was and now and was != now and was in SEV and now in SEV:
        return f"{base} — now {SEV[now]} (was {SEV[was]})"
    return base


THRESHOLD_LABELS = {"fail_amber_pct": ("failure rate that raises a finding", "pct"), "fail_red_pct": ("failure rate for a red finding", "pct"),
                    "slow_p95_s": ("p95 that counts as slow", "s"), "slow_op_s": ("p95 for a slow operation", "s"), "dep_fail_pct": ("dependency failure rate that raises a finding", "pct"),
                    "min_calls_per_day": ("calls a day an operation needs to count", "count"), "outlier_p95_s": ("p95 of a very slow operation", "s"),
                    "min_calls_sample": ("calls an operation needs in the sample", "count"), "slow_op_min_calls": ("calls a slow operation needs", "count"),
                    "dep_min_calls": ("calls a dependency needs", "count"), "dep_slow_s": ("p95 of a slow dependency", "s")}


RULE_TEXT = {"5xx_or_flag": "it returns a 5xx or records an application error; 4xx responses are not failures",
             "non_2xx": "it returns any status outside 2xx and 3xx (4xx included) or records an application error",
             "flag_only": "its span carries the error flag; status codes are not used"}


DEF_TEXT = {"5xx_or_flag": "a request that returns a 5xx or records an application error; 4xx responses are not failures",
            "non_2xx": "a request that returns any status outside 2xx and 3xx (4xx included) or records an application error",
            "flag_only": "a request whose span carries the error flag; status codes are not used"}


def _threshold_note(changed):
    parts = []
    for k, (a, b) in changed.items():
        if k in THRESHOLD_LABELS:
            label, unit = THRESHOLD_LABELS[k]
            f = (lambda x: LEDGER.add(f"{x:g} %")) if unit == "pct" else ((lambda x: LEDGER.add(f"{x:g} s")) if unit == "s" else n)
            parts.append(f"{label} {f(a)} → {f(b)}")
        else:
            parts.append(f"{k.replace('_', ' ')} {LEDGER.add(f'{a:g}')} → {LEDGER.add(f'{b:g}')}")
    if parts:
        return "The review's thresholds changed since last month (" + "; ".join(parts) + "), so a finding near a threshold may open or close for that reason alone."
    return "Some of the review's thresholds changed since last month, so a finding near a threshold may open or close for that reason alone."


NOISE_WORD = {"by_design": "by-design noise", "health": "health-check noise", "scanner": "scanner noise", "static": "static-file noise", "framework": "framework noise"}


def _probe_listed(F, s):
    return F["window"]["volumes_tier"] == "aggregate" and any(r.get("week") for r in F["resources"].get(s, []))


def _list_footnotes(F, svcs, tail):
    """¶ footnotes, one per reason a service's figures come from the Services list."""
    sl = F["window"]["phrase"]["services_list"]
    reason = lambda s: F["services"][s].get("list_reason") or ("markerless" if F["services"][s].get("spans_without_entries") else ("probe_listed" if _probe_listed(F, s) else "absent"))
    markerless = [s for s in svcs if reason(s) == "markerless"]
    sampled = [s for s in svcs if reason(s) == "probes_sampled"]
    probes = [s for s in svcs if reason(s) == "probe_listed"]
    absent = [s for s in svcs if reason(s) == "absent"]
    names = lambda xs: ", ".join(code(s) for s in xs[:3]) + (f" +{len(xs) - 3}" if len(xs) > 3 else "")
    out = []
    if markerless:
        out.append(f"\n¶ In the span sample, but none of their spans carries a request marker ({names(markerless)}): their figures come from the Services list ({sl}) {tail}.")
    if sampled:
        out.append(f"\n¶ In the span sample only through health probes ({names(sampled)}): their figures come from the Services list ({sl}) {tail}.")
    if probes:
        out.append(f"\n¶ Not in the span sample, and on the Endpoints list only through health or probe endpoints ({names(probes)}): their figures come from the Services list ({sl}) {tail}.")
    if absent:
        out.append(f"\n¶ Not in the span sample" + (" or the Endpoints list" if F["window"]["volumes_tier"] == "aggregate" else "") + f" ({names(absent)}): "
                   f"their figures come from the Services list ({sl}) {tail}.")
    return out


def _sample_footnotes(F, svcs, tail):
    """§ footnotes: services counted from the span sample, off the Endpoints list or on it only through probes."""
    sp = F["window"]["phrase"]["sample"]
    probes = [s for s in svcs if _probe_listed(F, s)]
    rest = [s for s in svcs if s not in probes]
    names = lambda xs: ", ".join(code(s) for s in xs[:3]) + (f" +{len(xs) - 3}" if len(xs) > 3 else "")
    out = []
    if rest or not svcs:
        out.append(f"\n§ Not on the Endpoints list" + (f" ({names(rest)})" if rest else "") + f": these counts come from the span sample ({sp}) {tail}.")
    if probes:
        out.append(f"\n§ On the Endpoints list only through health or probe endpoints ({names(probes)}): these counts come from the span sample ({sp}) {tail}.")
    return out


def domain_rate_text(F, d, name=code):
    """A domain's failure-rate cell: its business rate (and the all-traffic rate when noise changes it), a Services-list rate
    marked ◇ for members whose requests carry no failure signal, 'not recorded', or '—' with no business base.
    Returns (text, whether a ◇ rate is quoted)."""
    fs = F["coverage"]["failure_signal"]
    counted = d.get("counted_services", [])
    list_members = [F["services"][x] for x in counted if not F["services"][x].get("failure_known", True) and (F["services"][x].get("err_services_list") or {}).get("den")]
    if not d.get("failure_known", True) and list_members and len(list_members) == len(counted):
        lf, lr = sum(s_["err_services_list"]["num"] for s_ in list_members), sum(s_["err_services_list"]["den"] for s_ in list_members)
        return f"{pct(100.0 * lf / lr)} ◇", True
    if not fs or not d.get("failure_known", True):
        return NOT_RECORDED, False
    failed = pct(d["err"]["business"]) if d["err"]["business"].get("den") else "—"
    if d["failed"]["all"] and pct(d["err"]["all"]) != failed:
        failed += f" ({pct(d['err']['all'])} counting health checks and other noise)"
    nr = d.get("failure_not_recorded") or []
    listed = [x for x in nr if (F["services"][x].get("err_services_list") or {}).get("den")]
    plain = [x for x in nr if x not in listed]
    if listed:
        failed += " (" + ", ".join(f"{name(x)} {pct(F['services'][x]['err_services_list'])} ◇" for x in listed[:3]) + ")"
    if plain:
        failed += " (" + ", ".join(name(x) for x in plain[:3]) + " not recorded)"
    return failed, bool(listed)


def _targets_note(changes):
    def fmt(c, x):
        if x is None:
            return "none"
        return LEDGER.add(sec(x) if c["field"] == "slo_p95_s" else pct(x))
    parts = [f"agreed {'p95' if c['field'] == 'slo_p95_s' else 'success'} target for {c['label']} {fmt(c, c['then'])} → {fmt(c, c['now'])}" for c in changes]
    return "The agreed targets changed since last month (" + "; ".join(parts) + "), so a finding measured against one may open or close for that reason alone."


def since_last_month(F):
    m = F["mom"]
    prev_label = m["previous"]["month"]
    lines = [f"## 📈 Since last month ({prev_label})\n"]
    fm = m["findings"]
    closed, new, persisting = fm["closed"], fm["new"], fm["persisting"]
    demoted, promoted = fm.get("demoted", []), fm.get("promoted", [])
    unmeasured, first = fm.get("unmeasured", []), fm.get("first_measured", [])
    for row in closed + unmeasured + demoted:          # last month's titles and figures are printed too: register them
        for k in ("title", "then_text", "now_text", "label"):
            LEDGER.add(row.get(k, ""))
    truly_closed = [c for c in closed if c.get("status") == "closed"]
    within = [c for c in closed if c.get("status") == "within_target"]
    host_re = [c for c in closed if c.get("status") == "host_reclassified"]
    quiet = [c for c in closed if c.get("status") == "within_noise"]
    noted = [p for p in persisting + promoted if p.get("note")]
    thin = [c for c in closed if c.get("status") == "too_little_traffic"]
    reclassified = [c for c in closed if c.get("status") == "reclassified"]
    superseded = [c for c in closed if c.get("status") == "superseded"]
    below = [c for c in closed if c.get("status") == "below_threshold"]
    rank = {"info": 0, "amber": 1, "red": 2}
    escalated = [p for p in persisting + promoted if p.get("then_severity") in rank and p.get("severity") in rank and rank[p["severity"]] > rank[p["then_severity"]]]
    eased = [p for p in persisting + promoted if p.get("then_severity") in rank and p.get("severity") in rank and rank[p["severity"]] < rank[p["then_severity"]]]
    lead_bits = []
    if truly_closed:
        lead_bits.append(f"{words(len(truly_closed))} finding{'s' if len(truly_closed) != 1 else ''} closed")
    if within:
        lead_bits.append(f"{words(len(within))} closed within {'its' if len(within) == 1 else 'their'} agreed target{'s' if len(within) != 1 else ''}")
    if superseded:
        lead_bits.append(f"{words(len(superseded))} {'is' if len(superseded) == 1 else 'are'} now reported within another finding")
    if below:
        lead_bits.append(f"{words(len(below))} fell below this month's changed threshold while {'its' if len(below) == 1 else 'their'} figure still clears last month's")
    if m.get("rule_changed"):
        LEDGER.add(m["rule_changed"])
    if thin:
        lead_bits.append(f"{words(len(thin))} had too little traffic this month to judge")
    if reclassified:
        classes_ = sorted({NOISE_WORD.get(c.get("noise_class", "by_design"), "noise") for c in reclassified})
        lead_bits.append(f"{words(len(reclassified))} {'was' if len(reclassified) == 1 else 'were'} reclassified as {classes_[0] if len(classes_) == 1 else 'noise'}")
    if host_re:
        lead_bits.append(f"{words(len(host_re))} {'was' if len(host_re) == 1 else 'were'} reclassified by the hosts config while the calls continue")
    if quiet:
        lead_bits.append(f"{words(len(quiet))} fell under the bar within the sample's noise, so {'it is' if len(quiet) == 1 else 'they are'} not called closed")
    if noted:
        lead_bits.append(f"{words(len(noted))} of last month's headline operations {'was' if len(noted) == 1 else 'were'} reclassified as noise")
    if unmeasured:
        reasons = sorted({c.get("reason") or "this month's exports cannot check it" for c in unmeasured})
        lead_bits.append(f"{words(len(unmeasured))} could not be checked this month ({'; '.join(reasons)})")
    if new:
        lead_bits.append(f"{words(len(new))} {'are' if len(new) != 1 else 'is'} new")
    if first:
        reasons_f = sorted({x.get("reason") or "last month's export had no status code or error column" for x in first})
        lead_bits.append(f"{words(len(first))} {'are' if len(first) != 1 else 'is'} measured for the first time ({'; '.join(reasons_f)})")
    if persisting:
        lead_bits.append(f"{words(len(persisting))} persist{'s' if len(persisting) == 1 else ''}")
    if escalated:
        lead_bits.append(f"{words(len(escalated))} escalated in severity")
    if eased:
        lead_bits.append(f"{words(len(eased))} eased in severity")
    if promoted:
        lead_bits.append(f"{words(len(promoted))} {'was' if len(promoted) == 1 else 'were'} only noted last month and {'is' if len(promoted) == 1 else 'are'} now a finding")
    if demoted:
        lead_bits.append(f"{words(len(demoted))} persist{'s' if len(demoted) == 1 else ''} but moved to *Also noted*")
    if lead_bits:
        lines.append(cap(join_and(lead_bits)) + ".")
    elif m["moved"]:
        lines.append("No finding opened or closed; the numbers below show what moved.")
    else:
        lines.append("No finding last month or this month, and nothing moved beyond the tolerances.")
    if m["comparable"].get("note"):
        lines.append("\n*" + cap(m["comparable"]["note"]) + ".*")
    rows = []

    def trend(p):
        return "no comparable figure" if p["direction"] == "new" else trend_arrow(p["direction"]) + " " + p["direction"]
    for c in closed:
        within_txt = "closed — within its agreed success target" if c["id"].startswith("failing_operations") else "closed — within its agreed p95 target"
        reclass_txt = "reclassified as " + NOISE_WORD.get(c.get("noise_class", "by_design"), "noise")
        icon, status = {"superseded": ("🔁 ", "superseded"), "below_threshold": ("⚖️ ", "below this month's threshold"),
                        "within_noise": ("⚪ ", "under the bar, within sampling noise"),
                        "too_little_traffic": ("⚪ ", "too little traffic to judge"), "reclassified": ("🏷️ ", reclass_txt),
                        "within_target": ("🎯 ", within_txt),
                        "host_reclassified": ("🏷️ ", "reclassified by the hosts config")}.get(c.get("status"), ("✅ ", "closed"))
        rows.append([icon + c["title"], label_text(c.get("label", "")), c["then_text"], c["now_text"], "", status])
    for c in unmeasured:
        rows.append(["⚪ " + c["title"], label_text(c.get("label", "")), c["then_text"], c["now_text"], "", "not checked this month"])
    for p in persisting + promoted:
        rows.append([p["title"], label_text(p.get("label", "")), p["then_text"], p["now_text"], trend(p),
                     _status_with_severity("persisting" if p in persisting else "was noted, now a finding", p) + (f"; {p['note']}" if p.get("note") else "")])
    for p in demoted:
        rows.append([p["title"], label_text(p.get("label", "")), p["then_text"], p["now_text"], trend(p), "persisting — now in Also noted"])
    for x in new:
        rows.append([f"🆕 {x['title']}", label_text(x.get("label", "")), "—", x["now_text"], "", "new"])
    for x in first:
        rows.append([f"🆕 {x['title']}", label_text(x.get("label", "")), "not measured", x["now_text"], "", "first measured"])
    if rows:
        lines.append("\n" + table(["Finding", "Metric", prev_label, F["meta"]["month"], "Trend", "Status"], [[c if str(c).strip() else "—" for c in r] for r in rows]))
    if m["moved"]:
        lines.append("\n**What moved** (beyond the tolerances in the config):\n")
        lines.append(table(["Where", "Metric", prev_label, F["meta"]["month"], "Change"], [[r["where"], r["metric"], r["then_text"], r["now_text"], r["change_text"]] for r in m["moved"][:12]]))
        for r in m["moved"][:12]:
            if r.get("note"):
                lines.append("\n*" + LEDGER.add(r["note"]) + ".*")
    if m.get("thresholds_changed"):
        lines.append("\n*" + _threshold_note(m["thresholds_changed"]) + "*")
    if m.get("targets_changed"):
        lines.append("\n*" + _targets_note(m["targets_changed"]) + "*")
    return "\n".join(lines) + "\n\n---\n"


def _diagram_marker(cfg, what, prompt_file):
    if cfg["report"].get("diagrams") == "prompt":
        return f"*{what}: paste the Mermaid from `prompts/{prompt_file}` here.*\n"
    return ""


def landscape(F, diagrams, cfg):
    t = F["totals"]
    fs = F["coverage"]["failure_signal"]
    wp = F["window"]["phrase"]["volumes"]
    method = {"parent_child": "parent/child spans", "peer": "peer tags", "trace_cooccurrence": "spans sharing a trace",
              "none": "the inferred services list" if F["coverage"]["inputs"]["inferred"] else "none — this export does not say which service called which"}[F["coverage"]["graph_method"]]
    line = (f"{cap(words(t['services_entry']))} traced service{'s' if t['services_entry'] != 1 else ''}, {n(t['endpoints'])} endpoint{'s' if t['endpoints'] != 1 else ''} and {big(t['requests'])} request{'s' if t['requests'] != 1 else ''} {wp}, "
            f"across {words(t['domains'])} domain{'s' if t['domains'] != 1 else ''}.")
    if diagrams.get("landscape"):
        unnamed = [k for k, dd in F["dependencies"].items() if dd.get("host_unknown")]
        drawn_unnamed = any(x in diagrams["landscape"] for x in ("calls of ", "messages of ", "reads of "))
        if unnamed and drawn_unnamed and F["coverage"]["graph_method"] == "none":
            line += " The map shows each service's outbound calls to hosts the export does not name."
        elif F["coverage"]["graph_method"] == "none" and not F["coverage"]["inputs"]["inferred"]:
            line += (" The exports do not say which service called which, so the map shows the services only"
                     + ("; their outbound calls to unnamed hosts are left out for space." if unnamed else "."))
        else:
            line += f" The map shows only connections the data shows ({method})."
    out = ["## 🗺️ What Datadog sees today\n", line, ""]
    if diagrams.get("landscape"):
        out.append("```mermaid\n" + diagrams["landscape"] + "```\n")
    else:
        mk = _diagram_marker(cfg, "Service map", "01-diagram-landscape.md")
        if mk:
            out.append(mk)
    rows = []
    worst_kind = any(d.get("p95_kind") == "worst_endpoint" for d in F["domains"].values())
    marks = set()
    for key, d in F["domains"].items():
        if d.get("empty"):
            continue
        failed, used_list = domain_rate_text(F, d)
        if used_list:
            marks.add("rate")
        mark = lambda s: " ¶" if s in d.get("services_list_only", []) else (" §" if s in d.get("sample_only_services", []) else "")
        for s in d["services"][:4]:
            if mark(s):
                marks.add("services_list" if mark(s) == " ¶" else "sample")
        svcs = ", ".join(code(s) + mark(s) for s in d["services"][:4]) + (LEDGER.add(f" +{len(d['services']) - 4}") if len(d["services"]) > 4 else "")
        req = n(d["requests"]["all"])
        if d["requests"]["all"] and (d["requests"]["all"] - d["requests"]["business"]) / d["requests"]["all"] >= 0.05:
            req += f" ({n(d['requests']['business'])} business)"
        vt = d.get("volume_tier", F["window"]["volumes_tier"])
        if vt != F["window"]["volumes_tier"]:
            req += " ¶" if vt == "services_list" else " §"
            marks.add(vt)
        p95 = sec_range(d["p95_range_s"])
        rows.append([f"{d.get('icon', '')} {d['label']}".strip(), svcs, req, failed, f"**{p95}**" if (d["p95_range_s"][1] or 0) >= 2 else p95])
    p95_head = "p95" if not worst_kind else "p95 (worst busy endpoint where no service p95 exists)"
    out.append(table(["Domain", "Services", f"Requests ({wp.replace('in the ', '')})", "Failed (business)", p95_head], rows))
    if "sample" in marks:
        sample_svcs = sorted({s for d_ in F["domains"].values() for s in d_.get("sample_only_services", []) + (d_.get("counted_services", []) if d_.get("volume_tier") == "sample" and F["window"]["volumes_tier"] != "sample" else [])})
        out.extend(_sample_footnotes(F, sample_svcs, "and are not added to the totals"))
    if "services_list" in marks:
        listed_svcs = [s for d_ in F["domains"].values() for s in d_.get("services_list_only", []) + (d_.get("counted_services", []) if d_.get("volume_tier") == "services_list" else [])]
        out.extend(_list_footnotes(F, listed_svcs, "and are not added to the totals"))
    if "rate" in marks:
        out.append(f"\n◇ Failure rate from the Services list ({F['window']['phrase']['services_list']}): the requests of these services in the sample carry no status code or error flag.")
    if diagrams.get("pie"):
        out.append("\n```mermaid\n" + diagrams["pie"] + "```")
    s = F["window"]["spans"]
    rule = ("Volumes count every request (APM trace metrics), not a sample." if F["window"]["volumes_tier"] == "aggregate"
            else f"All counts come from a span sample of {_minutes(s['minutes_text'])} and are never scaled up to a day or a week.")
    if fs:
        weekly = (" Weekly figures from the Endpoints list use Datadog's own error count (5xx and application errors); this rule applies to figures from the span sample."
                  if cfg["failure"]["rule"] != "5xx_or_flag" and F["window"]["volumes_tier"] == "aggregate" else "")
        callout = f"a request is *failed* when {RULE_TEXT[cfg['failure']['rule']]}.{weekly} {rule}"
    else:
        callout = ("these exports record no error flag, which is what this review's failure rule counts, so this review says nothing about failures; add the Error column to the spans export to measure them. "
                   if cfg["failure"]["rule"] == "flag_only" else
                   "these exports record no status codes or errors, so this review says nothing about failures; add Status Code to the spans export to measure them. ") + rule
    out.append(f"\n> 🎨 **A rule used throughout:** {callout}\n\n---\n")
    return "\n".join(out)


def finding_section(F, f, diagrams, cfg, shown=None, dep_refs=None):
    shown = shown if shown is not None else {}
    dep_refs = dep_refs or []
    out = [f"## {SEV[f['severity']]} Finding {f['number']} — {f['title']}\n", f["lead"], ""]
    if f["table"] and f["table"]["rows"]:
        out.append(table(f["table"]["columns"], [[c if str(c).strip() else "—" for c in r] for r in f["table"]["rows"]]))
        out.append("")
    for b in f["bullets"]:
        out.append(f"- {b}")
    if f["bullets"]:
        out.append("")
    dg = f.get("diagram")
    if dg and diagrams.get(dg):
        if dg in shown:
            out.append(f"*The dependency map in Finding {shown[dg]} (shared by Findings {join_and(str(x) for x in dep_refs)}) shows these hosts.*\n")
        else:
            shown[dg] = f["number"]
            caption = ""
            if dg.startswith("inside:"):
                caption = f"*What the traces show inside a request ({F['window']['phrase']['sample']}; ratios are sample ratios):*\n\n"
            elif dg == "dependencies" and len(dep_refs) > 1:
                caption = f"*Dependency map for Findings {join_and(str(x) for x in dep_refs)} (the hosts those findings name):*\n\n"
            out.append(caption + "```mermaid\n" + diagrams[dg] + "```\n")
    elif dg and dg not in shown:
        shown[dg] = f["number"]
        if dg == "dependencies":
            mk = _diagram_marker(cfg, "Dependency map", "03-diagram-dependencies.md")
        else:
            mk = _diagram_marker(cfg, "Inside a request", inside_prompt_name(dg.split(":", 1)[-1]))
            if mk:
                mk = f"*What the traces show inside a request ({F['window']['phrase']['sample']}; ratios are sample ratios):*\n\n" + mk
        if mk:
            out.append(mk)
    if f["smaller"]:
        out.append(f["smaller"] + "\n")
    if f["actions"]:
        out.append("**Actions**")
        for i, a in enumerate(f["actions"], 1):
            out.append(f"{i}. {a['text']}.")
        out.append("")
    if f["live_graph"]:
        notes = [x for x in f["live_graph"] if x[0] == "note"]
        queries = [x for x in f["live_graph"] if x[0] != "note"]
        if queries:
            out.append("> ➕ **live graph** — " + "; ".join(f"`/{w}` · `{q}`" + (f" ({note})" if note else "") for w, q, note in queries))
        for _, q, _ in notes:
            out.append(f"> 🔎 {q}")
        out.append("")
    m = f.get("mom") or {}
    if m.get("direction") not in (None, "new"):
        sev_note = ""
        if m.get("then_severity") and m["then_severity"] != f["severity"] and m["then_severity"] in SEV:
            sev_note = f"; severity {SEV[m['then_severity']]} → {SEV[f['severity']]}"
        out.append(f"*Since last month: {_mom_text(m)} ({trend_arrow(m['direction'])} {m['direction']}{sev_note}).*\n")
    out.append("---\n")
    return "\n".join(out)


def also_noted(also, estate_lines, extra_also=()):
    out = ["## 📝 Also noted\n"]
    for f in also:
        out.append(f"- {SEV[f['severity']]} {f['summary']['finding']} — *{f['summary']['action']}*")
    for x in extra_also:
        out.append(f"- {SEV.get(x.get('severity'), '🔵')} {x['finding']} — *{x['action']}*")
    for line in estate_lines:
        out.append(f"- ✅ {cap(line)}")
    return "\n".join(out) + "\n\n---\n"


def recommendations(F, body):
    rows, seen = [], set()
    for horizon, label in (("now", "**Now**"), ("next", "**Next**"), ("later", "**Later**")):
        for f in body:
            for a in f["actions"]:
                if a["horizon"] == horizon and a["text"] not in seen:
                    seen.add(a["text"])
                    st = LEDGER.add(short_title(f["title"]))
                    rows.append([label, a["text"], f"Finding {f['number']}" + (f" — {st}" if st else "")])
    if not rows:
        entries = [v for v in F["services"].values() if v["role"] == "entry"]
        watched = F["coverage"].get("monitors_known") and entries and all(v["monitors"] > 0 for v in entries)
        text = "Nothing to do beyond keeping the monitors in place." if watched else "No action arises from the data analysed this month."
        if F["coverage"].get("detectors_skipped"):
            text = text[:-1] + " — see *Not covered this month* in the appendix for what the exports could not check."
        return f"## ✅ Recommendations, in order\n\n{text}\n\n---\n"
    return "## ✅ Recommendations, in order\n\n" + table(["When", "What", "Why"], rows) + "\n\n---\n"


def _skip_note(skipped):
    total = sum((skipped or {}).values())
    if not total:
        return ""
    parts = []
    for k, v in skipped.items():
        if v:
            one, many = SKIP_LABELS.get(k, (k.replace("_", " "), k.replace("_", " ")))
            parts.append(f"{n(v)} {one if v == 1 else many}")
    return f" ({n(total)} row{'s' if total != 1 else ''} skipped: " + ", ".join(parts) + ")"


def _span_line(i, pf, w):
    s = pf.get(i["file"]) or {}
    line = f"- Trace sample `{i['file']}`: {n(i['rows'])} spans"
    if s.get("start_local"):
        start, end = s["start_local"], s["end_local"]
        end_txt = end.split(" ")[-1] if end.split(" ")[0] == start.split(" ")[0] else end
        line += f", {start}–{end_txt} {w['spans']['tz']} ({_minutes(s['minutes_text'])})"
    if s.get("capped"):
        line += " — the export cap was hit, so these minutes are only part of the range that was exported; spans outside them are missing"
    return line + _skip_note(i.get("skipped")) + "."


def appendix(F, cfg, has_diagrams=True):
    fs = F["coverage"]["failure_signal"]
    out = ["## 📎 Appendix\n", "**Data analysed.**"]
    w = F["window"]
    pf = {x["file"]: x for x in w["spans"].get("files", [])}
    span_files = [i for i in F["meta"]["inputs"] if i["kind"] == "spans"]
    for i in F["meta"]["inputs"]:
        if i["kind"] == "spans":
            out.append(_span_line(i, pf, w))
        elif i["kind"] == "endpoints":
            a = w["aggregate"]
            probe = lambda p: f"{code(p['resource'])} on {code(p['service'])} {n(p['requests'])} requests = one every {LEDGER.add(format(p['implied_interval_s'], '.3f'))} s"
            if a["source"] == "proved":
                span = f"{a['days']} day{'s' if a['days'] != 1 else ''} to {F['meta']['as_of_label']}"
                proof = " — range proved by health checks at a fixed cadence: " + "; ".join(probe(p) for p in a["proof"][:3])
            elif a["source"] == "asserted":
                span = f"{a['days']} day{'s' if a['days'] != 1 else ''} to {F['meta']['as_of_label']}"
                if a.get("confidence") == "contradicted":
                    proof = f" — {a['days']} day{'s' if a['days'] != 1 else ''} as stated for this review, but the health-check cadence fits {join_and(str(LEDGER.add(d)) for d in a.get('tied', []))} days, so no per-day figures are quoted"
                elif a["proof"]:
                    others = [d for d in a.get("tied", []) if d != a["days"]]
                    proof = f" — {a['days']} day{'s' if a['days'] != 1 else ''} as stated for this review, consistent with the health-check cadence: " + "; ".join(probe(p) for p in a["proof"][:2])
                    if others:
                        proof += f" (the cadence also fits {join_and(str(LEDGER.add(d)) for d in others)} days, so it cannot confirm the range)"
                else:
                    proof = f" — {a['days']} day{'s' if a['days'] != 1 else ''} as stated for this review (no fixed-cadence health check confirms it)"
            elif a["source"] == "ambiguous":
                span = f"an export window to {F['meta']['as_of_label']}"
                proof = f" — the health-check cadence fits {join_and(str(LEDGER.add(d)) for d in a.get('tied', []))} days equally; {a['days']} days assumed and no per-day figures are quoted"
            else:
                span = f"an export window to {F['meta']['as_of_label']}"
                proof = f" — no fixed-cadence health check proves the range; {a['days']} days assumed and no per-day figures are quoted"
            out.append(f"- APM Endpoints list `{i['file']}`: {n(i['rows'])} endpoints, {span}{proof}{_skip_note(i.get('skipped'))}.")
        elif i["kind"] == "services":
            sl = w.get("services_list")
            out.append(f"- APM Services list `{i['file']}`: {n(i['rows'])} services (p95, monitors, SLOs, env tags" + (f"; its volumes cover {range_words(sl['days_estimate'])}, so they are not mixed with the Endpoints list" if sl and sl.get("days_estimate") else "") + ").")
        elif i["kind"] == "inferred":
            out.append(f"- Inferred services `{i['file']}`: {n(i['rows'])} hosts and entities the traced services call; volumes are {w['phrase']['services_list']}.")
        elif i["kind"] == "catalog":
            out.append(f"- Resource catalog `{i['file']}`: {n(i['rows'])} resources" + (" (export cap, so counts are minimums)" if F.get("catalog") and F["catalog"]["capped"] else "") + ".")
    if len(span_files) > 1 and w["spans"].get("minutes_text"):
        out.append(f"- Together the trace samples cover {_minutes(w['spans']['minutes_text'])} across {words(len(w['spans'].get('windows') or []))} window{'s' if len(w['spans'].get('windows') or []) != 1 else ''}.")
    for name in F["meta"].get("empty_inputs", []):
        out.append(f"- `{name}` was recognised but held no usable rows and was not used.")
    out.append("")
    entries = sorted(((k, v) for k, v in F["services"].items() if v["role"] == "entry"), key=lambda kv: -kv[1]["requests"]["all"])
    srcs = [(v.get("p95_source") or "") for _, v in entries if v.get("p95_s") is not None]
    if srcs and all(x == "services_list" for x in srcs):
        p95_def = "Service p95 comes from the APM Services list"
    elif srcs and all(x.startswith("sample") for x in srcs):
        p95_def = "A service's p95 is the request-weighted p95 of its business entry spans in the sample"
    else:
        p95_def = ("Service p95 comes from the APM Services list where the service has a row there; otherwise from its one Endpoints-list operation, "
                   "or from the request-weighted business entry spans in the sample when that agrees with the Endpoints list")
    worst_note = "; where no service p95 exists the tables show the highest p95 among the service's 30 busiest endpoints and say so in the column header" if any(v["p95_kind"] != "service" for _, v in entries) else ""
    weekly = (" Weekly figures from the Endpoints list use Datadog's own error count (5xx and application errors)."
              if cfg["failure"]["rule"] != "5xx_or_flag" and F["window"]["volumes_tier"] == "aggregate" else "")
    failed_def = (f"*Failed* = {DEF_TEXT[cfg['failure']['rule']]}.{weekly} " if fs
                  else ("*Failed* is not measured: the exports carry no error flag, which is what the configured rule counts. " if cfg["failure"]["rule"] == "flag_only"
                        else "*Failed* is not measured: the exports carry no status code or error flag. "))
    out.append("**Definitions.** " + failed_def + "*p95* = 95 % of requests finish within this time. "
               + f"{p95_def}{worst_note}. "
               + ("Request volumes are APM trace metrics, which count every request regardless of trace sampling. " if w["volumes_tier"] == "aggregate" else "Counts from the span sample are quoted as sample counts and never scaled. ")
               + f"Entry requests in the span sample were identified by the rule *{F['coverage']['entry_rule']}*.")
    out.append("")
    if F["noise"]["rules_applied"]:
        out.append("**Noise excluded from business totals** (every exclusion, with the rule that made it):\n")

        def rule_text(r):
            if r["source"] == "auto":
                return "fails on every call (auto; confirm with the team)"
            if r["source"] == "builtin":
                return f"built-in {r['class'].replace('_', ' ')} pattern"
            return code(r["rule"].split(":", 2)[-1][:60]) + (f" — {LEDGER.add(r['why'])}" if r.get("why") else "")
        grouped = {}
        for r in F["noise"]["rules_applied"]:
            key = (r["class"], rule_text(r), r["source"])
            g = grouped.setdefault(key, {"resources": 0, "requests": 0, "failed": 0})
            for k in ("resources", "requests", "failed"):
                g[k] += r[k]
        rows = [[cls.replace("_", " "), rule, src, n(g["resources"]), n(g["requests"]), n(g["failed"]) if fs else NOT_RECORDED]
                for (cls, rule, src), g in sorted(grouped.items(), key=lambda kv: (-kv[1]["requests"], kv[0]))[:15]]
        out.append(table(["Class", "Rule", "Source", "Endpoints", "Requests removed", "Failures removed"], rows))
        out.append("")
    if entries:
        mixed = any(v["p95_kind"] != "service" for _, v in entries)
        has_sl = F["coverage"]["inputs"]["services"]
        marked = has_sl and any(v.get("p95_s") is not None and (v.get("p95_source") or "") != "services_list" for _, v in entries)
        if has_sl and not marked:
            src_text = "from the Services list"
        elif has_sl:
            src_text = "from the Services list; ‡ = from the Endpoints list or the span sample, because the service has no Services-list row"
        else:
            src_text = "from the Endpoints list or the span sample" if F["coverage"]["inputs"]["endpoints"] else "from the span sample"
        out.append(f"**All traced services** (requests {w['phrase']['volumes']}; failure % on business operations; p95 {src_text}" + ("; † = the worst busy endpoint's p95" if mixed else "") + "):\n")
        rows, rate_marked = [], False
        for k, v in entries:
            if v["p95_s"] is not None:
                p95 = sec(v["p95_s"]) + (" ‡" if marked and (v.get("p95_source") or "") != "services_list" else "")
            elif v["p95_worst_s"] is not None:
                p95 = sec(v["p95_worst_s"]) + " †"
            else:
                p95 = "—"
            vt = v["requests"].get("tier")
            req = n(v["requests"]["all"]) + ("" if vt == w["volumes_tier"] else (" ¶" if vt == "services_list" else " §"))
            list_rate = vt == "services_list"
            esl = v.get("err_services_list")
            rate_cell = ((pct(v["err"]["business"]) if v["err"]["business"].get("den") else "—") if ((fs or list_rate) and v.get("failure_known", True))
                         else (f"{pct(esl)} ◇" if (esl and esl.get("den") and not v.get("failure_known", True)) else NOT_RECORDED))
            rate_marked = rate_marked or rate_cell.endswith("◇")
            rows.append([code(k) + (f" ({v['display']})" if v["display"] != k else ""), req, rate_cell, p95,
                         str(v["monitors"]) if v.get("monitors_known", F["coverage"].get("monitors_known")) else "—",
                         v["tech"] or (v["operation"] if v["operation_source"] != "placeholder" else "—")])
        out.append(table(["Service", "Requests", "Failure % (business)", "p95", "Monitors", "Stack / operation"], rows))
        tiers = {v["requests"].get("tier") for _, v in entries} - {w["volumes_tier"]}
        if "sample" in tiers:
            out.extend(_sample_footnotes(F, [k for k, v in entries if v["requests"].get("tier") == "sample"], f"and are not in the {w['phrase']['volumes'].replace('in the ', '')} totals"))
        if "services_list" in tiers:
            out.extend(_list_footnotes(F, [k for k, v in entries if v["requests"].get("tier") == "services_list"], "and are not in the totals"))
        if rate_marked:
            out.append(f"\n◇ Failure rate from the Services list ({w['phrase']['services_list']}): the service's requests in the sample carry no status code or error flag.")
        out.append("")
    skipped = F["coverage"].get("detectors_skipped", [])
    hints = F["coverage"].get("unlock_hints", [])
    if skipped or hints:
        if skipped:
            out.append("**Not covered this month.** " + " ".join(f"*{s.get('label', s['name'].replace('_', ' '))}* was not checked ({s['reason']}: {s['unlock']})." for s in skipped))
        if hints:
            if skipped:
                out.append("")
            out.append("To see more next month: " + "; ".join(hints[:4]) + ".")
        out.append("")
    if F["caveats"]:
        out.append("**Caveats.**")
        for c in F["caveats"]:
            out.append(f"- {c}")
        out.append("")
    out.append("**Method.** Every figure above was computed from the exports by the review generator (ddreview " + F["meta"]["kit_version"] + ") and recorded in `facts.json`; "
               + ("keep that file to compare next month." if not F.get("mom") else f"last month's `facts.json` ({F['mom']['previous']['month']}) provided the comparison.")
               + (" Mermaid diagrams are drawn from the same tables." if has_diagrams else " Diagrams are drawn from prompt files and checked after pasting.")
               + " The generator writes a first draft in one command; read every finding before sharing.")
    return "\n".join(out) + "\n"
