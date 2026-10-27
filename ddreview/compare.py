"""Month-over-month: align last month's facts.json with this month's by stable keys and report what moved.

Keys: service name (through services.aliases for renames, applied to every place a name appears: services, resources,
findings, dependency hosts and the health-check proof), domain key, KPI key, dependency host, node group, finding id.
A metric "moved" only beyond the tolerances in the config, and a failure rate only when the change is also statistically
real (enough failures and a two-proportion z score above mom_rate_z), so sampling noise never becomes narrative. A
finding's trend compares the SAME metric on the SAME subject in both months: when the worst operation, host or node
changed, last month's value of this month's subject is looked up, and when there is none, or the label or unit differs,
the trend is "no comparable figure". Volumes are compared only on the Endpoints list when both ranges are known to match
(and only for services counted on it in both months); Services-list counts only when both Services-list ranges agree.
Failure rates are compared only when both months carry a failure signal. Finding statuses: closed, superseded, new,
persisting, demoted, promoted, unmeasured (a failure finding this month's export cannot measure), first measured (a
failure finding last month's export could not measure) and below the threshold (closed only because a threshold changed).
"""
import copy
import json
import math
import os
import re

from .config import slug
from .narrative import LEDGER, code, n, pct, ratio_text, rule_words, sec

RELATED = {"slow_operations": {"slow_hot_paths", "chatty_requests"}, "slow_hot_paths": {"slow_operations", "chatty_requests"}, "chatty_requests": {"slow_hot_paths"},
           "egress_and_telemetry": {"failing_dependencies", "nonprod_from_prod"}, "failing_dependencies": {"egress_and_telemetry", "nonprod_from_prod"}}
PER_SERVICE = ("failing_operations", "slow_hot_paths", "noise_masks_signal", "client_errors", "scanner_exposure", "chatty_requests", "call_ratio")
FAILURE_DETECTORS = ("failing_operations", "noise_masks_signal", "client_errors")
THRESHOLD_KEYS = {"failing_operations": ("fail_amber_pct", "fail_red_pct", "fail_red_count", "min_calls_per_day", "min_calls_sample"),
                  "slow_operations": ("slow_op_s", "slow_op_min_calls", "outlier_p95_s", "outlier_min_calls"),
                  "slow_hot_paths": ("slow_p95_s", "slow_share_amber_pct", "slow_share_red_pct", "outlier_p95_s", "outlier_min_calls", "min_calls_per_day", "min_calls_sample"),
                  "failing_dependencies": ("dep_fail_pct", "dep_min_calls", "dep_dead_pct", "dep_dead_min_calls", "dep_slow_s", "dep_slow_min_calls"),
                  "noise_masks_signal": ("noise_share_pct", "noise_min_failed"), "client_errors": ("client_error_share_pct", "client_error_min_calls"),
                  "scanner_exposure": ("scanner_share_pct", "scanner_min_hits"), "node_imbalance": ("node_gap_pct", "node_idle_share_pct"),
                  "call_ratio": ("token_ratio",), "chatty_requests": ("db_per_request", "http_per_request", "cache_per_request", "secret_reads_per_request", "repeat_child_per_entry"),
                  "egress_and_telemetry": ("egress_red_calls",), "foundations": ("monitors_min_share_pct", "catalog_tag_pct", "framework_share_pct", "scanner_share_pct")}
NO_SIGNAL = "the export has no status code or error column"
NO_SIGNAL_LAST = "last month's export had no status code or error column"
SKIPPED_LAST = {"needs a failure signal": NO_SIGNAL_LAST, "needs status codes": "last month's export had no status code column",
                "needs caller data": "last month's exports did not show which service calls which",
                "needs child spans with their parents": "last month's export had no parent and child spans"}
VOLUME_METRICS = ("count",)
SEV_ICON = {"red": "🔴", "amber": "🟠", "info": "🔵"}


def _chg(text):
    return LEDGER.add(text)


def load_previous(path, org_short=None, before_month_key=None):
    """Read last month's facts.json and refuse one that is not comparable: wrong schema, another org, or not an earlier month."""
    try:
        with open(path, encoding="utf-8") as fh:
            prev = json.load(fh)
    except (OSError, ValueError) as e:
        raise SystemExit(f"cannot read last month's facts from {path}: {e}")
    if not isinstance(prev, dict):
        raise SystemExit(f"{path} is not a facts.json (expected a JSON object)")
    m = prev.get("meta", {})
    if m.get("schema_version") != 1:
        raise SystemExit(f"previous facts.json has schema_version {m.get('schema_version')}; this kit reads version 1")
    if org_short and m.get("org_short") and m["org_short"] != org_short:
        raise SystemExit(f"{path} is a facts.json for '{m['org_short']}', not '{org_short}' — pass --previous none or the right file")
    if before_month_key and m.get("month_key") and not m["month_key"] < before_month_key:
        raise SystemExit(f"{path} is for {m.get('month')} which is not earlier than this build ({before_month_key}) — pass --previous none or an earlier month")
    return prev


def _fmt(value, unit):
    if value is None:
        return "—"
    if unit == "pct":
        return pct(value)
    if unit == "s":
        return sec(value)
    if unit == "ratio":
        return ratio_text({"value": value})
    return n(value)


def _z(then_nd, now_nd):
    """Pooled two-proportion z score; None when either side has no denominator."""
    (x1, n1), (x2, n2) = then_nd, now_nd
    if not n1 or not n2:
        return None
    p = (x1 + x2) / (n1 + n2)
    if p <= 0 or p >= 1:
        return 0.0 if x1 / n1 == x2 / n2 else float("inf")
    se = math.sqrt(p * (1 - p) * (1 / n1 + 1 / n2))
    return (x2 / n2 - x1 / n1) / se if se else 0.0


def _direction(then, now, unit, T, higher_is_worse=True, then_nd=None, now_nd=None):
    """'worse' | 'better' | 'flat' | 'new' (no comparable value last month), and the delta."""
    if then is None or now is None:
        return ("new" if then is None and now is not None else "flat"), 0.0
    delta = now - then
    if delta == 0:
        return "flat", 0.0
    if unit == "pct":
        moved = abs(delta) >= T["mom_rate_pt"] and (then == 0 or abs(delta) / max(then, 0.0001) * 100 >= T["mom_rel_pct"])
        if moved and then_nd and now_nd and then_nd[1] and now_nd[1]:
            z = _z(then_nd, now_nd)
            if max(then_nd[0] or 0, now_nd[0] or 0) < T.get("mom_min_failures", 10) or z is None or abs(z) < T.get("mom_rate_z", 2.0):
                moved = False
    elif unit == "s":
        moved = abs(delta) * 1000 >= T["mom_p95_floor_ms"] and (then == 0 or abs(delta) / max(then, 0.0001) * 100 >= T["mom_p95_pct"])
    else:
        moved = (then == 0 and now != 0) or (then != 0 and abs(delta) / abs(then) * 100 >= T["mom_volume_pct"])
    if not moved:
        return "flat", delta
    worse = delta > 0 if higher_is_worse else delta < 0
    return ("worse" if worse else "better"), delta


def _nd(rate):
    return (rate.get("num"), rate.get("den")) if isinstance(rate, dict) and rate.get("den") else None


def apply_aliases(prev, aliases, node_groups):
    """Translate last month's names through services.aliases {old: new} everywhere they appear: services, resources,
    dependency hosts, the health-check proof, finding ids, subjects and metrics. A finding keyed on a service that now
    belongs to a node group is re-keyed on the group, as this month's ids are."""
    if not aliases and not node_groups:
        return prev
    prev = copy.deepcopy(prev)
    aliases = aliases or {}
    al = lambda x: aliases.get(x, x) if isinstance(x, str) else x
    alh = lambda x: (x[: len(x) - len(_pseudo_parts(x)[1])] + al(_pseudo_parts(x)[1])) if isinstance(x, str) and _pseudo_parts(x) else al(x)
    group_of = {m: g for g, grp in (node_groups or {}).items() for m in grp.get("names", grp["members"])}
    joined = set()
    for section in ("services", "resources", "dependencies"):
        if section in prev:
            merged = {}
            for k, v in prev[section].items():
                nk = alh(k) if section == "dependencies" else al(k)
                if section == "dependencies":
                    v = dict(v, callers=[dict(c, service=al(c["service"])) for c in v.get("callers", [])])
                if nk in merged:
                    joined.add(nk)
                merged[nk] = _merge_prev(section, merged[nk], v) if nk in merged else v
            prev[section] = merged
    for p in ((prev.get("window") or {}).get("aggregate") or {}).get("proof") or []:
        p["service"] = al(p.get("service"))
    for f in prev.get("findings", []) + prev.get("also_noted", []):
        det, _, key = f["id"].partition(":")
        subj = f.setdefault("subject", {})
        svc = subj.get("service")
        if svc in aliases:
            subj["service"] = aliases[svc]
            svc = subj["service"]
        if key in aliases:
            key = aliases[key]
        if svc and svc in group_of and det in PER_SERVICE:
            subj["group"] = group_of[svc]
            key = slug(group_of[svc])
        f["id"] = f"{det}:{key}"
        for k in ("nodes", "hosts"):
            if subj.get(k):
                subj[k] = [alh(x) for x in subj[k]]
        if f.get("merged"):
            f["merged"] = [(lambda d_, s_: f"{d_}:{slug(group_of[al(s_)])}" if al(s_) in group_of and d_ in PER_SERVICE else f"{d_}:{al(s_)}")(*m.split(":", 1)) if ":" in m else m
                           for m in f["merged"]]
        metrics = f.get("metrics") or {}
        prim = metrics.get("primary") or {}
        if isinstance(prim.get("subject"), str):
            prim["subject"] = alh(prim["subject"])
        elif isinstance(prim.get("subject"), list) and prim["subject"]:
            prim["subject"] = [al(prim["subject"][0])] + list(prim["subject"][1:])
        for k in ("worst_service", "subject_host"):
            if metrics.get(k):
                metrics[k] = alh(metrics[k])
        for old, new in aliases.items():
            if prim.get("label"):
                prim["label"] = re.sub(r"(?<![\w./{}-])" + re.escape(old) + r"(?![\w./{}-])", new, prim["label"])
        if subj.get("service") in joined or metrics.get("worst_service") in joined:
            prim["joined"] = True          # last month's figure covered one of the joined names: look it up again from the joined rows
    # two of last month's findings that now share an id are one finding (the worse severity, both merged lists)
    rank = {"red": 2, "amber": 1, "info": 0}
    seen = {}
    for key in ("findings", "also_noted"):
        kept = []
        for f in prev.get(key, []):
            g = seen.get(f["id"])
            if g is None:
                seen[f["id"]] = f
                kept.append(f)
                continue
            if rank.get(f.get("severity"), 0) > rank.get(g.get("severity"), 0):
                g["severity"] = f["severity"]
            g["merged"] = list(g.get("merged", [])) + [m for m in f.get("merged", []) if m not in g.get("merged", [])]
            ((g.get("metrics") or {}).get("primary") or {})["joined"] = True
        prev[key] = kept
    return prev


def _merge_prev(section, a, b):
    """Last month's entries for two names that one alias joins: counts add up and rates are recomputed."""
    from .stats import rate
    if section == "resources":
        out = [copy.deepcopy(x) for x in a]
        by_res = {x["resource"]: x for x in out}
        for y in b:
            x = by_res.get(y["resource"])
            if x is None:
                out.append(copy.deepcopy(y))
                by_res[y["resource"]] = out[-1]
                continue
            x["calls"] = x.get("calls", 0) + y.get("calls", 0)
            x["failed"] = x.get("failed", 0) + y.get("failed", 0)
            x["err"] = rate(x["failed"], x["calls"])
            x["p95_s"] = None                       # two operations' percentiles cannot be combined
        return out
    out = copy.deepcopy(a)
    if section == "services":
        for k in ("all", "business"):
            out["requests"][k] = a["requests"].get(k, 0) + b["requests"].get(k, 0)
            out["failed"][k] = a["failed"].get(k, 0) + b["failed"].get(k, 0)
        out["err"] = {k: rate(out["failed"][k], out["requests"][k]) for k in ("all", "business")}
        if a.get("err_base") is not None or b.get("err_base") is not None:
            out["err_base"] = {k: (a.get("err_base") or {}).get(k, a["requests"].get(k, 0)) + (b.get("err_base") or {}).get(k, b["requests"].get(k, 0)) for k in ("all", "business")}
        out["failure_known"] = a.get("failure_known", True) and b.get("failure_known", True)
        out["p95_s"] = None                         # two services' percentiles cannot be combined
        return out
    out["calls"] = a.get("calls", 0) + b.get("calls", 0)
    out["failed"] = a.get("failed", 0) + b.get("failed", 0)
    out["err"] = rate(out["failed"], out["calls"])
    out["callers"] = list(a.get("callers", [])) + list(b.get("callers", []))
    return out


def _probe_check(pw, cw):
    """Compare the health checks both months share. Returns (comparable: True | False | None, note); None = nothing shared."""
    pp = {(p["service"], p["resource"]): p for p in ((pw.get("aggregate") or {}).get("proof") or [])}
    cp = {(p["service"], p["resource"]): p for p in ((cw.get("aggregate") or {}).get("proof") or [])}
    shared = sorted(set(pp) & set(cp))
    if not shared:
        return None, ""
    for key in shared:
        a, b = pp[key]["implied_interval_s"], cp[key]["implied_interval_s"]
        if a and b and abs(a - b) / max(a, b) > 0.01:
            return False, (f"the health-check count for {code(key[0])} {code(key[1])} changed from {n(pp[key]['requests'])} to {n(cp[key]['requests'])} "
                           f"(implied interval {sec(a)} vs {sec(b)}): the export ranges differ or the probe cadence changed; volumes are not compared")
    return True, ""


def _range_known(a):
    return a.get("source") in ("proved", "asserted") and a.get("confidence") != "contradicted"


def _services_list_ranges_match(prev, F):
    a = ((prev.get("window") or {}).get("services_list") or {}).get("days_estimate")
    b = ((F.get("window") or {}).get("services_list") or {}).get("days_estimate")
    return bool(a and b and abs(a - b) <= 0.1 * max(a, b))


def compare_facts(F, prev, cfg):
    """Fill F['mom'] with service/domain/kpi/dependency deltas and regressions (before detectors run)."""
    from .detectors import kpi_labels
    T = cfg["thresholds"]
    pw, cw = prev.get("window", {}), F["window"]
    same_tier = pw.get("volumes_tier") == cw["volumes_tier"]
    pagg, cagg = (pw.get("aggregate") or {}), (cw.get("aggregate") or {})
    pdays, cdays = pagg.get("days"), cagg.get("days")
    probes_ok, probe_note = _probe_check(pw, cw)
    if same_tier and cw["volumes_tier"] == "aggregate":
        ranges_known = probes_ok is True or (probes_ok is None and _range_known(pagg) and _range_known(cagg))
    else:
        ranges_known = False
    vol_ok = bool(ranges_known and pdays and cdays and abs(pdays - cdays) <= 0.1 * max(pdays, cdays))
    per_day = bool(ranges_known and pdays and cdays and not vol_ok and probes_ok is not False)
    if same_tier and cw["volumes_tier"] == "sample":
        pm, cm = (pw.get("spans") or {}).get("minutes") or 0, cw["spans"]["minutes"]
        note = "both months are span samples: rates and percentiles are compared, volumes are not" + (f" (sample lengths differ: {pm:.0f} vs {cm:.0f} minutes)" if pm and abs(pm - cm) / max(pm, cm) > 0.5 else "")
    elif not same_tier:
        tw = {"aggregate": "the Endpoints list", "services_list": "the Services list", "sample": "a span sample only"}
        note = (f"last month's volumes came from {tw.get(pw.get('volumes_tier'), 'other exports')} and this month's from {tw.get(cw['volumes_tier'], 'other exports')}; "
                "only rates and p95 are compared")
    elif probes_ok is False:
        note = probe_note
    elif not ranges_known:
        note = "the export range of at least one month is neither proved nor stated, so volumes are not compared"
    else:
        note = "" if vol_ok else f"the export windows differ ({pdays} vs {cdays} days); volumes are compared per day"
    prev_fs = bool((prev.get("coverage") or {}).get("failure_signal", True))
    cur_fs = bool(F["coverage"].get("failure_signal"))
    rates_ok = prev_fs and cur_fs
    if not rates_ok:
        note = (note + "; " if note else "") + "failure rates are not compared because " + ("this month's" if not cur_fs else "last month's") + " export has no status code or error column"
    prev_rule, cur_rule = (prev.get("meta") or {}).get("failure_rule"), F["meta"].get("failure_rule")
    rule_changed = bool(prev_rule and cur_rule and prev_rule != cur_rule)
    if rule_changed and rates_ok:
        rates_ok = False
        note = (note + "; " if note else "") + f"failure rates are not compared because what counts as a failure changed: last month {rule_words(prev_rule)}, this month {rule_words(cur_rule)}"
    pt, ct = (prev.get("meta") or {}).get("thresholds") or {}, F["meta"].get("thresholds") or {}
    thresholds_changed = {k: [pt[k], ct[k]] for k in sorted(set(pt) & set(ct)) if pt[k] != ct[k]}
    targets_changed, renamed = [], {}
    if isinstance(prev.get("kpis"), dict):
        klabels = kpi_labels(F)
        pk_all, ck_all = prev["kpis"], F["kpis"]
        for ck in sorted(set(ck_all) - set(pk_all)):
            for pk_key in sorted(set(pk_all) - set(ck_all) - set(renamed.values())):
                if set(ck_all[ck].get("matched") or []) & set(pk_all[pk_key].get("matched") or []):
                    renamed[ck] = pk_key
                    break
        for key in sorted(set(ck_all) | (set(pk_all) - set(renamed.values()))):
            k = ck_all.get(key) or {}
            pk = pk_all.get(renamed.get(key, key)) or {}
            for field in ("slo_p95_s", "slo_success_pct"):
                a, b = pk.get(field), k.get(field)
                if a != b and (pk or b is not None) and (k or a is not None):
                    label = klabels.get(key) or (str(pk.get("label")) if pk.get("label") and not str(pk.get("label")).startswith("_todo") else code(key))
                    targets_changed.append({"kpi": key, "label": label, "field": field, "then": a, "now": b})
    prev_path = prev.get("meta", {}).get("_path")
    mom = {"previous": {"path": os.path.basename(prev_path) if prev_path else None, "month": prev.get("meta", {}).get("month", "last month"), "month_key": prev.get("meta", {}).get("month_key"),
                        "kit_version": prev.get("meta", {}).get("kit_version"), "config_sha256": prev.get("meta", {}).get("config_sha256"), "window_days": pdays,
                        "failure_signal": prev_fs, "had_findings": bool(prev.get("findings"))},
           "comparable": {"volumes": vol_ok, "rates": rates_ok, "p95": True, "note": note, "per_day": per_day, "services_list": _services_list_ranges_match(prev, F)},
           "config_changed": bool(prev.get("meta", {}).get("config_sha256") and F["meta"].get("config_sha256") and prev["meta"]["config_sha256"] != F["meta"]["config_sha256"]),
           "thresholds_changed": thresholds_changed, "targets_changed": targets_changed, "kpi_renamed": renamed if isinstance(prev.get("kpis"), dict) else {}, "rule_changed": [prev_rule, cur_rule] if rule_changed else None,
           "services": {}, "domains": {}, "kpis": {}, "dependencies": {}, "moved": [], "regressions": [],
           "findings": {"closed": [], "new": [], "persisting": [], "demoted": [], "promoted": [], "unmeasured": [], "first_measured": []}}
    prev_services = prev.get("services", {})
    for name, s in F["services"].items():
        if s["role"] != "entry":
            continue
        p = prev_services.get(name)
        if not p:
            mom["services"][name] = {"status": "new"}
            continue
        entry = {"status": "seen"}
        both_counted = (p.get("requests") or {}).get("tier", "aggregate") == "aggregate" and s["requests"].get("tier") == "aggregate"
        if (vol_ok or per_day) and both_counted:
            then_v, now_v = p["requests"]["all"], s["requests"]["all"]
            if per_day:
                then_v, now_v = then_v / pdays, now_v / cdays
            d, delta = _direction(then_v, now_v, "count", T, higher_is_worse=False)
            entry["requests"] = {"then": p["requests"]["all"], "now": s["requests"]["all"], "direction": d}
            if d not in ("flat", "new"):
                mom["moved"].append({"where": code(name), "metric": "requests" + (" per day" if per_day else ""), "then": then_v, "now": now_v, "then_text": n(then_v), "now_text": n(now_v),
                                     "change_text": _chg(f"{'+' if delta > 0 else ''}{100.0 * delta / max(then_v, 1):.0f} %"), "weight": s["requests"]["all"]})
        full = lambda x: (x.get("err_base") or {}).get("business", x["requests"]["business"]) == x["requests"]["business"]
        if rates_ok and p.get("failure_known", True) and s.get("failure_known", True) and full(p) and full(s) and p["err"]["business"].get("den") and s["err"]["business"].get("den"):
            pe, ce = p["err"]["business"], s["err"]["business"]
            moved_cls = _class_moved(prev.get("resources", {}).get(name), F["resources"].get(name))
            if moved_cls:                      # an operation became (or stopped being) noise: compare on the ones both months judged
                pe, ce = _business_rate(prev.get("resources", {}).get(name), moved_cls), _business_rate(F["resources"].get(name), moved_cls)
            then_e, now_e = pe["pct"], ce["pct"]
            d, delta = _direction(then_e, now_e, "pct", T, then_nd=_nd(pe), now_nd=_nd(ce)) if pe.get("den") and ce.get("den") else ("flat", 0.0)
            entry["err"] = {"then": then_e, "now": now_e, "direction": d}
            if d not in ("flat", "new"):
                mom["moved"].append({"where": code(name), "metric": "failure rate", "then": then_e, "now": now_e, "then_text": pct(then_e), "now_text": pct(now_e),
                                     "change_text": _chg(f"{'+' if delta > 0 else ''}{delta:.2f} pt"), "weight": s["requests"]["all"] * (2 if d == "worse" else 1)})
                if moved_cls:
                    shorts = sorted(r["short"] for r in F["resources"].get(name, []) if r["resource"] in moved_cls)
                    mom["moved"][-1]["note"] = (f"{code(name)} is compared without {join_names([code(x) for x in shorts[:3]])}, which one month counts as noise; "
                                                f"last month's review quoted {pct(p['err']['business'])} for the service")
                if d == "worse" and then_e > 0 and now_e / then_e >= T["regression_err_x"]:
                    mom["regressions"].append({"service": name, "metric": "failure rate", "then": then_e, "now": now_e, "unit": "pct", "then_text": pct(then_e), "now_text": pct(now_e),
                                               "change_text": _chg(f"+{delta:.2f} pt"), "direction_word": "rose", "weight": s["requests"]["all"], "crosses_red": now_e >= T["fail_red_pct"]})
        then_p, now_p = p.get("p95_s"), s.get("p95_s")
        comparable_p95 = (p.get("p95_source") or "").split(" ")[0] == (s.get("p95_source") or "").split(" ")[0]
        if _class_moved(prev.get("resources", {}).get(name), F["resources"].get(name)):
            comparable_p95 = False             # an operation became (or stopped being) noise: the pooled p95 is a different population
        if (s.get("p95_source") or "").startswith("sample") and min((p.get("spans") or {}).get("entry", 0), (s.get("spans") or {}).get("entry", 0)) < T["min_entry_spans_for_anatomy"]:
            comparable_p95 = False             # too few sampled requests for a p95 to move
        d, delta = _direction(then_p, now_p, "s", T) if comparable_p95 and then_p is not None else ("flat", 0.0)
        if d in ("worse", "better") and _ci_overlap(p.get("p95_ci_s"), s.get("p95_ci_s")):
            d = "flat"                          # the two sampled p95s are within each other's sampling uncertainty
        entry["p95"] = {"then": then_p, "now": now_p, "direction": d, "comparable": comparable_p95}
        if d not in ("flat", "new"):
            mom["moved"].append({"where": code(name), "metric": "p95", "then": then_p, "now": now_p, "then_text": sec(then_p), "now_text": sec(now_p),
                                 "change_text": _chg(f"{'+' if delta > 0 else ''}{100.0 * delta / max(then_p, 0.001):.0f} %"), "weight": s["requests"]["all"]})
            if d == "worse" and then_p and 100.0 * delta / then_p >= T["regression_p95_pct"]:
                mom["regressions"].append({"service": name, "metric": "p95", "then": then_p, "now": now_p, "unit": "s", "then_text": sec(then_p), "now_text": sec(now_p),
                                           "change_text": _chg(f"+{100.0 * delta / then_p:.0f} %"), "direction_word": "rose", "weight": s["requests"]["all"] / 2, "crosses_red": now_p >= T["outlier_p95_s"]})
        mom["services"][name] = entry
    for name in prev_services:
        if prev_services[name].get("role") == "entry" and name not in F["services"]:
            mom["services"][name] = {"status": "gone"}
    for key, d in F["domains"].items():
        p = prev.get("domains", {}).get(key)
        if p:
            row = {"requests": {"then": p["requests"]["all"], "now": d["requests"]["all"]}}
            same_base = p.get("failure_known", True) and d.get("failure_known", True) and sorted(p.get("failure_not_recorded") or []) == sorted(d.get("failure_not_recorded") or []) \
                and bool(p["err"]["business"].get("den")) and bool(d["err"]["business"].get("den")) \
                and not any(_class_moved(prev.get("resources", {}).get(m), F["resources"].get(m)) for m in d.get("counted_services") or d.get("services") or [])
            if rates_ok and same_base:
                pe, ce = p["err"]["business"], d["err"]["business"]
                dd, _ = _direction(pe["pct"], ce["pct"], "pct", T, then_nd=_nd(pe), now_nd=_nd(ce))
                row["err"] = {"then": pe["pct"], "now": ce["pct"], "direction": dd}
            mom["domains"][key] = row
    labels = kpi_labels(F)
    for key, k in F["kpis"].items():
        p = prev.get("kpis", {}).get(renamed.get(key, key))
        same_ops = p is not None and sorted(p.get("matched") or []) == sorted(k.get("matched") or [])      # an operation moved to noise changes the KPI
        if p and same_ops and rates_ok and p.get("failure_known", True) and k.get("failure_known", True) and (p.get("err") or {}).get("den") and (k.get("err") or {}).get("den"):
            dd, delta = _direction(p["err"]["pct"], k["err"]["pct"], "pct", T, then_nd=_nd(p["err"]), now_nd=_nd(k["err"]))
            mom["kpis"][key] = {"err": {"then": p["err"]["pct"], "now": k["err"]["pct"], "direction": dd}}
            if dd not in ("flat", "new"):
                where = labels.get(key) or code(key)
                mom["moved"].append({"where": f"{where} (operation)", "metric": "failure rate", "then": p["err"]["pct"], "now": k["err"]["pct"], "then_text": pct(p["err"]["pct"]), "now_text": pct(k["err"]["pct"]),
                                     "change_text": _chg(f"{'+' if delta > 0 else ''}{delta:.2f} pt"), "weight": k["calls"] * 3})
    for name, d in F["dependencies"].items():
        p = prev.get("dependencies", {}).get(name)
        if p and p.get("class", d.get("class")) == d.get("class") and d.get("failure_known", True) and p.get("failure_known", True) and p.get("tier", d.get("tier")) == d.get("tier") and not (rule_changed and d.get("tier") == "sample") \
                and (p.get("err") or {}).get("den") and (d.get("err") or {}).get("den"):
            dd, delta = _direction(p["err"]["pct"], d["err"]["pct"], "pct", T, then_nd=_nd(p["err"]), now_nd=_nd(d["err"]))
            mom["dependencies"][name] = {"err": {"then": p["err"]["pct"], "now": d["err"]["pct"], "direction": dd}}
            if dd not in ("flat", "new") and max(p["err"]["pct"], d["err"]["pct"]) >= 1:
                mom["moved"].append({"where": f"{code(name)} (dependency)", "metric": "failure rate", "then": p["err"]["pct"], "now": d["err"]["pct"], "then_text": pct(p["err"]["pct"]), "now_text": pct(d["err"]["pct"]),
                                     "change_text": _chg(f"{'+' if delta > 0 else ''}{delta:.2f} pt"), "weight": d["calls"]})
    mom["moved"].sort(key=lambda r: (-r["weight"], r["where"], r["metric"]))
    F["mom"] = mom
    return mom


def _same_subject(a, b):
    if a is None or b is None:
        return None
    if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
        return list(a) == list(b)
    return a == b


def _is_failure_finding(f):
    prim = ((f.get("metrics") or {}).get("primary") or {})
    return f.get("detector") in FAILURE_DETECTORS or (f.get("detector") == "failing_dependencies" and prim.get("unit") == "pct")


def _then_for(F, prev, p, f):
    """Last month's value of THIS month's primary metric on THIS month's subject: (value, text, (num, den) or None).
    The same metric means the same unit and label on the same subject (and, for per-service findings, the same service or
    node). When the subject changed (a different worst operation, host or node), last month's figure for this month's
    subject is looked up (the metric label, printed beside it, names that subject); when there is none, the metric
    differs, or the two months' figures cover different ranges, the result is (None, '—', None)."""
    pp = (p.get("metrics") or {}).get("primary", {}) or {}
    prim = f["metrics"].get("primary", {})
    unit, punit = prim.get("unit", "count"), pp.get("unit", "count")
    comp = (F.get("mom") or {}).get("comparable") or {}
    none = (None, "—", None)
    det = f["detector"]
    if det == "slow_hot_paths" and unit == "pct" and not f["metrics"].get("outlier_only"):
        svc_ = f["subject"].get("service")
        kpi_now = {r["resource"]: r.get("kpi") for r in F["resources"].get(svc_, [])}
        rows_ = [dict(r, kpi=kpi_now.get(r["resource"], r.get("kpi"))) for r in (prev.get("resources") or {}).get(svc_) or []]   # this month's KPIs
        v = _slow_share(rows_, (F.get("meta") or {}).get("thresholds") or {}, F["kpis"]) if rows_ else None
        return (round(v, 1), pct(round(v, 1)), None) if v is not None else none
    if det == "egress_and_telemetry":
        pin, cin = bool(((prev.get("coverage") or {}).get("inputs") or {}).get("inferred")), bool(F["coverage"]["inputs"].get("inferred"))
        if pin != cin or any(_was_unnamed(F, prev, h) for h in _hosts_of(f)) or _caller_now_unnamed(F, prev, _hosts_of(p)):
            return none                               # a host one month could see and the other could not is not a change
        pd_ = prev.get("dependencies") or {}
        if any(h in pd_ and h in F["dependencies"] and (pd_[h].get("class") != F["dependencies"][h].get("class") or bool(pd_[h].get("allowed")) != bool(F["dependencies"][h].get("allowed")))
               for h in set(_hosts_of(f)) | set(_hosts_of(p))):
            return none                               # a host the config reclassified or allow-listed is not a change in what production calls
    if det == "foundations":
        pm, cm = (p.get("metrics") or {}), f["metrics"]
        if pm.get("checkable") is None or sorted(pm.get("checkable") or []) != sorted(cm.get("checkable") or []):
            return none                               # a gap that could not be checked in one month is not an improvement
        return pp.get("value"), _fmt(pp.get("value"), punit), None
    if unit in VOLUME_METRICS and det != "egress_and_telemetry":
        tier = prim.get("tier", "aggregate")
        if tier == "sample" or pp.get("tier") == "sample":
            return none                               # a sample count is never compared with another month's
        if tier == "services_list" and not comp.get("services_list"):
            return none
        if tier == "aggregate" and not comp.get("volumes"):
            return none
    if det == "failing_dependencies" and unit == "pct" and (F.get("mom") or {}).get("rule_changed") and (F["dependencies"].get(f["metrics"].get("subject_host")) or {}).get("tier") == "sample":
        return none                                   # what counts as a failure changed between the two months
    if det == "failing_dependencies" and unit == "pct":
        host = f["metrics"].get("subject_host")
        pd, cd = _prev_dep_for(F, prev, p, host), F["dependencies"].get(host)
        if host and not (pd and cd and pd.get("failure_known", True) and cd.get("failure_known", True)):
            return none                               # each month must have measured this host's failures
    elif unit == "pct" and _is_failure_finding(f) and not comp.get("rates", True):
        return none
    same = _same_subject(pp.get("subject"), prim.get("subject"))
    if same is None:                                  # facts written before subjects were recorded: fall back to the label
        same = pp.get("label") == prim.get("label")
    same = same and pp.get("label", prim.get("label")) == prim.get("label")
    if det in PER_SERVICE:
        same = same and (p.get("subject") or {}).get("service") == f["subject"].get("service")
    if same and unit == punit and not pp.get("joined"):
        nd = (pp.get("num"), pp.get("den")) if pp.get("den") else None
        return pp.get("value"), _fmt(pp.get("value"), punit), nd
    svc = f["subject"].get("service")
    if det == "failing_operations" and unit == "pct":
        res = f["metrics"].get("worst_resource")
        for r in prev.get("resources", {}).get(svc, []) or []:
            if r["resource"] == res and r.get("failure_known", True):
                return r["err"]["pct"], pct(r["err"]["pct"]), _nd(r["err"])
    elif det == "slow_hot_paths" and unit == "s":
        res = f["metrics"].get("worst_resource")
        for r in prev.get("resources", {}).get(svc, []) or []:
            if r["resource"] == res and r.get("p95_s") is not None:
                return r["p95_s"], sec(r["p95_s"]), None
    elif det == "slow_operations" and unit == "s":
        wsvc, res = f["metrics"].get("worst_service"), f["metrics"].get("worst_resource")
        for r in prev.get("resources", {}).get(wsvc, []) or []:
            if r["resource"] == res and r.get("p95_s") is not None:
                return r["p95_s"], sec(r["p95_s"]), None
    elif det == "failing_dependencies" and unit == "pct":
        host = f["metrics"].get("subject_host")
        d = _prev_dep_for(F, prev, p, host)
        if d and d.get("failure_known", True):
            return d["err"]["pct"], pct(d["err"]["pct"]), _nd(d["err"])
    elif det == "nonprod_from_prod":
        host = f["metrics"].get("subject_host")
        d = prev.get("dependencies", {}).get(host)
        ok = comp.get("services_list") if (d or {}).get("tier") == "services_list" else (comp.get("volumes") and (d or {}).get("tier") == "aggregate")
        if d and ok:
            return d["calls"], n(d["calls"]), None
    return none


_PSEUDO = (("outbound calls", "http"), ("database calls", "db"), ("cache calls", "cache"), ("queue messages", "queue"), ("secret-store reads", "secret_store"))


def _pseudo_parts(name):
    """'database calls of shop-api' -> ('db', 'shop-api'); None for a named host."""
    for label, kind in _PSEUDO:
        if str(name).startswith(label + " of "):
            return kind, str(name)[len(label) + 4:]
    return None


def _dep_kind(name, d):
    pp = _pseudo_parts(name)
    if pp:
        return pp[0]
    t = (d.get("type") or "").lower()
    return "db" if t in ("db", "sql", "database") else ("cache" if t in ("cache", "redis", "memcached") else (t if t in ("queue", "secret_store") else "http"))


def _sum_rows(rows):
    from .stats import rate
    calls, failed = sum(r["calls"] for r in rows), sum(r["failed"] for r in rows)
    return {"calls": calls, "failed": failed, "err": rate(failed, calls), "failure_known": all(r.get("failure_known", True) for r in rows),
            "p95_s": None, "callers": [], "tier": "sample", "source": "spans"}


def _unnamed(name, d, svcs):
    """(kind, callers) when a dependency does not name the host it reaches: a pseudo name ('outbound calls of x') or an
    integration library's own service name ('orders-web-http-client', 'postgresql'); None for a named host."""
    pp = _pseudo_parts(name)
    if pp:
        return pp[0], {pp[1]}
    if ((svcs or {}).get(name) or {}).get("role") == "integration":
        return _dep_kind(name, d), {c["service"] for c in d.get("callers", [])}
    return None


def _caller_row(c, x):
    return {"calls": c["calls"], "failed": (c.get("err") or {}).get("num", 0), "failure_known": c.get("failure_known", x.get("failure_known", True))}


def _equivalent(src, name, dst, src_svcs=None, dst_svcs=None):
    """The same calls in another month's dependencies: the same name, or the same callers' calls of the same kind, named
    (peer.service) in one month and unnamed in the other. None when they cannot be isolated."""
    if name in dst:
        return dst[name]
    d = src.get(name) or {}
    un = _unnamed(name, d, src_svcs)
    if un is None:
        kind, rows = _dep_kind(name, d), []
        for c in [x["service"] for x in d.get("callers", [])]:
            cands = [k for k, x in dst.items() if (lambda u: u is not None and u[0] == kind and c in u[1])(_unnamed(k, x, dst_svcs))]
            if not cands:
                continue
            if any(h != name and _unnamed(h, x, src_svcs) is None and _dep_kind(h, x) == kind and any(y["service"] == c for y in x.get("callers", [])) for h, x in src.items()):
                return None                 # that caller's unnamed calls also reached other hosts: not this host's figure
            for k in cands:
                x = dst[k]
                own = [_caller_row(y, x) for y in x.get("callers", []) if y["service"] == c]
                rows.extend(own or ([x] if _pseudo_parts(k) else []))
        return _sum_rows(rows) if rows else None
    kind, callers = un
    rows = [_caller_row(c, x) for h, x in dst.items() if _unnamed(h, x, dst_svcs) is None and _dep_kind(h, x) == kind for c in x.get("callers", []) if c["service"] in callers]
    return _sum_rows(rows) if rows else None


def _was_unnamed(F, prev, host):
    """This month's host was reached last month by the same callers through calls of the same kind that named no host."""
    d = F["dependencies"].get(host) or {}
    kind, callers = _dep_kind(host, d), {c["service"] for c in d.get("callers", [])}
    return any((lambda u: u is not None and u[0] == kind and u[1] & callers)(_unnamed(k, x, prev.get("services"))) for k, x in (prev.get("dependencies") or {}).items())


def _caller_now_unnamed(F, prev, hosts):
    """A caller of last month's hosts whose calls of that kind this month carry no host name, or None."""
    pdeps = prev.get("dependencies") or {}
    for h in hosts:
        kind = _dep_kind(h, pdeps.get(h) or {})
        for c in (pdeps.get(h) or {}).get("callers", []):
            if any((lambda u: u is not None and u[0] == kind and c["service"] in u[1])(_unnamed(k, x, F.get("services"))) for k, x in F["dependencies"].items()):
                return c["service"]
    return None


def _prev_dep_for(F, prev, p, host):
    """Last month's figure for this month's host (see _equivalent)."""
    return _equivalent(F["dependencies"], host, prev.get("dependencies", {}) or {}, F.get("services"), prev.get("services"))


def _now_dep_for(F, prev, host):
    """This month's figure for last month's host (see _equivalent)."""
    return (_equivalent(prev.get("dependencies", {}) or {}, host, F["dependencies"], prev.get("services"), F.get("services"))
            if prev is not None else F["dependencies"].get(host))


def _slow_share(rows, T, kpis):
    """The slow-service share as the detector measures it: business calls on operations above slow_p95_s, operations within
    their agreed p95 target left out; None when nothing is timed."""
    biz = [r for r in rows if r.get("class") == "business" and r.get("p95_s") is not None and r.get("calls")
           and (r.get("tier") == "aggregate" or (r.get("sample") or {}).get("n", 0) >= 20)]
    total = sum(r["calls"] for r in rows if r.get("class") == "business" and r.get("calls")) if biz else 0
    if not total:
        return None
    return round(100.0 * sum(r["calls"] for r in biz if r["p95_s"] >= T["slow_p95_s"] and not _agreed_ok(r, kpis)) / total, 2)


def _class_moved(a, b):
    """Operations present in both months that are business in one and noise in the other (a by-design rule, a new pattern)."""
    ca = {r["resource"]: r.get("class") == "business" for r in a or []}
    cb = {r["resource"]: r.get("class") == "business" for r in b or []}
    return {res for res in set(ca) & set(cb) if ca[res] != cb[res]}


def _business_rate(rows, skip):
    from .stats import rate
    keep = [r for r in rows or [] if r.get("class") == "business" and r.get("failure_known", True) and r["resource"] not in skip]
    return rate(sum(r.get("failed", 0) for r in keep), sum(r.get("calls", 0) for r in keep))


def _agreed_ok(r, kpis):
    """An operation within the p95 target agreed for its KPI: the slow detectors leave it out."""
    t = ((kpis or {}).get(r.get("kpi")) or {}).get("slo_p95_s") if r.get("kpi") else None
    return t is not None and r.get("p95_s") is not None and r["p95_s"] <= t


def _ci_overlap(a, b):
    """Two sample p95 intervals that overlap: the change is within sampling noise."""
    return bool(a and b and len(a) == 2 and len(b) == 2 and a[0] <= b[1] and b[0] <= a[1])


def _p95_noisy(F, prev, prim, T):
    """Whether a finding's p95 trend on one operation is within sampling noise: either month's p95 comes from fewer sampled
    calls than the anatomy floor, the two months' p95s come from different sources, or their sample intervals overlap."""
    subj = prim.get("subject")
    if not (isinstance(subj, list) and len(subj) == 2):
        return False
    svc, res = subj
    now_r = next((r for r in F["resources"].get(svc, []) if r["resource"] == res), None)
    then_r = next((r for r in (prev.get("resources") or {}).get(svc, []) or [] if r["resource"] == res), None)
    if not now_r or not then_r:
        return False
    srcs = {now_r.get("p95_source"), then_r.get("p95_source")}
    if "sample" not in srcs:
        return False
    if srcs != {"sample"}:
        return True                         # a weekly p95 one month and a sampled one the other: not the same figure
    if min((now_r.get("sample") or {}).get("n") or 0, (then_r.get("sample") or {}).get("n") or 0) < T["min_entry_spans_for_anatomy"]:
        return True
    return _ci_overlap((then_r.get("sample") or {}).get("p95_ci_s"), (now_r.get("sample") or {}).get("p95_ci_s"))


def _targets_touch(F, f, prev=None):
    """Whether an agreed target that changed since last month belongs to an operation of this finding's service, in either
    month (a KPI deleted this month is tagged on last month's rows only)."""
    changed = {(c["kpi"], c["field"]) for c in (F.get("mom") or {}).get("targets_changed") or []}
    field = "slo_success_pct" if f["detector"] == "failing_operations" else "slo_p95_s"
    svc = f["subject"].get("service") or f["metrics"].get("worst_service")
    rows = list(F["resources"].get(svc, [])) + list(((prev or {}).get("resources") or {}).get(svc, []) or [])
    return bool(changed) and any((r.get("kpi"), field) in changed for r in rows if r.get("kpi"))


def _share_noisy(F, prev, f, T):
    """A slow share whose change comes only from operations whose sampled p95 crossed slow_p95_s within its sampling noise."""
    svc = f["subject"].get("service")
    now_r = {r["resource"]: r for r in F["resources"].get(svc, [])}
    then_r = {r["resource"]: r for r in (prev.get("resources") or {}).get(svc, []) or []}
    thr = T["slow_p95_s"]
    crossed = [res for res in set(now_r) & set(then_r) if now_r[res].get("p95_s") is not None and then_r[res].get("p95_s") is not None
               and (now_r[res]["p95_s"] >= thr) != (then_r[res]["p95_s"] >= thr)]
    return bool(crossed) and all(_p95_noisy(F, prev, {"subject": [svc, res]}, T) for res in crossed)


def _old_thresholds(prev, T):
    old = dict(T)
    old.update((prev.get("meta") or {}).get("thresholds") or {})
    return old


def _still_fires(F, p, T, prev=None, kpis=None):
    """Whether last month's finding's own metric would still raise it under thresholds T and the agreed p95 targets in
    kpis (this month's by default; {} for none) (True / False), or None for a detector whose figure this check does not measure."""
    kpis = F["kpis"] if kpis is None else kpis
    det = p.get("detector")
    unit = ((p.get("metrics") or {}).get("primary") or {}).get("unit")
    now = _current_value(F, p, T, prev, kpis)
    if det == "failing_operations":
        svc_ = (p.get("subject") or {}).get("service")
        row = next((r for r in F["resources"].get(svc_, []) if r["resource"] == (p.get("metrics") or {}).get("worst_resource")), None)
        key = row.get("kpi") if row else None
        tgt = ((kpis or {}).get(key) or {}).get("slo_success_pct") if key else None
        miss = False
        if tgt is not None:
            from .detectors import _beyond_noise, _kpi_achieved
            k_, ach = _kpi_achieved(F, row, svc_)
            miss = bool(ach.get("den")) and 100 - ach["pct"] < tgt
            if miss and (k_.get("tier") or row.get("tier")) == "sample":
                miss = _beyond_noise(ach, tgt, T)
        return now is not None and (now >= T["fail_amber_pct"] or miss)
    if det == "slow_operations":
        wsvc, wres = (p.get("metrics") or {}).get("worst_service"), (p.get("metrics") or {}).get("worst_resource")
        row = next((r for r in F["resources"].get(wsvc, []) if r["resource"] == wres), None)
        if now is not None and row is not None and _agreed_ok(row, kpis):
            return False                    # within the target the team agreed for it
        tgt = ((kpis or {}).get(row.get("kpi")) or {}).get("slo_p95_s") if row and row.get("kpi") else None
        return now is not None and (now >= T["slow_op_s"] or (tgt is not None and now > tgt))
    if det == "failing_dependencies":
        return now is not None and now >= (T["dep_fail_pct"] if unit == "pct" else T["dep_slow_s"])
    if det == "slow_hot_paths" and (p.get("metrics") or {}).get("outlier_only"):
        svc_ = (p.get("subject") or {}).get("service")
        res_ = set((p.get("metrics") or {}).get("outlier_resources") or [])
        return any(r["resource"] in res_ and r.get("p95_s") is not None and r["p95_s"] >= T["outlier_p95_s"] and not _agreed_ok(r, kpis) for r in F["resources"].get(svc_, []))
    if det == "slow_hot_paths":
        return now is not None and now >= T["slow_share_amber_pct"]
    if det == "chatty_requests":
        a = F["anatomy"].get((p.get("subject") or {}).get("service")) or {}
        if a.get("requests_with_tree", a.get("entries", 0)) < T["min_entry_spans_for_anatomy"]:
            return False                    # too few requests examined to say it still fires
        cpr = a.get("children_per_request") or {}
        sr = (a.get("secret_reads_per_request") or {}).get("value") or 0
        reps = [rc for rc in a.get("repeated_children") or [] if rc.get("median", 0) >= T["repeat_child_per_entry"] or (rc.get("traces_at_or_above") or {}).get("pct", 0) >= 20]
        return any(((cpr.get(k) or {}).get("value") or 0) >= T[t] for k, t in (("db", "db_per_request"), ("http", "http_per_request"), ("cache", "cache_per_request"))) \
            or bool(reps) or sr >= T["secret_reads_per_request"]
    return None


def _hosts_of(p):
    metrics = p.get("metrics") or {}
    return [metrics["subject_host"]] if metrics.get("subject_host") else list((p.get("subject") or {}).get("hosts") or [])


def _unmeasured_reason(F, prev, p, cur_fs):
    """Why last month's finding cannot be checked this month, or None when it can (so 'closed' would be true)."""
    det = p.get("detector")
    skipped = {d["name"]: d for d in F["coverage"].get("detectors_skipped", [])}
    metrics = p.get("metrics") or {}
    prim = metrics.get("primary") or {}
    if _is_failure_finding(p) and det != "failing_dependencies" and not cur_fs:
        return NO_SIGNAL
    if det in skipped:
        why = skipped[det]["reason"]
        return {"needs a failure signal": NO_SIGNAL, "needs status codes": "the export has no status code column",
                "needs caller data": "the exports do not show which service calls which",
                "needs child spans with their parents": "the export has no parent and child spans"}.get(why, "this month's exports cannot check it")
    if det in ("failing_dependencies", "nonprod_from_prod", "egress_and_telemetry"):
        hosts = _hosts_of(p)
        present = [h for h in hosts if (_now_dep_for(F, prev, h) if det == "failing_dependencies" else h in F["dependencies"])]
        if hosts and not present:
            had_inferred = (prev.get("coverage") or {}).get("inputs", {}).get("inferred")
            if had_inferred and not F["coverage"]["inputs"].get("inferred"):
                return "this month's exports do not include the inferred services list"
            if not F["coverage"].get("host_visibility"):
                return "this month's export does not name the hosts its services call"
            if det in ("nonprod_from_prod", "egress_and_telemetry"):
                c_ = _caller_now_unnamed(F, prev, hosts)
                if c_:
                    return f"this month's export does not name the hosts {c_} calls"
            pdeps = prev.get("dependencies") or {}
            callers = sorted({c["service"] for h in hosts for c in (pdeps.get(h) or {}).get("callers", [])})
            out_ = [c for c in callers if c in (F["meta"].get("filtered_out_services") or [])]
            if callers and len(out_) == len(callers):
                return f"{join_names(out_[:3])} {'is' if len(out_) == 1 else 'are'} left out of this review's scope by the config"
            if callers and not any(F["resources"].get(c) or (F["services"].get(c) or {}).get("spans", {}).get("rows") for c in callers):
                return f"this month's span sample does not include {join_names(callers[:3])}"
        if det == "failing_dependencies" and prim.get("unit") == "pct" and present and not any(_now_dep_for(F, prev, h).get("failure_known", True) for h in present):
            return NO_SIGNAL if not cur_fs else f"this month's calls to {join_names(present)} carry no status code or error flag"
        if hosts and not present:
            return None
    svc = (p.get("subject") or {}).get("service") or metrics.get("worst_service")
    if det == "node_imbalance":
        g = (p.get("subject") or {}).get("group")
        pg = (prev.get("node_groups") or {}).get(g) or {}
        nodes = [svc] + sorted(pg.get("members") or {})
        out_ = [x for x in nodes if x in (F["meta"].get("filtered_out_services") or [])]
        if out_:
            return f"{out_[0]} is left out of this review's scope by the config"
        gone = [x for x in nodes if x and x not in F["services"]]
        if gone:
            return f"{gone[0]} is not in this month's exports"
    if svc and (det in PER_SERVICE or det == "slow_operations") and det not in ("node_imbalance",):
        if svc in (F["meta"].get("filtered_out_services") or []):
            return f"{svc} is left out of this review's scope by the config"
        if svc not in F["services"]:
            return f"{svc} is not in this month's exports"
        if not F["resources"].get(svc):
            return f"this month's span sample" + (" and Endpoints list do" if F["coverage"]["inputs"].get("endpoints") else " does") + f" not include {svc}"
    if _is_failure_finding(p) and not cur_fs:
        return NO_SIGNAL
    rc = (F.get("mom") or {}).get("rule_changed")
    if rc and (_is_failure_finding(p) or det == "client_errors"):
        return "what counts as a failure changed since last month, so last month's figure is not comparable"
    if _is_failure_finding(p) and svc and not (F["services"].get(svc) or {}).get("failure_known", True):
        return f"this month's spans of {svc} carry no status code or error flag"
    if det == "failing_operations" and svc:
        row_ = next((r for r in F["resources"].get(svc, []) if r["resource"] == metrics.get("worst_resource")), None)
        if row_ is not None and not row_.get("failure_known", True):
            return f"this month's spans of {row_['short']} carry no status code or error flag"
    if det == "foundations":
        from .detectors import foundations_checkable
        then_c, now_c = set(metrics.get("checkable") or []), set(foundations_checkable(F))
        if then_c - now_c:
            return "this month's exports cannot check every gap"
    if prim.get("unit") == "count" and prim.get("tier") == "aggregate" and not F["coverage"]["inputs"].get("endpoints"):
        return "this month's exports do not include the Endpoints list"
    return None


def join_names(xs):
    xs = list(xs)
    return xs[0] if len(xs) == 1 else ", ".join(xs[:-1]) + " and " + xs[-1]


def _calls_and_floors(F, prev, p, cfg):
    """(calls now, call floor now, call floor last month) for the subject of a finding that has a floor, else (None, None, None).
    An operation, host or request sample absent from a short span sample has 0 calls: there is no evidence it closed."""
    from .detectors import min_calls, per_window
    det = p.get("detector")
    metrics = p.get("metrics") or {}
    unit = (metrics.get("primary") or {}).get("unit")
    T = cfg["thresholds"]
    old = _old_thresholds(prev, T)
    svc = metrics.get("worst_service") or (p.get("subject") or {}).get("service")
    s = F["services"].get(svc) if svc else None
    if det in ("failing_operations", "slow_operations"):
        if not s:
            return None, None, None
        tier = s["requests"].get("tier")
        if det == "failing_operations":
            floors = (min_calls(F, cfg, tier), min_calls(F, {"thresholds": old}, tier))
        else:
            floors = (per_window(F, T["slow_op_min_calls"], tier), per_window(F, old["slow_op_min_calls"], tier))
        for r in F["resources"].get(svc, []):
            if r["resource"] == metrics.get("worst_resource"):
                return (r["calls"],) + floors
        return ((0,) + floors) if tier != "aggregate" else (None, None, None)
    if det == "slow_hot_paths" and metrics.get("outlier_only"):
        if not s:
            return None, None, None
        tier = s["requests"].get("tier")
        res_ = set(metrics.get("outlier_resources") or [])
        calls = sum(r["calls"] for r in F["resources"].get(svc, []) if r["resource"] in res_)
        return calls, per_window(F, T["outlier_min_calls"], tier), per_window(F, old["outlier_min_calls"], tier)
    if det == "slow_hot_paths":
        if not s:
            return None, None, None
        tier = s["requests"].get("tier")
        biz = [r for r in F["resources"].get(svc, []) if r["class"] == "business" and r["p95_s"] is not None and r["calls"] and (r["tier"] == "aggregate" or r["sample"]["n"] >= 20)]
        calls = sum(r["calls"] for r in biz) if len(biz) >= 3 else 0          # the detector needs three timed operations
        return calls, min_calls(F, cfg, tier), min_calls(F, {"thresholds": old}, tier)
    if det == "chatty_requests":
        a = F["anatomy"].get(svc) or {}
        return a.get("requests_with_tree", a.get("entries", 0)) if a else 0, T["min_entry_spans_for_anatomy"], old["min_entry_spans_for_anatomy"]
    if det == "scanner_exposure" and s and s["requests"].get("tier") == "sample":
        tier = s["requests"]["tier"]
        hits = sum(r["calls"] for r in F["resources"].get(svc, []) if r["class"] == "scanner")
        return hits, per_window(F, T["scanner_min_hits"], tier), per_window(F, old["scanner_min_hits"], tier)
    if det == "client_errors" and s:
        subj = (metrics.get("primary") or {}).get("subject")
        res = subj[1] if isinstance(subj, list) and len(subj) == 2 else None
        row = next((r for r in F["resources"].get(svc, []) if r["resource"] == res), None)
        known = sum(v for k, v in (row or {}).get("status_mix", {}).items() if k != "none")
        return known, T["client_error_min_calls"] / 10, old["client_error_min_calls"] / 10
    if det == "noise_masks_signal" and s:
        tier = s["requests"].get("tier")
        return s["failed"]["all"] - s["failed"]["business"], per_window(F, T["noise_min_failed"], tier), per_window(F, old["noise_min_failed"], tier)
    if det == "node_imbalance":
        grp = (F.get("node_groups") or {}).get((p.get("subject") or {}).get("group")) or {}
        low = F["services"].get(grp.get("lowest")) if grp else None
        if not grp or not low:
            return None, None, None
        tier = low["requests"].get("tier")
        return grp.get("mean_others", 0), min_calls(F, cfg, tier), min_calls(F, {"thresholds": old}, tier)
    if det == "failing_dependencies":
        key = "dep_min_calls" if unit == "pct" else "dep_slow_min_calls"
        for h in _hosts_of(p):
            d = _now_dep_for(F, prev, h)
            if d:
                scale = (lambda v: v) if unit == "pct" else (lambda v: per_window(F, v, d.get("tier", "sample")))
                return d["calls"], scale(T[key]), scale(old[key])
        if any(((prev.get("dependencies") or {}).get(h) or {}).get("tier") == "sample" for h in _hosts_of(p)):
            return 0, T[key], old[key]
    return None, None, None


def _closed_status(F, prev, p, now_val, cfg):
    """reclassified (the operation is now listed as noise), too_little_traffic (its subject fell below a floor, or its figure
    still clears the bar and only a floor kept it out), below_threshold (a threshold this detector uses changed, the figure
    still clears last month's bar and not this month's) or closed."""
    T = cfg["thresholds"]
    det = p.get("detector")
    metrics = p.get("metrics") or {}
    svc = (p.get("subject") or {}).get("service")
    if det in ("nonprod_from_prod", "egress_and_telemetry"):
        from .config import matches_any
        hosts, pdeps = _hosts_of(p), prev.get("dependencies") or {}
        moved = lambda h: (F["dependencies"][h].get("class") != (pdeps.get(h) or {}).get("class")
                           or (det == "egress_and_telemetry" and (F["dependencies"][h].get("allowed") if "allowed" in F["dependencies"][h]
                                                                 else matches_any(cfg["_rx"].get("allowed_egress", []), h))))
        if hosts and all((F["dependencies"].get(h) or {}).get("calls") for h in hosts) and all(moved(h) for h in hosts):
            return "host_reclassified"
    if det == "slow_operations" and metrics.get("worst_resource"):
        r_ = next((r for r in F["resources"].get(metrics.get("worst_service"), []) if r["resource"] == metrics["worst_resource"]), None)
        if r_ is not None and r_["class"] != "business":
            return "reclassified"
    if det == "slow_hot_paths":
        res_ = set(metrics.get("slow_resources") or []) | set(metrics.get("outlier_resources") or [])
        if any(r["resource"] in res_ and r["class"] != "business" for r in F["resources"].get(svc, [])):
            return "reclassified"
    if det in ("failing_operations", "noise_masks_signal") and metrics.get("worst_resource"):
        for r in F["resources"].get(svc, []):
            if r["resource"] == metrics["worst_resource"] and r["class"] != "business":
                return "reclassified"
    changed = set(F["mom"].get("thresholds_changed") or {}) & set(THRESHOLD_KEYS.get(det, ()))
    old = _old_thresholds(prev, T)
    fires_old, fires_now = _still_fires(F, p, old, prev), _still_fires(F, p, T, prev)
    calls, floor_now, floor_old = _calls_and_floors(F, prev, p, cfg)
    if calls is not None and calls < floor_now:
        if changed and floor_old is not None and calls >= floor_old:
            return "below_threshold" if fires_old else "closed"
        return "too_little_traffic"
    if changed and fires_old and fires_now is False:
        return "below_threshold"
    if fires_now is False and _still_fires(F, p, T, prev, kpis=_prev_kpis_now(F, prev)):
        return "within_target"              # last month's agreed target would still raise it: the target change closed it
    if fires_now:
        return "too_little_traffic"         # the figure still clears the bar: a floor of the detector kept it out this month
    if _within_noise(F, prev, p, now_val, T):
        return "within_noise"
    return "closed"


def _prev_kpis_now(F, prev):
    """Last month's KPIs keyed by this month's keys (a renamed KPI keeps its old target), for 'last month's targets'."""
    pk = prev.get("kpis") or {}
    ren = (F.get("mom") or {}).get("kpi_renamed") or {}
    out = {k: v for k, v in pk.items()}
    for ck, pkey in ren.items():
        if pkey in pk:
            out[ck] = pk[pkey]
    return out


def _within_noise(F, prev, p, now_val, T):
    """A sampled failure finding whose drop under the bar does not pass the test a trend must pass (enough failures, the
    two-proportion z): it went quiet within the sample's noise, not closed."""
    det = p.get("detector")
    prim = (p.get("metrics") or {}).get("primary") or {}
    if det == "slow_hot_paths" and not (p.get("metrics") or {}).get("outlier_only"):
        svc_ = (p.get("subject") or {}).get("service")
        kpi_now = {r["resource"]: r.get("kpi") for r in F["resources"].get(svc_, [])}
        then_rows = [dict(r, kpi=kpi_now.get(r["resource"], r.get("kpi"))) for r in (prev.get("resources") or {}).get(svc_, []) or []]
        a, b = _slow_share_nd(then_rows, T, F["kpis"]), _slow_share_nd(F["resources"].get(svc_, []), T, F["kpis"])
        if a and b and not _significant(a, b, T):
            return True                             # the share moved within the sample's call-mix noise
        return _crossed_within_ci(F, prev, svc_, T)
    if det in ("slow_operations", "slow_hot_paths") and prim.get("unit") == "s":
        subj = prim.get("subject")
        return isinstance(subj, list) and len(subj) == 2 and _p95_close_noisy(F, prev, subj[0], subj[1])
    if det == "client_errors" and prim.get("den"):
        subj = prim.get("subject")
        res = subj[1] if isinstance(subj, list) and len(subj) == 2 else None
        row = next((r for r in F["resources"].get((p.get("subject") or {}).get("service"), []) if r["resource"] == res), None)
        if not row or row.get("tier") != "sample":
            return False
        mix = row.get("status_mix", {})
        known = sum(v for k, v in mix.items() if k != "none")
        return not _significant((prim.get("num") or 0, prim["den"]), (mix.get("4xx", 0), known), T)
    if det not in ("failing_operations", "failing_dependencies") or prim.get("unit") != "pct" or now_val is None or not prim.get("den"):
        return False
    if det == "failing_operations":
        svc_ = (p.get("subject") or {}).get("service")
        row = next((r for r in F["resources"].get(svc_, []) if r["resource"] == (p.get("metrics") or {}).get("worst_resource")), None)
        if not row or row.get("tier") != "sample":
            return False
        now_rate = row["err"]
    else:
        d = next((x for x in (_now_dep_for(F, prev, h) for h in _hosts_of(p)) if x), None)
        if not d or d.get("tier") != "sample":
            return False
        now_rate = d["err"]
    return not _significant((prim.get("num") or 0, prim.get("den") or 0), (now_rate.get("num") or 0, now_rate.get("den") or 0), T)


def _lists_service(f, svc):
    """Whether a finding names this service: its subject, or one of the services its table lists."""
    return bool(svc) and (svc == f["subject"].get("service") or svc == f["metrics"].get("worst_service") or svc in (f["subject"].get("services") or []))


def _significant(a, b, T):
    """Two counts (num, den) that differ beyond sampling noise: enough events and a two-proportion z at mom_rate_z."""
    if not a[1] or not b[1] or max(a[0], b[0]) < T.get("mom_min_failures", 10):
        return False
    z = _z(a, b)
    return z is not None and abs(z) >= T.get("mom_rate_z", 2.0)


def _slow_share_nd(rows, T, kpis):
    """(slow calls, business calls) behind _slow_share."""
    biz = [r for r in rows if r.get("class") == "business" and r.get("p95_s") is not None and r.get("calls")
           and (r.get("tier") == "aggregate" or (r.get("sample") or {}).get("n", 0) >= 20)]
    den = sum(r["calls"] for r in rows if r.get("class") == "business" and r.get("calls")) if biz else 0
    num = sum(r["calls"] for r in biz if r["p95_s"] >= T["slow_p95_s"] and not _agreed_ok(r, kpis))
    return (num, den) if den else None


def _p95_close_noisy(F, prev, svc, res):
    """An operation's p95 that crossed a bar between months within sampling noise: both months' sampled intervals overlap, or
    one month's weekly p95 lies inside the other's sampled interval. No interval, no claim of noise."""
    now_r = next((r for r in F["resources"].get(svc, []) if r["resource"] == res), None)
    then_r = next((r for r in (prev.get("resources") or {}).get(svc, []) or [] if r["resource"] == res), None)
    if not now_r or not then_r:
        return False
    ci = lambda r: (r.get("sample") or {}).get("p95_ci_s") if r.get("p95_source") == "sample" else None
    a, b = ci(then_r), ci(now_r)
    if a and b:
        return _ci_overlap(a, b)
    if a and now_r.get("p95_s") is not None:
        return a[0] <= now_r["p95_s"] <= a[1]
    if b and then_r.get("p95_s") is not None:
        return b[0] <= then_r["p95_s"] <= b[1]
    return False


def _crossed_within_ci(F, prev, svc, T):
    now_r = {r["resource"]: r for r in F["resources"].get(svc, [])}
    then_r = {r["resource"]: r for r in (prev.get("resources") or {}).get(svc, []) or []}
    thr = T["slow_p95_s"]
    crossed = [res for res in set(now_r) & set(then_r) if now_r[res].get("p95_s") is not None and then_r[res].get("p95_s") is not None
               and (now_r[res]["p95_s"] >= thr) != (then_r[res]["p95_s"] >= thr)]
    return bool(crossed) and all(_p95_close_noisy(F, prev, svc, res) for res in crossed)


def _successor(p, findings):
    subj = p.get("subject") or {}
    for f in findings:
        same_subject = (subj.get("service") and f["subject"].get("service") == subj.get("service")) or (subj.get("group") and f["subject"].get("group") == subj.get("group")) \
            or (subj.get("key") == "estate" and f["subject"].get("key") == "estate" and set(subj.get("hosts") or []) & set(f["subject"].get("hosts") or []))
        if same_subject and (f["detector"] == p.get("detector") or f["detector"] in RELATED.get(p.get("detector"), set())):
            return f
    return None


def compare_findings(F, prev, body, also, cfg):
    """After detectors ran: closed / superseded / new / persisting / demoted / promoted / unmeasured / first measured."""
    T = cfg["thresholds"]
    mom = F["mom"]
    for k in ("unmeasured", "first_measured"):
        mom["findings"].setdefault(k, [])
    prev_findings = {f["id"]: f for f in prev.get("findings", [])}
    prev_also = {f["id"]: f for f in prev.get("also_noted", [])}
    current = {f["id"]: f for f in body + also}
    body_ids = {f["id"] for f in body}
    merged_into = {mid: f for f in body + also for mid in f.get("merged", [])}
    prev_merged = {mid for x in list(prev_findings.values()) + list(prev_also.values()) for mid in x.get("merged", [])}
    prev_cov = prev.get("coverage") or {}
    prev_fs = mom["previous"].get("failure_signal", True)
    cur_fs = bool(F["coverage"].get("failure_signal"))
    disjoint = set()
    for fid, f in current.items():
        p = prev_findings.get(fid) or prev_also.get(fid)
        prim = f["metrics"].get("primary", {})
        unit = prim.get("unit", "count")
        lbl = prim.get("label", "")
        if p and _disjoint_hosts(p, f, F, prev):
            disjoint.add(fid)             # the same estate-wide id about entirely different hosts: one closed, one new
            p = None
        if not p and fid in prev_merged and fid not in disjoint:
            # reported last month inside another finding: it persists, with no figure of its own to compare
            f["mom"] = {"status": "persisting", "direction": "new", "then": None, "now": prim.get("value"), "then_text": "—", "now_text": _fmt(prim.get("value"), unit), "label": lbl}
            row = {"id": fid, "title": f["title"], "then_text": "—", "now_text": f["mom"]["now_text"], "direction": "new", "label": lbl, "severity": f["severity"], "then_severity": None}
            if fid in body_ids:
                mom["findings"]["persisting"].append(row)
            continue
        if not p and f["detector"] == "slow_operations":
            wres, wsvc = f["metrics"].get("worst_resource"), f["metrics"].get("worst_service")
            host_ = next((x for x in prev_findings.values() if x.get("detector") == "slow_hot_paths" and _lists_service(f, (x.get("subject") or {}).get("service"))
                          and set(f["subject"].get("resources") or [wres]) & set((x.get("metrics") or {}).get("slow_resources") or [])), None)
            prow = next((r for r in (prev.get("resources") or {}).get(wsvc, []) or [] if r["resource"] == wres), None) if host_ else None
            if host_ is not None:
                tv = prow.get("p95_s") if prow else None
                d, _ = _direction(tv, prim.get("value"), unit, T) if tv is not None else ("new", 0.0)
                if d in ("worse", "better") and _p95_noisy(F, prev, prim, T):
                    d = "flat"
                tt = sec(tv) if tv is not None else "—"
                f["mom"] = {"status": "persisting", "direction": d, "then": tv, "now": prim.get("value"), "then_text": tt, "now_text": _fmt(prim.get("value"), unit),
                            "label": lbl, "reported_in": host_["id"]}
                if fid in body_ids:
                    mom["findings"]["persisting"].append({"id": fid, "title": f["title"], "then_text": tt, "now_text": f["mom"]["now_text"], "direction": d, "label": lbl,
                                                          "severity": f["severity"], "then_severity": None})
                continue
        if not p:
            reason, first = None, False
            if f["detector"] in ("failing_dependencies", "nonprod_from_prod", "egress_and_telemetry"):
                hosts = _hosts_of(f)
                pdeps = prev.get("dependencies", {}) or {}
                unseen = bool(hosts) and not any(h in pdeps for h in hosts)
                eq = [x for x in (_prev_dep_for(F, prev, None, h) for h in hosts) if x]
                if unseen and eq and f["detector"] == "failing_dependencies" and all(x.get("failure_known", True) for x in eq):
                    unseen = False                  # the same calls were measured last month without a host name
                if unseen and prev_cov.get("host_visibility") is False:
                    first, reason = True, "last month's export did not name the hosts its services called"
                elif unseen and not (prev_cov.get("inputs") or {}).get("inferred") and all((F["dependencies"].get(h) or {}).get("source") == "inferred" for h in hosts):
                    first, reason = True, "last month's exports did not include the inferred services list"
                elif f["detector"] in ("nonprod_from_prod", "egress_and_telemetry") and unseen and any(_was_unnamed(F, prev, h) for h in hosts):
                    callers_ = sorted({c["service"] for h in hosts for c in (F["dependencies"].get(h) or {}).get("callers", [])})
                    first, reason = True, f"last month's export did not name the hosts {join_names(callers_[:3]) if callers_ else 'these services'} called"
                elif f["detector"] in ("nonprod_from_prod", "egress_and_telemetry") and hosts and all(
                        h in pdeps and h in F["dependencies"] and (pdeps[h].get("class") != F["dependencies"][h].get("class")
                                                                   or bool(pdeps[h].get("allowed")) != bool(F["dependencies"][h].get("allowed"))) for h in hosts):
                    first, reason = True, f"the hosts config reclassified {join_names(hosts[:3])}"
                elif f["detector"] == "failing_dependencies" and mom.get("rule_changed"):
                    first, reason = True, "what counts as a failure changed since last month"
                elif f["detector"] == "failing_dependencies":
                    host = f["metrics"].get("subject_host")
                    pd = pdeps.get(host) or {}
                    first = bool(host) and pd.get("failure_known") is False
                    if first:
                        reason = NO_SIGNAL_LAST if not prev_fs else f"last month's calls to {host} carried no status code or error flag"
            else:
                svc_f = f["subject"].get("service")
                members = list(f["subject"].get("nodes") or ([svc_f] if svc_f else []))
                prev_unrec = [m for m in members if ((prev.get("services") or {}).get(m) or {}).get("failure_known") is False]
                wres = f["metrics"].get("worst_resource")
                if f["detector"] == "failing_operations" and wres and not prev_unrec:
                    prow = next((r for m in members for r in (prev.get("resources") or {}).get(m, []) or [] if r["resource"] == wres), None)
                    if prow is not None and prow.get("failure_known") is False:
                        prev_unrec = [prow.get("short") or wres]
                rc = mom.get("rule_changed")
                first = _is_failure_finding(f) and (not prev_fs or bool(prev_unrec) or bool(rc))
                moved_ = _class_moved((prev.get("resources") or {}).get(svc_f), F["resources"].get(svc_f)) if f["detector"] == "noise_masks_signal" else set()
                if moved_ and not first:
                    shorts_ = sorted(r["short"] for r in F["resources"].get(svc_f, []) if r["resource"] in moved_)
                    first, reason_cls = True, f"the noise config reclassified {join_names([code(x) for x in shorts_[:3]])}"
                else:
                    reason_cls = None
                reason = reason_cls or (NO_SIGNAL_LAST if (first and not prev_fs) else
                                        (f"last month's spans of {prev_unrec[0]} carried no status code or error flag" if (first and prev_unrec) else
                                         ("what counts as a failure changed since last month" if first else None)))
                if f["detector"] == "foundations":
                    from .detectors import foundations_checkable
                    then_c = set(foundations_checkable(prev)) if prev.get("coverage") and prev.get("window") else set()
                    unseen_g = [g for g in (f["metrics"].get("gaps") or []) if g not in then_c]
                    if unseen_g and len(unseen_g) == len(f["metrics"].get("gaps") or []):
                        first, reason = True, "last month's exports could not check " + join_names(sorted(unseen_g))
            pskip = {x["name"]: x for x in prev_cov.get("detectors_skipped") or []}
            if not first and f["detector"] in pskip:
                first, reason = True, SKIPPED_LAST.get(pskip[f["detector"]].get("reason"), "last month's exports could not check it")
            f["mom"] = {"status": "first_measured" if first else "new", "direction": "new", "then": None, "now": prim.get("value"), "then_text": "—",
                        "now_text": _fmt(prim.get("value"), unit), "label": lbl}
            if fid in body_ids:
                mom["findings"]["first_measured" if first else "new"].append({"id": fid, "title": f["title"], "then_text": "—", "now_text": f["mom"]["now_text"], "label": lbl,
                                                                             "severity": f["severity"], "reason": reason})
            continue
        then_val, then_text, then_nd = _then_for(F, prev, p, f)
        now_nd = (prim.get("num"), prim.get("den")) if prim.get("den") else None
        d, delta = _direction(then_val, prim.get("value"), unit, T, prim.get("higher_is_worse", True), then_nd=then_nd, now_nd=now_nd)
        noisy = False
        if unit == "s" and d in ("worse", "better") and _p95_noisy(F, prev, prim, T):
            d, noisy = "flat", True
        if f["detector"] == "slow_hot_paths" and unit == "pct" and d in ("worse", "better") and _share_noisy(F, prev, f, T):
            d, noisy = "flat", True
        promoted = fid in prev_also and fid not in prev_findings
        then_sev = p.get("severity")
        if noisy or _targets_touch(F, f, prev):
            then_sev = None                   # an agreed target changed, or the move is sampling noise: the severities are not on one basis
        f["mom"] = {"status": "promoted" if promoted else "persisting", "direction": d, "then": then_val, "now": prim.get("value"), "then_text": then_text,
                    "now_text": _fmt(prim.get("value"), unit), "label": lbl, "then_severity": then_sev, "then_title": p.get("title")}
        row = {"id": fid, "title": f["title"], "then_text": then_text, "now_text": f["mom"]["now_text"], "direction": d, "label": lbl,
               "severity": f["severity"], "then_severity": then_sev}
        if f["detector"] == "failing_operations":         # 37: last month's headline operation now listed as noise
            wres = (p.get("metrics") or {}).get("worst_resource")
            r_ = next((r for r in F["resources"].get(f["subject"].get("service"), []) if r["resource"] == wres), None)
            if r_ is not None and r_["class"] != "business" and wres != f["metrics"].get("worst_resource"):
                row["note"] = f"{code(r_['short'])} reclassified as {r_['class'].replace('_', '-')} noise"
                f["mom"]["note"] = row["note"]
        if fid in body_ids:
            (mom["findings"]["promoted"] if promoted else mom["findings"]["persisting"]).append(row)
        elif fid in prev_findings:
            mom["findings"]["demoted"].append(row)
    for fid, p in prev_findings.items():
        if fid in current and fid not in disjoint:
            continue
        pp = (p.get("metrics") or {}).get("primary", {}) or {}
        then_text = _fmt(pp.get("value"), pp.get("unit", "count"))
        base = {"id": fid, "title": p.get("title", fid), "then_text": then_text, "then_severity": p.get("severity"), "label": pp.get("label", "")}
        if fid in merged_into:
            host = merged_into[fid]
            mom["findings"]["closed"].append(dict(base, now_text=f"see Finding {host['number']}" if host.get("number") else "see Also noted", status="superseded"))
            continue
        why = _unmeasured_reason(F, prev, p, cur_fs)
        if why:
            mom["findings"]["unmeasured"].append(dict(base, now_text="not measured", status="unmeasured", reason=why))
            continue
        old_T = _old_thresholds(prev, T)
        if fid in disjoint:
            now_val = _current_value(F, p, old_T, prev)
            st = _closed_status(F, prev, p, now_val, cfg)
            mom["findings"]["closed"].append(dict(base, now_text=_fmt(now_val, pp.get("unit", "count")) if now_val is not None else _gone_text(st), status=st))
            continue
        successor = _successor(p, body) or _successor(p, also)
        if successor and successor["detector"] == p.get("detector") and successor.get("mom", {}).get("status") == "new":
            # same detector, same subject, different key: a renamed service or node group — a continuation, not closed + new
            sprim = successor["metrics"].get("primary", {})
            tv, tt, tnd = _then_for(F, prev, p, successor)
            snd = (sprim.get("num"), sprim.get("den")) if sprim.get("den") else None
            d, _ = _direction(tv, sprim.get("value"), sprim.get("unit", "count"), T, sprim.get("higher_is_worse", True), then_nd=tnd, now_nd=snd)
            successor["mom"].update({"status": "persisting", "direction": d, "then": tv, "then_text": tt, "renamed_from": fid,
                                     "then_severity": p.get("severity"), "then_title": p.get("title")})
            mom["findings"]["new"] = [x for x in mom["findings"]["new"] if x["id"] != successor["id"]]
            target = "persisting" if successor["id"] in body_ids else "demoted"
            mom["findings"][target].append({"id": successor["id"], "title": successor["title"], "then_text": tt, "now_text": successor["mom"]["now_text"], "direction": d,
                                            "label": successor["mom"]["label"], "severity": successor["severity"], "then_severity": p.get("severity")})
            continue
        outl = set((p.get("metrics") or {}).get("outlier_resources") or [])
        holds = lambda f: bool(outl & ({f["metrics"].get("worst_resource")} | set(f["subject"].get("resources") or [])
                                       | set(f["metrics"].get("outlier_resources") or []) | set(f["metrics"].get("slow_resources") or [])))
        slow_res = set((p.get("metrics") or {}).get("slow_resources") or [])
        if (p.get("metrics") or {}).get("outlier_only") and outl:
            successor = next((f for f in body + also if _successor(p, [f]) and holds(f)), None)
            sup = successor is not None
        elif p.get("detector") == "slow_hot_paths" and slow_res and any(_lists_service(f, (p.get("subject") or {}).get("service")) and set(f["subject"].get("resources") or []) & slow_res
                                                                        for f in body + also if f["detector"] == "slow_operations"):
            successor = next(f for f in body + also if f["detector"] == "slow_operations" and _lists_service(f, (p.get("subject") or {}).get("service"))
                             and set(f["subject"].get("resources") or []) & slow_res)
            sup = True
        else:
            sup = bool(successor) and (_still_fires(F, p, T, prev) is not False or bool(outl and successor["metrics"].get("worst_resource") in outl))
        if sup:
            where = f"see Finding {successor['number']}" if successor["id"] in body_ids else "see Also noted"
            mom["findings"]["closed"].append(dict(base, now_text=where, status="superseded"))
            continue
        now_val = _current_value(F, p, old_T, prev)          # measured at the bar last month's label names
        status = _closed_status(F, prev, p, now_val, cfg)
        if status == "within_target":
            now_val = _current_value(F, p, old_T, prev, kpis=_prev_kpis_now(F, prev))     # on last month's basis; the status says why it closed
        elif status == "host_reclassified":
            now_val = None
        if status == "reclassified":
            r_ = next((r for r in F["resources"].get((p.get("subject") or {}).get("service"), []) if r["resource"] == (p.get("metrics") or {}).get("worst_resource")), None)
            base = dict(base, noise_class=(r_ or {}).get("class", "by_design"))
        gone = _gone_text(status)
        if status == "too_little_traffic" and now_val is None and (_calls_and_floors(F, prev, p, cfg)[0] or 0) > 0:
            gone = "below the detector's call floor"      # in the sample, with too few calls to judge
        mom["findings"]["closed"].append(dict(base, now_text=_fmt(now_val, pp.get("unit", "count")) if now_val is not None else gone, status=status))
    fm = mom["findings"]
    prev_svcs = set(prev.get("services") or {})

    def gone(p_):
        svc_, hosts_ = (p_.get("subject") or {}).get("service"), _hosts_of(p_)
        return bool(svc_ and svc_ not in F["services"]) or bool(hosts_ and not any(h in F["dependencies"] for h in hosts_))
    # an identity change is a finding now reported inside another, one whose subject has gone, or one about a service not seen
    # last month (a rename or a regrouping); a genuine close or a genuinely new problem is not one
    changed = sum(1 for c in fm["closed"] if c.get("status") == "superseded" or (c.get("status") == "closed" and gone(prev_findings.get(c["id"]) or {})))
    changed += sum(1 for x in fm["new"] if (current[x["id"]]["subject"].get("service") or "") not in prev_svcs and current[x["id"]]["subject"].get("service"))
    if prev_findings and changed > 0.3 * (len(prev_findings) + len(current)):
        F["caveats"].append("more than 30 % of the findings changed identity since last month — if the review's domains or thresholds were changed, month-over-month statuses are less meaningful")
    return mom


def _gone_text(status):
    return {"too_little_traffic": "not in this month's sample", "host_reclassified": "still called"}.get(status, "no longer fires")


def _pseudo_callers(deps, name):
    d = deps.get(name) or {}
    return {c["service"] for c in d.get("callers", [])} if (d.get("host_unknown") or str(name).startswith("outbound calls of ")) else set()


def _disjoint_hosts(p, f, F=None, prev=None):
    """An estate-wide finding (one id for the whole estate) whose hosts this month share none with last month's. Calls
    named in one month and unnamed in the other (same caller, same kind of call) are the same calls, not disjoint."""
    ps, fs = p.get("subject") or {}, f.get("subject") or {}
    if ps.get("key") != "estate" or fs.get("key") != "estate":
        return False
    a, b = set(ps.get("hosts") or []), set(fs.get("hosts") or [])
    if not (bool(a) and bool(b) and not (a & b)):
        return False
    if F is not None and prev is not None:
        pdeps = prev.get("dependencies", {}) or {}
        if any(_equivalent(F["dependencies"], h, pdeps, F.get("services"), prev.get("services")) for h in b) \
                or any(_equivalent(pdeps, h, F["dependencies"], prev.get("services"), F.get("services")) for h in a):
            return False
    return True


def _current_value(F, prev_finding, T=None, prev=None, kpis=None):
    """Best-effort current value of a closed finding's primary metric (e.g. the worst resource's failure rate now), measured
    with thresholds T where the metric depends on one (the slow share counts operations above T's slow_p95_s that are not
    within the p95 target agreed in kpis, this month's by default)."""
    T = T or F["meta"]["thresholds"]
    kpis = F["kpis"] if kpis is None else kpis
    svc = (prev_finding.get("subject") or {}).get("service")
    det = prev_finding.get("detector")
    metrics = prev_finding.get("metrics") or {}
    if det == "failing_operations" and svc in F["resources"] and F["coverage"].get("failure_signal") and F["services"].get(svc, {}).get("failure_known", True):
        res = metrics.get("worst_resource")
        for r in F["resources"][svc]:
            if r["resource"] == res:
                return r["err"]["pct"] if r.get("failure_known", True) else None
    if det == "slow_operations":
        wsvc, res = metrics.get("worst_service"), metrics.get("worst_resource")
        for r in F["resources"].get(wsvc, []):
            if r["resource"] == res:
                return r.get("p95_s")
    unit = (metrics.get("primary") or {}).get("unit")
    if det == "failing_dependencies":
        hosts = [metrics["subject_host"]] if metrics.get("subject_host") else (prev_finding.get("subject") or {}).get("hosts", [])
        for h in hosts:
            d = _now_dep_for(F, prev, h)
            if d:
                if unit == "pct" and d.get("failure_known", True):
                    return d["err"]["pct"]
                if unit == "s":
                    return d.get("p95_s")
    if det == "slow_hot_paths" and metrics.get("outlier_only") and svc in F["resources"]:
        res_ = set(metrics.get("outlier_resources") or [])
        vals = [r["p95_s"] for r in F["resources"][svc] if r["resource"] in res_ and r.get("p95_s") is not None]
        return max(vals) if vals else None
    if det == "slow_hot_paths" and svc in F["resources"]:
        biz = [r for r in F["resources"][svc] if r["class"] == "business" and r["p95_s"] is not None and r["calls"] and (r["tier"] == "aggregate" or r["sample"]["n"] >= 20)]
        total = sum(r["calls"] for r in biz)
        if total:
            return _slow_share(F["resources"][svc], T, kpis)
    if det == "chatty_requests":
        # the figure named by the finding's own label: the per-request kind, secret reads, or the repeated child
        a = F["anatomy"].get(svc) or {}
        prim = metrics.get("primary") or {}
        lbl = prim.get("label") or ""
        for prefix, kind in (("outbound HTTP calls", "http"), ("cache operations", "cache"), ("database statements", "db")):
            if lbl.startswith(prefix):
                return ((a.get("children_per_request") or {}).get(kind) or {}).get("value")
        if lbl.startswith("secret-store reads"):
            return (a.get("secret_reads_per_request") or {}).get("value")
        subj = prim.get("subject")
        if isinstance(subj, list) and len(subj) == 2 and (lbl.startswith("runs of") or lbl.startswith("share of requests")):
            rc = next((x for x in a.get("repeated_children") or [] if x["resource"] == subj[1] and ("target" not in prim or (x.get("target") or "") == prim["target"])), None)
            if not rc:
                return None
            return rc["median"] if lbl.startswith("runs of") else (rc.get("traces_at_or_above") or {}).get("pct")
        return ((a.get("children_per_request") or {}).get("db") or {}).get("value")
    return None
