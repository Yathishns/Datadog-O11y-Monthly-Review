"""Finding detectors: pure functions over facts. Each returns findings, healthy rows, or a skip reason.

A finding is a dict with a stable id ('{detector}:{subject}') so month-over-month is a join, and it records the subject of its
primary metric (the worst operation or host) so a month where a different subject is worst is never compared with the
wrong number. Severity follows the domain tier from the config: red when customers or patients are affected now (customer /
clinical tiers); staff tiers need a stronger signal for red; internal tops out at amber unless the failure is severe. A
production call to a non-production host is always red. Count thresholds are per 7 days of Endpoints-list data and scale
with the exported range. Every claim about monitors, hosts, failures or hourly rates is gated on the data that can support it.
"""
import math
from collections import defaultdict

from . import recipes
from .stats import rate
from .narrative import (LEDGER, big, cap, code, fail_phrase, frac, join_and, maybe_per_day, n, pct, plural, ratio_text, sec, sec_range, words, x_text)


def thr_pct(v):
    """A threshold as configured: 1 -> '1 %', 1.5 -> '1.5 %' (never rounded to a bar nobody set)."""
    return LEDGER.add(f"{v:g} %")


def thr_sec(v):
    return LEDGER.add(f"{v:g} s")

DETECTORS = []
PER_SERVICE = ("failing_operations", "slow_hot_paths", "noise_masks_signal", "client_errors", "scanner_exposure", "chatty_requests", "call_ratio")
DISPLAY = {"failing_operations": "failing operations", "slow_hot_paths": "slow services", "slow_operations": "slow operations", "failing_dependencies": "failing dependencies",
           "nonprod_from_prod": "production calling non-production hosts", "noise_masks_signal": "noise masking failures", "node_imbalance": "node imbalance",
           "call_ratio": "token reuse", "chatty_requests": "work inside a request", "scanner_exposure": "scanner exposure", "egress_and_telemetry": "public egress and telemetry",
           "client_errors": "client errors", "foundations": "observability foundations", "traffic_shift": "regressions since last month"}
NEEDS_TEXT = {"failure_signal": "needs a failure signal", "dependencies": "needs caller data", "request_anatomy": "needs child spans with their parents",
              "status_codes": "needs status codes", "previous": "needs last month's review"}


def detector(name, order, needs=()):
    def wrap(fn):
        DETECTORS.append({"name": name, "order": order, "needs": tuple(needs), "fn": fn})
        return fn
    return wrap


# ---------------------------------------------------------------- helpers
def window_days(F):
    return (F["window"]["aggregate"]["days"] or 7) if F["window"]["volumes_tier"] == "aggregate" else None


def per_window(F, v, tier="aggregate"):
    """Count thresholds are per 7 days of Endpoints-list data: scale them with the exported range (a 28-day export needs 4x the count)."""
    if tier == "aggregate" and F["window"]["volumes_tier"] == "aggregate":
        days = F["window"]["aggregate"]["days"] or 7
        agg = F["window"]["aggregate"]
        tied = agg.get("tied") or []
        if (agg.get("source") in ("ambiguous", "assumed") or agg.get("confidence") == "contradicted") and tied:
            days = max(days, max(tied))          # a range nothing confirms never makes a finding more severe
        return v * days / 7.0
    return v


def min_calls(F, cfg, tier=None):
    T = cfg["thresholds"]
    tier = tier or F["window"]["volumes_tier"]
    if tier == "aggregate" and F["window"]["volumes_tier"] == "aggregate":
        return int(per_window(F, T["min_calls_per_day"] * 7, tier))
    return int(T["min_calls_sample"])


def tiered(level, tier, strong=False):
    """Apply the audience tier to a severity computed for a customer-facing domain."""
    if level != "red":
        return level
    if tier in ("customer", "clinical"):
        return "red"
    return "red" if strong else "amber"


def svc_ctx(F, svc):
    s = F["services"][svc]
    dom = F["domains"].get(s["domain"], {})
    return s, dom, dom.get("tier", "staff"), dom.get("audience", "Users")


def subj(s, svc):
    """How a service is named in a title: its configured display name, else its id as code."""
    return s["display"] if s["display"] != svc else code(svc)


def res_label(r):
    lbl = code(r["short"])
    if r.get("kpi_label") and r["kpi_label"] != r["short"]:
        lbl += f" — {r['kpi_label']}"
    return lbl


def kpi_labels(F):
    """KPI key -> display label (None while still _todo); braces in a label are shown as code so they never read as placeholders."""
    out = {}
    for key, k in F["kpis"].items():
        lbl = str(k["label"])
        if lbl.startswith("_todo"):
            continue
        out[key] = code(lbl) if ("{" in lbl or "}" in lbl) else lbl
    return out


def finding(det, subject_key, severity, score, F, svc=None, **fields):
    f = {"id": f"{det}:{subject_key}", "detector": det, "severity": severity, "score": float(score), "subject": {"service": svc, "key": subject_key},
         "domain": "", "audience": "Users", "tier": "staff", "title": "", "lead": "", "bullets": [], "table": None, "diagram": None, "smaller": "",
         "actions": [], "live_graph": [], "metrics": {}, "summary": {}, "horizon": "now"}
    if svc and svc in F["services"]:
        s, dom, tier, aud = svc_ctx(F, svc)
        f["domain"], f["audience"], f["tier"] = s["domain"], aud, tier
    f.update(fields)
    return f


def live(F, svc, resource=None, kind="hits"):
    s = F["services"][svc]
    op = recipes.operation(s)
    row = next((r for r in F["resources"].get(svc, []) if r["resource"] == resource), None) if resource else None
    if row and row.get("operation") and s.get("operation_source") != "config":
        op = row["operation"]               # an operation's own span operation (a queue job is not trace.<web op>)
    tag = (row or {}).get("metric_service") or (s.get("metric_service") if not resource else None)
    if tag:
        svc = tag                           # the tracer tags these spans' metrics with the library's name ('kafka')
    note = recipes.placeholder_note(svc) if op == recipes.PLACEHOLDER else ""
    if kind == "hits":
        return [("timeseries", recipes.hits(op, svc, resource), "requests"), ("timeseries", recipes.errors(op, svc, resource), "failures, as red bars" + (f"; {note}" if note else ""))]
    if kind == "p95":
        return [("timeseries", recipes.p95(op, svc), "add a marker at the threshold" + (f"; {note}" if note else "")), ("toplist", recipes.p95_by_resource(op, svc), "slowest features")]
    return []


def window_phrase(F, tier=None):
    tier = tier or F["window"]["volumes_tier"]
    if tier == "services_list":
        return F["window"]["phrase"]["services_list"]
    if tier == "aggregate" and F["window"]["volumes_tier"] == "aggregate":
        return F["window"]["phrase"]["aggregate"]
    return F["window"]["phrase"]["sample"]


def nice_up(x):
    """Round a threshold up to a number people set, never below the target and within about 10 % of it: 373.8 -> 380, 37 -> 37, 4.4 -> 5."""
    x = max(float(x), 5.0)
    step = 10 ** max(0, math.floor(math.log10(x)) - 1)
    return int(math.ceil(x / step) * step)


def one_in_ok(p):
    return p is not None and 0.5 <= p <= 50


def unwatched(F, s):
    """True only when an export with a monitors column lists this service and it has none."""
    return bool(s.get("monitors_known", F["coverage"].get("monitors_known"))) and s["monitors"] == 0


def _paired(rows, T):
    """Two operations whose failure counts agree within the relative tolerance (and both have enough failures to mean it)."""
    for i, a in enumerate(rows):
        for b in rows[i + 1:]:
            if min(a["failed"], b["failed"]) < 50:
                continue
            if abs(a["failed"] - b["failed"]) <= T["paired_tolerance_pct"] / 100 * max(a["failed"], b["failed"]) and abs(a["calls"] - b["calls"]) <= 0.05 * max(a["calls"], 1):
                return a, b
    return None


NOISE_ADJ = {"by_design": "by-design"}
NOISE_NOUN = {"scanner": "scanner probes", "health": "health checks", "by_design": "by-design failures", "static": "static files", "framework": "framework events"}


def _desc(dom):
    d = dom.get("description")
    return f" (part of {dom['label']}: {d})" if d else ""


def achieved_text(v, target):
    """An achieved success rate with enough decimals that a missed target never reads as met (99.478 against 99.5 -> '99.48 %')."""
    if target is not None and v < target:
        for d in (1, 2, 3):
            txt = f"{v:.{d}f}"
            if float(txt) < target:
                return LEDGER.add(txt + " %")
    return pct(v)


def _kpi_achieved(F, r, svc=None):
    """(KPI, achieved failure rate) for an operation's KPI on one service: the KPI's own rate when it is recorded and all its
    operations are on this service, else the rate of its operations on this service."""
    k = (F["kpis"].get(r.get("kpi")) or {}) if r.get("kpi") else {}
    if not k:
        return k, r["err"]
    if svc and len(k.get("services") or [svc]) > 1:
        own_ = [x for x in F["resources"].get(svc, []) if x.get("kpi") == r["kpi"] and x["class"] == "business" and x.get("failure_known", True)]
        return k, rate(sum(x["failed"] for x in own_), sum(x["calls"] for x in own_)) if own_ else r["err"]
    return k, (k["err"] if k.get("failure_known", True) and (k.get("err") or {}).get("den") else r["err"])


def _beyond_noise(ach, target, T):
    """Whether a sampled failure count exceeds what the agreed target allows by more than sampling noise: enough failures
    and a one-sided z score at mom_rate_z against the target's failure budget."""
    budget = (100.0 - target) / 100.0
    x, m = ach.get("num") or 0, ach.get("den") or 0
    if not m or x < T.get("mom_min_failures", 10):
        return False
    sd = math.sqrt(m * budget * (1 - budget))
    return (x - m * budget) / sd >= T.get("mom_rate_z", 2.0) if sd else x > 0


def _missed_success(F, biz, bad, T, svc=None):
    """Operations under the review's failure bar whose KPI misses its agreed success target (beyond sampling noise when
    the figure comes from the span sample)."""
    out = []
    for r in biz:
        if r in bad or not r.get("kpi") or not r["failed"]:
            continue
        k, ach = _kpi_achieved(F, r, svc)
        tgt = k.get("slo_success_pct")
        if tgt is None or 100 - ach["pct"] >= tgt:
            continue
        if (k.get("tier") or r["tier"]) == "sample" and not _beyond_noise(ach, tgt, T):
            continue
        out.append(r)
    return sorted(out, key=lambda r: (-r["failed"], r["resource"]))


def _target_bullet(F, r, svc, with_count=False):
    k, ach = _kpi_achieved(F, r, svc)
    tgt = k["slo_success_pct"]
    budget = 100.0 - tgt
    times = (f" — {x_text(ach['pct'] / budget, 1)}× the {LEDGER.add(f'{budget:g} %')} of calls the target allows to fail"
             if with_count and budget > 0 and ach["pct"] / budget >= 1.05 else "")
    return (f"The agreed target for {r.get('kpi_label') or code(r['short'])} is {pct(tgt)} success; {window_phrase(F, k.get('tier'))} it achieved "
            f"{achieved_text(100 - ach['pct'], tgt)}" + ("" if ach is k["err"] else f" on {code(svc)}") + (f" ({n(ach['num'])} of {n(ach['den'])} failed)" if with_count else "")
            + times + ".")


def _target_miss_finding(F, T, svc, s, dom, tier, aud, stier, wp, missed, small_bad=(), mc=0):
    """A finding for operations that stay under the review's failure bar but miss the success target the team agreed."""
    top = missed[0]
    k, ach = _kpi_achieved(F, top, svc)
    tgt = k["slo_success_pct"]
    name = top.get("kpi_label") or f"{code(top['short'])} calls"
    no_mon = unwatched(F, s)
    bullets, seen = [], set()
    for r in missed[:8]:
        if r["kpi"] not in seen:
            seen.add(r["kpi"])
            bullets.append(_target_bullet(F, r, svc, with_count=True))
    if small_bad:
        bullets.append("Smaller operations fail more, each with too few calls to flag on its own: " + ", ".join(f"{code(r['short'])} {frac(r['err'])}" for r in small_bad[:3]) + ".")
    if no_mon:
        bullets.append(f"**No monitor watches {code(svc)}**, so none of these failures pages anyone.")
    table_rows = [[res_label(r), n(r["calls"]), n(r["failed"]), pct(r["err"]), sec(r["p95_s"])] for r in missed[:8]]
    actions = [{"text": f"Pull one day of {code(top['short'])} failures from the application log and classify them (validation, data, timeout, dependency)", "horizon": "now"},
               {"text": f"Track {name} against the agreed {pct(tgt)} success target as a Datadog SLO, so the burn rate pages before the target is missed", "horizon": "next"}]
    if no_mon:
        actions.insert(0, {"text": f"Add a failure-rate monitor on {code(svc)} — it has no monitor today", "horizon": "now"})
    title = f"{cap(name)} miss the agreed {pct(tgt)} success target on {subj(s, svc)}" + (", and nothing is watching it" if no_mon else "")
    which = "this operation" if len(missed) == 1 else "these operations"
    if s["err"]["business"]["pct"] < T["fail_amber_pct"]:
        lead = (f"{code(svc)}{_desc(dom)} handled {big(s['requests']['business'])} business requests {wp} and failed {frac(s['err']['business'])} — under the review's "
                f"{thr_pct(T['fail_amber_pct'])} bar, but not within the success target agreed for {which}:")
    else:
        lead = (f"{code(svc)}{_desc(dom)} handled {big(s['requests']['business'])} business requests {wp} and failed {frac(s['err']['business'])}. Each operation with at least "
                f"{n(mc)} calls stays under the review's {thr_pct(T['fail_amber_pct'])} bar, but {which} {'misses' if len(missed) == 1 else 'miss'} the success target agreed for {'it' if len(missed) == 1 else 'them'}:")
    return finding("failing_operations", svc, tiered("amber", tier), top["failed"], F, svc=svc, title=title, lead=lead, bullets=bullets,
                   table={"columns": ["Operation", "Calls", "Failed", "Failure rate", "p95"], "rows": table_rows}, actions=actions,
                   live_graph=live(F, svc, top["resource"]), subject={"service": svc, "key": svc, "resources": [r["resource"] for r in missed]},
                   metrics={"primary": {"label": f"failure rate of {top['short']}", "value": top["err"]["pct"], "unit": "pct", "higher_is_worse": True,
                                        "num": top["err"]["num"], "den": top["err"]["den"], "subject": [svc, top["resource"]]},
                            "worst_resource": top["resource"], "worst_short": top["short"], "failed": top["failed"], "calls": top["calls"], "target_success_pct": tgt},
                   summary={"finding": f"**{cap(name)} miss the agreed {pct(tgt)} success target** on {code(svc)} — {achieved_text(100 - ach['pct'], tgt)} achieved {wp}"
                                       + ("; it has **no monitors**" if no_mon else ""),
                            "who": aud, "action": f"Root-cause the {code(top['short'])} failures; track the agreed target as an SLO"})


def _lc_first(t):
    """Lower-case the first word unless it is an acronym ('SLOs', 'APM')."""
    w, _, rest = t.partition(" ")
    if len(w) > 1 and w[1:].islower():
        w = w[0].lower() + w[1:]
    return w + (" " + rest if rest else "")


# ---------------------------------------------------------------- 1. failing operations
@detector("failing_operations", 10, needs=("failure_signal",))
def det_failing_operations(F, cfg, prev):
    T, labels = cfg["thresholds"], kpi_labels(F)
    out, healthy = [], {}
    for svc, rows in F["resources"].items():
        s, dom, tier, aud = svc_ctx(F, svc)
        if s["role"] != "entry" or not s.get("failure_known", True):
            continue
        stier = s["requests"]["tier"]
        wp, mc = window_phrase(F, stier), min_calls(F, cfg, stier)
        biz = [r for r in rows if r["class"] == "business" and r["calls"] >= mc and r.get("failure_known", True)]
        allbiz = [r for r in rows if r["class"] == "business" and r.get("failure_known", True)]
        for r in biz:
            r["kpi_label"] = labels.get(r.get("kpi"))
        own = lambda r: r["kpi_label"] if r.get("kpi_label") and (F["kpis"].get(r["kpi"]) or {}).get("matched") == [r["resource"]] else None
        bad = sorted([r for r in biz if r["err"]["pct"] >= T["fail_amber_pct"]], key=lambda r: (-r["failed"], r["resource"]))
        worst_ok = max(biz, key=lambda r: r["err"]["pct"]) if biz else None
        missed = _missed_success(F, biz, bad, T, svc)
        small_bad = sorted([r for r in allbiz if r["calls"] < mc and r["err"]["pct"] >= T["fail_amber_pct"] and r["failed"] >= 5], key=lambda r: (-r["failed"], r["resource"]))
        if not bad and missed:
            out.append(_target_miss_finding(F, T, svc, s, dom, tier, aud, stier, wp, missed, small_bad, mc))
            continue
        if not bad:
            if worst_ok:
                line = (f"no business operation on {code(svc)} with at least {n(mc)} calls fails more than {thr_pct(T['fail_amber_pct'])} (the worst is {code(worst_ok['short'])} at {pct(worst_ok['err'])})")
                if small_bad:
                    line += "; smaller operations fail more: " + ", ".join(f"{code(r['short'])} {frac(r['err'])}" for r in small_bad[:3])
                healthy.setdefault(s["domain"], []).append(line)
            continue
        red_count = per_window(F, T["fail_red_count"], stier)
        red_rows = [r for r in bad if r["err"]["pct"] >= T["fail_red_pct"] or (stier == "aggregate" and r["failed"] >= red_count)
                    or (r.get("kpi") and F["kpis"][r["kpi"]].get("critical"))]
        strong = any(r["err"]["pct"] >= 2 * T["fail_red_pct"] or (stier == "aggregate" and r["failed"] >= 5 * red_count) for r in red_rows)
        sev = tiered("red" if red_rows else "amber", tier, strong)
        top = bad[0]
        rest_red = sorted([r for r in bad[1:] if r["err"]["pct"] >= T["fail_red_pct"]], key=lambda r: -r["err"]["pct"])
        table_rows = [[res_label(r), n(r["calls"]), f"**{n(r['failed'])}**" if r["err"]["pct"] >= T["fail_red_pct"] else n(r["failed"]),
                       f"**{pct(r['err'])}**" if r["err"]["pct"] >= T["fail_red_pct"] else pct(r["err"]), sec(r["p95_s"])] for r in (bad + missed)[:8]]
        bullets = []
        per_day = maybe_per_day(top["failed"], F["window"], stier)
        bullets.append(f"**{code(top['short'])} fails {frac(top['err'])}** {wp}" + (f" — {per_day}" if per_day else "") + ". Each failure is a user or an operator who has to retry, or work that is not done.")
        worst_rate = max(bad, key=lambda r: r["err"]["pct"])
        if worst_rate is not top and worst_rate["err"]["pct"] >= 2 * top["err"]["pct"]:
            bullets.append(f"**{code(worst_rate['short'])} fails {frac(worst_rate['err'])}** — fewer calls, but the highest failure rate on the service" + (f": {fail_phrase(worst_rate['err'], 'calls')}." if one_in_ok(worst_rate['err']['pct']) else "."))
        dead_ops = [r for r in bad if r["err"]["pct"] >= 99.5 and r["calls"] >= T["by_design_min_calls"]]
        if dead_ops:
            bullets.append(f"{join_and(code(r['short']) for r in dead_ops[:3])} fail{'s' if len(dead_ops) == 1 else ''} on every call — either down or failing by design; confirm with the team before anyone excludes {'it' if len(dead_ops) == 1 else 'them'} from the monitors.")
        pair = _paired(bad, T)
        if pair:
            bullets.append(f"{code(pair[0]['short'])} and {code(pair[1]['short'])} fail {n(pair[0]['failed'])} and {n(pair[1]['failed'])} times — the counts match almost exactly, so they fail together, most likely from one cause.")
        tot = s["err"]["business"]["num"]
        shown = sum(r["failed"] for r in bad)
        biggest_low = max((r for r in allbiz if r not in bad and r["failed"]), key=lambda r: r["failed"], default=None)
        if tot and shown / tot < 0.6 and biggest_low:
            largest = biggest_low["failed"] > max(r["failed"] for r in bad)
            reason = (f"too few calls to flag on its own (under {n(mc)})" if biggest_low["err"]["pct"] >= T["fail_amber_pct"] and biggest_low["calls"] < mc else "below the threshold")
            bullets.append(f"{code(biggest_low['short'])} fails {frac(biggest_low['err'])} — {reason}, but "
                           + ("the largest single source of failures on the service." if largest else "the largest source of failures among the operations below it."))
        targets = []                  # (row, KPI) with an agreed success target: the top operation's, and any other in the table that misses it
        for r in (bad + missed)[:8]:
            k = F["kpis"].get(r.get("kpi")) if r.get("kpi") else None
            if not k or k.get("slo_success_pct") is None or any(r["kpi"] == x["kpi"] for x, _ in targets):
                continue
            _, achieved = _kpi_achieved(F, r, svc)
            if r is not top and 100 - achieved["pct"] >= k["slo_success_pct"]:
                continue
            targets.append((r, k))
            bullets.append(_target_bullet(F, r, svc))
        no_mon = unwatched(F, s)
        if no_mon:
            bullets.append(f"**No monitor watches {code(svc)}**, so none of these failures pages anyone.")
        smaller = ("Smaller items: " + ", ".join(f"{code(r['short'])} {pct(r['err'])}" for r in bad[8:14]) + ".") if len(bad) > 8 else ""
        what = own(top) or f"{code(top['short'])} calls"
        others = ""
        if rest_red:
            r = rest_red[0]
            others = f"; {own(r) + ' fail' if own(r) else code(r['short']) + ' fails'} {pct(r['err'])}"
            if len(bad) > 1 + len(rest_red):
                others += f", and {plural(len(bad) - 1 - len(rest_red), 'more operation')} above {thr_pct(T['fail_amber_pct'])}"
        elif len(bad) > 1:
            others = f", and {plural(len(bad) - 1, 'more operation')} fail{'s' if len(bad) == 2 else ''} above {thr_pct(T['fail_amber_pct'])}"
        if len(bad) == 1:
            title = f"{cap(fail_phrase(top['err'], what))} on {subj(s, svc)}"
        elif top["err"]["pct"] >= T["fail_red_pct"] or rest_red:
            title = f"{cap(fail_phrase(top['err'], what))} on {subj(s, svc)}{others}"
        else:
            title = f"{cap(plural(len(bad), 'operation'))} on {subj(s, svc)} fail above {thr_pct(T['fail_amber_pct'])}"
        if no_mon:
            title += ", and nothing is watching it"
        nbiz = len(allbiz)
        if nbiz == 1:
            tail = "All of them are on its one operation:"
        elif tot and shown / tot >= 0.6:
            tail = "The failures concentrate on one operation:" if len(bad) == 1 else f"They concentrate on {words(len(bad))} operations:"
        else:
            floor_failed = sum(r["failed"] for r in allbiz if r not in bad and r["calls"] < mc and r["err"]["pct"] >= T["fail_amber_pct"])
            rest = tot - shown
            why = ("operations with too few calls to flag on their own" if floor_failed and floor_failed >= rest else
                   ("operations that fail less often or have too few calls to flag on their own" if floor_failed else "operations that fail less often"))
            tail = (f"{n(shown)} of those {n(tot)} failures are on the {plural(len(bad), 'operation')} above {thr_pct(T['fail_amber_pct'])}; "
                    f"the other {n(rest)} come from {why}:")
        lead = f"{code(svc)}{_desc(dom)} handled {big(s['requests']['business'])} business requests {wp} and failed {frac(s['err']['business'])}. {tail}"
        actions = [{"text": f"Pull one day of {code(top['short'])} failures from the application log and classify them (validation, data, timeout, dependency)", "horizon": "now"}]
        if stier == "aggregate" and F["window"]["per_day_allowed"]:
            days = F["window"]["aggregate"]["days"] or 7
            hourly = top["failed"] / days / 24
            if 2 * hourly < 5:
                daily = top["failed"] / days
                alert = f"Alert when {code(top['short'])} fails more than {n(nice_up(2 * daily))} times in a day (it fails about {x_text(daily, 1)} a day {wp})"
            else:
                thr = nice_up(2 * hourly)
                avg = x_text(hourly, 1) if hourly < 10 else n(round(hourly))
                alert = f"Alert when {code(top['short'])} fails more than {n(thr)} times in an hour (about {x_text(thr / hourly, 1)}× the hourly average {wp}, which is {avg})"
            alert += ", and lower the threshold as the fix lands"
        elif stier == "aggregate":
            alert = (f"Alert on the `trace.*.errors` metric for {code(top['short'])} at about twice its hourly average over the last 7 days "
                     "(read that average in Datadog's Metrics Explorer; the export range is not proved, so no number is quoted here)")
        else:
            alert = f"Alert on the `trace.*.errors` metric for {code(top['short'])} once a week of trace data is available, with the threshold at twice the observed hourly average"
        if no_mon:
            actions.insert(0, {"text": f"Add a failure-rate monitor on {code(svc)} — it has no monitor today — and {_lc_first(alert)}", "horizon": "now"})
        else:
            actions.append({"text": alert, "horizon": "now"})
        for r, k in targets:
            actions.append({"text": f"Track {r.get('kpi_label') or code(r['short']) + ' calls'} against the agreed {pct(k['slo_success_pct'])} success target as a Datadog SLO, "
                                    "so the burn rate pages before the target is missed", "horizon": "next"})
        # a success SLO is proposed for an operation that has none and is not failing on every call
        no_target = [r for r in bad if r not in dead_ops and (F["kpis"].get(r.get("kpi")) or {}).get("slo_success_pct") is None]
        if sev == "red" and no_target and (no_target[0] is top or not targets):
            r = no_target[0]
            actions.append({"text": f"Agree a success-rate SLO for {r.get('kpi_label') or code(r['short']) + ' calls'} (for example 99.5 % over 28 days) once the baseline is understood", "horizon": "later"})
        out.append(finding("failing_operations", svc, sev, top["failed"] * (2 if sev == "red" else 1), F, svc=svc, title=title, lead=lead, bullets=bullets,
                           table={"columns": ["Operation", "Calls", "Failed", "Failure rate", "p95"], "rows": table_rows}, smaller=smaller, actions=actions,
                           live_graph=live(F, svc, top["resource"]), subject={"service": svc, "key": svc, "resources": [r["resource"] for r in bad]},
                           metrics={"primary": {"label": f"failure rate of {top['short']}", "value": top["err"]["pct"], "unit": "pct", "higher_is_worse": True,
                                                "num": top["err"]["num"], "den": top["err"]["den"], "subject": [svc, top["resource"]]},
                                    "worst_resource": top["resource"], "worst_short": top["short"], "failed": top["failed"], "calls": top["calls"]},
                           summary={"finding": f"**{cap(fail_phrase(top['err'], what))}** on {code(svc)} ({n(top['failed'])} of {n(top['calls'])} {wp}){others}" + ("; it has **no monitors**" if no_mon else ""),
                                    "who": aud, "action": f"Root-cause the {code(top['short'])} failures; alert on regressions"}))
    return {"findings": out, "healthy": healthy}


# ---------------------------------------------------------------- 2. slow hot paths
@detector("slow_hot_paths", 20)
def det_slow_hot_paths(F, cfg, prev, failing=frozenset()):
    T = cfg["thresholds"]
    out, healthy = [], {}
    fs = F["coverage"]["failure_signal"]
    for svc, rows in F["resources"].items():
        s, dom, tier, aud = svc_ctx(F, svc)
        if s["role"] != "entry":
            continue
        stier = s["requests"]["tier"]
        wp, mc = window_phrase(F, stier), min_calls(F, cfg, stier)
        biz = [r for r in rows if r["class"] == "business" and r["p95_s"] is not None and r["calls"] and (r["tier"] == "aggregate" or r["sample"]["n"] >= 20)]
        timed = sum(r["calls"] for r in biz)
        if len(biz) < 3 or timed < mc:
            continue
        total = sum(r["calls"] for r in rows if r["class"] == "business" and r["calls"])     # the base the lead names
        agreed_ok = lambda r: bool(r.get("kpi")) and (F["kpis"].get(r["kpi"]) or {}).get("slo_p95_s") is not None and r["p95_s"] <= F["kpis"][r["kpi"]]["slo_p95_s"]
        share = lambda t: 100.0 * sum(r["calls"] for r in biz if r["p95_s"] >= t and not agreed_ok(r)) / total
        s2, s3, s5 = share(T["slow_p95_s"]), share(3.0), share(5.0)
        top30 = sorted(biz, key=lambda r: -r["calls"])[:30]
        floor = min(r["p95_s"] for r in top30)
        fixed = floor >= T["fixed_delay_floor_s"] and s2 >= T["fixed_delay_share_pct"]
        outlier_calls = per_window(F, T["outlier_min_calls"], stier)
        outliers = [r for r in biz if r["p95_s"] >= T["outlier_p95_s"] and r["calls"] >= outlier_calls and not agreed_ok(r)]
        if s2 < T["slow_share_amber_pct"] and not outliers:
            healthy.setdefault(s["domain"], []).append(f"{code(svc)} answers {pct(100 - s2, 0)} of its traffic within {sec(T['slow_p95_s'])} at p95")
            continue
        level = "red" if (fixed or s2 >= T["slow_share_red_pct"]) else "amber"
        sev = tiered(level, tier, strong=fixed or s2 >= 90)
        slow_biz = [r for r in biz if r["p95_s"] >= T["slow_p95_s"] and not agreed_ok(r)]
        agreed_slow = sorted([r for r in biz if r["p95_s"] >= T["slow_p95_s"] and agreed_ok(r)], key=lambda r: -r["calls"])
        top_slow = top30[0]["p95_s"] >= T["slow_p95_s"] and not agreed_ok(top30[0])
        scope = "every feature" if fixed else ("its busiest features" if top_slow else (f"{words(len(slow_biz))} of its features" if len(slow_biz) > 1 else "one of its features"))
        slow_rows = sorted([r for r in top30 if r["p95_s"] >= T["slow_p95_s"] and not agreed_ok(r)], key=lambda r: -r["calls"])[:6]
        slow_rows += [r for r in sorted((r for r in biz if not agreed_ok(r)), key=lambda r: -r["p95_s"])[:4] if r not in slow_rows and r["p95_s"] >= T["slow_p95_s"]]
        floor_calls = per_window(F, T["slow_op_min_calls"], stier)
        frequent_slow = [r for r in slow_rows if r["calls"] >= floor_calls]
        slowest = max(frequent_slow or [r for r in slow_rows if r in top30] or slow_rows, key=lambda r: r["p95_s"]) if slow_rows else None
        labels_ = kpi_labels(F)
        for r in slow_rows:
            r["kpi_label"] = labels_.get(r.get("kpi"))
        table_rows = [[res_label(r), n(r["calls"]), f"**{sec(r['p95_s'])}**" if r["p95_s"] >= 5 else sec(r["p95_s"])] for r in slow_rows[:8]]
        bullets = []
        sh = lambda x: pct(x, 0) if x >= 1 else pct(x)
        outlier_only = s2 < T["slow_share_amber_pct"]
        if (s3 or s5) and not outlier_only:
            bullets.append(f"{sh(s3)} of the traffic is on operations with p95 above 3 s" + (f", and {sh(s5)} above 5 s" if s5 else "") + ".")
        if fixed:
            bullets.append(f"Even the quickest of its {words(len(top30))} busiest operations has a p95 of {sec(floor)}, which points to a **fixed delay on every request** (bootstrapping, configuration or secret reads, a slow dependency on the path) rather than one slow feature.")
        if outliers and not outlier_only:
            bullets.append("Very slow and frequent: " + join_and(f"{code(r['short'])} {sec(r['p95_s'])} on {n(r['calls'])} calls" for r in outliers[:3]) + ".")
        for r in slow_rows + [r for r in biz if r not in slow_rows and r["p95_s"] < T["slow_p95_s"]]:
            k = F["kpis"].get(r.get("kpi")) if r.get("kpi") else None
            if k and k.get("slo_p95_s") is not None and r["p95_s"] > k["slo_p95_s"]:
                bullets.append(f"{labels_.get(r.get('kpi')) or code(r['short'])}: agreed p95 target {sec(k['slo_p95_s'])}, actual {sec(r['p95_s'])} on {code(svc)}.")
        anat = F["anatomy"].get(svc)
        diagram = None
        if anat:
            lat = anat["entry_latency_ms"]
            sample_phrase = F["window"]["phrase"]["sample"]
            if anat["bimodal"]:
                bullets.append(f"{cap(sample_phrase)}, requests were bimodal: a quarter finished within {sec(lat['p25'] / 1000)}, a quarter took over {sec(lat['p75'] / 1000)} (p99 {sec(lat['p99'] / 1000)}).")
            swc = anat.get("slow_without_children")
            if swc and anat.get("children_per_request") and swc["den"] >= 20 and swc["pct"] >= T["slow_without_children_pct"]:
                bullets.append(f"{frac(swc)} of the slow requests {sample_phrase} showed **no database, HTTP or cache spans inside them** — the time is spent where the tracer does not see (file I/O, locking, uninstrumented code). A profiler will show where.")
            if len(anat["children_per_request"]) >= 2 and anat.get("requests_with_tree", anat["entries"]) >= T["min_entry_spans_for_anatomy"]:
                diagram = f"inside:{svc}"
        target_ops = "busiest operations" if top_slow else "slow operations"
        actions = [{"text": f"Turn on the Datadog continuous profiler for {code(svc)} for a day — it shows wall time by function, including time the traces do not cover", "horizon": "next"},
                   {"text": f"Set a latency target for the {target_ops} of {code(svc)} (for example p95 under {sec(T['slow_p95_s'])}) and track it as an SLO", "horizon": "later"}]
        no_mon = unwatched(F, s)
        if no_mon:
            base = max(T["slow_p95_s"] * 2, 4.0)
            if any(agreed_ok(r) for r in biz):        # an operation within its agreed target does not size the monitor
                worst_now = max([r["p95_s"] for r in slow_rows] + [s["p95_s"] or 0.0])
            else:
                worst_now = max(s["p95_worst_s"] or 0.0, s["p95_s"] or 0.0)
            thr = base if worst_now < base else math.ceil(1.5 * worst_now * 2) / 2
            actions.insert(0, {"text": f"Add a latency monitor on {code(svc)} (p95 above {sec(thr)} for 10 minutes" + (", above today's worst feature; lower it as the fix lands" if thr > base else "") + ") — it has no monitor today", "horizon": "now"})
        o1 = max(outliers, key=lambda r: r["p95_s"]) if outliers else None
        if outlier_only and o1:
            title = f"{subj(s, svc)} has a very slow feature: {code(o1['short'])} takes {sec(o1['p95_s'])} at p95" + (", and nothing is watching it" if no_mon else "")
        else:
            title = f"{subj(s, svc)} is slow on {scope}" + (", and nothing is watching it" if no_mon else "")
        if not fs or not s.get("failure_known", True):
            fail_clause = ""
        elif s["err"]["business"]["pct"] < 0.5 and svc not in failing:
            fail_clause = f" with very few failures ({pct(s['err']['business'])})"
        else:
            fail_clause = f" with a {pct(s['err']['business'])} failure rate overall"
        if outlier_only and o1:
            olist = join_and(code(r["short"]) + " takes " + sec(r["p95_s"]) + " at p95 on " + n(r["calls"]) + " calls" for r in outliers[:3])
            osh = 100.0 * sum(r["calls"] for r in outliers[:3]) / total
            lead = (f"{code(svc)}{_desc(dom)} answers most of its {big(s['requests']['business'])} business requests {wp} quickly{fail_clause}"
                    + f", but **{olist}** ({sh(osh)} of the traffic):")
        else:
            lead = (f"{code(svc)}{_desc(dom)} handled {big(s['requests']['business'])} business requests {wp}{fail_clause}"
                    + f" — but **{sh(s2)} of that traffic sits on {'one operation whose' if len(slow_rows) == 1 else 'operations whose'} p95 is above {sec(T['slow_p95_s'])}**"
                    + (f" (leaving out {join_and(code(r['short']) for r in agreed_slow[:3])}, within {'its' if len(agreed_slow) == 1 else 'their'} agreed p95 target{'s' if len(agreed_slow) != 1 else ''})"
                       if agreed_slow else "") + ":")
        out.append(finding("slow_hot_paths", svc, sev, total * s2 / 100.0, F, svc=svc, title=title, lead=lead, bullets=bullets, diagram=diagram,
                           table={"columns": ["Feature", f"Calls ({wp.replace('in the ', '')})", "p95"], "rows": table_rows}, actions=actions, live_graph=live(F, svc, kind="p95"),
                           metrics={"primary": ({"label": f"p95 of {o1['short']}", "value": o1["p95_s"], "unit": "s", "higher_is_worse": True, "subject": [svc, o1["resource"]]}
                                                if (outlier_only and o1) else
                                                {"label": f"share of {svc} traffic with p95 above {sec(T['slow_p95_s'])}", "value": round(s2, 1), "unit": "pct", "higher_is_worse": True, "subject": svc}),
                                    "worst_resource": o1["resource"] if (outlier_only and o1) else None,
                                    "floor_s": floor, "share_3s": round(s3, 1), "share_5s": round(s5, 1),
                                    "outlier_only": bool(outlier_only and outliers), "outlier_resources": [r["resource"] for r in outliers[:5]],
                                    "slow_resources": [r["resource"] for r in slow_rows[:8]]},
                           summary={"finding": ((f"**{subj(s, svc)} has a very slow feature** — {code(o1['short'])} takes {sec(o1['p95_s'])} at p95 on {n(o1['calls'])} calls {wp}" if outlier_only and o1 else
                                                 f"**{subj(s, svc)} is slow on {scope}** — {sh(s2)} of its traffic is on operations with p95 above {sec(T['slow_p95_s'])}"
                                                 + (" outside their agreed targets" if agreed_slow else "")
                                                 + (f"; the slowest frequent one is {code(slowest['short'])} at {sec(slowest['p95_s'])}" if slowest else ""))
                                                + ("; it has **no monitors**" if no_mon else "")),
                                    "who": aud, "action": "Profile the request path; set a latency target" + ("; add monitors" if no_mon else "")}))
    return {"findings": out, "healthy": healthy}


# ---------------------------------------------------------------- 3. slow operations (services not covered by 2)
@detector("slow_operations", 30)
def det_slow_operations(F, cfg, prev, covered=None):
    T = cfg["thresholds"]
    out = []
    labels = kpi_labels(F)
    by_domain = defaultdict(list)
    for svc, rows in F["resources"].items():
        s = F["services"][svc]
        if s["role"] != "entry" or (covered and svc in covered):
            continue
        min_c = per_window(F, T["slow_op_min_calls"], s["requests"]["tier"])
        for r in rows:
            agreed = (F["kpis"].get(r.get("kpi")) or {}).get("slo_p95_s") if r.get("kpi") else None
            over = r["p95_s"] is not None and (r["p95_s"] >= T["slow_op_s"] or (agreed is not None and r["p95_s"] > agreed))
            if r["class"] == "business" and over and r["calls"] >= min_c and (r["tier"] == "aggregate" or r["sample"]["n"] >= 20) \
                    and not (agreed is not None and r["p95_s"] <= agreed):
                r["kpi_label"] = labels.get(r.get("kpi"))
                by_domain[(s["domain"], r["tier"])].append((svc, r))
    for (dkey, rtier), items in sorted(by_domain.items(), key=lambda kv: (kv[0][0], kv[0][1] != "aggregate")):
        dom = F["domains"].get(dkey, {})
        tier, aud = dom.get("tier", "staff"), dom.get("audience", "Users")
        items.sort(key=lambda t: (-(t[1]["calls"] * t[1]["p95_s"]), t[1]["resource"]))
        svcs = sorted({svc for svc, _ in items})
        wp = window_phrase(F, rtier)
        biz_all = [r for svc in svcs for r in F["resources"][svc] if r["class"] == "business" and r["p95_s"] is not None and r["tier"] == rtier]
        tot = sum(r["calls"] for r in biz_all) or 1
        agreed_ok = lambda r: bool(r.get("kpi")) and (F["kpis"].get(r["kpi"]) or {}).get("slo_p95_s") is not None and r["p95_s"] <= F["kpis"][r["kpi"]]["slo_p95_s"]
        slow_share = 100.0 * sum(r["calls"] for r in biz_all if r["p95_s"] >= T["slow_op_s"] and not agreed_ok(r)) / tot
        above = [x for x in items if x[1]["p95_s"] >= T["slow_op_s"]]
        target_only = [x for x in items if x not in above]
        outlier_calls = per_window(F, T["outlier_min_calls"], rtier)
        outl = [(svc, r) for svc, r in items if r["p95_s"] >= T["outlier_p95_s"] and r["calls"] >= outlier_calls]
        sev = tiered("red" if outl else "amber", tier, strong=any(r["p95_s"] >= 3 * T["outlier_p95_s"] for _, r in outl))
        table_rows = [[f"{res_label(r)} ({code(svc)})" if len(svcs) > 1 else res_label(r), n(r["calls"]), f"**{sec(r['p95_s'])}**" if r["p95_s"] >= T["outlier_p95_s"] else sec(r["p95_s"])] for svc, r in items[:8]]
        smaller_ops = ("Smaller items: " + ", ".join(f"{code(r['short'])} {sec(r['p95_s'])}" for _, r in items[8:14]) + ("…" if len(items) > 14 else "") + ".") if len(items) > 8 else ""
        worst_svc, worst = items[0]
        lbl = dom.get("label", dkey)
        wname = code(worst["short"]) + (f" ({worst['kpi_label']})" if worst.get("kpi_label") and worst["kpi_label"] != worst["short"] else "")
        if len(items) == 1 and target_only:
            title = f"{wname} misses its agreed p95 target in {lbl}: it takes {sec(worst['p95_s'])}"
        elif len(items) == 1:
            title = f"One slow operation in {lbl}: {wname} takes {sec(worst['p95_s'])} at p95"
        elif not target_only:
            title = f"{cap(words(len(items)))} slow operations in {lbl}, above {thr_sec(T['slow_op_s'])} at p95"
        elif not above:
            title = f"{cap(words(len(items)))} operations in {lbl} miss their agreed p95 targets"
        else:
            title = f"{cap(words(len(items)))} slow operations in {lbl}"
        names = join_and(code(x) for x in svcs)
        verb = "serves" if len(svcs) == 1 else "serve"
        ops_n = f"{words(len(above))} frequent operation{'s' if len(above) > 1 else ''}"
        be = "are" if len(above) > 1 else "is"
        missing_t = (f"{words(len(target_only))} {'misses its agreed p95 target' if len(target_only) == 1 else 'miss their agreed p95 targets'}" if target_only else "")
        any_over = any(r["p95_s"] >= T["slow_op_s"] for svc_ in svcs for r in F["resources"][svc_] if r["class"] == "business" and r["p95_s"] is not None)
        agreed_over = [(svc_, r) for svc_ in svcs for r in F["resources"][svc_] if r["class"] == "business" and r["p95_s"] is not None and r["p95_s"] >= T["slow_op_s"] and agreed_ok(r)]
        small_over = [(svc_, r) for svc_ in svcs for r in F["resources"][svc_] if r["class"] == "business" and r["p95_s"] is not None
                      and r["p95_s"] >= T["slow_op_s"] and not agreed_ok(r) and not any(r is x[1] for x in above)]
        if not above and not any_over:
            lead = f"{names} {'stays' if len(svcs) == 1 else 'stay'} under the review's {thr_sec(T['slow_op_s'])} bar {wp}, but {missing_t}:"
        elif not above and small_over:
            lead = (f"Only {join_and(code(r['short']) + ' (' + n(r['calls']) + ' calls, too few to flag)' for _, r in small_over[:3])} "
                    f"{'is' if len(small_over) == 1 else 'are'} above the review's {thr_sec(T['slow_op_s'])} bar {wp}, but {missing_t}:")
        elif not above:
            lead = f"No operation of {names} outside the agreed targets is above the review's {thr_sec(T['slow_op_s'])} bar {wp}, but {missing_t}:"
        elif slow_share >= 99:
            lead = f"Every operation {names} {verb} is slow {wp} — {ops_n} {be} above {thr_sec(T['slow_op_s'])} at p95:"
        elif slow_share >= 50:
            lead = f"{pct(slow_share, 0)} of what {names} {verb} is slow {wp}; {ops_n} {be} above {thr_sec(T['slow_op_s'])} at p95:"
        elif slow_share >= 30:
            lead = f"{names} {'is' if len(svcs) == 1 else 'are'} quick on most calls, but {pct(slow_share, 0)} of the traffic sits on slow operations — {ops_n} {be} slow {wp}:"
        else:
            lead = f"Most of what {names} {verb} is quick, but {ops_n} {be} slow {wp}:"
        if above and target_only:
            lead = lead[:-1] + f"; {missing_t}:"
        if above and agreed_over:
            lead = (f"Leaving out {join_and(code(r['short']) for _, r in agreed_over[:3])} ({'within its agreed p95 target' if len(agreed_over) == 1 else 'within their agreed p95 targets'}), "
                    + _lc_first(lead))
        bullets = [f"{code(worst['short'])} is the costliest: {n(worst['calls'])} calls at {sec(worst['p95_s'])} p95 — " + ("each call keeps a user waiting" if tier in ("customer", "clinical") else "each call keeps someone waiting") + "."]
        k = F["kpis"].get(worst.get("kpi")) if worst.get("kpi") else None
        for svc_, r_ in items[:8]:
            k_ = F["kpis"].get(r_.get("kpi")) if r_.get("kpi") else None
            if k_ and k_.get("slo_p95_s") is not None:
                bullets.append(f"The agreed p95 target for {r_.get('kpi_label') or code(r_['short'])} is {sec(k_['slo_p95_s'])}; it runs at {sec(r_['p95_s'])} on {code(svc_)}.")
        if k and k.get("slo_p95_s") is not None:
            actions = [{"text": f"Profile {code(worst['short'])} on {code(worst_svc)} (query plan, downstream calls) to bring it under its agreed {sec(k['slo_p95_s'])} p95", "horizon": "next"}]
        else:
            actions = [{"text": f"Profile {code(worst['short'])} on {code(worst_svc)} (query plan, downstream calls) and set a p95 target", "horizon": "next"}]
        fkey = dkey if rtier == F["window"]["volumes_tier"] else f"{dkey}-{rtier}"
        calls_head = "Calls" if rtier == F["window"]["volumes_tier"] else f"Calls ({wp.replace('in the ', '')})"
        out.append(finding("slow_operations", fkey, sev, sum(r["calls"] for _, r in items) / 10.0, F, svc=worst_svc, title=title, lead=lead, bullets=bullets, actions=actions,
                           table={"columns": ["Operation", calls_head, "p95"], "rows": table_rows}, smaller=smaller_ops, live_graph=live(F, worst_svc, worst["resource"], kind="p95"),
                           subject={"service": worst_svc, "key": fkey, "resources": [r["resource"] for _, r in items], "services": svcs}, domain=dkey, audience=aud, tier=tier,
                           metrics={"primary": {"label": f"p95 of {worst['short']}", "value": worst["p95_s"], "unit": "s", "higher_is_worse": True, "subject": [worst_svc, worst["resource"]]},
                                    "worst_resource": worst["resource"], "worst_service": worst_svc, "worst_short": worst["short"]},
                           summary={"finding": _slow_ops_summary(F, items, above, target_only, lbl, wname, worst, wp),
                                    "who": aud, "action": "Tune the slow queries and downstream calls"}))
    return {"findings": out, "healthy": {}}


def _slow_ops_summary(F, items, above, target_only, lbl, wname, worst, wp):
    """The summary line of a slow-operations finding, worded as its title: the review's bar, an agreed target, or both."""
    k = F["kpis"].get(worst.get("kpi")) if worst.get("kpi") else None
    vs = f" against its agreed {sec(k['slo_p95_s'])}" if k and k.get("slo_p95_s") is not None else ""
    if len(items) == 1 and target_only:
        return f"**{wname} misses its agreed p95 target in {lbl}** — {sec(worst['p95_s'])} at p95{vs} on {n(worst['calls'])} calls {wp}"
    if len(items) == 1:
        return f"**One slow operation in {lbl}** — {code(worst['short'])} takes {sec(worst['p95_s'])} at p95 on {n(worst['calls'])} calls {wp}"
    if not above:
        return f"**{cap(words(len(items)))} operations in {lbl} miss their agreed p95 targets** — the costliest is {code(worst['short'])}, {sec(worst['p95_s'])} at p95{vs} on {n(worst['calls'])} calls {wp}"
    extra = (f" ({words(len(target_only))} {'misses its agreed p95 target' if len(target_only) == 1 else 'miss their agreed p95 targets'})" if target_only else "")
    return f"**{cap(words(len(items)))} slow operations in {lbl}**{extra} — the costliest is {code(worst['short'])}, {sec(worst['p95_s'])} at p95 on {n(worst['calls'])} calls {wp}"


# ---------------------------------------------------------------- 4. failing dependencies
@detector("failing_dependencies", 40, needs=("dependencies",))
def det_failing_dependencies(F, cfg, prev):
    T = cfg["thresholds"]
    rows = []
    real = {k: d for k, d in F["dependencies"].items() if d["class"] != "telemetry"}
    known = {k: d for k, d in real.items() if d.get("failure_known", True)}
    for name, d in real.items():
        fk = d.get("failure_known", True)
        failing = fk and d["err"]["pct"] >= T["dep_fail_pct"] and d["calls"] >= T["dep_min_calls"]
        dead = fk and d["err"]["pct"] >= T["dep_dead_pct"] and d["calls"] >= T["dep_dead_min_calls"]
        slow = d["p95_s"] is not None and d["p95_s"] >= T["dep_slow_s"] and d["calls"] >= per_window(F, T["dep_slow_min_calls"], d["tier"])
        if failing or dead or slow:
            rows.append((name, d, failing, dead, slow))
    below = sorted([(k, d) for k, d in known.items() if T["fail_amber_pct"] <= d["err"]["pct"] < T["dep_fail_pct"] and d["calls"] >= T["dep_min_calls"]
                    and k not in {r[0] for r in rows}], key=lambda kv: -kv[1]["failed"])
    if not rows:
        if known and F["coverage"].get("host_visibility") and not below:
            worst = max(known.values(), key=lambda d: d["err"]["pct"])
            k = len(known)
            ceiling = f"under {pct(0.1, 1)}" if worst["err"]["pct"] < 0.1 else f"at most {pct(worst['err'])}"
            if k == 1:
                line = f"the one dependency with a failure signal ({code(next(iter(known)))}) answers with {ceiling} failures"
            else:
                line = f"{'both' if k == 2 else 'all ' + words(k)} dependencies with a failure signal answer with {ceiling} failures"
            if len(real) > k:
                rest = len(real) - k
                line += f"; the other {'one carries' if rest == 1 else words(rest) + ' carry'} no failure signal in the data"
            return {"findings": [], "healthy": {"_estate": [line]}}
        if below:
            return {"findings": [], "healthy": {}, "also": [_below_bar_note(F, T, below)]}
        return {"findings": [], "healthy": {}}
    rows.sort(key=lambda t: (-(t[1]["failed"] or 0), -t[1]["calls"], t[0]))
    callers_tier = "staff"
    for name, d, *_ in rows:
        for c in d["callers"]:
            if F["services"].get(c["service"], {}).get("tier") in ("customer", "clinical"):
                callers_tier = F["services"][c["service"]]["tier"]
    red = any(dead or (failing and d["calls"] >= per_window(F, T["dep_red_calls"], d["tier"])) for name, d, failing, dead, slow in rows)
    shown_deps = [d for _, d, *_ in rows[:10]] + [d for _, d in below[: max(0, 10 - len(rows[:10]))]]
    mixed = len({d["tier"] for d in shown_deps}) > 1
    calls_cell = lambda d: n(d["calls"]) + (f" ({window_phrase(F, d['tier']).replace('in the ', '')})" if mixed else "")
    sev = tiered("red" if red else "amber", callers_tier, strong=any(dead or d["err"]["pct"] >= 50 for _, d, *_ in rows))
    tier_phrase = window_phrase(F, rows[0][1]["tier"])
    method = F["coverage"]["graph_method"]
    table_rows = []
    for name, d, failing, dead, slow in rows[:10]:
        flag = " — fails every time" if dead else (f" — a **{d['marker']}** host called from production" if d["class"] == "nonprod" else "")
        failed_cell = ("not recorded in the sample" if not d.get("failure_known", True) else
                       "—" if d["failed"] == 0 and d["err"]["pct"] == 0 else (f"**{pct(d['err'])} ({n(d['failed'])})**" if failing or dead else pct(d["err"])))
        callers = ", ".join(code(c["service"]) for c in d["callers"][:3]) or ("not resolved (see the appendix)" if d.get("unjoined") else
                                                                              ("none in the sample" if method != "none" else "see the dependency page"))
        table_rows.append([code(name) + flag, calls_cell(d), failed_cell, f"**{sec(d['p95_s'])}**" if slow else sec(d["p95_s"]), callers])
    for name, d in below[: max(0, 10 - len(table_rows))]:
        table_rows.append([code(name), calls_cell(d), f"{pct(d['err'])} ({n(d['failed'])})", sec(d["p95_s"]), ", ".join(code(c["service"]) for c in d["callers"][:3]) or "—"])
    nfail = sum(1 for t in rows if t[2] or t[3])
    worst = max((t for t in rows if t[2] or t[3]), key=lambda t: t[1]["failed"], default=None)
    dead_rows = [t for t in rows if t[3]]
    bullets = []
    if worst:
        prefix = f"{code(worst[0])} matters most: " if nfail > 1 and not mixed else f"{code(worst[0])}: "
        tail = " — either decommissioned with something still calling, or down" if worst[2] and worst[3] else ""
        bullets.append(prefix + f"{n(worst[1]['failed'])} of {n(worst[1]['calls'])} calls failed {window_phrase(F, worst[1]['tier'])}"
                       + (f" (p95 {sec(worst[1]['p95_s'])} across all its calls)" if worst[1]["p95_s"] and worst[1]["p95_s"] >= 1 else "") + tail + ".")
    other_dead = [t for t in dead_rows if t is not worst]
    if other_dead:
        bullets.append(f"{join_and(code(t[0]) for t in other_dead[:3])} fail{'s' if len(other_dead) == 1 else ''} every time — either decommissioned with something still calling, or down.")
    slow_only = [t for t in rows if t[4] and not (t[2] or t[3])]
    if slow_only:
        bullets.append("Slow but answering: " + join_and(f"{code(t[0])} p95 {sec(t[1]['p95_s'])}" for t in slow_only[:3]) + ".")
    if below:
        bullets.append(_below_bar_note(F, T, below, sentence=True))
    if any(d.get("host_unknown") for _, d, *_ in rows[:10]):
        callers_u = sorted({c["service"] for _, d, *_ in rows if d.get("host_unknown") for c in d["callers"]})
        bullets.append(f"The export does not record the host {'these calls reach' if all(d.get('host_unknown') for _, d, *_ in rows[:10]) else 'behind the unnamed calls'}; "
                       f"the page of {join_and(code(c) for c in callers_u[:3])} in APM › Software Catalog › Dependencies lists it.")
    elif method in ("parent_child", "peer", "trace_cooccurrence"):
        bullets.append("Callers come from the span sample (" + {"parent_child": "parent/child span join", "peer": "peer tags", "trace_cooccurrence": "spans sharing a trace"}[method] + "); the dependency's page in APM › Software Catalog shows the full picture.")
    else:
        bullets.append("Open each dependency's page (APM › Software Catalog › the name › Dependencies) to see which services call it.")
    title = f"{cap(words(nfail))} dependenc{'y is' if nfail == 1 else 'ies are'} failing" if nfail else f"{cap(words(len(rows)))} dependenc{'y is' if len(rows) == 1 else 'ies are'} slow"
    sources = {d["source"] for d in shown_deps}
    if sources == {"inferred"}:
        who = "Datadog infers these services from outbound calls"
    elif "inferred" not in sources:
        who = "These are the callees of traced spans in the sample"
    else:
        who = "Datadog infers some of these from outbound calls; the others are callees seen in the span sample"
    lead = who + (". Each count says its window:" if mixed else f". {cap(tier_phrase)}:")
    one = len(rows) == 1
    actions = []
    if nfail:
        actions.append({"text": (f"Assign an owner to {code(rows[0][0])} and agree a fix or a retirement date" if one else "Assign an owner to each failing dependency and agree a fix or a retirement date"), "horizon": "now"})
    unknown_hosts = [t[0] for t in rows if t[1].get("host_unknown") and (t[2] or t[3])]
    if unknown_hosts:
        if F["coverage"]["columns"].get("peer"):
            actions.insert(0, {"text": f"Have the tracer set peer.service on the client spans behind {join_and(code(h) for h in unknown_hosts[:2])} (for example "
                                       "DD_TRACE_PEER_SERVICE_DEFAULTS_ENABLED=true, or export the out.host column) so the review can name the host, then agree the fix with its owner",
                               "horizon": "now"})
        else:
            ask = "peer.service" + ("" if F["coverage"]["columns"].get("parent_id") else " (or Parent ID)")
            actions.insert(0, {"text": f"Add {ask} to the spans export so the review can name the host behind {join_and(code(h) for h in unknown_hosts[:2])}, "
                                       "then agree the fix with that host's owner", "horizon": "now"})
        if one:
            actions = [a for a in actions if "retirement date" not in a["text"]]
    if slow_only:
        actions.append({"text": (f"Agree a timeout and a p95 target for {code(slow_only[0][0])} with its owner" if len(slow_only) == 1 else "Agree timeouts and latency targets with the owners of the slow dependencies"), "horizon": "next"})
    if dead_rows:
        actions.append({"text": f"Retire the calls to {join_and(code(t[0]) for t in dead_rows[:3])} or restore {'the service' if len(dead_rows) == 1 else 'them'}", "horizon": "now"})
    subject_host = (worst or rows[0])[0]
    if nfail:
        primary = {"label": f"failure rate of {subject_host}", "value": (worst or rows[0])[1]["err"]["pct"], "unit": "pct", "higher_is_worse": True, "subject": subject_host,
                   "num": (worst or rows[0])[1]["err"]["num"], "den": (worst or rows[0])[1]["err"]["den"]}
    else:
        primary = {"label": f"p95 of {subject_host}", "value": rows[0][1]["p95_s"], "unit": "s", "higher_is_worse": True, "subject": subject_host}
    fail_text = "; ".join(f"{code(t[0])} fails {pct(t[1]['err'], 0 if t[1]['err']['pct'] >= 99.5 else 1)}" + (f" ({n(t[1]['failed'])} of {n(t[1]['calls'])} calls {window_phrase(F, t[1]['tier'])}" + (f", p95 {sec(t[1]['p95_s'])}" if t[1]['p95_s'] is not None else "") + ")" if t is worst else "") for t in rows[:4] if t[2] or t[3])
    slow_text = "; ".join(f"{code(t[0])} p95 {sec(t[1]['p95_s'])}" for t in rows[:3])
    host_red = {name for name, d, failing, dead, slow in rows if dead or (failing and d["calls"] >= per_window(F, T["dep_red_calls"], d["tier"]))}
    host_severity = {name: (sev if name in host_red else ("amber" if sev == "red" else sev)) for name, *_ in rows}
    out = finding("failing_dependencies", "estate", sev, sum(d["failed"] for _, d, *_ in rows) * (2 if sev == "red" else 1), F, title=title, lead=lead, bullets=bullets, diagram="dependencies",
                  table={"columns": ["Dependency", "Calls", "Failed", "p95", "Callers"], "rows": table_rows}, actions=actions, tier=callers_tier,
                  audience="Users and staff, depending on the caller", domain=F["services"].get(rows[0][1]["callers"][0]["service"], {}).get("domain", "") if rows[0][1]["callers"] else "",
                  live_graph=[("note", recipes.dependency_note(rows[0][1]["callers"][0]["service"] if rows[0][1].get("host_unknown") and rows[0][1]["callers"] else rows[0][0]), "")],
                  subject={"service": None, "key": "estate", "hosts": [t[0] for t in rows], "host_severity": host_severity},
                  metrics={"primary": primary, "subject_host": subject_host},
                  summary={"finding": f"**{title}** — " + (fail_text or slow_text), "who": "Users and staff, depending on the caller",
                           "action": ("Name the host, then agree the fix with its owner" if (one and unknown_hosts) else ("Assign an owner; fix or retire" if one else "Assign an owner to each; fix or retire"))
                           if nfail else "Agree a timeout and a latency target"})
    return {"findings": [out], "healthy": {}}


def _below_bar_note(F, T, below, sentence=False):
    parts = [f"{code(k)} {pct(d['err'])} ({n(d['failed'])} of {n(d['calls'])} calls" + (f"; callers {join_and(code(c['service']) for c in d['callers'][:2])}" if d["callers"] else "") + ")" for k, d in below[:3]]
    text = f"Also failing, below the {thr_pct(T['dep_fail_pct'])} dependency bar: " + join_and(parts)
    return text + "." if sentence else {"severity": "amber", "finding": f"**Dependencies failing below the {thr_pct(T['dep_fail_pct'])} bar** — " + join_and(parts), "action": "Check the error budget with each provider"}


# ---------------------------------------------------------------- 5. non-production references from production
@detector("nonprod_from_prod", 50)
def det_nonprod(F, cfg, prev):
    scope = F["coverage"].get("env_scope", {})
    if scope.get("export_is_nonprod"):
        return {"findings": [], "healthy": {}}            # a UAT export calling UAT hosts is not production reaching UAT
    nonprod_services = set(scope.get("nonprod_services", []))
    hits = []
    for name, d in F["dependencies"].items():
        if d["class"] != "nonprod" or d["calls"] <= 0:
            continue
        if d["callers"] and scope.get("column") and all(c["service"] in nonprod_services for c in d["callers"]):
            continue                                       # only non-production services call it
        if d["callers"] and scope.get("column") and any(c["service"] in nonprod_services for c in d["callers"]):
            d = dict(d, callers=[c for c in d["callers"] if c["service"] not in nonprod_services])   # span figures are narrowed in aggregate
        hits.append((name, d))
    if not hits:
        return {"findings": [], "healthy": {}}
    hits.sort(key=lambda t: (-t[1]["calls"], t[0]))
    mixed = len({d["tier"] for _, d in hits}) > 1
    table_rows = [[f"{code(name)} — a **{d['marker']}** host", n(d["calls"]) + (f" ({window_phrase(F, d['tier']).replace('in the ', '')})" if mixed else ""), sec(d["p95_s"]),
                   ", ".join(code(c["service"]) for c in d["callers"][:3]) or "see the dependency page"] for name, d in hits]
    worst = hits[0]
    title = f"Production calls a {worst[1]['marker']} endpoint" if len(hits) == 1 else "Production reaches non-production systems"
    lead = ("A production service that calls a test or UAT host is a configuration error waiting to become an incident: the host can be rebuilt, "
            "reset or switched off without notice, and its data is not production data.")
    callers = join_and(code(c["service"]) for c in worst[1]["callers"][:3])
    lead += f" {code(worst[0])} was called {n(worst[1]['calls'])} times" + (f" from {callers}" if callers else "") + f" {window_phrase(F, worst[1]['tier'])}" + (f" (p95 {sec(worst[1]['p95_s'])})" if worst[1]["p95_s"] else "") + "."
    actions = [{"text": f"Remove or repoint the {code(h[0])} reference in production", "horizon": "now"} for h in hits[:3]]
    parts = [f"{code(h[0])} ({n(h[1]['calls'])} calls {window_phrase(F, h[1]['tier'])})" for h in hits[:3]]
    out = finding("nonprod_from_prod", "estate", "red", sum(d["calls"] for _, d in hits) * 10 + 1, F, title=title, lead=lead, bullets=[], diagram="dependencies",
                  table={"columns": ["Reference", "Calls", "p95", "Called from"], "rows": table_rows}, actions=actions, audience="Everyone who depends on the production estate",
                  tier="customer", domain=F["services"].get(hits[0][1]["callers"][0]["service"], {}).get("domain", "") if hits[0][1]["callers"] else "",
                  live_graph=[("note", recipes.dependency_note(hits[0][0]), "")], subject={"service": None, "key": "estate", "hosts": [h[0] for h in hits]},
                  metrics={"primary": {"label": f"calls to {worst[0]}", "value": worst[1]["calls"], "unit": "count", "higher_is_worse": True, "subject": worst[0], "tier": worst[1]["tier"]},
                           "subject_host": worst[0]},
                  summary={"finding": f"**{title}** — " + join_and(parts), "who": "Everyone", "action": "Remove the non-production references"})
    return {"findings": [out], "healthy": {}}


# ---------------------------------------------------------------- 6. noise masks the signal
@detector("noise_masks_signal", 60, needs=("failure_signal",))
def det_noise(F, cfg, prev):
    T = cfg["thresholds"]
    out = []
    for svc, s in F["services"].items():
        if s["role"] != "entry":
            continue
        noise_failed = s["failed"]["all"] - s["failed"]["business"]
        min_failed = per_window(F, T["noise_min_failed"], s["requests"]["tier"])
        if not s["failed"]["all"] or s["failed"]["all"] < min_failed or noise_failed < min_failed:
            continue
        share = 100.0 * noise_failed / s["failed"]["all"]
        if share < T["noise_share_pct"]:
            continue
        s_, dom, tier, aud = svc_ctx(F, svc)
        rows = [r for r in F["resources"][svc] if r["class"] != "business" and r["failed"] > 0]
        rows.sort(key=lambda r: (-r["failed"], r["resource"]))
        table_rows = [[res_label(r), r["class"].replace("_", " "), n(r["calls"]), n(r["failed"]), pct(r["err"])] for r in rows[:6]]
        real = s["err"]["business"]
        by_design_all = [r for r in rows if r["class"] == "by_design"]
        listed = [r for r in by_design_all if (r.get("rule") or "").startswith("config:")]       # the team's own by-design rules
        by_design = [r for r in by_design_all if r not in listed and r["err"]["pct"] >= T["by_design_err_pct"]]
        by_design_some = [r for r in by_design_all if r not in by_design]
        why_of = {rx_.pattern: why for _, rx_, why in cfg["_rx"]["by_design"] if why and not str(why).startswith("_todo")}
        title = f"{subj(s, svc)} looks worse than it is: {pct(share, 0)} of its recorded errors are noise"
        lead = (f"{code(svc)} records {pct(s['err']['all'])} failures {window_phrase(F, s['requests']['tier'])}, but {n(noise_failed)} of its {n(s['failed']['all'])} failures come from "
                + join_and(f"{NOISE_ADJ.get(r['class'], r['class'].replace('_', ' '))} traffic" for r in {r["class"]: r for r in rows}.values()) + f". Without them the real failure rate is **{pct(real)}**.")
        bullets = []
        if by_design:
            bullets.append("Endpoints that fail on every call by design (" + join_and(code(r["short"]) for r in by_design[:3]) + ") hide real regressions in the service's error rate; confirm each one with the team and either fix it or exclude it from the monitors.")
        if by_design_some:
            whys = [why_of.get((r.get("rule") or "").split(":", 2)[-1]) for r in by_design_some[:3]]
            bullets.append("Failures the team listed as by design: " + join_and(f"{code(r['short'])}, {frac(r['err'])}" + (f" — {LEDGER.add(w)}" if w else "") for r, w in zip(by_design_some[:3], whys))
                           + ". Keep them out of the error monitors so a real regression stays visible.")
        if unwatched(F, s):
            actions = [{"text": f"When you add the first monitor on {code(svc)}, filter the noise resources out (resource_name) so a real regression is visible", "horizon": "now"}]
        else:
            actions = [{"text": f"Exclude the noise resources from {code(svc)}'s error monitors (filter on resource_name) so a real regression is visible", "horizon": "now"}]
        if by_design:
            actions.append({"text": f"Fix or remove {code(by_design[0]['short'])}, which fails on every call", "horizon": "next"})
        out.append(finding("noise_masks_signal", svc, "amber", noise_failed / 10.0, F, svc=svc, title=title, lead=lead, bullets=bullets, actions=actions,
                           table={"columns": ["Resource", "Class", "Calls", "Failed", "Failure rate"], "rows": table_rows}, live_graph=live(F, svc, rows[0]["resource"]) if rows else [],
                           metrics={"primary": {"label": f"noise share of {svc} failures", "value": round(share, 1), "unit": "pct", "higher_is_worse": True, "subject": svc}},
                           summary={"finding": f"**{subj(s, svc)} is healthier than its error rate says** — {pct(share, 0)} of its errors are {join_and(sorted({NOISE_NOUN.get(r['class'], r['class']) for r in rows}))}; the real failure rate is {pct(real)}",
                                    "who": aud, "action": "Exclude the noise from monitors" + ("; fix by-design failures" if by_design else "")}))
    return {"findings": out, "healthy": {}}


# ---------------------------------------------------------------- 7. node imbalance
@detector("node_imbalance", 70)
def det_node_imbalance(F, cfg, prev):
    T = cfg["thresholds"]
    out, healthy = [], {}
    for g, grp in F["node_groups"].items():
        members = grp["members"]
        if not grp.get("comparable", True) or len(members) < 2:
            continue
        low = grp["lowest"]
        s, dom, tier, aud = svc_ctx(F, low)
        wp = window_phrase(F, s["requests"]["tier"])
        kind = "business requests" if grp.get("basis") == "business" else "requests"
        if grp["mean_others"] < min_calls(F, cfg, s["requests"]["tier"]):
            healthy.setdefault(s["domain"], []).append(f"the {words(len(members))} {g} nodes have too little traffic to compare")
            continue
        if not grp["idle"] and grp["gap"]["pct"] < T["node_gap_pct"]:
            healthy.setdefault(s["domain"], []).append(f"the {words(len(members))} {g} nodes share traffic within {pct(grp['gap'], 0)}")
            continue
        others = [(k, v) for k, v in members.items() if k != low]
        others_err = max(grp["err_pct"][k] for k, _ in others)
        others_p95 = max((grp["p95_s"][k] or 0) for k, _ in others)
        err_worst = grp["err_pct"][low] > others_err + 0.5 and all(F["services"].get(k, {}).get("failure_known", True) for k in members)
        p95_worst = (grp["p95_s"][low] or 0) > 1.2 * others_p95 and grp.get("p95_kind") == "service"
        worst_ep = grp.get("p95_worst_s", {})
        p95_head = "p95" if grp.get("p95_kind") == "service" else "p95 († = worst busy endpoint)"

        def p95_cell(k):
            if grp["p95_s"][k] is not None:
                return sec(grp["p95_s"][k])
            return sec(worst_ep[k]) + " †" if worst_ep.get(k) is not None else "—"
        fs = F["coverage"]["failure_signal"]
        known_k = lambda k: fs and F["services"].get(k, {}).get("failure_known", True)
        table_rows = [[code(k), n(v), pct(grp["err_pct"][k]) if known_k(k) else "— (not recorded)", p95_cell(k)] for k, v in sorted(members.items(), key=lambda kv: (-kv[1], kv[0]))]
        avg = n(grp["mean_others"]) if float(grp["mean_others"]).is_integer() else x_text(grp["mean_others"], 1)
        against = f"against {'an average of ' if len(others) > 1 else ''}{avg} on the other {'node' if len(others) == 1 else words(len(others)) + ' nodes'} {wp}"
        if grp["idle"]:
            title = f"{g}: node {code(low)} is idle"
            lead = f"{code(low)} handled {n(grp['lowest_requests'])} {kind} {against} — it is outside the load balancer, or stopped."
            level = "red" if len(members) == 2 else "amber"
        else:
            title = f"{g}: node {code(low)} takes {pct(grp['gap'], 0)} less traffic than its peer{'s' if len(others) > 1 else ''}"
            lead = f"{code(low)} handled {n(grp['lowest_requests'])} {kind} {against}."
            level = "amber"
        bullets = []
        if err_worst or p95_worst:
            if len(others) == 1:
                what = join_and([f"fails more often ({pct(grp['err_pct'][low])} vs {pct(others_err)})" if err_worst else "", f"is slower ({sec(grp['p95_s'][low])} vs {sec(others_p95)} at p95)" if p95_worst else ""])
                bullets.append(f"The same node also {what}.")
            else:
                bullets.append(f"The same node also has the {join_and([x for x in ('highest failure rate' if err_worst else '', 'highest p95' if p95_worst else '') if x])} of the group.")
        sev = tiered(level, tier, strong=grp["idle"] and len(members) == 2)
        actions = [{"text": f"Check the load-balancer pool and weights for {code(low)}" + (" and confirm the node is healthy" if grp["idle"] else ""), "horizon": "now"}]
        op = recipes.operation(F["services"][low])
        prefix = low.rstrip("0123456789").rstrip("-_")
        out.append(finding("node_imbalance", g, sev, grp["share_of_total"]["pct"] * 100, F, svc=low, title=title, lead=lead, bullets=bullets, actions=actions,
                           table={"columns": ["Node", kind.capitalize(), "Failure rate", p95_head], "rows": table_rows},
                           live_graph=[("timeseries", recipes.node_balance(op, prefix), "requests per node" + ("; " + recipes.placeholder_note(low) if op == recipes.PLACEHOLDER else ""))],
                           subject={"service": low, "key": g, "group": g},
                           metrics={"primary": {"label": f"traffic gap of {low}", "value": grp["gap"]["pct"], "unit": "pct", "higher_is_worse": True, "subject": low,
                                                "num": grp["lowest_requests"], "den": grp["lowest_requests"] + sum(v for k_, v in grp["members"].items() if k_ != low)}},
                           summary={"finding": f"**{title}**", "who": aud, "action": "Check the load-balancer pool and weights"}))
    return {"findings": out, "healthy": healthy}


# ---------------------------------------------------------------- 8. call ratios (token churn)
def token_phrase(v):
    """'request a new token for almost every call (1.20 per business call)' or 'request about 2.92 new tokens per business call'."""
    if v < 1.5:
        return f"request a new token for almost every call ({ratio_text({'value': v})} per business call)"
    return f"request about {ratio_text({'value': v})} new tokens per business call"


@detector("call_ratio", 80)
def det_call_ratio(F, cfg, prev):
    T = cfg["thresholds"]
    out = []
    from .config import slug
    configured_svcs = {x for r in F["ratios"] if r.get("source") == "config" and r["numerator"].get("token") for x in r["numerator"].get("services", [])}
    for r in F["ratios"]:
        v = r["value"].get("value")
        if v is None or v < r["expect_max"]:
            continue
        if r.get("source") == "config":
            cands = [x for x in r["numerator"].get("services", []) if F["services"].get(x, {}).get("role") == "entry"]
            if not cands:
                continue
            svc, key = cands[0], f"{cands[0]}:{slug(r['label'])}"
        else:
            svc, key = r["numerator"]["service"], r["numerator"]["service"]
            if svc not in F["services"] or svc in configured_svcs:
                continue
        s, dom, tier, aud = svc_ctx(F, svc)
        if r.get("source") == "config":
            num, den = r["numerator"]["calls"], r["denominator"]["calls"]
            wp = window_phrase(F, r["tier"])
            title = f"{cap(r['label'])}: {ratio_text(r['value'])} per call, above the expected {x_text(r['expect_max'], 2)}"
            names = lambda side: (join_and(code(x) for x in side["resources"][:3]) + (f" and {n(len(side['resources']) - 3)} more" if len(side["resources"]) > 3 else "")) \
                if side.get("resources") else code(side["match"])
            lead = (f"{n(num)} calls to {names(r['numerator'])} for {n(den)} calls to {names(r['denominator'])} {wp} "
                    f"is **{ratio_text(r['value'])} per call**; the review expects at most {x_text(r['expect_max'], 2)}.")
            out.append(finding("call_ratio", key, tiered("amber", tier), num / 50.0, F, svc=svc, title=title, lead=lead, bullets=[],
                               actions=[{"text": f"Find out why {r['label']} runs at {ratio_text(r['value'])} per call and bring it under {x_text(r['expect_max'], 2)}", "horizon": "next"}],
                               table={"columns": ["Calls", "Count"], "rows": [[names(r["numerator"]), n(num)], [names(r["denominator"]), n(den)]]}, live_graph=live(F, svc),
                               metrics={"primary": {"label": f"{r['label']} on {svc}", "value": v, "unit": "ratio", "higher_is_worse": True, "subject": [svc, r["label"]]},
                                        "source": "config", "label": r["label"], "expect_max": r["expect_max"]},
                               summary={"finding": f"**{cap(r['label'])}** runs at {ratio_text(r['value'])} per call (expected at most {x_text(r['expect_max'], 2)})", "who": aud,
                                        "action": f"Bring {r['label']} under {x_text(r['expect_max'], 2)}"}))
            continue
        tok_p95 = r.get("token_p95_s")
        level = "red" if (v >= T["token_red_ratio"] and tok_p95 is not None and tok_p95 >= T["token_red_p95_s"]) else "amber"
        sev = tiered(level, tier, strong=v >= 2 * T["token_red_ratio"])
        num, den = r["numerator"]["calls"], r["denominator"]["calls"]
        tokens = join_and(code(x) for x in r.get("token_resources", [r["numerator"]["match"]]))
        wp = window_phrase(F, r["tier"])
        title = f"Tokens are not being reused on {subj(s, svc)}: {ratio_text(r['value'])} token requests per business call"
        lead = (f"{n(num)} calls to {tokens} for {n(den)} calls to {code(svc)}'s other business operations {wp} is **{ratio_text(r['value'])} per call**. Tokens are normally cached until they expire; "
                f"requesting them per call multiplies the load on the token endpoint" + (f", and each token request takes up to {sec(tok_p95)} (p95) before the business call can start" if tok_p95 else "") + ".")
        actions = [{"text": f"Cache the token in each client of {code(svc)} until shortly before it expires — that removes at least {n(max(num - den, 0))} calls {wp} "
                             f"(the excess over one token per business call) and up to nearly all {n(num)}", "horizon": "next"}]
        out.append(finding("call_ratio", key, sev, num / 50.0, F, svc=svc, title=title, lead=lead, bullets=[], actions=actions,
                           table={"columns": ["Calls", "Count"], "rows": [[tokens, n(num)], ["other business operations", n(den)]]}, live_graph=live(F, svc, r.get("token_resources", [""])[0] if r.get("token_resources") else None),
                           metrics={"primary": {"label": f"token requests per business call on {svc}", "value": v, "unit": "ratio", "higher_is_worse": True, "subject": svc}, "source": "auto"},
                           summary={"finding": f"**Clients of {code(svc)} {token_phrase(v)}**", "who": aud, "action": "Cache tokens until expiry"}))
    return {"findings": out, "healthy": {}}


# ---------------------------------------------------------------- 9. chatty requests (sample anatomy)
def _chatty_primary(svc, cpr, a, T):
    """The figure that raised a work-inside-a-request finding: the first per-request kind above its bar, else secret reads,
    else database statements (the repeated child is named in the bullets)."""
    for kind, key, label in (("db", "db_per_request", "database statements"), ("http", "http_per_request", "outbound HTTP calls"), ("cache", "cache_per_request", "cache operations")):
        v = (cpr.get(kind) or {}).get("value")
        if v is not None and v >= T[key]:
            return {"label": f"{label} per request on {svc}", "value": v, "unit": "ratio", "higher_is_worse": True, "subject": svc}
    sr = (a.get("secret_reads_per_request") or {}).get("value")
    if sr is not None and sr >= T["secret_reads_per_request"]:
        return {"label": f"secret-store reads per request on {svc}", "value": sr, "unit": "ratio", "higher_is_worse": True, "subject": svc}
    rc = next(iter(a.get("repeated_children") or []), None)
    what = (rc["resource"] + (f" to {rc['target']}" if rc.get("target") and rc["target"] not in rc["resource"].lower() else "")) if rc else ""
    if rc and rc.get("by_median"):
        return {"label": f"runs of {what} per request on {svc}", "value": rc["median"], "unit": "ratio", "higher_is_worse": True, "subject": [svc, rc["resource"]],
                "target": rc.get("target") or ""}
    if rc:
        share = rc["traces_at_or_above"]
        return {"label": f"share of requests on {svc} that run {what} {T['repeat_child_per_entry']:g} times or more", "value": share["pct"], "num": share["num"], "den": share["den"],
                "unit": "pct", "higher_is_worse": True, "subject": [svc, rc["resource"]], "target": rc.get("target") or ""}
    return {"label": f"database statements per request on {svc}", "value": (cpr.get("db") or {}).get("value") or 0, "unit": "ratio", "higher_is_worse": True, "subject": svc}


@detector("chatty_requests", 90, needs=("request_anatomy",))
def det_chatty(F, cfg, prev):
    T = cfg["thresholds"]
    out = []
    sp = F["window"]["phrase"]["sample"]
    for svc, a in F["anatomy"].items():
        examined = a.get("requests_with_tree", a["entries"])
        if examined < T["min_entry_spans_for_anatomy"] or not a["children_per_request"]:
            continue
        s, dom, tier, aud = svc_ctx(F, svc)
        cpr = a["children_per_request"]
        bullets, rows = [], []
        fan_out = 0
        for kind, thr, label in (("db", T["db_per_request"], "database statements"), ("http", T["http_per_request"], "outbound HTTP calls"), ("cache", T["cache_per_request"], "cache operations")):
            v = cpr.get(kind, {}).get("value")
            if v is not None:
                rows.append([label, x_text(v)])
                if v >= thr:
                    vt = x_text(v, 0) if (v >= 20 or float(v).is_integer()) else x_text(v, 1)
                    how = "well above" if v >= 1.5 * thr else ("above" if float(vt.replace(",", "")) > thr else "at")
                    fan = [x for x in a.get("http_targets") or [] if x["requests"] >= 0.5 * examined]
                    explained = sum(x["median"] * x["requests"] for x in fan) / max(examined, 1)
                    if kind == "http" and len(fan) >= 3 and all(x["median"] <= 1.2 for x in fan) and explained >= 0.8 * v:
                        fan_out = len(fan)
                        bullets.append(f"**~{vt} {label} per request** {sp} — {how} the {LEDGER.add(f'{thr:g}')} that a request normally needs, spread over {words(len(fan))} "
                                       "targets called once each: fan-out, not a loop; check that every call is needed on every request and cache what can be cached.")
                    else:
                        bullets.append(f"**~{vt} {label} per request** {sp} — {how} the {LEDGER.add(f'{thr:g}')} that a request normally needs; look for N+1 loops and missing batching.")
        sr = a.get("secret_reads_per_request", {}).get("value")
        if sr is not None and sr >= T["secret_reads_per_request"]:
            rows.append(["secret-store reads", x_text(sr)])
            bullets.append(f"**Secrets are read at runtime** — {n(a['secret_reads'])} reads of {plural(a['secret_distinct'], 'distinct secret')} {sp} ({x_text(sr)} per request). Load them once per deployment instead.")
        for b in a["bursts"][:2]:
            bullets.append(f"Calls to {code(b['target'])} arrive in bursts at the same seconds of every minute ({pct(b['concentration'], 0)} of {n(b['calls'])} calls on {words(len(b['seconds_of_minute']))} seconds-of-minute) — the signature of a scheduler bootstrapping and re-reading its configuration.")
        thr = T["repeat_child_per_entry"]
        for rc in a["repeated_children"][:2]:
            to = f" to {code(rc['target'])}" if rc.get("target") and rc["target"] not in (rc["resource"] or "").lower() else ""
            med = rc["median"]
            med_txt = x_text(med, 0 if float(med).is_integer() else 1)
            if rc.get("by_median"):
                bullets.append(f"{code(rc['resource'])}{to} on {code(rc['service'])} runs **{med_txt} time{'s' if med != 1 else ''} in a typical request**, and {LEDGER.add(f'{thr:g}')} times or more in {frac(rc['traces_at_or_above'])} requests — an N+1 pattern.")
            else:
                bullets.append(f"**{frac(rc['traces_at_or_above'])} requests run {code(rc['resource'])}{to} on {code(rc['service'])} {LEDGER.add(f'{thr:g}')} times or more** (up to {n(rc['max'])} in one request; the median request runs it {med_txt} time{'s' if med != 1 else ''}) — an N+1 pattern in those requests.")
        if a["cache_table_queries"]:
            bullets.append(f"The cache and locks live in the database: {n(a['cache_table_queries'])} cache-table statements {sp}. A Redis or Memcached store removes them from the database's path.")
        fs = a["framework_share"]
        if fs["den"] and fs["pct"] >= T["framework_share_pct"]:
            bullets.append(f"**{pct(fs, 0)} of {code(svc)}'s spans are framework events** ({n(fs['num'])} of {n(fs['den'])} rows {sp}) — ingestion cost and harder trace reading; drop them with a span-sampling rule.")
        if not bullets:
            continue
        diagram = f"inside:{svc}" if len(cpr) >= 2 else None
        title = f"{subj(s, svc)} does more work per request than it needs to"
        left_out = (f"; {n(a['entries'] - examined)} of its {n(a['entries'])} requests were left out because they share a trace with another service and the export has no Parent ID to split them"
                    if a.get("traces_skipped") and a["entries"] > examined else "")
        lead = f"The span sample shows what happens inside a request to {code(svc)} ({n(examined)} requests {sp}, with their child spans{left_out}):"
        scope_note = (f"Per-request figures cover {n(examined)} of its {n(a['entries'])} requests; the rest share a trace with another service and the export has no Parent ID to split them."
                      if left_out else "")
        fan_only = bool(fan_out) and len(bullets) == 1
        if fan_only:
            actions = [{"text": f"Check that each of the {words(fan_out)} downstream calls of {code(svc)} is needed on every request; call them in parallel and cache what can be cached", "horizon": "next"}]
        else:
            actions = [{"text": f"Review the request path of {code(svc)} for repeated calls (N+1 queries, per-call secret or config reads) and batch or cache them", "horizon": "next"}]
        out.append(finding("chatty_requests", svc, "amber", a["entries"] / 10.0, F, svc=svc, title=title, lead=lead, bullets=bullets, actions=actions, diagram=diagram,
                           table={"columns": ["Per request", "Count"], "rows": rows}, live_graph=live(F, svc, kind="p95"),
                           metrics={"primary": _chatty_primary(svc, cpr, a, T),
                                    "scope_note": scope_note},
                           summary={"finding": f"**{subj(s, svc)} is chatty** — " + _lc_first(bullets[0].replace("**", "").split(" — ")[0]), "who": aud, "action": "Trim and parallelise the fan-out" if fan_only else "Batch and cache repeated calls"}))
    return {"findings": out, "healthy": {}}


# ---------------------------------------------------------------- 10. scanner exposure
@detector("scanner_exposure", 100)
def det_scanner(F, cfg, prev):
    T = cfg["thresholds"]
    out = []
    sensitive = ("manager", "host-manager", "examples", "docs", "phpinfo", ".env", ".git", "actuator", "cgi-bin", "console", "jmx")
    for svc, rows in F["resources"].items():
        s, dom, tier, aud = svc_ctx(F, svc)
        if s["role"] != "entry":
            continue
        probes = [r for r in rows if r["class"] == "scanner"]
        if not probes:
            continue
        hits = sum(r["calls"] for r in probes)
        share = 100.0 * len(probes) / max(len(rows), 1)
        if share < T["scanner_share_pct"] and hits < per_window(F, T["scanner_min_hits"], s["requests"]["tier"]):
            continue
        answered, unconfirmed, label = [], [], {}
        for r in probes:
            mix = r["status_mix"]
            known = sum(v for k, v in mix.items() if k != "none")
            if known:
                if mix.get("2xx", 0) >= 0.5 * known:
                    label[r["resource"]] = "**answered (2xx)**"
                    if any(k in r["resource"].lower() for k in sensitive):
                        answered.append(r)
                elif mix.get("4xx", 0) >= 0.5 * known:
                    label[r["resource"]] = "rejected (4xx)"
                else:
                    label[r["resource"]] = "errored (5xx)"
            elif r["err"]["pct"] >= 50:
                label[r["resource"]] = "failed (no status code in the export)"
            else:
                label[r["resource"]] = "did not fail (no status code in the export: 2xx or 4xx)"
                if any(k in r["resource"].lower() for k in sensitive):
                    unconfirmed.append(r)
        level = "red" if answered else "amber"
        sev = tiered(level, tier, strong=bool(answered))
        table_rows = [[code(r["short"]), n(r["calls"]), pct(r["err"]), label[r["resource"]]] for r in sorted(probes, key=lambda r: (-r["calls"], r["resource"]))[:8]]
        title = f"Vulnerability scans reach {subj(s, svc)}" + (" — and some paths answer" if answered else "")
        lead = (f"{n(len(probes))} of {code(svc)}'s {n(len(rows))} endpoints in Datadog {'is a scanner probe' if len(probes) == 1 else 'are scanner probes'} ({n(hits)} requests {window_phrase(F, s['requests']['tier'])}): "
                + join_and(code(r["short"]) for r in probes[:4]) + ("…" if len(probes) > 4 else "") + ".")
        bullets = []
        if answered:
            bullets.append("**Sensitive paths answered**: " + join_and(code(r["short"]) for r in answered[:4]) + " returned 2xx, so a default application or diagnostic page is reachable in production.")
        if unconfirmed:
            one = len(unconfirmed) == 1
            bullets.append(join_and(code(r["short"]) for r in unconfirmed[:4]) + f" did not fail, and the export has no status code to say whether {'it' if one else 'they'} answered or {'was' if one else 'were'} refused — check {'it' if one else 'them'} by hand.")
        if not answered and not unconfirmed:
            ok2xx = [r for r in probes if label[r["resource"]] == "**answered (2xx)**"]
            noise_where = "in the endpoint catalogue" + (" and in the error rate" if sum(r["failed"] for r in probes) else "")
            if ok2xx:
                bullets.append(f"{join_and(code(r['short']) for r in ok2xx[:3])} answered 2xx but {'is not a' if len(ok2xx) == 1 else 'are not'} sensitive path{'s' if len(ok2xx) != 1 else ''}; otherwise this is hygiene: noise {noise_where}.")
            else:
                bullets.append(f"No sensitive path answered, so this is hygiene: noise {noise_where}.")
        actions = []
        if answered:
            actions.append({"text": f"Undeploy the default and diagnostic applications that answered on {code(svc)}", "horizon": "now"})
        if unconfirmed:
            actions.append({"text": f"Request {join_and(code(r['short']) for r in unconfirmed[:4])} on {code(svc)} by hand to see whether {'it answers' if len(unconfirmed) == 1 else 'they answer'}", "horizon": "now"})
        actions.append({"text": "Drop scanner traffic at the edge (WAF or IP allow-list) and exclude it from APM error monitors", "horizon": "next"})
        out.append(finding("scanner_exposure", svc, sev, hits * (10 if answered else 1), F, svc=svc, title=title, lead=lead, bullets=bullets, actions=actions,
                           table={"columns": ["Probe", "Requests", "Failure rate", "Result"], "rows": table_rows}, live_graph=[],
                           metrics={"primary": {"label": f"scanner probe requests on {svc}", "value": hits, "unit": "count", "higher_is_worse": True, "subject": svc, "tier": s["requests"]["tier"]}},
                           summary={"finding": f"**Vulnerability scans reach {code(svc)}**" + (f"; {join_and(code(r['short']) for r in answered[:2])} answered" if answered else
                                                    (f" ({n(len(probes))} probe endpoint{'s' if len(probes) != 1 else ''}; {join_and(code(r['short']) for r in unconfirmed[:2])} may have answered — check by hand)" if unconfirmed else
                                                     (f" ({n(len(probes))} probe endpoint{'s' if len(probes) != 1 else ''}; {join_and(code(r['short']) for r in probes if label[r['resource']] == '**answered (2xx)**')} answered 2xx, not a sensitive path)"
                                                      if any(label[r["resource"]] == "**answered (2xx)**" for r in probes) else
                                                      f" ({n(len(probes))} probe endpoint{'s' if len(probes) != 1 else ''}, none answered)"))),
                                    "who": "Security", "action": "Remove default apps; block scanner traffic at the edge" if answered else ("Check the unconfirmed probes; block scanner traffic at the edge" if unconfirmed else "Block scanner traffic at the edge")}))
    return {"findings": out, "healthy": {}}


# ---------------------------------------------------------------- 11. egress and duplicate telemetry
@detector("egress_and_telemetry", 110, needs=("dependencies",))
def det_egress(F, cfg, prev):
    T = cfg["thresholds"]
    rx = cfg["_rx"]["allowed_egress"]
    public = [(name, d) for name, d in F["dependencies"].items() if d["class"] == "public" and d["calls"] > 0 and not any(p.search(name) for p in rx)]
    telem = [(name, d) for name, d in F["dependencies"].items() if d["class"] == "telemetry" and d["calls"] > 0]
    if not public and not telem:
        if F["coverage"].get("host_visibility"):
            return {"findings": [], "healthy": {"_estate": ["no production service calls the public internet outside the allowed list"]}}
        return {"findings": [], "healthy": {}}
    public.sort(key=lambda t: (-t[1]["calls"], t[0]))
    telem.sort(key=lambda t: (-t[1]["calls"], t[0]))
    is_big = lambda d: d["calls"] >= per_window(F, T["egress_red_calls"], d["tier"])
    red = any(is_big(d) or (d.get("failure_known", True) and d["err"]["pct"] >= 99.5) for _, d in public)
    tp = window_phrase(F, (public or telem)[0][1]["tier"])
    mixed = len({d["tier"] for _, d in public[:8] + telem[:4]}) > 1
    calls_cell = lambda d: n(d["calls"]) + (f" ({window_phrase(F, d['tier']).replace('in the ', '')})" if mixed else "")
    rows = []
    for name, d in public[:8]:
        fk = d.get("failure_known", True)
        note = "fails every call — an update check or a retired feed?" if fk and d["err"]["pct"] >= 99.5 else ("high volume — identify the owner and the contract" if is_big(d) else "")
        rows.append([code(name), calls_cell(d), pct(d["err"]) if fk else "not recorded", ", ".join(code(c["service"]) for c in d["callers"][:2]) or "—", note])
    for name, d in telem[:4]:
        rows.append([code(name), calls_cell(d), pct(d["err"]) if d.get("failure_known", True) else "not recorded", ", ".join(code(c["service"]) for c in d["callers"][:2]) or "—", "telemetry to another tool, alongside Datadog"])
    title = ("Servers call the public internet" + (" and send telemetry to a second tool" if telem else "")) if public else "Telemetry also goes to a second tool"
    has_callers = any(d["callers"] for _, d in public + telem)
    parts = []
    if public:
        parts.append(f"{words(len(public))} public host{'s' if len(public) != 1 else ''}")
    if telem:
        parts.append(f"{words(len(telem))} telemetry endpoint{'s' if len(telem) != 1 else ''}")
    notes_ = []
    if public:
        notes_.append(f"{'the public host' if len(public) == 1 else 'each public host'} needs a named owner and a place on the egress allow-list")
    if telem:
        notes_.append(f"{'the telemetry endpoint' if len(telem) == 1 else 'each telemetry endpoint'} sends data to a second tool, a cost paid twice")
    tail = cap("; ".join(notes_)) + "."
    when = "" if mixed else f"{cap(tp)}, "
    if has_callers:
        lead = f"{when}{'production' if when else 'Production'} services reached {join_and(parts)}. {tail}"
    else:
        one = len(public) + len(telem) == 1
        lead = (f"{when}{join_and(parts) if when else cap(join_and(parts))} {'appears' if one else 'appear'} in the export as {'a service of its own' if one else 'services of their own'}; "
                f"no calling service is in the data. {tail}")
    if mixed:
        lead += " Each count says its window."
    bullets = []
    big_ones = [t for t in public if is_big(t[1])]
    if big_ones:
        bullets.append(f"{join_and(code(t[0]) for t in big_ones[:3])} receive{'s' if len(big_ones) == 1 else ''} production traffic at scale — confirm the contract, the timeout and the fallback for each.")
    actions = []
    if public:
        actions.append({"text": "Restrict outbound internet access to the hosts on an allow-list", "horizon": "next"})
    if telem:
        actions.append({"text": f"Decide whether {join_and(code(t[0]) for t in telem[:2])} is still needed alongside Datadog, and retire it if not", "horizon": "later"})
    sev = "amber" if not red else tiered("red", "staff", strong=any(d.get("failure_known", True) and d["err"]["pct"] >= 99.5 for _, d in public))
    host_red = {name for name, d in public if is_big(d) or (d.get("failure_known", True) and d["err"]["pct"] >= 99.5)}
    host_severity = {name: (sev if name in host_red else ("amber" if sev == "red" else sev)) for name, _ in public + telem}
    out = finding("egress_and_telemetry", "estate", sev, sum(d["calls"] for _, d in public + telem) / 100.0, F, title=title, lead=lead, bullets=bullets, actions=actions, diagram="dependencies",
                  table={"columns": ["Destination", "Calls", "Failure rate", "Caller", "Notes"], "rows": rows}, audience="Security, cost", tier="staff",
                  live_graph=[("note", recipes.dependency_note((public or telem)[0][0]), "")],
                  subject={"service": None, "key": "estate", "hosts": [t[0] for t in public + telem], "host_severity": host_severity},
                  metrics={"primary": {"label": "public hosts called from production", "value": len(public), "unit": "count", "higher_is_worse": True, "subject": "estate"}},
                  summary={"finding": f"**{title}** — " + join_and(code(t[0]) for t in (public + telem)[:4]), "who": "Security, cost",
                           "action": join_and([a for a in ("Allow-list egress" if public else "", "retire duplicate telemetry" if telem else "") if a])})
    return {"findings": [out], "healthy": {}}


# ---------------------------------------------------------------- 12. client errors
@detector("client_errors", 120, needs=("status_codes",))
def det_client_errors(F, cfg, prev):
    T = cfg["thresholds"]
    out = []
    sp = F["window"]["phrase"]["sample"]
    for svc, rows in F["resources"].items():
        s, dom, tier, aud = svc_ctx(F, svc)
        if s["role"] != "entry":
            continue
        hits = []
        for r in rows:
            mix = r["status_mix"]
            known = sum(v for k, v in mix.items() if k != "none")
            if r["class"] != "business" or known < T["client_error_min_calls"] / 10:
                continue
            four = mix.get("4xx", 0)
            if cfg["failure"]["rule"] == "non_2xx" and r["tier"] == "sample":
                continue                          # these 4xx are failures under this review's rule and are reported as such
            if known and 100.0 * four / known >= T["client_error_share_pct"]:
                hits.append((r, four, known))
        if not hits:
            continue
        hits.sort(key=lambda t: (-t[1], t[0]["resource"]))
        r, four, known = hits[0]
        share = 100.0 * four / known
        level = "amber" if tier in ("customer", "clinical") else "info"
        title = f"{pct(share, 0)} of {code(r['short'])} responses on {subj(s, svc)} are client errors (4xx)"
        counted = ("This review's rule counts them as failures, but the weekly figures from the Endpoints list use Datadog's error count, which leaves 4xx out; "
                   "a 4xx storm is an auth, versioning or dead-link problem on the client side." if cfg["failure"]["rule"] == "non_2xx"
                   else "They are not counted as failures, but a 4xx storm is an auth, versioning or dead-link problem on the client side.")
        lead = f"{n(four)} of {n(known)} responses to {code(r['short'])} {sp} were 4xx. {counted}"
        table_rows = [[res_label(rr), n(kk), n(ff), pct(100.0 * ff / kk, 0)] for rr, ff, kk in hits[:6]]
        actions = [{"text": f"Break the 4xx responses on {code(r['short'])} down by status code and client (app version, API key) and fix the top cause", "horizon": "next"}]
        out.append(finding("client_errors", svc, level, four / 10.0, F, svc=svc, title=title, lead=lead, bullets=[], actions=actions,
                           table={"columns": ["Operation", "Responses (sample)", "4xx", "Share"], "rows": table_rows}, live_graph=live(F, svc, r["resource"]),
                           metrics={"primary": {"label": f"4xx share on {r['short']}", "value": round(share, 1), "unit": "pct", "higher_is_worse": True, "subject": [svc, r["resource"]], "num": four, "den": known}},
                           summary={"finding": f"**{pct(share, 0)} of {code(r['short'])} responses are 4xx** on {code(svc)}", "who": aud, "action": "Fix the client-side cause"}))
    return {"findings": out, "healthy": {}}


# ---------------------------------------------------------------- 13. observability foundations
def foundations_checkable(F):
    """The observability gaps these exports can check at all (a gap that cannot be checked is never 'closed')."""
    inputs = F["coverage"]["inputs"]
    return sorted(c for c, ok in (("monitors", bool(F["coverage"].get("monitors_known"))), ("slos", inputs["services"]), ("integration", inputs["services"]),
                                  ("env", inputs["services"] or F["coverage"]["columns"].get("env")), ("catalog", bool(F.get("catalog"))),
                                  ("framework", True), ("scanner", inputs["endpoints"] or any(r["class"] == "scanner" for rows in F["resources"].values() for r in rows))) if ok)


HYGIENE = {"Scanner traffic in APM": "exclude scanner traffic", "Framework events traced as spans": "drop framework event spans",
           "Integration libraries appear as services": "name the integration services"}


@detector("foundations", 130)
def det_foundations(F, cfg, prev):
    T = cfg["thresholds"]
    rows = []
    entries = {k: v for k, v in F["services"].items() if v["role"] == "entry"}
    vt = F["window"]["volumes_tier"]
    total = sum(v["requests"]["all"] for v in entries.values() if v["requests"].get("tier") == vt) or 1
    have_monitor_info = bool(F["coverage"].get("monitors_known"))
    unmon = []
    known = {k: v for k, v in entries.items() if v.get("monitors_known", have_monitor_info)}
    if have_monitor_info and known:
        unmon = [k for k, v in sorted(known.items(), key=lambda kv: (-kv[1]["requests"]["all"], kv[0])) if v["monitors"] == 0]
        if unmon:
            watched = len(known) - len(unmon)
            bigs = [k for k in unmon if entries[k]["requests"].get("tier") != vt or 100.0 * entries[k]["requests"]["all"] / total >= T["monitors_min_share_pct"]]
            names = bigs or unmon
            ev = "none on " + join_and(code(k) for k in names[:6]) + (f" +{len(names) - 6} more" if len(names) > 6 else "")
            if bigs and len(unmon) > len(bigs):
                ev += f"; also {plural(len(unmon) - len(bigs), 'smaller service')} under {thr_pct(T['monitors_min_share_pct'])} of traffic"
            if len(known) < len(entries):
                ev += f"; coverage unknown for {plural(len(entries) - len(known), 'service')} not in the Services or Endpoints export"
            rows.append([f"Monitors on {n(watched)} of {n(len(known))} services", ev, "a service can fail silently"])
    if F["coverage"]["inputs"]["services"]:
        if sum(1 for v in F["services"].values() if v.get("slos")) == 0:
            rows.append(["No SLOs", "the SLOs column is empty for every service", "no shared definition of \"good enough\""])
        no_env = [k for k, v in entries.items() if not v.get("envs")]
        if entries and len(no_env) >= 0.5 * len(entries):
            rows.append([f"No env tag on {n(len(no_env))} of {n(len(entries))} services", "the Envs column is empty", "cannot filter by environment or see a slowdown arrive with a deployment"])
        integ = [k for k, v in F["services"].items() if v["role"] == "integration" and v.get("requests_services_list")]
        if integ:
            rows.append(["Integration libraries appear as services", join_and(code(k) for k in integ[:5]), "dependencies are hard to attribute to their caller — set the service name on the integration"])
    elif F["coverage"]["columns"].get("env"):
        no_env = [k for k, v in entries.items() if not v.get("envs")]
        if entries and len(no_env) >= 0.5 * len(entries):
            rows.append([f"No env tag on {n(len(no_env))} of {n(len(entries))} services", "the Env column is empty in the span sample", "cannot filter by environment"])
    cat = F.get("catalog")
    if cat:
        low = [tag for tag, r in cat["tag_coverage"].items() if r["pct"] < T["catalog_tag_pct"]]
        if low:
            rows.append(["Cloud resource tagging", join_and(f"{code(tag)} on {pct(cat['tag_coverage'][tag])} of {n(cat['rows'])} resources" for tag in low), "infrastructure cannot be tied to services or costs"])
    fw = [(k, v) for k, v in entries.items() if v["spans"]["rows"] and 100.0 * v["spans"]["framework_rows"] / v["spans"]["rows"] >= T["framework_share_pct"]]
    if fw:
        k, v = max(fw, key=lambda kv: kv[1]["spans"]["framework_rows"])
        rows.append(["Framework events traced as spans", f"{pct(100.0 * v['spans']['framework_rows'] / v['spans']['rows'], 0)} of {code(k)}'s spans in the sample", "ingestion cost, harder trace reading"])
    sc = [(k, v) for k, v in entries.items() if v["noise"].get("scanner") and 100.0 * v["noise"]["scanner"]["resources"] / max(v["endpoints"], 1) >= T["scanner_share_pct"]]
    if sc:
        k, v = max(sc, key=lambda kv: kv[1]["noise"]["scanner"]["resources"])
        rows.append(["Scanner traffic in APM", f"{n(v['noise']['scanner']['resources'])} of {n(v['endpoints'])} endpoints on {code(k)} are probes", "noise in the endpoint catalogue and error rates"])
    cats = {"Monitors": "monitors", "No SLOs": "slos", "No env tag": "env", "Integration libraries": "integration", "Cloud resource": "catalog",
            "Framework events": "framework", "Scanner traffic": "scanner"}
    gaps = sorted({c for r in rows for k, c in cats.items() if r[0].startswith(k)})
    checkable = foundations_checkable(F)
    placeholders = [k for k, v in entries.items() if v["operation_source"] == "placeholder"]
    if placeholders:
        F["meta"].setdefault("analyst_notes", []).append("live-graph recipes still need the operation name for " + ", ".join(placeholders[:5]) + " — add the Operation Name column to the spans export or set services.operations")
    if not rows:
        if have_monitor_info:
            env_n = sum(1 for v in entries.values() if v.get("envs"))
            slo_n = sum(1 for v in entries.values() if v.get("slos"))
            full = [x for x, k, c in (("env tags", "env", env_n), ("SLOs", "slos", slo_n)) if k in checkable and c == len(entries)]
            partial = [f"{x} on {c} of {len(entries)} services" for x, k, c in (("env tags", "env", env_n), ("SLOs", "slos", slo_n)) if k in checkable and c < len(entries)]
            parts = ["monitors"] + full
            unknown_n = len(entries) - len(known)
            scope = "the services Datadog sees" if not unknown_n else f"the {plural(len(known), 'service')} the exports give monitor counts for"
            line = f"{join_and(parts)} {'are' if len(parts) > 1 or parts[0].endswith('s') else 'is'} in place on {scope}"
            if unknown_n:
                line += f"; monitor coverage is unknown for {plural(unknown_n, 'other service')}"
            if partial:
                line += "; " + "; ".join(LEDGER.add(x) for x in partial)
            unchecked = [x for x, k in (("SLOs", "slos"), ("env tags", "env")) if k not in checkable]
            if unchecked:
                line += f"; {join_and(unchecked)} {'were' if len(unchecked) > 1 or unchecked[0].endswith('s') else 'was'} not checked (export the Services list)"
            return {"findings": [], "healthy": {"_estate": [line]}}
        return {"findings": [], "healthy": {}}
    sev = "amber" if len(rows) >= 2 or any(r[0].startswith("Monitors") for r in rows) else "info"
    title = "Observability foundations are thin" if len(rows) >= 2 else "One observability gap"
    lead = (f"{cap(words(len(rows)))} gaps in how the estate is instrumented and tagged. Not one of them hurts a user today; each makes the next incident longer." if len(rows) > 1
            else "One gap in how the estate is instrumented and tagged. It does not hurt a user today; it makes the next incident longer.")
    actions, parts = [], []
    if any(r[0].startswith("Monitors") for r in rows):
        actions.append({"text": "Add failure-rate and latency monitors to the unwatched services, starting with the busiest", "horizon": "now", "covers": unmon})
        parts.append("monitors on every service")
    if any(r[0].startswith("No env tag") or r[0] == "Cloud resource tagging" for r in rows):
        actions.append({"text": "Unified service tagging (DD_ENV, DD_SERVICE, DD_VERSION) and deployment tracking on every service", "horizon": "next"})
        parts.append("tagging")
    if any("SLO" in r[0] for r in rows):
        actions.append({"text": "Agree SLOs for the customer-facing services and review them monthly", "horizon": "later"})
        parts.append("SLOs")
    clauses = [HYGIENE[r[0]] for r in rows if r[0] in HYGIENE]
    if clauses:
        actions.append({"text": "Ingestion hygiene: " + join_and(clauses), "horizon": "next"})
        parts.append("ingestion hygiene")
    out = finding("foundations", "estate", sev, len(rows) * 100.0, F, title=title, lead=lead, bullets=[], actions=actions, audience="Everyone who operates the estate", tier="staff",
                  table={"columns": ["Gap", "Evidence", "Why it matters"], "rows": rows}, live_graph=[], subject={"service": None, "key": "estate"},
                  metrics={"primary": {"label": "foundation gaps", "value": len(rows), "unit": "count", "higher_is_worse": True, "subject": "estate", "tier": "estate"},
                           "gaps": gaps, "checkable": checkable},
                  summary={"finding": f"**{title}** — " + "; ".join([rows[0][0]] + [_lc_first(r[0]) for r in rows[1:4]]), "who": "Everyone who operates the estate",
                           "action": (actions[0]["text"] if len(rows) == 1 and actions else (cap(join_and(parts)) if parts else "Close the gaps listed"))})
    return {"findings": [out], "healthy": {}}


# ---------------------------------------------------------------- 14. regressions since last month (nobody else owns them)
@detector("traffic_shift", 140, needs=("previous",))
def det_traffic_shift(F, cfg, prev, owned=()):
    mom = F.get("mom") or {}
    out = []
    regs = [r for r in mom.get("regressions", []) if r["service"] not in owned]
    if not regs:
        return {"findings": [], "healthy": {}}
    regs.sort(key=lambda r: (-r["weight"], r["service"]))
    rows = [[code(r["service"]) + (f" {code(r['resource'])}" if r.get("resource") else ""), r["metric"], r["then_text"], r["now_text"], r["change_text"]] for r in regs[:8]]
    worst = regs[0]
    tier = F["services"].get(worst["service"], {}).get("tier", "staff")
    sev = tiered("red" if worst.get("crosses_red") else "amber", tier)
    title = f"Since {mom['previous']['month']}: {worst['metric']} on {code(worst['service'])} {worst['direction_word']} from {worst['then_text']} to {worst['now_text']}"
    lead = f"{cap(words(len(regs)))} metric{'s' if len(regs) > 1 else ''} moved beyond tolerance since last month without an existing finding owning {'them' if len(regs) > 1 else 'it'}. Correlate with the deployments in the period."
    out.append(finding("traffic_shift", worst["service"], sev, worst["weight"], F, svc=worst["service"], title=title, lead=lead, bullets=[], actions=[{"text": f"Correlate the change on {code(worst['service'])} with deployments and configuration changes in the period", "horizon": "now"}],
                       table={"columns": ["Service", "Metric", mom["previous"]["month"], F["meta"]["month"], "Change"], "rows": rows}, live_graph=live(F, worst["service"], worst.get("resource")),
                       metrics={"primary": {"label": f"{worst['metric']} of {worst['service']}", "value": worst["now"], "unit": worst["unit"], "higher_is_worse": True, "subject": worst["service"]}},
                       summary={"finding": f"**{title}**", "who": F["domains"].get(F["services"].get(worst["service"], {}).get("domain", ""), {}).get("audience", "Users"), "action": "Correlate with deployments"}))
    return {"findings": out, "healthy": {}}


# ---------------------------------------------------------------- merging and ranking
def _merge_node_groups(findings, F):
    """Nodes of one application (orders-api01/02/03) read as one finding, not three: keep the strongest and note the twins.
    Ids are keyed on the group whichever node fires, so month-over-month joins hold."""
    from .config import slug
    out, dropped = [], set()
    for det in PER_SERVICE:
        groups = defaultdict(list)
        for f in findings:
            svc = f["subject"].get("service")
            g = F["services"].get(svc, {}).get("node_group") if svc else None
            if f["detector"] == det and g:
                lab = f["metrics"].get("label") if f["metrics"].get("source") == "config" else None
                groups[(g, lab)].append(f)
                f["merged"] = [_regroup(m, F) for m in f.get("merged", [])]            # one metric on many nodes; two configured ratios stay two findings
                f["id"] = f"{det}:{slug(g)}" + (f":{slug(lab)}" if lab else "")
                f["subject"]["group"] = g
        for (g, lab), fs in groups.items():
            if len(fs) < 2:
                continue
            fs.sort(key=lambda f: (-{"red": 2, "amber": 1, "info": 0}[f["severity"]], -f["score"], f["subject"]["service"]))
            keep, rest = fs[0], fs[1:]
            twins = []
            for r in rest:
                prim = r["metrics"].get("primary", {})
                val = prim.get("value")
                unit = prim.get("unit")
                shown = pct(val) if unit == "pct" else (sec(val) if unit == "s" else (ratio_text({"value": val}) if unit == "ratio" else n(val)))
                twins.append(f"{code(r['subject']['service'])} ({shown})")
                keep["score"] += r["score"]
                dropped.add(id(r))
            keep["bullets"].append(f"The same picture on the other node{'s' if len(rest) > 1 else ''} of {g}: {join_and(twins)}.")
            keep["subject"]["nodes"] = [keep["subject"]["service"]] + [r["subject"]["service"] for r in rest]
            keep["summary"]["finding"] += f"; also on {join_and(code(r['subject']['service']) for r in rest)}"
    for f in findings:
        if id(f) not in dropped:
            out.append(f)
    return out


def _regroup(fid, F):
    """'chatty_requests:orders-api01' -> 'chatty_requests:orders-api' when the service belongs to a node group."""
    from .config import slug
    det, _, svc = fid.partition(":")
    g = F["services"].get(svc, {}).get("node_group")
    return f"{det}:{slug(g)}" if (g and det in PER_SERVICE) else fid


def _merge_into(host, f, extra_summary, extra_action):
    if f["metrics"].get("scope_note"):
        host["bullets"].append(f["metrics"]["scope_note"])
    host["bullets"].extend(b for b in f["bullets"] if b not in host["bullets"])
    if f["lead"] and f["detector"] == "call_ratio":
        host["bullets"].append(f["lead"])
    host["actions"].extend(a for a in f["actions"] if a not in host["actions"])
    host["diagram"] = host["diagram"] or f["diagram"]
    host["merged"] = host.get("merged", []) + [f["id"]]
    host["score"] += f["score"] / 2
    if extra_summary:
        host["summary"]["finding"] += extra_summary
    if extra_action and extra_action not in host["summary"]["action"]:
        host["summary"]["action"] += extra_action
    if {"red": 2, "amber": 1, "info": 0}[f["severity"]] > {"red": 2, "amber": 1, "info": 0}[host["severity"]]:
        host["severity"] = f["severity"]


def run_all(F, cfg, prev=None):
    """Run every detector; merge same-service findings the way a person would; rank; cap; build healthy rows."""
    T = cfg["thresholds"]
    findings, healthy, skipped, ran, extra_also = [], defaultdict(list), [], [], []
    caps = F["coverage"]["capabilities"]
    available = {"failure_signal": F["coverage"]["failure_signal"], "dependencies": caps["dependencies"] != "none",
                 "request_anatomy": bool(caps.get("request_anatomy")), "status_codes": caps["status_codes"], "previous": bool(F.get("mom"))}
    tree_no_children = caps.get("trace_tree_columns") and not caps.get("request_anatomy")
    unlock = {"failure_signal": "add Status Code or Error to the spans export, or export the Endpoints list",
              "dependencies": "add Parent ID or peer.service to the spans export, or export the inferred services list",
              "request_anatomy": ("the sample holds entry spans only — export spans of every type, with no span-type filter" if tree_no_children else "add Trace ID, Span ID and Parent ID to the spans export"),
              "status_codes": "add Status Code to the spans export",
              "previous": ("no earlier month was compared in this build" if F["meta"].get("previous_reason") == "none" else
                           "no earlier month's review was found for this organisation; next month's review compares itself with this one")}
    covered_slow, owned, failing = set(), set(), set()
    for d in sorted(DETECTORS, key=lambda d: d["order"]):
        missing = [k for k in d["needs"] if not available.get(k)]
        if missing:
            skipped.append({"name": d["name"], "label": DISPLAY.get(d["name"], d["name"].replace("_", " ")), "reason": "; ".join(NEEDS_TEXT[m] for m in missing),
                            "unlock": "; ".join(unlock[m] for m in missing)})
            continue
        if d["name"] == "slow_operations":
            res = d["fn"](F, cfg, prev, covered=covered_slow)
        elif d["name"] == "traffic_shift":
            res = d["fn"](F, cfg, prev, owned=owned)
        elif d["name"] == "slow_hot_paths":
            res = d["fn"](F, cfg, prev, failing=frozenset(failing))
        else:
            res = d["fn"](F, cfg, prev)
        ran.append(d["name"])
        for f in res["findings"]:
            if f["detector"] == "failing_operations":
                svc = f["subject"]["service"]
                failing.add(svc)
                g = F["services"].get(svc, {}).get("node_group")
                if g:
                    failing.update(F["node_groups"][g].get("names", F["node_groups"][g]["members"]))
            if f["detector"] == "slow_hot_paths":
                svc = f["subject"]["service"]
                covered_slow.add(svc)
                g = F["services"].get(svc, {}).get("node_group")
                if g:
                    covered_slow.update(F["node_groups"][g].get("names", F["node_groups"][g]["members"]))
            if f["subject"].get("service"):
                owned.add(f["subject"]["service"])
        findings.extend(res["findings"])
        for dom, lines in res["healthy"].items():
            healthy[dom].extend(lines)
        extra_also.extend(res.get("also", []))
    # merge chatty_requests into slow_hot_paths, and call_ratio into the failing / slow finding of the same service
    by_svc = defaultdict(dict)
    for f in findings:
        if f["subject"].get("service"):
            by_svc[f["subject"]["service"]][f["detector"]] = f
    merged = []
    for f in findings:
        svc = f["subject"].get("service")
        if f["detector"] == "chatty_requests" and svc in by_svc and "slow_hot_paths" in by_svc[svc]:
            _merge_into(by_svc[svc]["slow_hot_paths"], f, "", "")
            continue
        if f["detector"] == "call_ratio" and svc in by_svc:
            host = by_svc[svc].get("failing_operations") or by_svc[svc].get("slow_hot_paths")
            if host:
                v = f["metrics"]["primary"]["value"]
                m = f["metrics"]
                if m.get("source") == "config":
                    _merge_into(host, f, f"; {m['label']} runs at {ratio_text({'value': v})} per call (expected at most {x_text(m['expect_max'], 2)})",
                                f"; bring {m['label']} under {x_text(m['expect_max'], 2)}")
                else:
                    _merge_into(host, f, f"; clients {token_phrase(v)}", "; cache tokens until expiry")
                continue
        merged.append(f)
    findings = _merge_node_groups(merged, F)
    # the foundations monitor action names only the services no per-finding monitor action already covers
    per_finding_monitor = {f["subject"].get("service") for f in findings if any("monitor on" in a["text"] and not a["text"].startswith("When you add") for a in f["actions"])}
    covered_only = set()
    for f in findings:
        if f["detector"] == "foundations":
            for a in list(f["actions"]):
                if a.get("covers") is not None:
                    left = [s for s in a["covers"] if s not in per_finding_monitor]
                    if not left:
                        f["actions"].remove(a)
                        if len(f["table"]["rows"]) == 1:
                            covered_only.add(id(f))          # its one gap is a monitor another finding already adds
                            for g2 in findings:
                                if g2 is not f and g2["subject"].get("service") in a["covers"] and any("monitor on" in x["text"] for x in g2["actions"]):
                                    g2["merged"] = g2.get("merged", []) + [f["id"]]
                        elif not f["actions"]:
                            f["actions"].append({"text": f"Add the monitors that the findings on {join_and(code(s) for s in a['covers'][:6])} call for", "horizon": "now"})
                    else:
                        a["text"] = "Add failure-rate and latency monitors to " + join_and(code(s) for s in left[:6]) + (f" and {len(left) - 6} more" if len(left) > 6 else "")
                    a.pop("covers", None)
    findings = [f for f in findings if id(f) not in covered_only]
    order = {"red": 0, "amber": 1, "info": 2}
    findings.sort(key=lambda f: (order[f["severity"]], -f["score"], f["id"]))
    body = [f for f in findings if f["severity"] in ("red", "amber")][: int(cfg["report"]["max_findings"])]
    also = [f for f in findings if f not in body]
    for i, f in enumerate(body, 1):
        f["number"] = i
    F["coverage"]["detectors_run"] = ran
    F["coverage"]["detectors_skipped"] = skipped
    fs = F["coverage"]["failure_signal"]
    healthy_rows = []
    with_finding = {f["subject"].get("service") for f in body + also} | {f["metrics"].get("worst_service") for f in body + also} \
        | {x for f in body + also for x in f["subject"].get("nodes", [])}
    for dkey, d in F["domains"].items():
        if d.get("empty"):
            continue
        apart = [x for x in d.get("services_list_only", []) + d.get("sample_only_services", []) if x in F["services"] and x not in with_finding]
        notes, triggers = [], []
        for x in apart[:3]:
            sx = F["services"][x]
            tier_x = sx["requests"].get("tier")
            known_x = sx.get("failure_known", True) and (fs or tier_x == "services_list")
            enough = sx["requests"]["business"] >= min_calls(F, cfg, tier_x)
            failing_x = known_x and enough and bool(sx["err"]["business"]["den"]) and sx["err"]["business"]["pct"] >= T["fail_amber_pct"]
            slow_x = enough and max(sx["p95_s"] or 0, sx["p95_worst_s"] or 0) >= T["slow_p95_s"]
            p95_x = sx["p95_s"] if sx["p95_s"] is not None else sx["p95_worst_s"]
            esl = sx.get("err_services_list")
            if not known_x and esl and esl.get("den"):
                fail_txt = f"{n(sx['requests']['business'])} requests, failures not recorded in the sample; the Services list gives {frac(esl)} failed"
                failing_x = esl["den"] >= min_calls(F, cfg, "services_list") and esl["pct"] >= T["fail_amber_pct"]
            elif not known_x:
                fail_txt = f"{n(sx['requests']['business'])} requests, failures not recorded"
            else:
                fail_txt = f"{frac(sx['err']['business'])} failed" if sx["err"]["business"]["den"] else "no requests"
            notes.append(f"{code(x)} (counted apart, {window_phrase(F, tier_x)}): " + fail_txt + (f", p95 {sec(p95_x)}" if p95_x is not None else ""))
            if failing_x or slow_x:
                triggers.append((x, "fail" if failing_x else "slow", tier_x))

        def apart_action():
            failing_n = list(list_failing) + [x for x, k, _ in triggers if k == "fail" and x not in list_failing]
            slow_n = [x for x, k, _ in triggers if k == "slow"]
            parts = []
            if failing_n:
                parts.append(f"Find out why {join_and(code(x) for x in failing_n)} {'fails' if len(failing_n) == 1 else 'fail'}")
            if slow_n:
                parts.append(("review" if parts else "Review") + f" the p95 of {join_and(code(x) for x in slow_n)}")
            listed_ = [x for x, _, tr in triggers if tr == "services_list"]
            its = "its" if len(listed_) == 1 else "their"
            probes_ = all(F["services"][x].get("list_reason") in ("probe_listed", "probes_sampled") for x in listed_)
            if listed_ and all(F["services"][x].get("list_reason") == "markerless" for x in listed_):
                how = f"; add Type or Operation Name to the spans export so {its} requests can be named"
            elif listed_ and probes_:
                who_ = (f"{code(listed_[0])} runs" if len(listed_) == 1 and len(failing_n) + len(slow_n) > 1 else ("it runs" if len(listed_) == 1 else "they run"))
                how = f"; export spans of every type at an hour {who_} to name {its} operations"
            elif listed_:
                how = f"; export {its} spans or the Endpoints list to name the operations"
            else:
                how = ""
            # (a member judged on its Services-list rate ('list_rate') is in the sample already: no export hint for it)
            return "; ".join(parts) + how
        list_judged = {x: F["services"][x]["err_services_list"] for x in d["counted_services"]
                       if not F["services"][x].get("failure_known", True) and (F["services"][x].get("err_services_list") or {}).get("den")}
        list_failing = [x for x, esl in list_judged.items() if esl["den"] >= min_calls(F, cfg, "services_list") and esl["pct"] >= T["fail_amber_pct"]]
        list_txt = lambda xs: "; ".join(f"{code(x)} {frac(list_judged[x])} failed {window_phrase(F, 'services_list')}" for x in xs[:3])
        if any(f["domain"] == dkey and f["severity"] in ("red", "amber") and (f["subject"] or {}).get("key") != "estate" for f in body + also):
            failure_subjects = {f["subject"].get("service") for f in body + also if f["detector"] in ("failing_operations", "noise_masks_signal")}
            for x in [x for x in list_failing if x not in failure_subjects]:
                notes.append(f"{code(x)}: its requests in the sample carry no failure signal; {frac(list_judged[x])} failed {window_phrase(F, 'services_list')}")
                triggers.append((x, "fail", "list_rate"))
            if triggers:
                healthy_rows.append({"status": "review_apart", "sentence": f"{d['label']} — counted apart from the figures above: " + "; ".join(notes), "action": apart_action(),
                                     "domain": dkey, "label": d["label"], "icon": d.get("icon", ""), "audience": d.get("audience", "Users")})
            continue
        lines = healthy.get(dkey, [])
        biz = d["requests"]["business"]
        dt = d.get("volume_tier", F["window"]["volumes_tier"])
        mc = min_calls(F, cfg, dt)
        wp = window_phrase(F, dt)
        p95_label = "p95" if d.get("p95_kind") == "service" else "p95 (worst busy endpoint)"
        e = d["err"]["business"]
        dom_known = fs and d.get("failure_known", True)
        not_rec = d.get("failure_not_recorded") or []
        not_rec_plain = [x for x in not_rec if x not in list_judged]
        not_rec_txt = ((f"; failures are not recorded for {join_and(code(x) for x in not_rec_plain[:3])}" + (f" and {len(not_rec_plain) - 3} more" if len(not_rec_plain) > 3 else "") if not_rec_plain else "")
                       + (f"; the requests of {join_and(code(x) for x in [x for x in not_rec if x in list_judged][:3])} in the sample carry no failure signal, so the Services list is used: "
                          + list_txt([x for x in not_rec if x in list_judged]) if any(x in list_judged for x in not_rec) else ""))
        failed_txt_dom = (f"{n(e['num'])} of {n(e['den'])} requests with a failure signal ({pct(e)}) failed" if (not_rec or d.get("failure_base_partial")) else f"{pct(e)} failed")
        small_material = sorted([r for svc in d["counted_services"] for r in F["resources"].get(svc, []) if r["class"] == "business" and r["calls"] < mc
                                 and r.get("failure_known", True) and r["err"]["pct"] >= T["fail_amber_pct"] and r["failed"] >= 5], key=lambda r: (-r["failed"], r["resource"])) if dom_known else []
        if biz == 0:
            row = {"status": "quiet", "sentence": f"{d['label']} — no business traffic seen {wp}"}
        elif not dom_known and list_judged:
            any_failed = any(list_judged[x]["num"] for x in list_judged)
            row = {"status": "review" if list_failing else "healthy",
                   "sentence": f"{d['label']} — {big(biz)} business requests {wp}, {p95_label} {sec_range(d['p95_range_s'])}; its requests in the sample carry no failure signal, "
                               + ("so the Services list is used: " if any_failed else "and the Services list records no failures: ") + list_txt(list(list_judged))}
            if list_failing:
                row["action"] = f"Find out why {join_and(code(x) for x in list_failing[:3])} {'fails' if len(list_failing) == 1 else 'fail'}"
        elif biz < mc:
            row = {"status": "quiet", "sentence": f"{d['label']} — too little traffic to judge: {n(biz)} business requests {wp}" + (f", {failed_txt_dom}" if dom_known else "") + not_rec_txt}
        elif dt == "services_list":
            failed_txt = f", {frac(e)} failed" if e["den"] else ""
            reasons_ = [F["services"][x].get("list_reason", "absent") for x in d["counted_services"]]
            in_sample = sum(F["services"][x].get("unmarked_spans") or F["services"][x].get("spans_without_entries", 0) for x in d["counted_services"] if F["services"][x].get("list_reason") == "markerless")
            probes_only = bool(reasons_) and all(x_ in ("probe_listed", "probes_sampled") for x_ in reasons_)
            if reasons_ and all(x_ == "markerless" for x_ in reasons_):
                why = f"its {n(in_sample)} spans in the sample carry no request marker, so no operation can be named"
            elif reasons_ and all(x_ == "probes_sampled" for x_ in reasons_):
                why = "the span sample shows only its health probes, so no operation can be named"
            elif reasons_ and all(x_ == "probe_listed" for x_ in reasons_):
                why = "the span sample does not show it and the Endpoints list holds only its health checks, so no operation can be named"
            else:
                why = "the span sample" + (" and the Endpoints list do" if F["window"]["volumes_tier"] == "aggregate" else " does") + " not show it, so no operation can be named"
            in_sample = in_sample if reasons_ and all(x_ == "markerless" for x_ in reasons_) else 0
            row = {"status": "review_list" if e["pct"] >= T["fail_amber_pct"] else "healthy",
                   "sentence": f"{d['label']} — {big(biz)} requests {wp}{failed_txt}, p95 {sec_range(d['p95_range_s'])}; {why}"}
            if in_sample and row["status"] == "review_list":
                row["action"] = "Add Type or Operation Name to the spans export so its requests can be named"
            elif probes_only and row["status"] == "review_list":
                row["action"] = "Export spans of every type at an hour it runs to name the failing operations"
        elif not dom_known:
            where = "in this export" if not fs else ("for its spans (no status code or error flag)" if cfg["failure"]["rule"] != "flag_only" else "for its spans (no error flag)")
            row = {"status": "unmeasured", "sentence": f"{d['label']} — {big(biz)} business requests {wp}, {p95_label} {sec_range(d['p95_range_s'])}; failures are not recorded {where}"}
            cols = F["coverage"]["columns"]
            if cfg["failure"]["rule"] == "flag_only" or cols.get("status"):
                row["action"] = "Add the Error column (or a Status column of ok/error) to the spans export to judge failures of these spans"
        elif e["pct"] >= T["fail_amber_pct"] or small_material:
            small = small_material
            base_txt = f"{n(e['num'])} of {n(e['den'])} requests with a failure signal ({pct(e)})" if (not_rec or d.get("failure_base_partial")) else f"{frac(e)} business requests"
            if small:
                mostly = sum(r["failed"] for r in small) >= 0.5 * max(e["num"], 1)
                lead_small = (f"{base_txt} failed, mostly on {plural(len(small), 'operation')} too small to flag on {'its' if len(small) == 1 else 'their'} own (each under {n(mc)} calls): " if mostly else
                              f"{base_txt} failed; {plural(len(small), 'operation')} too small to flag on {'its' if len(small) == 1 else 'their'} own (each under {n(mc)} calls) "
                              f"{'fails' if len(small) == 1 else 'fail'} above {thr_pct(T['fail_amber_pct'])}: ")
                row = {"status": "review", "sentence": f"{d['label']} — {lead_small}" + ", ".join(f"{code(r['short'])} {frac(r['err'])}" for r in small[:3]) + not_rec_txt}
            else:
                row = {"status": "review", "sentence": f"{d['label']} — {base_txt} failed; no single operation crosses the bar for a finding of its own" + not_rec_txt}
        else:
            row = {"status": "healthy", "sentence": f"{d['label']} — healthy: {big(biz)} business requests {wp}, {failed_txt_dom}, {p95_label} {sec_range(d['p95_range_s'])}{not_rec_txt}"}
            if lines and len(d["services"]) > 1:
                row["sentence"] += "; " + lines[0]
        if notes and row["status"] in ("healthy", "quiet", "review"):
            if triggers:
                row["status"] = "review_apart"
                row["action"] = apart_action()
                row["sentence"] = row["sentence"].replace(" — healthy:", " — the counted services are healthy:", 1)
            row["sentence"] += "; " + "; ".join(notes)
        if list_failing and row["status"] in ("healthy", "quiet", "unmeasured"):
            row["status"] = "review"
            row["action"] = f"Find out why {join_and(code(x) for x in list_failing[:3])} {'fails' if len(list_failing) == 1 else 'fail'}"
            row["sentence"] = row["sentence"].replace(" — healthy:", " — the services the sample records are healthy:", 1)
        row.update({"domain": dkey, "label": d["label"], "icon": d.get("icon", ""), "audience": d.get("audience", "Users")})
        healthy_rows.append(row)
    return body, also, healthy_rows, healthy.get("_estate", []), extra_also
