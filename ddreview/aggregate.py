"""One pass over the exports into ``facts``: every figure the notebook, the diagrams and the prompts quote.

Evidence tiers
- aggregate ... the APM Endpoints list: complete counts over the exported range (HTTP endpoints only)
- services_list the APM Services list and inferred services: complete counts over the catalog's own (different) range
- sample ...... the Trace Explorer span export: capped (observed 100,000 rows ≈ minutes); counts are quoted "in the N-minute sample"
- catalog ..... the resource catalog: tag coverage only

The spans export is sufficient on its own; each extra export unlocks more (facts['coverage'] says what, and what is missing).
When an Endpoints list is present, a service it covers is counted from the list; operations the list does not carry are
left out of that service's totals and named in a caveat; a service the list does not cover at all (a queue worker, say)
keeps its span-sample figures, labelled as a sample and kept out of the weekly totals.
"""
import os
import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

from . import KIT_VERSION, SCHEMA_VERSION
from . import readers
from .classify import (_SQL, CLIENT_TYPES, SERVER_TYPES, child_kind, consumes, host_class, is_auth_resource, is_entry, is_entry_candidate, is_hostlike, is_integration_name, is_secret_store, nonprod_marker,
                       is_bare_queue, queue_consumer_name, redact, server_evidence, registrable_domain, resource_class, short_resource)
from .config import ConfigError, humanise, matches_any, service_stem, slug
from .narrative import range_words, rule_words
from .stats import p95_interval_s, pctl, percentiles, r1, r3, rate, ratio

CANDIDATE_DAYS = (1, 2, 3, 5, 7, 14, 28, 30)
DURATION_CAP = 2_000_000      # durations kept per (service, resource); far above any export, so percentiles are never truncated
MERGE_SLACK = timedelta(minutes=2)   # span files further apart than this are separate samples
_VERB = re.compile(r"^(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\s+", re.I)


def match_key(resource, method=""):
    """Join key between the spans export ('/api/x' + Method 'POST') and the Endpoints list ('POST /api/x')."""
    r = re.sub(r"\s+", " ", (resource or "").strip())
    if method and not _VERB.match(r) and not r.upper().startswith(method.upper() + " ") and r.upper() != method.upper():
        r = f"{method.upper()} {r}"
    return r.lower()


def _config_text(cfg):
    """Strings the analyst wrote in the config and the notebook prints as given (validate never treats their digits as figures)."""
    out = [cfg["org"].get(k) for k in ("name", "prepared_for", "prepared_by")] + [cfg["report"].get(k) for k in ("delivered_alongside", "closing_note", "thesis")]
    for dom in cfg.get("domains", []):
        out += [dom.get("label"), dom.get("audience"), dom.get("description")]
    out += [op.get("label") or op.get("key") for op in cfg.get("operations", [])] + list(cfg["services"].get("display", {}).values())
    out += [r.get("label") for r in cfg.get("ratios", [])] + list(cfg["services"].get("node_groups", {}))
    out += [p.strip(" ,;:—–-") for p in re.split(r"\{[^}]*\}", cfg["report"].get("title_template") or "")]
    return sorted({s for s in out if isinstance(s, str) and any(ch.isdigit() for ch in s)})


_VERSION_SUFFIX = re.compile(r"(?i)(^|[-_.])v\d+$")


def _parent_id(text):
    """'0', '0000000000000000', 'null' or an empty cell all mean 'no parent' (Datadog stores 0 for a root span)."""
    v = readers.unquote(text)
    return "" if v.strip("0") == "" or v.lower() in ("null", "none", "nil", "-", "n/a") else v


def _dep_class(name, kind, cfg):
    """A dependency's host class; a database, cache or queue reached by a traced call is internal whatever its name looks like."""
    cls = host_class(name, cfg)
    return "internal" if cls in ("public", "cloud") and kind in ("db", "cache", "queue") else cls


def sample_length_text(minutes_text):
    """'9' -> '9 minutes'; '42 seconds' -> '42 seconds'; '' -> ''."""
    if not minutes_text:
        return ""
    return minutes_text if "second" in minutes_text else f"{minutes_text} minute{'s' if minutes_text != '1' else ''}"


class Inputs:
    """Loaded exports with canonical rows. Only ``spans`` is mandatory."""

    def __init__(self, inventory, cfg, warnings):
        self.cfg, self.warnings = cfg, warnings
        self.files = [f for f in inventory if f["kind"] != "unknown" and not f["duplicate"]]
        self.spans, self.span_caps, self.span_meta = [], {}, []
        self.endpoints, self.services, self.inferred, self.catalog = [], [], [], []
        self.assumed_utc = 0
        self.tz_offsets = defaultdict(int)
        self.seen_span_ids, self.seen_rows, self.seen_rows_noid = set(), Counter(), Counter()
        self.low_confidence_units = []
        rich = ("trace_id", "operation", "type", "kind", "peer", "status", "error", "method")
        rank = lambda f: (((0 if "span_id" in f["columns"] else 1) + (0 if "parent_id" in f["columns"] else 1)), -sum(1 for k in rich if k in f["columns"])) if f["kind"] == "spans" else (0, 0)
        for f in sorted(self.files, key=lambda f: (rank(f), self.files.index(f))):
            {"spans": self._load_spans, "endpoints": self._load_endpoints, "services": self._load_services,
             "inferred": self._load_inferred, "catalog": self._load_catalog}[f["kind"]](f)
        self._refuse_overlapping_lists()

    def _refuse_overlapping_lists(self):
        """Two Endpoints (or Services) lists in one folder that list the same row are two different ranges, not one export:
        never pick a winner silently. Lists of different services are merged; the same services from two environments are
        refused, because the review is production-scoped."""
        for label, rows, key in (("Endpoints list", self.endpoints, lambda r: (r["service"], match_key(r["resource"]))),
                                 ("Services list", self.services, lambda r: r["name"]), ("inferred services list", self.inferred, lambda r: r["name"])):
            seen = {}
            for r in rows:
                k = key(r)
                if k in seen and seen[k] != r["_file"]:
                    raise SystemExit(f"{seen[k]} and {r['_file']} both list {k if isinstance(k, str) else ' '.join(k)} in the {label} — they are two different exports "
                                     f"(another range, or another environment); keep only this month's production {label} in the folder")
                seen.setdefault(k, r["_file"])

    # ---------------------------------------------------------- spans
    def _load_spans(self, f):
        cols, cfg = f["columns"], self.cfg
        caps = {k: True for k in ("ts", "service", "resource", "duration", "method", "status", "error", "trace_id", "span_id", "parent_id",
                                  "operation", "env", "host", "type", "kind", "peer", "version") if k in cols}
        dc = bool(f.get("decimal_comma"))
        hdr_unit, unit_src = None, "header"
        if "duration" in cols:
            norm = f["normalised"][cols["duration"]]
            raw_hdr = f["headers"][cols["duration"]].strip().lower()
            units_cfg = cfg.get("_units", {})
            if raw_hdr in units_cfg or norm in units_cfg:
                hdr_unit, unit_src = units_cfg.get(raw_hdr) or units_cfg.get(norm), "config"
            else:
                hdr_unit = readers.unit_from_header(f["headers"][cols["duration"]])
        rows, bare = [], []
        raw_n = bad_ts = bad_dur = no_svc = dup_ids = 0
        ids_in_file, status_numeric, status_text = set(), 0, 0
        file_has_ids = "span_id" in cols
        date_format = cfg["columns"].get("date_format")
        for raw in readers.iter_rows(f):
            raw_n += 1
            g = lambda k: raw[cols[k]] if k in cols and cols[k] < len(raw) else ""
            ts, assumed = readers.parse_ts(g("ts"), date_format) if "ts" in cols else (None, False)
            if ts is None and "ts" in cols:
                bad_ts += 1
                continue
            svc = readers.unquote(g("service"))
            if not svc:
                no_svc += 1
                continue
            tzmark = None
            if "ts" in cols and not assumed:
                m = re.search(r"([+-]\d{2}:?\d{2}|Z)\s*$", readers.unquote(g("ts")))
                tzmark = m.group(1) if m else "?"
            dtext = readers.unquote(g("duration"))
            ms = readers.parse_duration_ms(dtext, hdr_unit or "ms", dc)
            if ms is None:
                bad_dur += 1
                continue
            is_bare = bool(readers._NUMBER.match(dtext))
            if is_bare:
                bare.append(float(readers.normalise_number_text(dtext, dc)))
            span_id = readers.unquote(g("span_id"))
            if span_id:
                if span_id in self.seen_span_ids:
                    dup_ids += 1
                    continue
                self.seen_span_ids.add(span_id)
                ids_in_file.add(span_id)
            status = readers.parse_status(g("status")) if "status" in cols else None
            error = readers.parse_flag(g("error")) if "error" in cols else None
            if "status_pending" in cols and cols["status_pending"] < len(raw):
                p = raw[cols["status_pending"]].strip()
                if p:
                    code = readers.parse_status(p)
                    if code is not None:
                        status_numeric += 1
                        if status is None:
                            status = code
                    else:
                        status_text += 1
                        if error is None:
                            error = readers.parse_flag(p)
            rows.append({"ts": ts, "service": svc, "resource": readers.unquote(g("resource")), "ms": ms, "bare": is_bare, "assumed_utc": assumed, "tz": tzmark,
                         "method": readers.unquote(g("method")).upper(), "status": status, "error": error, "trace_id": readers.unquote(g("trace_id")),
                         "span_id": span_id, "parent_id": _parent_id(g("parent_id")), "operation": readers.unquote(g("operation")),
                         "env": readers.unquote(g("env")), "host": readers.unquote(g("host")), "type": readers.unquote(g("type")), "kind": readers.unquote(g("kind")).lower(),
                         "peer": readers.unquote(g("peer")), "version": readers.unquote(g("version"))})
        unit, evidence, inferred, low = readers.decide_bare_unit(bare, hdr_unit, "ms", f["normalised"][cols["duration"]] if "duration" in cols else "", unit_src)
        if bare and unit != (hdr_unit or "ms"):
            factor = readers._UNIT_MS[unit] / readers._UNIT_MS[hdr_unit or "ms"]
            for r in rows:
                if r["bare"]:
                    r["ms"] *= factor
        if bare and inferred:
            self.warnings.append(f"{f['file']}: Duration is a bare number with no unit in the header; read as {unit} ({evidence}). Set columns.units in org.json to confirm it.")
            if low:
                self.low_confidence_units.append(f["file"])
                self.warnings.append(f"{f['file']}: the duration unit is a low-confidence guess — the typical span it implies is implausible; set columns.units in org.json")
        if status_numeric:
            caps["status"] = True
        if status_text:
            caps["error"] = True
        if status_numeric and status_text:
            self.warnings.append(f"{f['file']}: the Status column mixes HTTP codes ({status_numeric:,}) and words ({status_text:,}); codes were read as status, words as error flags")
        dup_rows = 0
        if rows:
            # rows identical to rows from an EARLIER file are an overlap; identical rows within one file are distinct spans.
            # A file without Span IDs is checked against every earlier file; a file with Span IDs (already de-duplicated by id
            # against other files with ids) is checked against earlier files without ids, so an export and its re-export
            # with ids added never count twice.
            against = self.seen_rows if not file_has_ids else self.seen_rows_noid
            used, out, mine = Counter(), [], Counter()
            for r in rows:
                k = (r["ts"], r["service"], r["resource"], r["method"], r["status"], round(r["ms"], 3))
                mine[k] += 1
                if against[k] > used[k]:
                    used[k] += 1
                    dup_rows += 1
                    continue
                out.append(r)
            self.seen_rows.update(mine)
            if not file_has_ids:
                self.seen_rows_noid.update(mine)
            rows = out
        for r in rows:
            r.pop("bare", None)
            if r.pop("assumed_utc"):
                self.assumed_utc += 1
            z = r.pop("tz")
            if z is not None:
                self.tz_offsets[z] += 1
        skipped = {"unreadable_timestamp": bad_ts, "unreadable_duration": bad_dur, "empty_service": no_svc, "repeated_span_id": dup_ids, "overlap_duplicate": dup_rows}
        for key, text in (("unreadable_timestamp", "rows had an unreadable Date and were skipped (set columns.date_format in org.json if the export uses another date shape)"),
                          ("unreadable_duration", "rows had an unreadable Duration and were skipped"), ("empty_service", "rows had an empty Service and were skipped"),
                          ("repeated_span_id", "spans whose Span ID was already loaded were skipped (overlapping exports?)"),
                          ("overlap_duplicate", "rows identical to rows in an earlier span file were skipped (overlapping exports without Span ID?)")):
            if skipped[key]:
                self.warnings.append(f"{f['file']}: {skipped[key]:,} {text}")
        if raw_n and bad_dur > 0.5 * raw_n:
            raise SystemExit(f"{f['file']}: {bad_dur:,} of {raw_n:,} rows have an unreadable Duration — the file is not a usable span export")
        if not rows and raw_n and (dup_rows + dup_ids) > 0 and dup_rows + dup_ids >= raw_n - bad_ts - bad_dur - no_svc:
            self.warnings.append(f"{f['file']}: every row repeats an earlier span file, so it was counted once")
            f["repeat_of_earlier"] = True
        elif not rows:
            self.warnings.append(f"{f['file']}: no usable span rows ({raw_n:,} data rows read)")
        f["rows"], f["raw_rows"], f["skipped"] = len(rows), raw_n, skipped
        f["units"] = {"duration": {"unit": unit if bare else "text", "evidence": evidence if bare else "human-readable durations ('1.23 ms')", "inferred": bool(bare and inferred), "low_confidence": bool(bare and low)}}
        if rows:
            self.span_caps.update(caps)          # only columns that rows actually carry into the review
            for r in rows:
                r["_caps"] = caps
                r["_file"] = f["file"]
        self.spans.extend(rows)
        self.span_meta.append({"file": f["file"], "rows": len(rows), "raw_rows": raw_n, "start": min((r["ts"] for r in rows if r["ts"]), default=None),
                               "end": max((r["ts"] for r in rows if r["ts"]), default=None)})

    # ---------------------------------------------------------- aggregate lists
    def _rate_scale(self, values):
        vals = [v for v in values if v is not None]
        return 0.01 if vals and max(vals) > 1.0 else 1.0

    def _p95_units(self, f, cols, default):
        """(unit, source): columns.units first, then a header hint, then the export's observed default."""
        if "p95" not in cols:
            return default, "default"
        raw_hdr, norm = f["headers"][cols["p95"]].strip().lower(), f["normalised"][cols["p95"]]
        units_cfg = self.cfg.get("_units", {})
        if raw_hdr in units_cfg or norm in units_cfg:
            return units_cfg.get(raw_hdr) or units_cfg.get(norm), "config"
        hint = readers.unit_from_header(f["headers"][cols["p95"]])
        return (hint, "header") if hint else (default, "default")

    def _load_endpoints(self, f):
        cols = f["columns"]
        dc = bool(f.get("decimal_comma"))
        unit, src = self._p95_units(f, cols, "s")
        out, rates, text_p95, bare_p95, bad_counts = [], [], 0, 0, 0
        for raw in readers.iter_rows(f):
            g = lambda k: raw[cols[k]] if k in cols and cols[k] < len(raw) else ""
            calls = readers.parse_int(g("request_count"), dc)
            if calls is None:
                bad_counts += 1
                continue
            er = readers.parse_number(g("error_rate"), dc)
            rates.append(er)
            p95_ms = readers.parse_duration_ms(g("p95"), unit, dc) if "p95" in cols else None
            if "p95" in cols and readers.unquote(g("p95")):
                if readers._NUMBER.match(readers.unquote(g("p95"))):
                    bare_p95 += 1
                else:
                    text_p95 += 1
            mon, states = readers.parse_monitors(g("monitors")) if "monitors" in cols else (0, {})
            out.append({"service": readers.unquote(g("service")), "resource": readers.unquote(g("resource")), "calls": calls, "err_rate": er, "has_monitors": "monitors" in cols,
                        "p95_s": p95_ms / 1000.0 if p95_ms is not None else None, "monitors": mon, "pct_text": "%" in readers.unquote(g("error_rate")), "_file": f["file"]})
        scale = 0.01 if any(r["pct_text"] for r in out) else self._rate_scale(rates)
        for r in out:
            r["err_rate"] = (r["err_rate"] or 0.0) * scale
            r["failed"] = int(round(r["calls"] * r["err_rate"]))
            r.pop("pct_text", None)
        if bad_counts:
            self.warnings.append(f"{f['file']}: {bad_counts:,} row{'s' if bad_counts != 1 else ''} with an unreadable Request Count skipped")
            f["skipped"] = {"unreadable_count": bad_counts}
        f["rows"] = len(out)
        f["units"] = {"error_rate": {"unit": "percent" if scale == 0.01 else "fraction", "evidence": "values above 1 or a % sign" if scale == 0.01 else "all values within 0..1"}}
        if "p95" in cols:
            ev = {"config": "set in columns.units", "header": "unit from the column header", "default": "Endpoints list P95 Duration is seconds (observed)"}[src]
            f["units"]["p95"] = {"unit": "text" if text_p95 and not bare_p95 else unit, "evidence": "durations carry their own unit" if text_p95 and not bare_p95 else ev}
        f["has_monitor_counts"] = "monitors" in cols
        self.endpoints.extend(out)

    def _load_services(self, f, inferred=False):
        cols = f["columns"]
        dc = bool(f.get("decimal_comma"))
        unit, src = self._p95_units(f, cols, "ns")
        out, rates = [], []
        for raw in readers.iter_rows(f):
            g = lambda k: raw[cols[k]] if k in cols and cols[k] < len(raw) else ""
            name = readers.unquote(g("service"))
            if not name:
                continue
            req = readers.parse_int(g("request_count"), dc) or 0
            er = readers.parse_number(g("error_rate"), dc)
            rates.append(er)
            p95_ms = readers.parse_duration_ms(g("p95"), unit, dc) if "p95" in cols else None
            mon, states = readers.parse_monitors(g("monitors")) if "monitors" in cols else (0, {})
            slos = readers.unquote(g("slos")) if "slos" in cols else ""
            envs = readers.unquote(g("envs")) if "envs" in cols else ""
            out.append({"name": name, "type": readers.unquote(g("svc_type")).lower(), "requests": req, "err_rate": er, "has_monitors": "monitors" in cols,
                        "p95_s": p95_ms / 1000.0 if p95_ms is not None else None, "monitors": mon, "monitor_states": states,
                        "slos": "" if slos in ("", "0", "[]") else slos, "envs": "" if envs in ("", "[]") else envs,
                        "pct_text": "%" in readers.unquote(g("error_rate")), "_file": f["file"]})
        scale = 0.01 if any(r["pct_text"] for r in out) else self._rate_scale(rates)
        for r in out:
            r["err_rate"] = (r["err_rate"] or 0.0) * scale
            r["failed"] = int(round(r["requests"] * r["err_rate"]))
            r.pop("pct_text", None)
        f["rows"] = len(out)
        f["units"] = {"error_rate": {"unit": "percent" if scale == 0.01 else "fraction", "evidence": "values above 1 or a % sign" if scale == 0.01 else "all values within 0..1"}}
        if "p95" in cols:
            ev = {"config": "set in columns.units", "header": "unit from the column header", "default": "Services list p95 is nanoseconds (observed)"}[src]
            f["units"]["p95"] = {"unit": unit, "evidence": ev}
        f["has_monitor_counts"] = "monitors" in cols
        (self.inferred if inferred else self.services).extend(out)

    def _load_inferred(self, f):
        self._load_services(f, inferred=True)

    def _load_catalog(self, f):
        cols = f["columns"]
        seen, out = set(), []
        for raw in readers.iter_rows(f):
            g = lambda k: raw[cols[k]] if k in cols and cols[k] < len(raw) else ""
            key = g("resource_key") if "resource_key" in cols else None
            if key and key in seen:
                continue
            if key:
                seen.add(key)
            out.append({"type": g("resource_type"), "account": g("account"), "env": g("env"), "service": g("service"), "team": g("team"),
                        "display_name": g("display_name")})
        f["rows"] = len(out)
        self.catalog.extend(out)


def parse_month(month):
    """'November 2026' (any case) -> datetime(2026, 11, 1); anything else is an input error."""
    try:
        return datetime.strptime(month.strip(), "%B %Y")
    except (ValueError, AttributeError):
        raise SystemExit(f'--month "{month}" is not a month such as "November 2026"')


def _minutes_text(minutes):
    return f"{minutes:.0f}" if minutes >= 1.5 else f"{max(1, round(minutes * 60))} seconds"


# ---------------------------------------------------------------- facts
def build_facts(inventory, cfg, month=None, as_of=None, window_days=None, warnings=None):
    warnings = warnings if warnings is not None else []
    month_dt = parse_month(month) if month else None
    inp = Inputs(inventory, cfg, warnings)
    empty = [f for f in inp.files if not f["rows"] and not f.get("repeat_of_earlier")]
    for f in empty:
        warnings.append(f"{f['file']}: recognised as {f['kind']} but holds no usable data rows — ignored; re-export it")
    if not inp.spans and not inp.endpoints:
        raise SystemExit("no usable spans export (and no Endpoints list) found — see the warnings above for what was skipped in each file")
    if not inp.spans:
        warnings.append("no spans export — volumes and failure counts come from the Endpoints list only; sample-tier findings are skipped")
    F = {"meta": {"schema_version": SCHEMA_VERSION, "kit_version": KIT_VERSION, "org": cfg["org"].get("name", ""), "org_short": cfg["org"]["short"],
                  "timezone": str(cfg["_tzinfo"]), "config_path": os.path.basename(cfg["_path"]) if cfg.get("_path") else None, "config_sha256": cfg.get("_sha256"),
                  "thresholds": dict(cfg["thresholds"]), "failure_rule": cfg["failure"]["rule"], "aliases": dict(cfg["services"].get("aliases") or {}), "inputs": [], "empty_inputs": [f["file"] for f in empty], "default_integration": sorted(cfg["_defaults"]["integration_services"]),
                  "analyst_notes": []},
         "coverage": {}, "window": {}, "totals": {}, "services": {}, "domains": {}, "resources": {}, "dependencies": {}, "edges": [],
         "node_groups": {}, "anatomy": {}, "noise": {}, "kpis": {}, "ratios": [], "catalog": None, "tables": {}, "caveats": []}
    F["meta"]["config_text"] = _config_text(cfg)
    tz = cfg["_tzinfo"]
    # integration libraries: the configured and built-in names, plus every seen service named after one ('<app>-sql-server', 'postgres.db')
    integration = set(cfg["_integration"]) | {s.lower() for s in {r["service"] for r in inp.spans} | {x["name"] for x in inp.services} if is_integration_name(s, cfg)}
    kids_pre = defaultdict(list)
    for r in inp.spans:
        if r["parent_id"]:
            kids_pre[r["parent_id"]].append(r)
    renamed = Counter()
    picks = defaultdict(Counter)                       # (library, topic, host) -> apps found below its consume spans
    bare = [r for r in inp.spans if r["span_id"] and is_bare_queue(r["service"]) and consumes(r, cfg)]
    own_app = {}
    for r in bare:
        app = _app_below(r, kids_pre, integration, cfg)
        if app:
            own_app[id(r)] = app
            picks[(r["service"], r["resource"], r["host"])][app] += 1
    for r in bare:
        app = own_app.get(id(r))
        if app is None:                                 # nothing below this message: the topic's only consumer here, if there is one
            apps = picks.get((r["service"], r["resource"], r["host"]))
            app = next(iter(apps)) if apps and len(apps) == 1 else None
        if app:
            renamed[(r["service"], app)] += 1
            r["_metric_service"] = r["service"]
            r["service"] = app
    for (lib, app), c in sorted(renamed.items()):
        F["caveats"].append(f"{c:,} `{lib}` consume spans run the consuming app `{app}`; they are counted as its requests (the tracer names consume spans after the library)")
    queue_consumers = {r["service"].lower() for r in inp.spans if queue_consumer_name(r["service"], r, cfg)}
    integration -= queue_consumers
    producer_row = lambda r: r["service"].lower() in queue_consumers and not r["entry"] and not queue_consumer_name(r["service"], r, cfg)
    for f in inp.files:
        if not f["rows"]:
            continue
        F["meta"]["inputs"].append({"file": f["file"], "kind": f["kind"], "rows": f["rows"], "raw_rows": f.get("raw_rows"), "skipped": f.get("skipped", {}), "sha256": f["sha256"],
                                    "delimiter": f["delimiter"], "encoding": f.get("encoding", "utf-8"), "columns": {k: f["headers"][i] for k, i in f["columns"].items()},
                                    "extras": [h for i, h in enumerate(f["headers"]) if i not in f["columns"].values()], "units": f.get("units", {})})
        u = (f.get("units") or {}).get("duration") or {}
        if f["kind"] == "spans" and u.get("inferred"):
            F["caveats"].append(f"`{f['file']}` gives Duration as bare numbers with no unit; they were read as {u['unit']} ({u['evidence']}) — every latency in this review depends on that"
                                + (" and the guess is low-confidence" if u.get("low_confidence") else ""))
    caps = dict(inp.span_caps)
    has = {"spans": bool(inp.spans), "endpoints": bool(inp.endpoints), "services": bool(inp.services), "inferred": bool(inp.inferred), "catalog": bool(inp.catalog)}
    has_monitor_counts = any(f.get("has_monitor_counts") for f in inp.files if f["kind"] in ("endpoints", "services") and f["rows"])
    T = cfg["thresholds"]

    # ---- window of the span sample: per-file intervals merged only when they overlap or abut
    intervals = sorted(((m["start"], m["end"]) for m in inp.span_meta if m["start"] and m["end"]), key=lambda t: t[0])
    merged = []
    for s, e in intervals:
        if merged and (s - merged[-1][1]) <= MERGE_SLACK:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    lo = merged[0][0] if merged else None
    hi = merged[-1][1] if merged else None
    minutes = sum((e - s).total_seconds() / 60.0 for s, e in merged) if merged else 0.0
    minutes = max(minutes, 1 / 60) if merged else 0.0
    if len(merged) > 1:
        same_day = len({s.astimezone(tz).date() for s, _ in merged}) == 1
        msg = ("the span exports cover separate windows (" + "; ".join(f"{s.astimezone(tz):%d %b %H:%M}–{e.astimezone(tz):%H:%M}" for s, e in merged) + "); they are pooled as one sample"
               + (" — keep one month's exports per folder unless that is intended" if not same_day else ""))
        F["caveats"].append(msg)
        warnings.append(msg)
    if as_of:
        as_of_date = as_of
    elif hi:
        as_of_date = hi.astimezone(tz).strftime("%Y-%m-%d")
    elif month_dt:
        as_of_date = month_dt.strftime("%Y-%m-%d")
        msg = "the exports carry no timestamps; the review is dated to the reporting month and the sample window is unknown"
        F["caveats"].append(msg)
        warnings.append(msg)
    else:
        raise SystemExit("the exports carry no readable timestamps to date the review — pass --as-of YYYY-MM-DD (the last day of the export range) or --month \"November 2026\"")
    as_of_dt = datetime.strptime(as_of_date, "%Y-%m-%d")
    if hi and as_of:
        gap = (as_of_dt.date() - hi.astimezone(tz).date()).days
        tol = window_days or cfg["window"].get("days") or 7
        if gap < 0 or gap > tol:
            F["caveats"].append(f"the review is dated {as_of_date}, {abs(gap)} day{'s' if abs(gap) != 1 else ''} {'before' if gap < 0 else 'after'} the latest span in the sample ({hi.astimezone(tz):%Y-%m-%d})")
            warnings.append(f"--as-of {as_of_date} is {abs(gap)} day{'s' if abs(gap) != 1 else ''} {'before' if gap < 0 else 'after'} the latest span timestamp ({hi.astimezone(tz):%Y-%m-%d}); the window phrases use the date given")
    if month_dt:
        month_end = (month_dt.replace(day=28) + timedelta(days=4)).replace(day=1)
        if not (month_dt <= as_of_dt < month_end + timedelta(days=7)):
            warnings.append(f"--month {month_dt:%B %Y} does not contain the data's as-of date {as_of_date}")
    as_of_label = f"{as_of_dt.day} {as_of_dt:%b %Y}"
    F["meta"]["month"] = month_dt.strftime("%B %Y") if month_dt else f"{as_of_dt:%B %Y}"
    F["meta"]["month_key"] = month_dt.strftime("%Y-%m") if month_dt else f"{as_of_dt:%Y-%m}"
    F["meta"]["as_of"] = as_of_date
    F["meta"]["as_of_label"] = as_of_label
    tzname = _tz_label(tz, hi or as_of_dt.replace(tzinfo=timezone.utc))
    if not merged:
        sample_phrase, sample_when = "in the sample", ""
    elif minutes >= 1.5:
        sample_phrase = f"in the {minutes:.0f}-minute sample" if len(merged) == 1 else f"in the {len(merged)} samples totalling {minutes:.0f} minutes"
        sample_when = " and ".join(f"{s.astimezone(tz):%d %b %H:%M}–{e.astimezone(tz):%H:%M}" for s, e in merged) + f" {tzname}"
    else:
        sample_phrase = f"in the {max(1, round(minutes * 60))}-second sample"
        sample_when = f"{lo.astimezone(tz):%d %b %H:%M:%S}–{hi.astimezone(tz):%H:%M:%S} {tzname}"
    sample_phrase_long = f"{sample_phrase} ({sample_when})" if sample_when else sample_phrase
    rows_total = len(inp.spans)
    file_windows = []
    for m in inp.span_meta:
        if m["start"] and m["end"]:
            fm = max((m["end"] - m["start"]).total_seconds() / 60.0, 1 / 60)
            file_windows.append({"file": m["file"], "rows": m["rows"], "raw_rows": m["raw_rows"], "start_utc": m["start"].isoformat(), "end_utc": m["end"].isoformat(),
                                 "start_local": m["start"].astimezone(tz).strftime("%Y-%m-%d %H:%M:%S"), "end_local": m["end"].astimezone(tz).strftime("%Y-%m-%d %H:%M:%S"),
                                 "minutes_text": _minutes_text(fm), "capped": (m["raw_rows"] or 0) in readers.CAP_VALUES or (m["raw_rows"] or 0) >= 100000})
        else:
            file_windows.append({"file": m["file"], "rows": m["rows"], "raw_rows": m["raw_rows"], "start_utc": None, "end_utc": None, "start_local": None, "end_local": None,
                                 "minutes_text": "", "capped": False})
    F["window"]["spans"] = {"tier": "sample", "rows": rows_total, "start_utc": lo.isoformat() if lo else None, "end_utc": hi.isoformat() if hi else None,
                            "start_local": lo.astimezone(tz).strftime("%Y-%m-%d %H:%M:%S") if lo else None,
                            "end_local": hi.astimezone(tz).strftime("%Y-%m-%d %H:%M:%S") if hi else None, "minutes": round(minutes, 1),
                            "minutes_text": _minutes_text(minutes) if merged else "",
                            "capped": any(w["capped"] for w in file_windows),
                            "windows": [{"start_utc": s.isoformat(), "end_utc": e.isoformat(), "minutes": round((e - s).total_seconds() / 60.0, 1)} for s, e in merged],
                            "files": file_windows, "phrase": sample_phrase, "phrase_long": sample_phrase_long, "when": sample_when, "tz": tzname}
    if inp.assumed_utc:
        F["caveats"].append(f"{inp.assumed_utc:,} span timestamps carried no timezone and were read as UTC")
    if cfg["_tz_note"]:
        F["caveats"].append(cfg["_tz_note"])
    if cfg.get("_tz_hint"):
        F["meta"]["analyst_notes"].append(cfg["_tz_hint"])

    # ---- entries: first pass per row, then (with Parent IDs) a span whose parent belongs to another service is that service's entry
    by_id = {r["span_id"]: r for r in inp.spans if r["span_id"]}
    rows_caps = lambda r: r.get("_caps") or caps
    rules = Counter()
    for r in inp.spans:
        r["entry"], rule = is_entry(r, rows_caps(r), cfg)
        r["_rule"] = rule
        rules[rule] += 1
    rule_used = rules.most_common(1)[0][0] if rules else None
    op_only = lambda rc: bool(rc.get("operation")) and not (rc.get("parent_id") or rc.get("type") or rc.get("kind"))
    ambiguous_client_ops = False
    client_ops = cfg["_rx"]["client_ops"]
    outbound_spans = defaultdict(list)                       # (service, kind) -> its outbound client spans (no host recorded)
    op_rows = [r for r in inp.spans if op_only(rows_caps(r))]
    if op_rows:
        # 'http.request' is a client span in Java/Node tracers and the server span in Go: when a service also has a more
        # specific server operation (express.request, servlet.request), its generic client operations are outbound calls
        specific = {r["service"] for r in op_rows if r["entry"] and r["operation"] and not matches_any(client_ops, r["operation"])}
        outbound = defaultdict(lambda: [0, 0, 0])            # service -> [calls, failed, with a failure signal]
        for r in op_rows:
            if r["entry"] and r["operation"] and matches_any(client_ops, r["operation"]):
                if r["service"] in specific:
                    r["entry"] = False
                    o = outbound[r["service"]]
                    o[0] += 1
                    o[1] += 1 if _failed(r, cfg["failure"]["rule"]) else 0
                    o[2] += 1 if (r["status"] is not None or r["error"] is not None) else 0
                else:
                    ambiguous_client_ops = True
        for svc, (calls, failed, sig) in sorted(outbound.items()):
            F["caveats"].append(f"{calls:,} spans of `{svc}` are outbound calls (a client operation beside its own server operation) and are not counted as its requests"
                                + (f"; {failed:,} of them failed" if sig and failed else "") + " — add peer.service to the spans export to see which host they call")
    if caps.get("parent_id") and by_id:
        for r in inp.spans:
            if r["entry"] or not r["parent_id"] or not rows_caps(r).get("parent_id") or not is_entry_candidate(r, cfg) or _SQL.match(r["resource"] or ""):
                continue
            p = _caller_of(r, by_id, integration, producer_row)
            rc = rows_caps(r)
            par = by_id.get(r["parent_id"])
            client_parent = par is not None and par["service"] != r["service"] and _outbound_span(par, by_id, integration, cfg)
            shows = server_evidence(r) or (bool(r["operation"]) and matches_any(cfg["_rx"]["entry_ops"], r["operation"])) or consumes(r, cfg) \
                or client_parent or not (rc.get("type") or rc.get("operation") or rc.get("kind"))
            if p is not None:
                if (p["service"] != r["service"] and shows) or (p["service"] == r["service"] and consumes(r, cfg)):
                    r["entry"] = True
            elif (r["type"] or "").lower() in ("web", "server", "http_server") or (r["kind"] or "") in ("server", "consumer") \
                    or (r["operation"] and matches_any(cfg["_rx"]["entry_ops"], r["operation"])):
                r["entry"] = True          # parent missing from the sample: an entry only if the span itself says so
    # without parents, an in-process framework span (spring.handler, aspnet_core_mvc.request) under the service's own server span
    # would count every request twice: when a service has entries from another operation, these are not requests
    inproc = cfg["_rx"].get("inprocess_ops", [])
    ops_by_svc = defaultdict(set)
    for r in inp.spans:
        if r["entry"] and r["operation"]:
            ops_by_svc[r["service"]].add(r["operation"])
    for r in inp.spans:
        if r["entry"] and r["operation"] and not rows_caps(r).get("parent_id") and matches_any(inproc, r["operation"]) \
                and any(not matches_any(inproc, o) for o in ops_by_svc[r["service"]]):
            r["entry"] = False
    flat_typed = lambda r: rows_caps(r).get("type") and not rows_caps(r).get("operation") and not rows_caps(r).get("parent_id")
    served_with_http = {r["service"] for r in inp.spans if r["entry"] and flat_typed(r) and (r["method"] or r["status"] is not None)}
    demoted = Counter()
    for r in inp.spans:
        if r["entry"] and flat_typed(r) and r["service"] in served_with_http and not r["method"] and r["status"] is None \
                and (r["type"] or "").lower() in SERVER_TYPES and (r["kind"] or "") not in ("server", "consumer"):
            r["entry"] = False
            demoted[r["service"]] += 1
    for svc, c in sorted(demoted.items()):
        F["caveats"].append(f"{c:,} server-typed spans of `{svc}` carry no method and no status code beside its requests that do; they are in-process framework spans "
                            "(spring.handler, aspnet_core_mvc.request) and are not counted as requests — add Operation Name to the spans export to confirm")
    caps["entry_rule"] = rule_used or "no spans"
    fallback_rules = ("method or a VERB", "no Method column")
    fallback = [r for r in inp.spans if r["_rule"].startswith(fallback_rules)]
    fallback_demoted = 0
    if fallback and len(fallback) < len(inp.spans):
        # a call a typed file shows only as a client span is not a request in a file read by method or resource shape
        key_of = lambda r: (r["service"], match_key(r["resource"], r["method"]))
        typed = [r for r in inp.spans if not r["_rule"].startswith(fallback_rules)]
        typed_client = {key_of(r) for r in typed if not r["entry"] and ((r["type"] or "").lower() in CLIENT_TYPES or (r["kind"] or "") in ("client", "producer")
                                                                     or (r["operation"] and matches_any(cfg["_rx"]["client_ops"], r["operation"])))}
        typed_entry = {key_of(r) for r in typed if r["entry"]}
        for r in fallback:
            if r["entry"] and key_of(r) in typed_client and key_of(r) not in typed_entry:
                r["entry"] = False
                fallback_demoted += 1
    if ambiguous_client_ops and not caps.get("peer"):
        only_client = sorted({r["service"] for r in inp.spans if r["entry"] and r["operation"] and matches_any(cfg["_rx"]["client_ops"], r["operation"])})
        F["caveats"].append(", ".join(f"`{s}`" for s in only_client[:5]) + f" record{'s' if len(only_client) == 1 else ''} only a client-named operation such as http.request, which is the "
                            "server operation in Go and an outbound call in Java or Node; without Type, span.kind or Parent ID in the export they are counted as requests")
    if fallback and not caps.get("peer"):
        files = sorted({r["_file"] for r in fallback})
        where = " in the spans export" if len(fallback) == len(inp.spans) else " in " + ", ".join(f"`{x}`" for x in files[:3])
        F["caveats"].append(f"without Parent ID, Type or Operation Name{where}, outbound HTTP calls that carry the calling service's name cannot be told apart from incoming requests; request totals may include some outbound calls"
                            + (f" ({fallback_demoted:,} spans that another file shows as outbound calls were left out)" if fallback_demoted else ""))

    # ---- per-row accumulation (sample tier)
    acc = defaultdict(lambda: {"calls": 0, "failed": 0, "durs": [], "capped": False, "status": defaultdict(int), "ops": Counter(), "signal": 0, "metric_service": None})
    svc_ops_http = defaultdict(lambda: defaultdict(int))     # operations of entries that carry a method or status: the HTTP operation
    svc_rows, svc_entry, svc_env, svc_hosts, svc_ops, svc_versions = defaultdict(int), defaultdict(int), defaultdict(lambda: defaultdict(int)), defaultdict(set), defaultdict(lambda: defaultdict(int)), defaultdict(set)
    svc_failed_rows, svc_signal_rows = defaultdict(int), defaultdict(int)
    framework_rows = defaultdict(int)
    entries_by_trace, children_by_trace = defaultdict(list), defaultdict(list)
    fail_rule = cfg["failure"]["rule"]
    call_spans = Counter()                    # spans that are calls: client types or kinds, client operations, or below another service
    for r in inp.spans:
        par_ = by_id.get(r["parent_id"]) if r["parent_id"] else None
        if (r["type"] or "").lower() in CLIENT_TYPES or (r["kind"] or "") in ("client", "producer") \
                or (bool(r["operation"]) and matches_any(cfg["_rx"]["client_ops"], r["operation"])) or (par_ is not None and par_["service"] != r["service"] and not r["entry"]):
            call_spans[r["service"]] += 1
    for r in inp.spans:
        svc_rows[r["service"]] += 1
        if _has_signal(r, fail_rule):
            svc_signal_rows[r["service"]] += 1
        if _failed(r, fail_rule):
            svc_failed_rows[r["service"]] += 1
        if r["env"]:
            svc_env[r["service"]][r["env"]] += 1
        if r["host"]:
            svc_hosts[r["service"]].add(r["host"])
        if r["version"]:
            svc_versions[r["service"]].add(r["version"])
        if r["entry"]:
            if r["operation"]:
                svc_ops[r["service"]][r["operation"]] += 1
                if r["method"] or r["status"] is not None:
                    svc_ops_http[r["service"]][r["operation"]] += 1
            svc_entry[r["service"]] += 1
            a = acc[(r["service"], r["resource"], r["method"])]
            a["calls"] += 1
            if r["operation"]:
                a["ops"][r["operation"]] += 1
            if _has_signal(r, fail_rule):
                a["signal"] += 1
            if r.get("_metric_service"):
                a["metric_service"] = r["_metric_service"]
            if _failed(r, fail_rule):
                a["failed"] += 1
            if len(a["durs"]) < DURATION_CAP:
                a["durs"].append(r["ms"])
            else:
                a["capped"] = True
            a["status"][f"{r['status'] // 100}xx" if r["status"] is not None else "none"] += 1
        elif matches_any(cfg["_rx"]["framework"], r["resource"]):
            framework_rows[r["service"]] += 1
        if r["trace_id"]:
            (entries_by_trace if r["entry"] else children_by_trace)[r["trace_id"]].append(r)

    # ---- resources: Endpoints list (aggregate) joined to the sample by (service, match key)
    ep_index = {(e["service"], match_key(e["resource"])): e for e in inp.endpoints}
    resources = defaultdict(dict)
    for (svc, res, meth), a in acc.items():
        key = match_key(res, meth)
        row = resources[svc].setdefault(key, _new_resource(svc, res if not meth or _VERB.match(res) or res.upper().startswith(meth.upper() + " ") or res.strip().upper() == meth.upper() else f"{meth} {res}", cfg))
        row["sample"]["calls"] += a["calls"]
        row["sample"]["failed"] += a["failed"]
        row.setdefault("_ops", Counter()).update(a["ops"])
        row["sample"]["signal"] = row["sample"].get("signal", 0) + a["signal"]
        if a["metric_service"]:
            row["metric_service"] = a["metric_service"]          # the tracer tags this operation's metrics with the library's name
        row["sample"]["_durs"].extend(a["durs"])
        row["sample"]["capped"] = row["sample"]["capped"] or a["capped"]
        for k, v in a["status"].items():
            row["status_mix"][k] = row["status_mix"].get(k, 0) + v
    for (svc, key), e in ep_index.items():
        row = resources[svc].setdefault(key, _new_resource(svc, e["resource"], cfg))
        row["week"] = {"calls": e["calls"], "failed": e["failed"], "p95_s": r3(e["p95_s"])}
        row["monitors"] = max(row.get("monitors", 0), e["monitors"])
    volumes_tier = "aggregate" if has["endpoints"] else "sample"
    svc_on_list = {svc for svc, rows in resources.items() if any(r["week"] and resource_class(svc, r["resource"], cfg)[0] == "business" for r in rows.values())}
    left_off_list = []
    for svc, rows in resources.items():
        for row in rows.values():
            s = row["sample"]
            durs = sorted(s["_durs"])
            s["p95_s"] = r3(pctl(durs, 95) / 1000.0) if durs else None
            s["p50_s"] = r3(pctl(durs, 50) / 1000.0) if durs else None
            s["n"] = len(durs)
            s["p95_ci_s"] = p95_interval_s(durs)
            ops_r = row.pop("_ops", None)
            row["operation"] = max(sorted(ops_r), key=lambda k: ops_r[k]) if ops_r else None
            s["err"] = rate(s["failed"], s["calls"])
            if row["week"] and svc in svc_on_list:
                primary, row["tier"] = row["week"], "aggregate"
            elif volumes_tier == "aggregate" and svc in svc_on_list:
                primary, row["tier"] = {"calls": 0, "failed": 0, "p95_s": None}, "sample"   # on a listed service but not on the list: never mixed into weekly counts
                if s["calls"]:
                    left_off_list.append((svc, row, s["calls"]))
            else:
                primary, row["tier"] = s, "sample"
            row["calls"], row["failed"] = primary["calls"], primary["failed"]
            row["err"] = rate(row["failed"], row["calls"])
            row["p95_s"] = primary.get("p95_s") if primary.get("p95_s") is not None else (s.get("p95_s") if row["tier"] == "sample" and row["calls"] else None)
            row["p95_source"] = "endpoints" if row["tier"] == "aggregate" and row["week"].get("p95_s") is not None else ("sample" if row["p95_s"] is not None else None)
            row["source"] = "both" if (row["week"] and s["calls"]) else ("endpoints" if row["week"] else "spans")
            row["failure_known"] = row["tier"] != "sample" or s.get("signal", 0) > 0 or not row["calls"]    # weekly rows carry Datadog's error count
    if left_off_list:
        tot_sample = sum(r["sample"]["calls"] for rows in resources.values() for r in rows.values())
        share = rate(sum(c for _, _, c in left_off_list), tot_sample)
        names = ", ".join(f"`{r['short']}` on `{svc}` ({c:,})" for svc, r, c in sorted(left_off_list, key=lambda t: -t[2])[:5])
        msg = (f"{len(left_off_list)} operation{'s' if len(left_off_list) != 1 else ''} ({share['num']:,} of the sample's {share['den']:,} entry spans) appear only in the span sample, not in the Endpoints list, "
               f"and are left out of the weekly figures: {names}")
        F["caveats"].append(msg)
        if share["pct"] > 10:
            warnings.append(msg + " — over 10 % of the sample: is the Endpoints list from another range or environment?")
    auto_bd = {(svc, row["resource"]) for svc, rows in resources.items() for row in rows.values()
               if row["calls"] >= T["by_design_min_calls"] and row["err"]["pct"] >= T["by_design_err_pct"]}
    noise_rules = defaultdict(lambda: {"resources": 0, "requests": 0, "failed": 0, "services": set()})
    by_design_candidates, noise_keys = [], set()
    svc_durs = defaultdict(list)
    for svc, rows in resources.items():
        for key, row in rows.items():
            cls, rule = resource_class(svc, row["resource"], cfg)
            row["class"], row["rule"] = cls, rule
            if cls == "business":
                svc_durs[svc].extend(row["sample"]["_durs"])
            else:
                if cls in ("health", "scanner", "static"):
                    noise_keys.add((svc, key))
                in_review = row["tier"] == volumes_tier and any(p.search(svc) for p in cfg["_rx"]["include"]) and not any(p.search(svc) for p in cfg["_rx"]["exclude"])
                nr = noise_rules[(cls, rule)] if in_review else {"resources": 0, "requests": 0, "failed": 0, "services": set()}
                nr["resources"] += 1
                nr["requests"] += row["calls"]
                nr["failed"] += row["failed"]
                nr["services"].add(svc)
            if cls == "business" and (svc, row["resource"]) in auto_bd:
                by_design_candidates.append({"service": svc, "resource": row["resource"], "calls": row["calls"], "failed": row["failed"], "err_pct": row["err"]["pct"]})
            row["sample"].pop("_durs", None)

    # ---- services
    services = {}
    monitor_listed = {e["service"] for e in inp.endpoints if e.get("has_monitors")} | {s["name"] for s in inp.services if s.get("has_monitors")}
    svc_list = {s["name"]: s for s in inp.services}
    all_names = set(resources) | set(svc_rows) | set(svc_list)
    ops_cfg = cfg["services"].get("operations", {})
    filtered_out = []
    renamed_libs = {lib for lib, _ in renamed}
    lib_apps = defaultdict(set)
    for lib, app in renamed:
        lib_apps[lib].add(app)
    lib_of_app = {next(iter(apps)): lib for lib, apps in lib_apps.items() if len(apps) == 1}
    all_names -= {lib for lib in renamed_libs if not svc_rows.get(lib)}       # a library whose spans all became its app's requests
    for name in sorted(all_names):
        if not any(p.search(name) for p in cfg["_rx"]["include"]) or any(p.search(name) for p in cfg["_rx"]["exclude"]):
            filtered_out.append(name)
            continue
        rows = list(resources.get(name, {}).values())
        biz = [r for r in rows if r["class"] == "business"]
        all_calls = sum(r["calls"] for r in rows)
        sl = svc_list.get(name, {}) if name not in renamed_libs else {}
        if not sl and name in lib_of_app and lib_of_app[name] in svc_list:
            sl = svc_list[lib_of_app[name]]            # the tracer reports this app's consumer under the library's name
        low = name.lower()
        if low in integration or sl.get("type") in ("db", "cache", "database"):
            role = "integration"
        elif rows and (any(r["week"] for r in rows) or svc_entry.get(name, 0) > 0):
            role = "entry"
        elif not rows and sl and sl.get("type") not in ("db", "cache", "database", "sql", "redis") and not is_integration_name(name, cfg) and not call_spans.get(name):
            role = "entry"                 # on the Services list, missing from the span sample and the Endpoints list
        elif svc_rows.get(name, 0) or is_hostlike(name) or (sl and sl.get("type") not in ("web", "")):
            role = "child"
        else:
            role = "entry" if sl else "child"
        ops_http_b, ops_b = Counter(), Counter()
        for r_ in biz:
            if r_.get("operation"):
                ops_b[r_["operation"]] += r_["sample"]["calls"]
                if any(k != "none" for k in r_["status_mix"]) or _VERB.match(r_["resource"] or ""):
                    ops_http_b[r_["operation"]] += r_["sample"]["calls"]
        # business operations first (never a health probe), the HTTP-carrying ones before queue jobs of the same service
        ops = ops_http_b or ops_b or svc_ops_http.get(name) or svc_ops.get(name, {})
        op, op_src, op_conf = "", "placeholder", None
        if ops:
            op = max(sorted(ops), key=lambda k: ops[k])
            op_src, op_conf = "column", round(100.0 * ops[op] / sum(ops.values()), 1)
        exact = ops_cfg.get(name)
        if exact and not str(exact).startswith("_todo"):
            op, op_src, op_conf = exact, "config", None
        else:
            for key_rx, cfg_op in cfg["_rx"]["operations_by_service"]:
                if key_rx.fullmatch(name) and not str(cfg_op).startswith("_todo"):
                    op, op_src, op_conf = cfg_op, "config", None
                    break
        if not op and cfg["services"].get("default_operation"):
            op, op_src = cfg["services"]["default_operation"], "default"
        # service p95: the Services list (complete, request-weighted); else the Endpoints list when it has one business row; else the
        # pooled business entry spans of the sample when that agrees with the weekly endpoint p95s; else none (the worst busy endpoint is kept apart)
        p95_s, p95_src, p95_kind, p95_ci = sl.get("p95_s"), None, None, None
        weekly = [r["p95_s"] for r in biz if r["p95_source"] == "endpoints"]
        if p95_s is not None:
            p95_src, p95_kind = "services_list", "service"
        elif len(weekly) == 1 and len(biz) == 1:
            p95_s, p95_src, p95_kind = weekly[0], "endpoints", "service"
        elif svc_durs.get(name):
            pool = sorted(svc_durs[name])
            cand = pctl(pool, 95) / 1000.0
            if not weekly or min(weekly) <= cand <= max(weekly):
                p95_s, p95_src, p95_kind = cand, f"sample (request-weighted over {len(pool):,} entry spans)", "service"
                p95_ci = p95_interval_s(pool)
        busiest = sorted((r for r in biz if r["p95_s"] is not None), key=lambda r: -r["calls"])[:30]
        p95_worst = max((r["p95_s"] for r in busiest), default=None)
        svc_tier = ("aggregate" if name in svc_on_list else "sample") if volumes_tier == "aggregate" else "sample"
        # a service on the Endpoints list only through probes and absent from the span sample keeps its Services-list figures
        biz_sampled = any(r["sample"]["calls"] for r in rows if resource_class(name, r["resource"], cfg)[0] == "business")
        use_rows = bool(rows) and not (sl and name not in svc_on_list and not biz_sampled)
        services[name] = {
            "display": cfg["services"].get("display", {}).get(name, name), "role": role, "type": sl.get("type", ""), "domain": None, "tier": None,
            "operation": op, "operation_source": op_src, "operation_confidence": op_conf,
            "tech": cfg["_defaults"]["operation_tech"].get(op, ""), "node_group": None,
            "requests": ({"all": all_calls, "business": sum(r["calls"] for r in biz), "tier": svc_tier} if use_rows or not sl else
                         {"all": sl.get("requests", 0), "business": sl.get("requests", 0), "tier": "services_list"}),
            "failed": ({"all": sum(r["failed"] for r in rows), "business": sum(r["failed"] for r in biz)} if use_rows or not sl else
                       {"all": sl.get("failed", 0), "business": sl.get("failed", 0)}),
            "err": ({"all": rate(sum(r["failed"] for r in rows), sum(r["calls"] for r in rows if r.get("failure_known", True))),
                     "business": rate(sum(r["failed"] for r in biz), sum(r["calls"] for r in biz if r.get("failure_known", True)))} if use_rows or not sl else
                    {"all": rate(sl.get("failed", 0), sl.get("requests", 0)), "business": rate(sl.get("failed", 0), sl.get("requests", 0))}),
            "err_base": ({"all": sum(r["calls"] for r in rows if r.get("failure_known", True)), "business": sum(r["calls"] for r in biz if r.get("failure_known", True))} if use_rows or not sl else
                         {"all": sl.get("requests", 0), "business": sl.get("requests", 0)}),
            "p95_s": r3(p95_s), "p95_source": p95_src, "p95_kind": p95_kind, "p95_ci_s": p95_ci, "p95_worst_s": r3(p95_worst),
            "p95_worst_resource": next((r["short"] for r in busiest if r["p95_s"] == p95_worst), None) if p95_worst is not None else None,
            "endpoints": len(rows), "business_endpoints": len(biz),
            "noise": {cls: {"resources": sum(1 for r in rows if r["class"] == cls), "calls": sum(r["calls"] for r in rows if r["class"] == cls),
                            "failed": sum(r["failed"] for r in rows if r["class"] == cls)} for cls in ("health", "scanner", "static", "framework", "by_design")
                      if any(r["class"] == cls for r in rows)},
            "status_mix": _sum_mix(rows), "monitors": max([sl.get("monitors", 0)] + [r.get("monitors", 0) for r in rows]),
            "monitors_known": name in monitor_listed or bool(sl.get("has_monitors")),
            "monitor_states": sl.get("monitor_states", {}), "slos": sl.get("slos", ""), "envs": sl.get("envs", "") or ",".join(sorted(svc_env.get(name, {}))),
            "versions": sorted(svc_versions.get(name, set()))[:5], "hosts": sorted(svc_hosts.get(name, set()))[:10],
            "requests_services_list": sl.get("requests"), "err_services_list": rate(sl.get("failed", 0), sl.get("requests", 0)) if sl else None,
            "spans_without_entries": svc_rows.get(name, 0) if not rows else 0,
            "spans": {"rows": svc_rows.get(name, 0), "entry": svc_entry.get(name, 0), "framework_rows": framework_rows.get(name, 0),
                      "failed": svc_failed_rows.get(name, 0), "with_signal": svc_signal_rows.get(name, 0)},
        }
        judged = biz                          # a traced health check does not speak for the business requests
        no_biz_sampled = not any(r["sample"]["calls"] for r in biz)
        services[name]["failure_known"] = services[name]["requests"]["tier"] != "sample" or any(r.get("failure_known") and r["sample"]["calls"] for r in judged)
        if not services[name]["failure_known"] and no_biz_sampled and any(r.get("failure_known") and r["sample"]["calls"] for r in rows):
            services[name]["failure_known"] = True     # no business request in the sample to judge; its spans do carry a failure signal
        sp_ = services[name]["spans"]
        unmarked = sp_["rows"] - sp_["entry"]
        services[name]["unmarked_spans"] = unmarked
        services[name]["list_reason"] = ("markerless" if no_biz_sampled and unmarked > 0 else
                                         "probes_sampled" if no_biz_sampled and sp_["entry"] > 0 else
                                         "probe_listed" if volumes_tier == "aggregate" and any(r.get("week") for r in rows) else "absent")
        tags = {r.get("metric_service") for r in rows if r["sample"]["calls"]}
        if rows and len(tags) == 1 and None not in tags:
            services[name]["metric_service"] = next(iter(tags))
    F["services"] = services
    if all_names and not services:
        raise ConfigError(f"services.include / services.exclude leave none of the {len(all_names):,} services in the exports (for example "
                          + ", ".join(sorted(all_names)[:3]) + ") — check the patterns")
    if filtered_out:
        F["meta"]["analyst_notes"].append(f"services.include / services.exclude left out {len(filtered_out):,} service{'s' if len(filtered_out) != 1 else ''}: "
                                          + ", ".join(filtered_out[:8]) + (" …" if len(filtered_out) > 8 else ""))
        F["meta"]["filtered_out_services"] = filtered_out
    entry_names = [n for n, s in services.items() if s["role"] == "entry"]
    dropped_entries = [(n, s["spans"]["entry"]) for n, s in services.items() if s["role"] != "entry" and s["spans"]["entry"]]
    if dropped_entries:
        F["caveats"].append("spans named after " + ", ".join(f"`{n}` ({c:,} rows)" for n, c in dropped_entries[:5]) + " look like requests but the name is an integration library or a host, so they are treated as calls, not as services")

    # ---- domains (config, else auto by service family) and node groups
    domains, auto_domains = {}, not cfg["_rx"]["domains"]
    if auto_domains:
        stems = defaultdict(list)
        for n in entry_names:
            stems[service_stem(n)].append(n)
        used_keys = set()
        for stem, members in sorted(stems.items()):
            key = slug(stem) or "services"
            k, i = key, 2
            while k in used_keys:
                k, i = f"{key}-{i}", i + 1
            used_keys.add(k)
            domains[k] = {"label": humanise(stem) or stem, "icon": "🧩", "audience": "Users", "tier": "staff", "services": sorted(members), "description": "", "source": "auto", "empty": False}
        if entry_names:
            F["caveats"].append("services were grouped by name, not by business domain")
            F["meta"]["analyst_notes"].append("no domains configured — run `init` and name the domains, audiences and tiers")
    else:
        for dom, pats in cfg["_rx"]["domains"]:
            aud = dom.get("audience", "")
            if not aud or str(aud).startswith("_todo"):
                aud = "Users"
                warnings.append(f"domain '{dom['key']}': audience still _todo in the config — shown as 'Users'")
            tier = dom.get("tier", "staff")
            if str(tier).startswith("_todo"):
                tier = "staff"
                warnings.append(f"domain '{dom['key']}': tier still _todo in the config — treated as staff (red findings need customer or clinical)")
            domains[dom["key"]] = {"label": dom["label"], "icon": dom.get("icon", "🧩"), "audience": aud, "tier": tier,
                                   "services": [], "description": dom.get("description", ""), "source": "config", "empty": False}
        unmatched = []
        for n in entry_names:
            hits = [dom["key"] for dom, pats in cfg["_rx"]["domains"] if any(p.search(n) for p in pats)]
            if not hits:
                unmatched.append(n)
                continue
            domains[hits[0]]["services"].append(n)
            if len(hits) > 1:
                labels = [domains[h]["label"] for h in hits]
                F["caveats"].append(f"`{n}` fits more than one domain ({', '.join(labels)}); it is shown under {labels[0]}")
                warnings.append(f"`{n}` matches domains {', '.join(hits)} — placed in '{hits[0]}' because it comes first in org.json; tighten the patterns")
        pats_of = {dom["key"]: pats for dom, pats in cfg["_rx"]["domains"]}
        for key, d in domains.items():
            if not d["services"]:
                d["empty"] = True
                if any(p.search(x) for x in filtered_out for p in pats_of.get(key, [])):
                    F["caveats"].append(f"{d['label']} is outside this review's scope: its services are left out by the config")
                    warnings.append(f"domain '{key}' ({d['label']}): every service it matches is left out by services.include / services.exclude")
                    continue
                F["caveats"].append(f"{d['label']} has no traced service in these exports")
                warnings.append(f"domain '{key}' ({d['label']}) matched no traced service — check its services patterns in org.json")
        if unmatched:
            domains["other"] = {"label": "Other services", "icon": "📦", "audience": "Users", "tier": "staff", "services": unmatched, "description": "", "source": "auto", "empty": False}
            F["caveats"].append("services outside the named domains, shown under Other services: " + ", ".join(f"`{x}`" for x in unmatched))
    for key, d in domains.items():
        for n in d["services"]:
            services[n]["domain"], services[n]["tier"] = key, d["tier"]
        members = [services[n] for n in d["services"]]
        on_tier = [s for s in members if s["requests"]["tier"] == volumes_tier]
        sample_m = [s for s in members if s["requests"]["tier"] == "sample"]
        list_m = [s for s in members if s["requests"]["tier"] == "services_list"]
        counted = on_tier or sample_m or list_m or members
        d["volume_tier"] = volumes_tier if on_tier or not members else ("sample" if sample_m else "services_list")
        d["counted_services"] = [n for n in d["services"] if services[n] in counted]
        d["sample_only_services"] = [n for n in d["services"] if services[n] not in counted and services[n]["requests"]["tier"] == "sample"]
        d["services_list_only"] = [n for n in d["services"] if services[n] not in counted and services[n]["requests"]["tier"] == "services_list"]
        req = sum(s["requests"]["all"] for s in counted)
        biz = sum(s["requests"]["business"] for s in counted)
        known = [s for s in counted if s.get("failure_known", True)]
        d["failure_known"] = bool(known) or not counted
        d["failure_not_recorded"] = [n for n in d["counted_services"] if not services[n].get("failure_known", True)] if known else []
        kreq = sum(s.get("err_base", s["requests"])["all"] for s in known)
        kbiz = sum(s.get("err_base", s["requests"])["business"] for s in known)
        d["failure_base_partial"] = bool(known) and kbiz < sum(s["requests"]["business"] for s in counted)
        p95s = [s["p95_s"] if s["p95_s"] is not None else s["p95_worst_s"] for s in counted]
        p95s = [x for x in p95s if x is not None]
        d.update({"requests": {"all": req, "business": biz}, "failed": {"all": sum(s["failed"]["all"] for s in known), "business": sum(s["failed"]["business"] for s in known)},
                  "err": {"all": rate(sum(s["failed"]["all"] for s in known), kreq), "business": rate(sum(s["failed"]["business"] for s in known), kbiz)},
                  "p95_range_s": [r3(min(p95s)), r3(max(p95s))] if p95s else [None, None],
                  "p95_kind": "service" if counted and all(s["p95_kind"] == "service" for s in counted) else ("worst_endpoint" if p95s else None),
                  "noise_share_of_failed": rate(sum(s["failed"]["all"] - s["failed"]["business"] for s in counted), sum(s["failed"]["all"] for s in counted))})
    for n, s in services.items():
        if s["domain"] is None:
            s["domain"], s["tier"] = "", "internal"
    total_req = sum(d["requests"]["all"] for d in domains.values() if d["volume_tier"] == volumes_tier)
    for d in domains.values():
        d["share"] = rate(d["requests"]["all"] if d["volume_tier"] == volumes_tier else 0, total_req)
    F["domains"] = dict(sorted(domains.items(), key=lambda kv: (kv[1]["volume_tier"] != volumes_tier, -kv[1]["requests"]["all"], kv[0])))
    F["node_groups"] = _node_groups(entry_names, services, cfg)

    # ---- resources per service
    for svc, rows in resources.items():
        if svc in services:
            F["resources"][svc] = sorted(rows.values(), key=lambda r: (-r["calls"], -r["sample"]["calls"], r["resource"]))
    why_of = {res_rx.pattern: why for _, res_rx, why in cfg["_rx"]["by_design"] if not str(why).startswith("_todo")}
    for _, res_rx, why in cfg["_rx"]["by_design"]:
        if str(why).startswith("_todo"):
            warnings.append(f"noise.by_design '{res_rx.pattern}': why still _todo in the config — the notebook shows the rule without a reason")
    F["noise"] = {"by_class": _noise_by_class(F["resources"]),
                  "rules_applied": sorted([{"class": cls, "rule": rule, "source": rule.split(":")[0], "resources": v["resources"], "requests": v["requests"], "failed": v["failed"],
                                            "services": sorted(v["services"]), "why": why_of.get(rule.split(":", 2)[-1], "") if rule.startswith("config:by_design:") else ""}
                                           for (cls, rule), v in noise_rules.items()], key=lambda x: (-x["requests"], x["rule"])),
                  "by_design_candidates": sorted(by_design_candidates, key=lambda x: (-x["calls"], x["resource"]))}
    for c in F["noise"]["by_design_candidates"][:5]:
        F["meta"]["analyst_notes"].append(f"{c['service']} {c['resource']} fails on every call ({c['failed']:,} of {c['calls']:,}); it is reported as a failure — "
                                          "if the team confirms it fails by design, list it under noise.by_design in org.json")

    # ---- dependencies and edges: each call counted once, for the service that made it
    deps, edges = {}, defaultdict(lambda: {"calls": 0, "failed": 0, "durs": [], "signal": 0})
    graph_method, unresolved, children_total = "none", 0, 0
    called_through = set()     # integration libraries whose calls were attributed to a peer or a traced callee

    handled, producer_edges, orphans = set(), set(), defaultdict(list)
    filtered = set(F["meta"].get("filtered_out_services") or [])
    prod_envs_ = {e.lower() for e in cfg["org"].get("production_envs", [])}
    prod_row = lambda r: _is_prod_env(r["env"], prod_envs_, cfg["_rx"]["nonprod"]) if r["env"] else bool(cfg["org"].get("treat_missing_env_as_production", True))

    def bump(key, r, failed=None):
        handled.add(id(r))
        if key[0] in filtered:
            return                    # a service the config leaves out: its calls are not the review's
        e = edges[key]
        e["calls"] += 1
        if _has_signal(r, fail_rule):
            e["signal"] += 1
        if (_failed(r, fail_rule) if failed is None else failed):
            e["failed"] += 1
        if prod_row(r):
            e["prod_calls"] = e.get("prod_calls", 0) + 1
            e["prod_failed"] = e.get("prod_failed", 0) + (1 if (_failed(r, fail_rule) if failed is None else failed) else 0)
            e.setdefault("prod_durs", []).append(r["ms"]) if len(e.get("prod_durs", [])) < DURATION_CAP else None
        if len(e["durs"]) < DURATION_CAP:
            e["durs"].append(r["ms"])

    tree_row = lambda r: bool(rows_caps(r).get("parent_id") and rows_caps(r).get("span_id"))
    flat_rows = [r for r in inp.spans if not tree_row(r)] if by_id else list(inp.spans)
    methods_used = Counter()
    if caps.get("parent_id") and caps.get("span_id") and by_id:
        methods_used["parent_child"] = len(inp.spans) - len(flat_rows)
        kids_of = defaultdict(list)
        for r in inp.spans:
            if r["parent_id"]:
                kids_of[r["parent_id"]].append(r)
        for r in inp.spans:
            if not tree_row(r):
                continue                  # a file without Parent ID / Span ID joins by peer tag or by trace below
            if r["entry"] and r["parent_id"]:
                par = by_id.get(r["parent_id"])
                if par is not None and par["service"].lower() not in integration and not producer_row(par) and not (par["peer"] and par["peer"].lower() == r["service"].lower()):
                    p = _caller_of(r, by_id, integration, producer_row)
                    if p is not None and p["service"] != r["service"]:
                        bump((p["service"], r["service"], "http"), r)
                continue
            if r["entry"] or not r["parent_id"]:
                continue
            if r["service"].lower() in integration and not r["peer"] and r["operation"] and matches_any(cfg["_rx"].get("inprocess_ops", []), r["operation"]):
                handled.add(id(r))                # an ORM session (hibernate.*) is in-process work; its queries below are the calls
                continue
            children_total += 1
            p = _caller_of(r, by_id, integration, producer_row)
            if not p:
                unresolved += 1
                handled.add(id(r))                # covered by the lower-bound caveat, never a library-named dependency of its own
                if r["peer"] and r["peer"].lower() != r["service"].lower() and (not caps.get("env") or prod_row(r)):
                    orphans[r["peer"]].append(r)
                continue
            caller = p["service"]
            failed = None
            callee = next((c for c in kids_of.get(r["span_id"], []) if c["entry"] and c["service"] != r["service"]), None) if r["span_id"] else None
            consumers_ = {c["service"] for c in kids_of.get(r["span_id"], []) if c["entry"]} if r["span_id"] else set()
            multi = (r["service"].lower() in integration or producer_row(r)) and len(consumers_) > 1
            via_dns = (callee is not None and bool(r["peer"]) and r["peer"].lower() != callee["service"].lower() and not producer_row(r) and not multi
                       and host_class(r["peer"], cfg) != "nonprod" and (prod_row(callee) or not prod_row(r)))
            if via_dns and r["service"].lower() not in integration:
                handled.add(id(r))                # the callee's own entry span records this call, under the service it reached
                called_through.add(r["service"].lower())
                continue
            if via_dns:
                target = callee["service"]          # an integration's call: counted here, under the service it reached
                called_through.add(r["service"].lower())
            elif r["peer"] and r["peer"].lower() != r["service"].lower() and not multi:
                target = r["peer"]
                called_through.add(r["service"].lower())
            elif r["service"].lower() in integration or producer_row(r):
                served = [c for c in kids_of.get(r["span_id"], []) if c["entry"]]
                consumers = sorted({c["service"] for c in served})
                if len(consumers) > 1:                # one message, several consumer groups: each is a callee of the request
                    called_through.add(r["service"].lower())
                    for t_ in consumers:
                        if t_ == caller or (t_.lower() in integration and caller.lower() in integration):
                            continue
                        f_ = _failed(r, fail_rule) or any(_failed(c, fail_rule) for c in served if c["service"] == t_)
                        bump((caller, t_, child_kind(r["service"], r["resource"], r["type"], cfg)), r, f_)
                    handled.add(id(r))
                    continue
                if served:
                    target = served[0]["service"]
                    failed = _failed(r, fail_rule) or any(_failed(c, fail_rule) for c in served)
                    called_through.add(r["service"].lower())
                else:
                    target = r["service"]
            else:
                target = r["service"]
            if target.lower() in integration and caller.lower() in integration:
                handled.add(id(r))                # an integration inside an integration: its outer call is counted
                continue
            if caller == target:
                continue                          # the service's own span: an outbound call or in-process work, decided below
            key = (caller, target, child_kind(r["service"], r["resource"], r["type"], cfg))
            if producer_row(r) and target == r["service"]:
                producer_edges.add(key)
            bump(key, r, failed)
    peer_rows = [r for r in flat_rows if rows_caps(r).get("peer")]
    if peer_rows:
        methods_used["peer"] = len(peer_rows)
        for r in peer_rows:
            if r["entry"] or not r["peer"] or r["peer"].lower() == r["service"].lower():
                continue
            caller = r["service"]
            if caller.lower() in integration:
                svcs = {e["service"] for e in entries_by_trace.get(r["trace_id"], [])} if r["trace_id"] else set()
                caller = next(iter(svcs)) if len(svcs) == 1 else "an integration library (caller not resolved)"
                called_through.add(r["service"].lower())
            bump((caller, r["peer"], child_kind(r["service"], r["resource"], r["type"], cfg)), r)
    co_kids = defaultdict(list)
    for r in flat_rows:
        if not rows_caps(r).get("peer") and rows_caps(r).get("trace_id") and r["trace_id"] and not r["entry"]:
            co_kids[r["trace_id"]].append(r)
    if co_kids:
        methods_used["trace_cooccurrence"] = sum(len(v) for v in co_kids.values())
        for tid, kids in co_kids.items():
            ents = entries_by_trace.get(tid)
            if not ents or len({e["service"] for e in ents}) != 1:
                continue
            caller = ents[0]["service"]
            for r in kids:
                if services.get(r["service"], {}).get("role") == "entry" and r["service"] != caller and not producer_row(r):
                    continue
                target = r["peer"] if r["peer"] and r["service"].lower() in integration else r["service"]
                if target == caller:
                    continue
                key = (caller, target, child_kind(r["service"], r["resource"], r["type"], cfg))
                if producer_row(r) and target == r["service"]:
                    producer_edges.add(key)
                bump(key, r)
    if methods_used:
        graph_method = methods_used.most_common(1)[0][0]
    if len(methods_used) > 1:
        F["caveats"].append("the span files carry different id columns, so calls were joined " + " and ".join(
            {"parent_child": "by parent and child span", "peer": "by peer tag", "trace_cooccurrence": "by trace"}[m] for m in sorted(methods_used)) + " depending on the file")
    if children_total and unresolved:
        share = rate(unresolved, children_total)
        F["caveats"].append(f"{unresolved:,} of {children_total:,} child spans ({share['pct']:.0f} %) had no parent in the sample, so dependency call counts from the sample are lower bounds")
    edge_rows = []
    for (a, b, kind), e in sorted(edges.items(), key=lambda kv: (-kv[1]["calls"], kv[0])):
        durs = sorted(e["durs"])
        per_req = ratio(e["calls"], svc_entry.get(a, 0), 2) if svc_entry.get(a) else None
        pd_ = sorted(e.get("prod_durs", []))
        edge_rows.append({"from": a, "to": b, "kind": kind, "calls": e["calls"], "failed": e["failed"], "err": rate(e["failed"], e["calls"]), "failure_known": e["signal"] > 0,
                          "p95_s": r3(pctl(durs, 95) / 1000.0) if durs else None, "per_request": per_req, "tier": "sample", "method": graph_method,
                          "prod": {"calls": e.get("prod_calls", 0), "failed": e.get("prod_failed", 0), "p95_s": r3(pctl(pd_, 95) / 1000.0) if pd_ else None}})
    F["edges"] = edge_rows
    for d in inp.inferred:
        deps[d["name"]] = {"class": host_class(d["name"], cfg), "source": "inferred", "tier": "services_list", "calls": d["requests"], "failed": d["failed"], "failure_known": True,
                           "err": rate(d["failed"], d["requests"]), "p95_s": r3(d["p95_s"]), "type": d["type"], "callers": [], "marker": nonprod_marker(d["name"], cfg)}
    for e in edge_rows:
        target = e["to"]
        if target in services and services[target]["role"] == "entry" and (e["from"], target, e["kind"]) not in producer_edges:
            continue
        d = deps.get(target)
        if d is None:
            d = deps[target] = {"class": _dep_class(target, e["kind"], cfg), "source": "spans", "tier": "sample", "calls": e["calls"], "failed": e["failed"], "err": e["err"], "p95_s": e["p95_s"],
                                "type": e["kind"], "callers": [], "marker": nonprod_marker(target, cfg), "failure_known": e["failure_known"]}
        elif d["source"] == "spans":
            d["calls"] += e["calls"]
            d["failed"] += e["failed"]
            d["err"] = rate(d["failed"], d["calls"])
            d["failure_known"] = d["failure_known"] or e["failure_known"]
        d["callers"].append({"service": e["from"], "calls": e["calls"], "err": e["err"], "p95_s": e["p95_s"], "known": e["from"] in services, "failure_known": e["failure_known"],
                             "prod": e["prod"]})
    rows_by_svc = defaultdict(list)
    for r in inp.spans:
        rows_by_svc[r["service"]].append(r)
    unjoined, extra_durs = [], defaultdict(list)
    entry_lower = {k.lower() for k, v in services.items() if v["role"] == "entry"}
    for host, rows_o in sorted(orphans.items()):
        if host.lower() in entry_lower:
            continue                                  # a call into a traced service whose caller left the sample: not a dependency
        d = deps.get(host)
        failed_o = sum(1 for r in rows_o if _failed(r, fail_rule))
        if d is None:
            d = deps[host] = {"class": _dep_class(host, child_kind(rows_o[0]["service"], rows_o[0]["resource"], rows_o[0]["type"], cfg), cfg), "source": "spans", "tier": "sample",
                              "calls": 0, "failed": 0, "err": rate(0, 0), "p95_s": None, "type": child_kind(rows_o[0]["service"], rows_o[0]["resource"], rows_o[0]["type"], cfg),
                              "callers": [], "marker": nonprod_marker(host, cfg), "failure_known": False}
        if d["source"] == "spans":
            d["calls"] += len(rows_o)
            d["failed"] += failed_o
            d["err"] = rate(d["failed"], d["calls"])
            d["failure_known"] = d["failure_known"] or any(_has_signal(r, fail_rule) for r in rows_o)
            d["unjoined"] = d.get("unjoined", 0) + len(rows_o)
            extra_durs[host].extend(r["ms"] for r in rows_o)

    def dep_from_rows(name, rows_dep, s_type):
        failed_n = sum(1 for r in rows_dep if _failed(r, fail_rule))
        sig_n = sum(1 for r in rows_dep if _has_signal(r, fail_rule))
        durs = sorted(r["ms"] for r in rows_dep)
        return {"class": _dep_class(name, child_kind(name, "", s_type or "", cfg), cfg), "source": "spans", "tier": "sample", "calls": len(rows_dep), "failed": failed_n,
                "err": rate(failed_n, len(rows_dep)), "p95_s": r3(pctl(durs, 95) / 1000.0) if durs else None, "type": s_type or "", "callers": [],
                "marker": nonprod_marker(name, cfg), "failure_known": sig_n > 0, "unjoined": len(rows_dep)}
    for name, s in services.items():
        if s["role"] in ("integration", "child") and name in deps and deps[name]["source"] == "spans" and graph_method != "none":
            left = [r for r in rows_by_svc.get(name, []) if not r["entry"] and id(r) not in handled]
            if left:                      # some of its calls reached an edge, the rest (a file with no join column) did not: count both
                extra = dep_from_rows(name, left, s["type"])
                d = deps[name]
                d["calls"] += extra["calls"]
                d["failed"] += extra["failed"]
                d["err"] = rate(d["failed"], d["calls"])
                d["failure_known"] = d["failure_known"] or extra["failure_known"]
                d["unjoined"] = d.get("unjoined", 0) + len(left)
                extra_durs[name].extend(r["ms"] for r in left)
                unjoined.append((name, len(left)))
            continue
        if s["role"] in ("integration", "child") and name not in deps:
            sig = s["spans"]["with_signal"]
            sl = svc_list.get(name)
            if graph_method != "none" and svc_rows.get(name) and (name.lower() in integration or name.lower() in called_through):
                left = [r for r in rows_by_svc.get(name, []) if not r["entry"] and id(r) not in handled]
                if left:
                    deps[name] = dep_from_rows(name, left, s["type"])      # calls no join could attribute: counted, with no caller
                    unjoined.append((name, len(left)))
                continue          # the rest are already counted under the host or service they reached
            if not svc_rows.get(name) and sl:
                deps[name] = {"class": _dep_class(name, child_kind(name, "", s["type"] or "", cfg), cfg), "source": "services_list", "tier": "services_list", "calls": sl["requests"],
                              "failed": sl["failed"], "err": rate(sl["failed"], sl["requests"]), "p95_s": r3(sl["p95_s"]), "type": s["type"] or "", "callers": [],
                              "marker": nonprod_marker(name, cfg), "failure_known": True}
                continue
            deps[name] = {"class": _dep_class(name, child_kind(name, "", s["type"] or "", cfg), cfg), "source": "spans", "tier": "sample", "calls": svc_rows.get(name, 0), "failed": s["spans"]["failed"],
                          "err": rate(s["spans"]["failed"], svc_rows.get(name, 0)),
                          "p95_s": s["p95_s"] if s["p95_s"] is not None else _p95_of(rows_by_svc.get(name, [])), "type": s["type"] or "", "callers": [],
                          "marker": nonprod_marker(name, cfg), "failure_known": sig > 0}
    for name in sorted(queue_consumers):
        svc = next((x for x in rows_by_svc if x.lower() == name), None)
        left = [r for r in rows_by_svc.get(svc, []) if producer_row(r) and id(r) not in handled] if svc else []
        if left and svc not in deps:
            deps[svc] = dep_from_rows(svc, left, "queue")                 # the consuming app's library also produced: calls with no caller resolved
            unjoined.append((svc, len(left)))
    if unjoined:
        F["caveats"].append(", ".join(f"{c:,} spans of `{n_}`" for n_, c in unjoined[:4]) + " could not be joined to a calling service; their calls and failures are counted without a caller")
    kids_index = defaultdict(list)
    if caps.get("parent_id"):
        for r in inp.spans:
            if r["parent_id"]:
                kids_index[r["parent_id"]].append(r)
    for r in inp.spans:
        if r["entry"] or r["peer"] or services.get(r["service"], {}).get("role") != "entry" or producer_row(r) or id(r) in handled or (caps.get("env") and not prod_row(r)):
            continue
        t_, k_ = (r["type"] or "").lower(), (r["kind"] or "").lower()
        if not (t_ in CLIENT_TYPES or k_ in ("client", "producer") or (r["operation"] and matches_any(client_ops, r["operation"]))):
            continue
        if r["span_id"] and any(c["entry"] for c in kids_index.get(r["span_id"], [])):
            continue                           # it reached a traced service: that call is an edge of the map
        outbound_spans[(r["service"], child_kind(r["service"], r["resource"], r["type"], cfg))].append(r)
    OUT_LABEL = {"db": "database calls", "cache": "cache calls", "queue": "queue messages", "secret_store": "secret-store reads"}
    for (svc, okind), rows_out in sorted(outbound_spans.items()):
        # outbound calls with no host recorded still fail: they become a dependency of their caller so the detectors see them
        name = f"{OUT_LABEL.get(okind, 'outbound calls')} of {svc}"
        failed_n = sum(1 for r in rows_out if _failed(r, fail_rule))
        sig = sum(1 for r in rows_out if _has_signal(r, fail_rule))
        durs = sorted(r["ms"] for r in rows_out)
        deps[name] = {"class": "unknown", "source": "spans", "tier": "sample", "calls": len(rows_out), "failed": failed_n, "err": rate(failed_n, len(rows_out)),
                      "p95_s": r3(pctl(durs, 95) / 1000.0) if durs else None, "type": okind, "marker": "", "failure_known": sig > 0, "host_unknown": True,
                      "callers": [{"service": svc, "calls": len(rows_out), "err": rate(failed_n, len(rows_out)), "p95_s": r3(pctl(durs, 95) / 1000.0) if durs else None, "known": True,
                                   "failure_known": sig > 0}]}
    target_durs, prod_durs_edge = defaultdict(list), defaultdict(list)
    for (a_, b_, k_), e in edges.items():
        if a_ not in filtered:
            target_durs[b_].extend(e["durs"])
            prod_durs_edge[(a_, b_)].extend(e.get("prod_durs", []))
    for name, d in deps.items():
        if d["source"] == "spans" and (target_durs.get(name) or extra_durs.get(name)):
            all_d = sorted(target_durs.get(name, []) + extra_durs.get(name, []))
            d["p95_s"] = r3(pctl(all_d, 95) / 1000.0)
    # ---- 41: callers whose calls fail come first wherever a few callers are named
    for d in deps.values():
        d["callers"].sort(key=lambda c: (-((c.get("err") or {}).get("num") or 0), -c["calls"], c["service"]))
    F["dependencies"] = dict(sorted(deps.items(), key=lambda kv: (-kv[1]["calls"], kv[0])))
    F["coverage"]["graph_method"] = graph_method
    peer_values = any(r["peer"] for r in inp.spans if not r["entry"])
    host_visibility = has["inferred"] or peer_values or any(d["source"] == "spans" and d["callers"] and (is_hostlike(name) or d["class"] in ("nonprod", "public", "telemetry")) for name, d in deps.items())

    # ---- anatomy per entry service (sample tier)
    F["anatomy"] = _anatomy(inp.spans, entries_by_trace, children_by_trace, by_id, services, caps, cfg, minutes, noise_keys)

    # ---- window of the Endpoints list: asserted (window.days / --window-days) or checked against health-check cadence
    agg = {"tier": "aggregate", "days": None, "source": "none", "confidence": "none", "proof": [], "candidates": {}, "tied": []}
    asserted_days = window_days or cfg["window"].get("days")
    if has["endpoints"]:
        probes_by_days = {days: _probes(inp.endpoints, days, cfg) for days in CANDIDATE_DAYS}
        agg["candidates"] = {str(d): len(p) for d, p in probes_by_days.items()}
        top = max(len(p) for p in probes_by_days.values())
        tied = [d for d in CANDIDATE_DAYS if top and len(probes_by_days[d]) == top]
        if 7 in tied:
            tied = [d for d in tied if d >= 7]          # a shorter range needs several times the probers at the same period
        agg["tied"] = tied
        if asserted_days:
            agg["days"], agg["source"] = asserted_days, "asserted"
            agg["proof"] = _probes(inp.endpoints, asserted_days, cfg)
            if agg["proof"]:
                agg["confidence"] = "consistent" if len(tied) <= 1 else "consistent_not_unique"
            elif tied and len(probes_by_days[tied[0]]) >= 2:      # two services agree on another range, none on the stated one
                agg["confidence"] = "contradicted"
                msg = f"the stated window of {asserted_days} days does not match the health-check cadence, which fits {', '.join(str(d) for d in tied)} days — no per-day figures are quoted"
                F["caveats"].append(msg)
                warnings.append(f"window.days / --window-days = {asserted_days} contradicts the health-check cadence ({', '.join(str(d) for d in tied)} days fit); per-day and hourly figures are withheld")
            else:
                agg["confidence"] = "unverified"
        elif len(tied) == 1 and len(probes_by_days[tied[0]]) >= 2:
            agg["days"], agg["source"], agg["proof"], agg["confidence"] = tied[0], "proved", probes_by_days[tied[0]], "high"
        elif len(tied) == 1:
            agg["days"], agg["source"] = 7, "assumed"
            F["caveats"].append(f"one health check's cadence fits {tied[0]} days, which one service cannot prove; 7 days assumed and no per-day figures are quoted")
            F["meta"]["analyst_notes"].append("set window.days in org.json (or pass --window-days) to the range the Endpoints list was exported over")
        elif tied:
            days = 7 if 7 in tied else tied[0]
            why = "the standard export range" if days == 7 else "the shortest range that fits"
            agg["days"], agg["source"], agg["proof"], agg["confidence"] = days, "ambiguous", probes_by_days[days], "medium"
            F["caveats"].append(f"the health-check cadence fits {', '.join(str(d) for d in tied)} days equally; {days} days assumed ({why}) and no per-day figures are quoted")
            F["meta"]["analyst_notes"].append("set window.days in org.json (or pass --window-days) to the range the Endpoints list was exported over, so per-day figures can be quoted")
        else:
            agg["days"], agg["source"] = 7, "assumed"
            F["caveats"].append("no fixed-cadence health check confirms the Endpoints-list window; 7 days assumed and no per-day figures are quoted")
            F["meta"]["analyst_notes"].append("set window.days in org.json (or pass --window-days) to the range the Endpoints list was exported over")
        window_known = bool(agg["days"]) and (agg["source"] == "proved" or (agg["source"] == "asserted" and agg["confidence"] != "contradicted"))
        if inp.services:
            ep_by_svc = defaultdict(int)
            for e in inp.endpoints:
                if e["service"] in svc_on_list:          # a worker listed only through its health check says nothing about the range
                    ep_by_svc[e["service"]] += e["calls"]
            ratios = [s["requests"] / ep_by_svc[s["name"]] for s in inp.services if ep_by_svc.get(s["name"], 0) > 1000 and s["requests"]]
            if ratios:
                ratios.sort()
                mid = len(ratios) // 2
                med = ratios[mid] if len(ratios) % 2 else (ratios[mid - 1] + ratios[mid]) / 2
                F["window"]["services_list"] = {"ratio_vs_endpoints": round(med, 3), "days_estimate": round(med * agg["days"], 3) if agg["days"] and window_known else None,
                                                "note": "the Services list covers a different range than the Endpoints list; the two are never divided into one ratio"}
                if abs(med - 1) > 0.1 and window_known:
                    F["caveats"].append(f"the Services list volumes cover {range_words(med * agg['days'])} against {agg['days']} day{'s' if agg['days'] != 1 else ''} for the Endpoints list (median ratio {med:.2f}); rates and p95 are comparable, volumes are not")
                elif abs(med - 1) > 0.1:
                    F["caveats"].append(f"the Services list volumes are about {med:.2f} times the Endpoints list's, so the two exports cover different ranges; rates and p95 are comparable, volumes are not")
    elif asserted_days and cfg["window"].get("days") and not window_days:
        pass                       # a config default for a client that sometimes sends only spans: nothing to say
    elif asserted_days:
        F["meta"]["analyst_notes"].append("--window-days was given but no Endpoints list was found, so it was ignored")
    F["window"]["aggregate"] = agg
    F["window"]["per_day_allowed"] = bool(agg["days"]) and (agg["source"] == "proved" or (agg["source"] == "asserted" and agg["confidence"] != "contradicted"))
    if agg["days"] and F["window"]["per_day_allowed"]:
        agg_phrase = (f"in the week to {as_of_label}" if agg["days"] == 7 else (f"in the day to {as_of_label}" if agg["days"] == 1 else f"in the {agg['days']} days to {as_of_label}"))
    elif agg["days"]:
        agg_phrase = f"in the export window to {as_of_label}"
    else:
        agg_phrase = ""
    sl = F["window"].get("services_list")
    sl_phrase = f"in the Services-list export, {range_words(sl['days_estimate'])}" if sl and sl.get("days_estimate") else "in the Services-list export, range not proved"
    F["window"]["phrase"] = {"aggregate": agg_phrase, "sample": sample_phrase, "sample_long": sample_phrase_long, "services_list": sl_phrase,
                             "volumes": agg_phrase if volumes_tier == "aggregate" else sample_phrase}
    F["window"]["volumes_tier"] = volumes_tier

    # ---- KPIs (named business operations) and call ratios
    kpis = {}
    for op, svc_rx, res_rx in cfg["_rx"]["kpis"]:
        pairs = [(svc, r) for svc, rows in F["resources"].items() if svc_rx.search(svc) for r in rows if res_rx.search(r["resource"])]
        matched = [r for _, r in pairs if r["class"] == "business"]          # a by-design or health operation never counts against a target
        owned = []
        for r in matched:
            if r.get("kpi") and r["kpi"] != op["key"]:
                warnings.append(f"operations '{r['kpi']}' and '{op['key']}' in org.json both match {r['short']}; it is counted under '{r['kpi']}', the first one")
            else:
                owned.append(r)
        matched = owned
        kpi_tiers = {r["tier"] for r in matched}
        kpi_tier = next(iter(kpi_tiers)) if len(kpi_tiers) == 1 else volumes_tier
        if len(kpi_tiers) > 1:
            matched = [r for r in matched if r["tier"] == volumes_tier]
            F["meta"]["analyst_notes"].append(f"operation '{op['key']}' matches operations on the Endpoints list and in the span sample only; the figures count the Endpoints-list ones")
        calls, failed = sum(r["calls"] for r in matched), sum(r["failed"] for r in matched)
        p95 = max((r["p95_s"] for r in matched if r["p95_s"] is not None), default=None)
        label = op.get("label", op["key"])
        ids_ = {id(r) for r in matched}
        kpi_services = sorted({s for s, r in pairs if id(r) in ids_})
        kpis[op["key"]] = {"label": label, "service": op.get("service", ".*"), "match": op["match"], "critical": bool(op.get("critical")),
                           "failure_known": all(r.get("failure_known", True) for r in matched),
                           "matched": [r["resource"] for r in matched][:10], "calls": calls, "failed": failed, "err": rate(failed, calls), "p95_s": p95,
                           "slo_success_pct": op.get("slo_success_pct"), "slo_p95_s": op.get("slo_p95_s"), "tier": kpi_tier, "services": kpi_services}
        if str(label).startswith("_todo"):
            warnings.append(f"operation '{op['key']}': label still _todo in the config — the resource name is shown instead")
        if not matched and pairs:
            F["meta"]["analyst_notes"].append(f"operation '{op['key']}' in org.json matches only health, probe or by-design operations, which are not counted")
        elif not matched:
            F["meta"]["analyst_notes"].append(f"operation '{op['key']}' in org.json matched no resource in these exports")
        for r in matched:
            r["kpi"] = op["key"]
    F["kpis"] = kpis
    rat = []
    for r, ns, nm, ds, dm in cfg["_rx"]["ratios"]:
        nrows = [(svc, x) for svc, rows in F["resources"].items() if ns.search(svc) for x in rows if nm.search(x["resource"])]
        drows = [(svc, x) for svc, rows in F["resources"].items() if ds.search(svc) for x in rows if dm.search(x["resource"])]
        tiers = {x["tier"] for _, x in nrows + drows}
        if len(tiers) <= 1:
            rtier = next(iter(tiers)) if tiers else volumes_tier
            num, den = sum(x["calls"] for _, x in nrows), sum(x["calls"] for _, x in drows)
        else:
            rtier = "sample"            # never a weekly count over a sample count: both sides from the span sample
            num, den = sum(x["sample"]["calls"] for _, x in nrows), sum(x["sample"]["calls"] for _, x in drows)
            F["meta"]["analyst_notes"].append(f"ratio '{r['label']}': its operations sit on the Endpoints list and in the span sample, so both sides are counted from the span sample")
        for side, side_rows in (("numerator", nrows), ("denominator", drows)):
            if not side_rows:
                note = f"ratio '{r['label']}': its {side} matched no operation of a traced entry service in these exports (ratios count operations of entry services only)"
                F["meta"]["analyst_notes"].append(note)
                warnings.append(note)
        num_svcs = sorted({svc for svc, _ in nrows})
        rat.append({"label": r["label"], "numerator": {"service": r["numerator"].get("service", ".*"), "match": r["numerator"]["match"], "calls": num, "services": num_svcs,
                                                       "token": any(is_auth_resource(x["resource"], cfg) for _, x in nrows),
                                                       "resources": sorted({x["short"] for _, x in nrows})},
                    "denominator": {"service": r["denominator"].get("service", ".*"), "match": r["denominator"]["match"], "calls": den,
                                    "resources": sorted({x["short"] for _, x in drows})},
                    "value": ratio(num, den), "expect_max": r.get("expect_max", 1.2), "source": "config", "tier": rtier})
    for svc in entry_names:
        rows = F["resources"].get(svc, [])
        auth = [x for x in rows if is_auth_resource(x["resource"], cfg)]
        if not auth:
            continue
        num = sum(x["calls"] for x in auth)
        biz = [x for x in rows if x["class"] == "business" and x not in auth]
        den = sum(x["calls"] for x in biz)
        if den:
            rat.append({"label": f"token requests per business call on {svc}", "numerator": {"service": svc, "match": "|".join(x["short"] for x in auth[:3]), "calls": num},
                        "denominator": {"service": svc, "match": "the service's other business operations", "calls": den}, "value": ratio(num, den),
                        "expect_max": T["token_ratio"], "source": "auto", "tier": services[svc]["requests"]["tier"],
                        "token_p95_s": max((x["p95_s"] for x in auth if x["p95_s"] is not None), default=None), "token_resources": [x["short"] for x in auth[:3]]})
    F["ratios"] = rat

    # ---- catalog
    if has["catalog"]:
        rows = inp.catalog
        tag = lambda c: rate(sum(1 for r in rows if r.get(c) and r[c] not in ("[]", "")), len(rows))
        envs = defaultdict(int)
        for r in rows:
            if r.get("env") and r["env"] not in ("[]", ""):
                envs[r["env"].strip('[]"')] += 1
        types = defaultdict(int)
        for r in rows:
            types[r["type"]] += 1
        F["catalog"] = {"tier": "catalog", "rows": len(rows), "capped": len(rows) in readers.CAP_VALUES, "accounts": len({r["account"] for r in rows if r["account"]}),
                        "tag_coverage": {"env": tag("env"), "service": tag("service"), "team": tag("team")},
                        "env_values": dict(sorted(envs.items(), key=lambda kv: -kv[1])[:8]), "counts_by_type": dict(sorted(types.items(), key=lambda kv: -kv[1])[:12])}

    # ---- production scope from the env column
    prod_envs = {e.lower() for e in cfg["org"].get("production_envs", [])}
    missing_is_prod = cfg["org"].get("treat_missing_env_as_production", True)
    env_scope = {"column": bool(caps.get("env")), "production_services": [], "nonprod_services": [], "mixed_services": [], "unknown_services": []}
    for n in entry_names:
        envs = {e.lower(): c for e, c in svc_env.get(n, {}).items()}
        if not envs:
            (env_scope["production_services"] if missing_is_prod else env_scope["unknown_services"]).append(n)
            continue
        prod_rows = sum(c for e, c in envs.items() if _is_prod_env(e, prod_envs, cfg["_rx"]["nonprod"]))
        other_rows = sum(c for e, c in envs.items() if not _is_prod_env(e, prod_envs, cfg["_rx"]["nonprod"]))
        if prod_rows and other_rows:
            env_scope["mixed_services"].append({"service": n, "nonprod": other_rows, "rows": prod_rows + other_rows, "envs": sorted(e for e in envs if not _is_prod_env(e, prod_envs, cfg["_rx"]["nonprod"]))})
            env_scope["production_services"].append(n)
        elif prod_rows:
            env_scope["production_services"].append(n)
        else:
            env_scope["nonprod_services"].append(n)
    env_scope["export_is_nonprod"] = bool(caps.get("env")) and bool(env_scope["nonprod_services"]) and not env_scope["production_services"]
    if env_scope["column"] and not env_scope["export_is_nonprod"]:
        # a non-production host is judged on the calls made from production-env spans (a service that runs under one name in
        # prod and uat is both); a Services-list volume cannot be split, so only its callers are narrowed
        nonprod_set = set(env_scope["nonprod_services"])
        for name in [k for k, d in F["dependencies"].items() if d["class"] in ("nonprod", "public", "telemetry") and d["callers"]]:
            d = F["dependencies"][name]
            if d["source"] != "spans":
                d["callers"] = [dict(c, calls=c["prod"]["calls"], err=rate(c["prod"]["failed"], c["prod"]["calls"]), p95_s=c["prod"]["p95_s"]) if (c.get("prod") or {}).get("calls") else c
                                for c in d["callers"] if c["service"] not in nonprod_set and (c.get("prod") or {}).get("calls", 1)]
                continue
            prod = [dict(c, calls=c["prod"]["calls"], err=rate(c["prod"]["failed"], c["prod"]["calls"]), p95_s=c["prod"]["p95_s"])
                    for c in d["callers"] if (c.get("prod") or {}).get("calls")]
            callers_calls = sum(c["calls"] for c in d["callers"])
            callers_failed = sum((c.get("err") or {}).get("num", 0) for c in d["callers"])
            if sum(c["calls"] for c in prod) == callers_calls:
                continue
            rest_calls, rest_failed = d["calls"] - callers_calls, d["failed"] - callers_failed       # production calls with no caller
            if not prod and rest_calls <= 0:
                del F["dependencies"][name]          # no production-env span calls it: not production reaching UAT
                continue
            d["nonprod_callers"] = [c["service"] for c in d["callers"] if not (c.get("prod") or {}).get("calls")]
            d["callers"] = prod
            d["calls"] = sum(c["calls"] for c in prod) + max(rest_calls, 0)
            d["failed"] = sum(c["err"]["num"] for c in prod) + max(rest_failed, 0)
            d["err"] = rate(d["failed"], d["calls"])
            pool = sorted([x for c in prod for x in prod_durs_edge.get((c["service"], name), [])] + extra_durs.get(name, []))
            d["p95_s"] = r3(pctl(pool, 95) / 1000.0) if pool else None
    if env_scope["export_is_nonprod"]:
        seen_envs = sorted({e for n in env_scope["nonprod_services"] for e in svc_env.get(n, {})})
        F["caveats"].append(f"this export is not production traffic: every traced service runs with env {', '.join(seen_envs)}")
        warnings.append("the export is not production traffic — set org.production_envs or re-export from production")
    elif env_scope["nonprod_services"] or env_scope["mixed_services"]:
        parts = [f"`{n}` runs only as {join_envs(svc_env[n])}" for n in env_scope["nonprod_services"][:3]]
        parts += [f"`{m['service']}` mixes production and {', '.join(m['envs'])} ({m['nonprod']:,} of {m['rows']:,} spans)" for m in env_scope["mixed_services"][:3]
                  if 100.0 * m["nonprod"] / m["rows"] >= T["nonprod_env_share_pct"]]
        if parts:
            F["caveats"].append("this export mixes environments: " + "; ".join(parts) + " — their figures include non-production traffic")
            warnings.append("the span export mixes environments — re-export with an env:prod filter or set org.production_envs")
    F["coverage"]["env_scope"] = env_scope

    # ---- totals and coverage (the domain-table population: entry services counted on the review's volume tier)
    counted = {n for d in F["domains"].values() if d["volume_tier"] == volumes_tier for n in d["counted_services"]}
    all_rows = [r for svc in counted for r in F["resources"].get(svc, [])]
    sample_only = sorted({n for d in F["domains"].values() for n in (d["sample_only_services"] + (d["counted_services"] if d["volume_tier"] == "sample" and volumes_tier != "sample" else []))})
    list_only = sorted({n for d in F["domains"].values() for n in (d.get("services_list_only", []) + (d["counted_services"] if d["volume_tier"] == "services_list" else []))})
    by_reason = defaultdict(list)
    for n_ in list_only:
        by_reason[services[n_].get("list_reason", "absent")].append(n_)
    slp = F["window"]["phrase"]["services_list"]
    names_ = lambda xs: ", ".join(f"`{x}`" for x in xs[:5])
    one_ = lambda xs: len(xs) == 1
    for reason_, where_ in (("absent", " but not in the span sample" + (" or the Endpoints list" if volumes_tier == "aggregate" else "")),
                            ("probe_listed", " but not in the span sample, and on the Endpoints list only through health or probe endpoints"),
                            ("probes_sampled", ", and in the span sample only through health probes")):
        xs = by_reason.get(reason_, [])
        if xs:
            F["caveats"].append(f"{names_(xs)} {'is' if one_(xs) else 'are'} on the Services list{where_}; {'its' if one_(xs) else 'their'} figures come from the Services list "
                                f"({slp}), no operation can be named, and {'it is' if one_(xs) else 'they are'} kept out of the totals")
    present = by_reason.get("markerless", [])
    if present:
        F["caveats"].append(f"{', '.join(f'`{n}` ({services[n].get('unmarked_spans') or services[n]['spans_without_entries']:,} spans)' for n in present[:5])} appear{'s' if len(present) == 1 else ''} in the span sample, but no span "
                            "carries a request marker (a Method, a VERB /path resource, Type web or span.kind server), so the figures come from the Services list "
                            f"({slp}) and are kept out of the totals")
    probe_listed = [n for n in sample_only if any(r.get("week") for r in F["resources"].get(n, []))]
    unlisted = [n for n in sample_only if n not in probe_listed]
    if unlisted and volumes_tier == "aggregate":
        F["caveats"].append(f"{', '.join(f'`{n}`' for n in unlisted[:5])} {'is' if len(unlisted) == 1 else 'are'} not in the Endpoints list (it carries HTTP endpoints only); "
                            f"{'its' if len(unlisted) == 1 else 'their'} figures come from the span sample and are kept out of the weekly totals")
    if probe_listed and volumes_tier == "aggregate":
        F["caveats"].append(f"{', '.join(f'`{n}`' for n in probe_listed[:5])} {'is' if len(probe_listed) == 1 else 'are'} on the Endpoints list only through health or probe endpoints; "
                            f"{'its' if len(probe_listed) == 1 else 'their'} figures come from the span sample and are kept out of the weekly totals")
    for name_, d_ in F["dependencies"].items():
        d_["allowed"] = matches_any(cfg["_rx"].get("allowed_egress", []), name_.lower())
    F["totals"] = {"tier": volumes_tier, "requests": sum(r["calls"] for r in all_rows), "failed": sum(r["failed"] for r in all_rows),
                   "err": rate(sum(r["failed"] for r in all_rows), sum(r["calls"] for r in all_rows)),
                   "business_requests": sum(r["calls"] for r in all_rows if r["class"] == "business"), "business_failed": sum(r["failed"] for r in all_rows if r["class"] == "business"),
                   "err_business": rate(sum(r["failed"] for r in all_rows if r["class"] == "business"), sum(r["calls"] for r in all_rows if r["class"] == "business")),
                   "noise_requests": sum(r["calls"] for r in all_rows if r["class"] != "business"), "services_entry": len(entry_names), "services_counted": len(counted),
                   "services_sample_only": sample_only, "services_all": len(services),
                   "services_inferred": len(inp.inferred), "endpoints": len(all_rows), "span_rows": rows_total, "traces": len(entries_by_trace) if caps.get("trace_id") else None,
                   "dependencies": len(deps), "domains": len([d for d in F["domains"].values() if not d.get("empty")])}
    has_tree = bool(caps.get("trace_id")) and bool(caps.get("parent_id"))
    failure_signal = bool((caps.get("error") if fail_rule == "flag_only" else (caps.get("status") or caps.get("error"))) or has["endpoints"])
    if fail_rule == "flag_only" and caps.get("status") and not caps.get("error"):
        warnings.append("failure.rule is flag_only but the spans export has no Error column (only Status Code): span-sample failures are not recorded — add the Error column")
    if fail_rule != "5xx_or_flag" and has["endpoints"]:
        F["caveats"].append(f"this review counts {rule_words(fail_rule)} as failures in figures from the span sample; weekly figures from the Endpoints list use Datadog's own error count (5xx and application errors)")
        warnings.append(f"failure.rule {fail_rule} cannot change the Endpoints list's error counts: weekly figures still count 5xx and application errors only")
    F["coverage"].update({"inputs": has, "columns": {k: bool(caps.get(k)) for k in ("ts", "trace_id", "span_id", "parent_id", "status", "error", "operation", "env", "host", "type", "peer", "version")},
                          "entry_rule": caps.get("entry_rule"), "volumes_tier": volumes_tier, "failure_signal": failure_signal,
                          "monitors_known": has_monitor_counts, "host_visibility": host_visibility, "child_spans_present": bool(children_by_trace) or any(not r["entry"] for r in inp.spans),
                          "capabilities": {"weekly_volumes": has["endpoints"], "services_list": has["services"], "dependencies": graph_method if graph_method != "none" else ("inferred" if has["inferred"] else ("outbound" if outbound_spans else "none")),
                                           "inferred_list": has["inferred"], "request_anatomy": any(a["children_per_request"] for a in F["anatomy"].values()), "trace_tree_columns": has_tree,
                                           "operation_names": bool(caps.get("operation")), "status_codes": bool(caps.get("status")), "env": bool(caps.get("env")), "catalog": has["catalog"]},
                          "unlock_hints": _unlock_hints(has, caps, graph_method, any(a["children_per_request"] for a in F["anatomy"].values()))})
    if not failure_signal:
        F["caveats"].append("the spans export carries no Error column (the configured rule counts the error flag only), so failures are not recorded in this review" if fail_rule == "flag_only"
                            else "the spans export carries no Status Code or Error column, so failures are not recorded in this review")
    # init helpers
    offs = sorted(inp.tz_offsets.items(), key=lambda kv: -kv[1])
    F["meta"]["utc_offset_guess"] = offs[0][0].replace("Z", "+00:00") if offs and offs[0][0] != "?" else "+00:00"
    F["meta"]["timezone_guess"] = "UTC" if F["meta"]["utc_offset_guess"] == "+00:00" else ""
    regs = defaultdict(int)
    for name, d in deps.items():
        rd = registrable_domain(name)
        if rd and d["class"] in ("public", "internal", "nonprod"):
            regs[rd] += 1
    F["meta"]["internal_domains_guess"] = sorted(k for k, v in regs.items() if v >= 2)
    own = {x.lower().lstrip("*.") for x in cfg["org"].get("internal_domains", [])}
    public_unconfirmed = sorted({registrable_domain(nm) for nm, d in deps.items() if d["class"] == "public" and registrable_domain(nm) in F["meta"]["internal_domains_guess"]} - own)
    if public_unconfirmed:
        F["meta"]["analyst_notes"].append("hosts under " + ", ".join(public_unconfirmed[:4]) + " are counted as public internet; if a domain is the client's own, list it under org.internal_domains")
    F["meta"]["warnings"] = list(warnings)
    return F


def join_envs(envs):
    return ", ".join(sorted(envs))


# ---------------------------------------------------------------- helpers
def _has_signal(r, rule):
    """The span records whether it failed, under the configured rule (flag_only reads the error flag only)."""
    return r["error"] is not None if rule == "flag_only" else (r["status"] is not None or r["error"] is not None)


def _failed(r, rule):
    if rule == "flag_only":
        return bool(r["error"])
    if rule == "non_2xx":
        return (r["status"] is not None and not 200 <= r["status"] < 400) or bool(r["error"])
    return (r["status"] is not None and r["status"] >= 500) or bool(r["error"])


def _app_below(r, kids, integration, cfg, depth=4):
    """The app that runs a consume span: the most frequent service, at the nearest level below it, that is the app's own (not
    an integration library, a queue library or a host). The search stays in the consuming process: it never goes below a
    client, producer or consume span, whose children run elsewhere."""
    level = kids.get(r["span_id"], [])
    for _ in range(depth):
        found, nxt = Counter(), []
        for k in level:
            s = k["service"]
            if s.lower() not in integration and not is_integration_name(s, cfg) and not is_bare_queue(s) and not is_hostlike(s):
                found[s] += 1
            outbound = (k["kind"] or "") in ("client", "producer", "consumer") or consumes(k, cfg) or (k["type"] or "").lower() in CLIENT_TYPES \
                or (bool(k["operation"]) and matches_any(cfg["_rx"]["client_ops"], k["operation"]))
            if k["span_id"] and not outbound:
                nxt.extend(kids.get(k["span_id"], []))
        if found:
            return max(sorted(found), key=lambda s: found[s])
        level = nxt
    return None


def _outbound_span(par, by_id, integration, cfg):
    """A span that calls out of its service: client or producer kind, a client operation, an integration library, or an
    rpc/http/queue-typed span inside its own service's request. A server or consume span (a gRPC server, a Kafka listener)
    is not, even though Java and Python give client and server spans the same type."""
    k = par["kind"] or ""
    if k in ("client", "producer"):
        return True
    if k in ("server", "consumer") or par["entry"] or consumes(par, cfg):
        return False
    if par["service"].lower() in integration or (bool(par["operation"]) and matches_any(cfg["_rx"]["client_ops"], par["operation"])):
        return True
    if bool(par["operation"]) and matches_any(cfg["_rx"]["entry_ops"], par["operation"]) and (par["type"] or "").lower() in SERVER_TYPES | {""}:
        return False
    if (par["type"] or "").lower() in CLIENT_TYPES | {"rpc", "grpc"}:
        gp = by_id.get(par["parent_id"]) if par["parent_id"] else None
        return gp is not None and gp["service"] == par["service"]
    return False


def _caller_of(r, by_id, integration, skip=None):
    """Nearest ancestor that is not an integration library (pdo, curl, http-client), nor a span ``skip`` says is one (a
    consuming app's own producer span): the service that made the call."""
    p = by_id.get(r["parent_id"])
    hops = 0
    while p is not None and (p["service"].lower() in integration or (skip is not None and skip(p))) and p["parent_id"] and hops < 12:
        p = by_id.get(p["parent_id"])
        hops += 1
    return p


def _entry_ancestor(r, by_id):
    """The entry span this child span runs under (walking parents), or None when the chain leaves the sample."""
    p = by_id.get(r["parent_id"]) if r["parent_id"] else None
    hops = 0
    while p is not None and not p["entry"] and hops < 64:
        p = by_id.get(p["parent_id"]) if p["parent_id"] else None
        hops += 1
    return p


def _new_resource(svc, resource, cfg):
    short = short_resource(resource, cfg)
    return {"resource": resource, "short": short, "redacted": redact(short), "class": "business", "rule": "",
            "calls": 0, "failed": 0, "err": rate(0, 0), "p95_s": None, "p95_source": None, "status_mix": {}, "week": None,
            "sample": {"calls": 0, "failed": 0, "_durs": [], "capped": False}, "monitors": 0, "kpi": None, "source": "spans", "tier": "sample"}


def _sum_mix(rows):
    mix = defaultdict(int)
    for r in rows:
        for k, v in r["status_mix"].items():
            mix[k] += v
    return dict(mix)


def _noise_by_class(resources):
    out = defaultdict(lambda: {"requests": 0, "failed": 0, "resources": 0})
    for rows in resources.values():
        for r in rows:
            if r["class"] != "business":
                o = out[r["class"]]
                o["requests"] += r["calls"]
                o["failed"] += r["failed"]
                o["resources"] += 1
    return dict(out)


_VERB_ONLY = re.compile(r"(?i)^(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)$")
_SCRAPE = re.compile(r"(?i)/(metrics|prometheus)(/[a-z0-9_.-]+)?/?$")
PROBE_PERIODS = (1, 2, 3, 5, 10, 15, 20, 30, 60)          # the probe periods kubelets and load balancers are configured with


def _probes(endpoints, days, cfg):
    """Health-check endpoints whose request count implies a fixed cadence over ``days``: the implied interval sits within
    0.05 % (at least 2 ms) of a multiple of 0.25 s. Real traffic almost never does; probes at 3.000 s or 5.000 s do."""
    secs = days * 86400
    best = {}
    for e in endpoints:
        if e["calls"] < max(1000, secs // 120):
            continue
        if not matches_any(cfg["_rx"]["health"], e["resource"]) or _SCRAPE.search(e["resource"] or ""):
            continue                      # a metrics scrape follows the number of targets, not a cadence
        interval = secs / e["calls"]
        if interval > 120:
            continue
        fit = None
        for k in range(1, 13):                  # k probers (pods or load-balancer nodes) at one real period
            period = interval * k
            per = min(PROBE_PERIODS, key=lambda x: abs(x - period))
            if abs(period - per) <= max(per * 0.0005, 0.002):
                fit = (k, per)
                break
        if fit:
            row = {"service": e["service"], "resource": e["resource"], "requests": e["calls"], "implied_interval_s": round(interval, 3), "probers": fit[0], "period_s": fit[1]}
            if e["service"] not in best or e["calls"] > best[e["service"]]["requests"]:
                best[e["service"]] = row            # liveness and readiness of the same pods are one cadence
    return sorted(best.values(), key=lambda p: (-p["requests"], p["service"]))[:8]


def _p95_of(rows):
    durs = sorted(r["ms"] for r in rows if r.get("ms") is not None)
    return r3(pctl(durs, 95) / 1000.0) if durs else None


def _is_prod_env(env, prod_envs, nonprod_rx=None):
    """A production env as configured, or one with a region suffix of up to four parts ('prod-eu', 'prod-us-east-1',
    'production-ap-southeast-2'); a suffix that carries a non-production marker ('prod-staging', 'prod-uat') is not."""
    e = (env or "").lower()
    if e in prod_envs:
        return True
    for p in prod_envs:
        m = re.match(rf"^{re.escape(p)}((?:[-_.][a-z0-9]{{1,12}}){{1,4}})$", e)
        if m and len(e) <= 40:
            return not (nonprod_rx is not None and nonprod_rx.search(m.group(1)))
    return False


def _host_of(resource):
    m = re.search(r"(?i)(?:https?://)?([a-z0-9-]+(?:\.[a-z0-9-]+)+)", resource or "")
    return m.group(1).lower() if m else ""


def _in_process(k, cfg):
    """A child span that is the service's own in-process work: a server-typed or server/internal-kind span, or a known
    framework operation (spring.handler, aspnet_core_mvc.request, a middleware)."""
    t, kind = (k.get("type") or "").lower(), (k.get("kind") or "").lower()
    return t in ("web", "server", "http_server") or kind in ("server", "internal") or (bool(k.get("operation")) and matches_any(cfg["_rx"].get("inprocess_ops", []), k["operation"]))


def _node_groups(entry_names, services, cfg):
    """Nodes of one application, compared on business requests (health checks hit every node equally and hide an idle one)."""
    groups = defaultdict(list)
    source = {}
    for g, rx in cfg["_rx"]["node_groups"].items():
        for n in entry_names:
            if rx.search(n):
                groups[g].append(n)
                source[g] = "config"
    if not groups:
        stems, numbered = defaultdict(list), set()
        for n in entry_names:
            m = None if _VERSION_SUFFIX.search(n) else re.match(r"^(.*?)[-_]?(\d+)$", n)
            stem = m.group(1) if m else n
            if m:
                numbered.add(stem)
            stems[stem].append(n)
        for stem, members in stems.items():
            if len(members) >= 2 and stem in numbered:
                groups[humanise(stem)] = members
                source[humanise(stem)] = "auto"
    out = {}
    prio = {"aggregate": 0, "sample": 1, "services_list": 2}
    for g, all_names in groups.items():
        if len(all_names) < 2:
            continue
        for n in all_names:
            services[n]["node_group"] = g
        tiers = defaultdict(list)
        for n in all_names:
            tiers[services[n]["requests"]["tier"]].append(n)
        tier = min(tiers, key=lambda t: (-len(tiers[t]), prio.get(t, 9)))
        names = tiers[tier]                      # only nodes whose volumes come from the same export are compared
        apart = sorted(n for n in all_names if n not in names)
        if len(names) < 2:
            out[g] = {"source": source[g], "names": sorted(all_names), "members": {}, "members_all": {}, "comparable": False, "not_comparable": apart, "tier": tier,
                      "err_pct": {}, "p95_s": {}, "p95_worst_s": {}, "p95_kind": None, "lowest": None, "lowest_requests": 0, "mean_others": 0,
                      "gap": rate(0, 0), "idle": False, "share_of_total": rate(0, 0)}
            continue
        basis = "business" if any(services[n]["requests"]["business"] for n in names) else "all"
        members = {n: services[n]["requests"][basis] for n in names}
        ordered = sorted(members.items(), key=lambda kv: (kv[1], kv[0]))
        lowest, others = ordered[0], ordered[1:]
        sum_others = sum(v for _, v in others)
        mean_others = sum_others / len(others)
        gap = rate(max(0, sum_others - len(others) * lowest[1]), sum_others)
        out[g] = {"source": source[g], "basis": basis, "names": sorted(all_names), "comparable": True, "not_comparable": apart, "tier": tier,
                  "members": dict(sorted(members.items())), "members_all": {n: services[n]["requests"]["all"] for n in sorted(names)},
                  "err_pct": {n: services[n]["err"]["business"]["pct"] for n in names},
                  "p95_s": {n: services[n]["p95_s"] for n in names}, "p95_worst_s": {n: services[n]["p95_worst_s"] for n in names}, "p95_kind": "service" if all(services[n]["p95_kind"] == "service" for n in names) else "mixed",
                  "lowest": lowest[0], "lowest_requests": lowest[1], "mean_others": round(mean_others, 1),
                  "gap": gap, "idle": bool(mean_others) and lowest[1] < mean_others * cfg["thresholds"]["node_idle_share_pct"] / 100.0,
                  "share_of_total": rate(sum(services[n]["requests"]["all"] for n in names), sum(s["requests"]["all"] for s in services.values()))}
    return out


def _anatomy(spans, entries_by_trace, children_by_trace, by_id, services, caps, cfg, minutes, noise_keys):
    """Per entry service: children per request by kind, repeated children (per request: the median request, so one pathological
    request cannot invent an N+1), framework share, bursts, entry latency, slow entries without children. Child spans count for a service only when they
    run under that service's own entry span (a downstream service's database calls are its own), and health-check or scanner
    requests are left out of the denominator."""
    import statistics
    T = cfg["thresholds"]
    integration = set(cfg["_integration"]) | {r["service"].lower() for r in spans if is_integration_name(r["service"], cfg)}
    integration -= {r["service"].lower() for r in spans if queue_consumer_name(r["service"], r, cfg)}
    out = {}
    if not spans:
        return out
    tree_rows = lambda rows: all((x.get("_caps") or caps).get("parent_id") for x in rows)
    noise = lambda e: (e["service"], match_key(e["resource"], e["method"])) in noise_keys
    kind_of = lambda k: "secret_store" if is_secret_store(k.get("peer"), cfg) else child_kind(k["service"], k["resource"], k["type"], cfg)
    callee_of = {r["parent_id"]: r["service"] for r in spans if r["entry"] and r["parent_id"]}
    call_target = lambda k: ((k.get("peer") or _host_of(k["resource"]) or callee_of.get(k.get("span_id"), "")).lower() if kind_of(k) == "http" else "")   # HTTP fan-out only
    per_svc_entries = defaultdict(list)
    for r in spans:
        if r["entry"] and not noise(r):
            per_svc_entries[r["service"]].append(r)
    have_tree = bool(caps.get("trace_id")) and bool(entries_by_trace)
    have_parents = bool(caps.get("parent_id")) and bool(by_id)
    for svc, ents in per_svc_entries.items():
        if services.get(svc, {}).get("role") != "entry":
            continue
        n = len(ents)
        durs = sorted(e["ms"] for e in ents)
        lat = percentiles(durs, (25, 50, 75, 95, 99))
        a = {"tier": "sample", "entries": n, "minutes": round(minutes, 1), "entry_latency_ms": {k: r1(v) for k, v in lat.items()},
             "bimodal": bool(lat["p25"]) and lat["p75"] / max(lat["p25"], 0.001) >= 8 and lat["p75"] >= 1000,
             "framework_share": rate(services[svc]["spans"]["framework_rows"], services[svc]["spans"]["rows"]), "children_per_request": {}, "repeated_children": [],
             "secret_reads": 0, "secret_distinct": 0, "cache_table_queries": 0, "bursts": [], "slow_without_children": None, "traces": 0}
        if have_tree and n >= min(T["min_entry_spans_for_anatomy"], 50):
            # one unit per request: with Parent IDs every entry span gathers the children that run under it; without them a
            # trace's children are shared evenly among this service's entries in that trace (a service called 12 times in one
            # trace is 12 requests, not one request with 12x the work)
            kinds, units, secrets, slow_no_kids, slow_total, bursts_src = defaultdict(int), [], set(), 0, 0, defaultdict(list)
            slow_cut = max(2500.0, lat["p75"])
            traces_seen, skipped_traces = set(), 0
            for tid, tr_ents in entries_by_trace.items():
                mine = [e for e in tr_ents if e["service"] == svc and not noise(e)]
                if not mine:
                    continue
                single_service = len({e["service"] for e in tr_ents}) == 1
                parents_here = have_parents and tree_rows(tr_ents + children_by_trace.get(tid, []))
                if not parents_here and not single_service:
                    skipped_traces += 1
                    continue                      # its children cannot be told apart: neither its requests nor its children count
                traces_seen.add(tid)
                by_entry = {id(e): [] for e in mine}
                shared = []
                for k in children_by_trace.get(tid, []):
                    if (k["service"] == svc or (k["service"].lower() in integration and not k.get("peer"))) and _in_process(k, cfg):
                        continue                  # spring.handler, a middleware span: the service's own work, not a call
                    if parents_here:
                        anc = _entry_ancestor(k, by_id)
                        if anc is not None:
                            if anc["service"] != svc or id(anc) not in by_entry:
                                continue
                            by_entry[id(anc)].append(k)
                            continue
                        if not single_service:
                            continue
                        if len(mine) == 1:
                            by_entry[id(mine[0])].append(k)
                        else:
                            shared.append(k)
                    else:
                        if not single_service:
                            continue              # without parents, another service's children cannot be told from ours
                        if services.get(k["service"], {}).get("role") == "entry" and k["service"] != svc:
                            continue
                        shared.append(k)
                for k in [k for ks in by_entry.values() for k in ks] + shared:
                    kind = kind_of(k)
                    kinds[kind] += 1
                    if kind == "secret_store":
                        secrets.add(k["resource"])
                        a["secret_reads"] += 1
                        target = k.get("peer") or _host_of(k["resource"]) or (k["service"] if k["service"] != svc and not is_integration_name(k["service"], cfg) else "") \
                            or (k["service"] if k["service"] != svc else "") or "the secret store"
                        bursts_src[target].append(k["ts"])
                    if kind == "db" and re.search(r"(?i)\b(cache|cache_locks|sessions?)\b", k["resource"] or ""):
                        a["cache_table_queries"] += 1
                weight_shared = 1.0 / len(mine)
                for e in mine:
                    counts = defaultdict(float)
                    for k in by_entry[id(e)]:
                        counts[(kind_of(k), k["service"], k["resource"], call_target(k))] += 1
                    for k in shared:
                        counts[(kind_of(k), k["service"], k["resource"], call_target(k))] += weight_shared
                    units.append(counts)
                    if e["ms"] >= slow_cut:
                        slow_total += 1
                        if not by_entry[id(e)] and not shared:
                            slow_no_kids += 1
            denom = max(len(units), 1)
            a["traces"] = len(traces_seen)
            a["traces_skipped"] = skipped_traces
            a["requests_with_tree"] = len(units)
            a["children_per_request"] = {k: ratio(v, denom, 2) for k, v in sorted(kinds.items())}
            keys = {k for c in units for k in c}
            repeated = []
            for key in keys:
                if key[0] == "http" and not key[3] and _VERB_ONLY.match(key[2] or ""):
                    continue                  # 'GET' to an unknown host: the export cannot say these are the same call
                counts = sorted(round(u.get(key, 0), 3) for u in units)
                median = statistics.median(counts)
                median = int(median) if float(median).is_integer() else round(median, 1)
                at_or_above = sum(1 for c in counts if c >= T["repeat_child_per_entry"])
                share = rate(at_or_above, denom)
                by_median = median >= T["repeat_child_per_entry"]
                if by_median or share["pct"] >= 20:
                    mx = counts[-1]
                    repeated.append({"kind": key[0], "service": key[1], "resource": short_resource(key[2], cfg), "target": key[3], "per_entry": round(sum(counts) / denom, 1),
                                     "median": median, "by_median": by_median, "max": int(mx) if float(mx).is_integer() else round(mx, 1),
                                     "traces_at_or_above": share, "n": int(round(sum(counts)))})
            a["repeated_children"] = sorted(repeated, key=lambda x: (not x["by_median"], -x["median"], -x["n"], x["resource"]))[:5]
            per_target = defaultdict(list)                # HTTP target -> calls in each request that calls it
            for u in units:
                calls_t = defaultdict(float)
                for key, v in u.items():
                    if key[0] == "http" and key[3]:
                        calls_t[key[3]] += v
                for tgt, v in calls_t.items():
                    per_target[tgt].append(v)
            a["http_targets"] = sorted(({"target": tgt, "requests": len(vs), "median": statistics.median(vs)} for tgt, vs in per_target.items()),
                                       key=lambda x: (-x["requests"], x["target"]))[:60]
            a["secret_distinct"] = len(secrets)
            a["secret_reads_per_request"] = ratio(a["secret_reads"], denom, 2)
            a["slow_without_children"] = rate(slow_no_kids, slow_total)
            a["slow_cut_ms"] = round(slow_cut)
            for target, stamps in sorted(bursts_src.items()):
                if len(stamps) < 50:
                    continue
                secs = defaultdict(int)
                mins = set()
                for ts in stamps:
                    if ts:
                        secs[ts.second] += 1
                        mins.add(ts.replace(second=0, microsecond=0))
                if len(mins) < 5:
                    continue
                top = sorted(secs.values(), reverse=True)[:3]
                conc = rate(sum(top), len(stamps))
                if conc["pct"] >= T["burst_concentration_pct"]:
                    a["bursts"].append({"target": target, "calls": len(stamps), "per_minute": round(len(stamps) / max(len(mins), 1), 1), "concentration": conc,
                                        "seconds_of_minute": sorted(k for k, v in secs.items() if v in top)[:3]})
        out[svc] = a
    return out


def _unlock_hints(has, caps, graph_method, anatomy_unlocked=False):
    hints = []
    if not has["endpoints"]:
        hints.append("export APM › Software Catalog › Endpoints (7 days) to get complete weekly volumes and per-endpoint failure counts")
    if not has["services"]:
        hints.append("export APM › Software Catalog › Services to get monitor and SLO coverage, env tags and service-level p95")
    if not has["inferred"]:
        hints.append("export the Services list with inferred services shown to get the hosts your services call (dependency health, non-prod and public egress)")
    if not caps.get("trace_id"):
        hints.append("add Trace ID, Span ID and Parent ID columns to the spans export to get the request-anatomy diagram and caller attribution")
    elif not caps.get("parent_id"):
        hints.append("add the Parent ID column to the spans export for exact caller attribution")
    elif not anatomy_unlocked:
        hints.append("the spans export holds entry spans only — export spans of every type (no span-type filter) to get the request-anatomy diagram")
    if not caps.get("operation"):
        hints.append("add the Operation Name column to the spans export so live-graph recipes carry the exact trace.OPERATION metric name")
    if not caps.get("status") and not caps.get("error"):
        hints.append("add Status Code (or Error) to the spans export so failures can be counted from the sample")
    if not caps.get("env"):
        hints.append("add Env to the spans export to confirm production scope")
    if graph_method == "none" and not has["inferred"]:
        hints.append("dependencies could not be drawn: add Parent ID or peer.service to the spans export, or export the inferred services list")
    return hints


def _tz_label(tz, when):
    try:
        local = when.astimezone(tz)
        name = local.tzname() or str(tz)
        if local.utcoffset() is not None and local.utcoffset().total_seconds() == 0:
            return "UTC"
    except Exception:
        name = str(tz)
    return name
