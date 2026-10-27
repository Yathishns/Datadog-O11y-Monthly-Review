"""Per-org configuration: defaults.json merged with the client's org.json, validated, and drafted from data by ``init``.

Keys starting with '_' (``_comment``, ``_todo``, ``_confidence``) are ignored everywhere. Unknown keys are an error that names
the nearest valid key, so a typo never silently disables a rule.
"""
import copy
import difflib
import hashlib
import json
import os
import re
from datetime import timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))

CONFIG_KEYS = {
    "org": {"name", "short", "prepared_for", "prepared_by", "timezone", "utc_offset", "production_envs", "treat_missing_env_as_production", "internal_domains"},
    "report": {"title_template", "max_findings", "diagrams", "delivered_alongside", "closing_note", "thesis"},
    "window": {"days"},
    "columns": {"aliases", "units", "date_format"},
    "services": {"include", "exclude", "display", "aliases", "integration", "node_groups", "operations", "default_operation"},
    "domains": None,          # list
    "operations": None,       # list of named business operations (KPIs)
    "ratios": None,           # list
    "noise": {"health", "scanner", "static", "framework", "by_design", "not_noise"},
    "hosts": {"nonprod_markers", "telemetry", "allowed_egress", "internal_suffixes", "production"},
    "failure": {"rule"},
    "thresholds": None,       # dict of numbers, validated against defaults
    "prompts": {"mask_resources"},
    "previous": None,
}
TIERS = ("customer", "clinical", "staff", "internal")
DOMAIN_KEYS = {"key", "label", "icon", "audience", "tier", "services", "description"}
OPERATION_KEYS = {"key", "label", "service", "match", "critical", "slo_success_pct", "slo_p95_s"}
RATIO_KEYS = {"label", "numerator", "denominator", "expect_max"}


class ConfigError(Exception):
    pass


def _check_keys(item, allowed, where):
    if not isinstance(item, dict):
        raise ConfigError(f"{where} must be an object")
    for k in item:
        if k not in allowed and not str(k).startswith("_"):
            raise ConfigError(f"unknown config key '{where}.{k}'{_nearest(k, sorted(allowed))}")


def load_defaults():
    with open(os.path.join(HERE, "defaults.json"), encoding="utf-8") as fh:
        return json.load(fh)


def _strip_private(obj):
    if isinstance(obj, dict):
        return {k: _strip_private(v) for k, v in obj.items() if not str(k).startswith("_")}
    if isinstance(obj, list):
        return [_strip_private(v) for v in obj]
    return obj


def _drop_nulls(obj, path=""):
    """A null means 'use the default': the key is removed, except window.days and previous, where null has a meaning of its own."""
    if isinstance(obj, dict):
        return {k: _drop_nulls(v, f"{path}.{k}" if path else k) for k, v in obj.items() if v is not None or (f"{path}.{k}" if path else k) in ("window.days", "previous")}
    if isinstance(obj, list):
        return [_drop_nulls(v, path) for v in obj]
    return obj


def _nearest(key, options):
    m = difflib.get_close_matches(key, options, n=1)
    return f" (did you mean '{m[0]}'?)" if m else ""


def empty_config():
    return {
        "org": {"name": "", "short": "", "prepared_for": "", "prepared_by": "", "timezone": "UTC", "utc_offset": "+00:00",
                "production_envs": ["prod", "prd", "production", "live"], "treat_missing_env_as_production": True, "internal_domains": []},
        "report": {"title_template": "{org} — Observability Review, {month}", "max_findings": 8, "diagrams": "both", "delivered_alongside": "", "closing_note": "", "thesis": ""},
        "window": {"days": None},
        "columns": {"aliases": {}, "units": {}, "date_format": None},
        "services": {"include": [".*"], "exclude": [], "display": {}, "aliases": {}, "integration": [], "node_groups": {}, "operations": {}, "default_operation": ""},
        "domains": [],
        "operations": [],
        "ratios": [],
        "noise": {"health": [], "scanner": [], "static": [], "framework": [], "by_design": [], "not_noise": []},
        "hosts": {"nonprod_markers": [], "telemetry": [], "allowed_egress": [], "internal_suffixes": [], "production": []},
        "failure": {"rule": "5xx_or_flag"},
        "thresholds": {},
        "prompts": {"mask_resources": True},
        "previous": "auto",
    }


def load_config(path=None):
    """Return the effective config: defaults merged with the file at ``path`` (or an empty org when None)."""
    defaults = load_defaults()
    cfg = empty_config()
    cfg["thresholds"] = dict(defaults["thresholds"])
    cfg["_defaults"] = defaults
    cfg["_path"] = path
    cfg["_sha256"] = None
    if not path:
        return _compile(cfg)
    try:
        raw_text = read_text_file(path)
        user = json.loads(raw_text)
    except FileNotFoundError:
        raise ConfigError(f"config not found: {path}")
    except (IsADirectoryError, PermissionError) as e:
        raise ConfigError(f"cannot read config {path}: {e}")
    except UnicodeDecodeError:
        raise ConfigError(f"{path} is not UTF-8 text — save it as UTF-8")
    except json.JSONDecodeError as e:
        raise ConfigError(f"config is not valid JSON ({path}): {e}")
    cfg["_sha256"] = hashlib.sha256(raw_text.encode("utf-8")).hexdigest()
    if not isinstance(user, dict):
        raise ConfigError("the config must be a JSON object")
    user = _drop_nulls(_strip_private(user))
    for key, value in user.items():
        if key not in CONFIG_KEYS:
            raise ConfigError(f"unknown config key '{key}'{_nearest(key, list(CONFIG_KEYS))}")
        allowed = CONFIG_KEYS[key]
        if allowed is None:
            if key in ("domains", "operations", "ratios") and not isinstance(value, list):
                raise ConfigError(f"config key '{key}' must be a list of objects")
            if key == "thresholds" and not isinstance(value, dict):
                raise ConfigError("config key 'thresholds' must be an object")
            if key == "thresholds":
                for t, v in value.items():
                    if t == "max_findings":
                        raise ConfigError("max_findings is not a threshold — set report.max_findings instead")
                    if t not in defaults["thresholds"]:
                        raise ConfigError(f"unknown threshold '{t}'{_nearest(t, list(defaults['thresholds']))}")
                    if isinstance(v, bool) or not isinstance(v, (int, float)):
                        raise ConfigError(f"threshold '{t}' must be a number")
                cfg["thresholds"].update(value)
            else:
                cfg[key] = value
            continue
        if not isinstance(value, dict):
            raise ConfigError(f"config key '{key}' must be an object")
        for sub in value:
            if sub not in allowed:
                raise ConfigError(f"unknown config key '{key}.{sub}'{_nearest(sub, list(allowed))}")
        cfg[key].update(value)
    return _compile(cfg)


def read_text_file(path):
    """A text file saved as UTF-8 (with or without a BOM) or as UTF-16 with a BOM (PowerShell 5.1's '>'); anything else raises
    UnicodeDecodeError so the caller can say how to save it."""
    with open(path, "rb") as fh:
        raw = fh.read()
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16")
    return raw.decode("utf-8-sig")


def _opt_str(value, where):
    if value is not None and not isinstance(value, str):
        raise ConfigError(f"{where} must be a string, got {type(value).__name__}")
    return value


def _opt_bool(value, where):
    if value is not None and not isinstance(value, bool):
        raise ConfigError(f"{where} must be true or false (without quotes), got {value!r}")
    return value


def _rx(pattern, where):
    if not isinstance(pattern, str):
        raise ConfigError(f"{where} must be a string pattern, got {type(pattern).__name__}")
    try:
        return re.compile(pattern)
    except re.error as e:
        raise ConfigError(f"invalid regex in {where}: {pattern!r} ({e})")


def _safe_rx(key):
    """services.operations keys are exact service names or full-match patterns; a name that is not a valid pattern is matched literally."""
    try:
        return re.compile(key)
    except re.error:
        return re.compile(re.escape(key))


def _str_list(value, where, example='["^GET /health$"]'):
    if not isinstance(value, list) or not all(isinstance(p, str) for p in value):
        raise ConfigError(f"{where} must be a list of strings, e.g. {example}")
    return value


def _str_dict(value, where):
    if not isinstance(value, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in value.items()):
        raise ConfigError(f"{where} must be an object mapping names to strings")
    return value


UNIT_NAMES = {"ns": "ns", "us": "us", "µs": "us", "μs": "us", "ms": "ms", "s": "s"}


def _compile(cfg):
    """Validate types and values, and pre-compile every pattern into cfg['_rx']."""
    import string
    d = cfg["_defaults"]
    for k in ("name", "short", "prepared_for", "prepared_by", "timezone", "utc_offset"):
        _opt_str(cfg["org"].get(k), f"org.{k}")
    _opt_bool(cfg["org"].get("treat_missing_env_as_production"), "org.treat_missing_env_as_production")
    for k in ("delivered_alongside", "closing_note", "thesis"):
        _opt_str(cfg["report"].get(k), f"report.{k}")
    _opt_bool(cfg["prompts"].get("mask_resources"), "prompts.mask_resources")
    _opt_str(cfg["services"].get("default_operation"), "services.default_operation")
    if not cfg["org"].get("short"):
        cfg["org"]["short"] = slug(cfg["org"].get("name") or "org") or "org"
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,59}", cfg["org"]["short"]):
        raise ConfigError(f"org.short must be lower-case letters, digits and hyphens (it names the output folder), e.g. '{slug(cfg['org']['short']) or 'org'}'")
    off = cfg["org"].get("utc_offset")
    if off:
        m = re.match(r"^([+-])(\d{2}):?(\d{2})$", off.strip())
        if not m or int(m.group(2)) > 23 or int(m.group(3)) > 59:
            raise ConfigError(f"org.utc_offset must look like '+08:00' or '-05:30', got {off!r}")
    if cfg["report"].get("diagrams") not in ("both", "code", "prompt"):
        raise ConfigError("report.diagrams must be 'both', 'code' or 'prompt'")
    if cfg["failure"].get("rule") not in ("5xx_or_flag", "flag_only", "non_2xx"):
        raise ConfigError("failure.rule must be '5xx_or_flag', 'flag_only' or 'non_2xx'")
    tt = cfg["report"].get("title_template")
    if not isinstance(tt, str):
        raise ConfigError("report.title_template must be a string")
    try:
        fields = {f for _, f, _, _ in string.Formatter().parse(tt) if f is not None}
    except ValueError as e:
        raise ConfigError(f"report.title_template is not a valid template: {e}")
    if fields - {"org", "month"}:
        raise ConfigError("report.title_template may only use {org} and {month}")
    if any(spec or conv for _, f, spec, conv in string.Formatter().parse(tt) if f is not None):
        raise ConfigError("report.title_template may only use {org} and {month}, without format specs or conversions")
    mf = cfg["report"].get("max_findings")
    if isinstance(mf, bool) or not isinstance(mf, int) or mf <= 0:
        raise ConfigError("report.max_findings must be a positive whole number")
    prev = cfg.get("previous")
    if prev is not None:
        if not isinstance(prev, str) or not prev.strip():
            raise ConfigError("previous must be 'auto', 'none' or a path to last month's facts.json")
        if prev.strip().lower() in ("auto", "none"):
            cfg["previous"] = prev.strip().lower()
        elif cfg.get("_path") and not os.path.isabs(prev):
            cfg["previous"] = os.path.join(os.path.dirname(os.path.abspath(cfg["_path"])), prev)
    for k in ("production_envs", "internal_domains"):
        _str_list(cfg["org"].get(k, []), f"org.{k}", '["prod"]')
    for k in ("health", "scanner", "static", "framework", "not_noise"):
        _str_list(cfg["noise"].get(k, []), f"noise.{k}")
    if not isinstance(cfg["noise"].get("by_design", []), list):
        raise ConfigError("noise.by_design must be a list of objects")
    for k in ("telemetry", "allowed_egress", "internal_suffixes", "nonprod_markers", "production"):
        _str_list(cfg["hosts"].get(k, []), f"hosts.{k}", '["^uat-pricing\\.acme\\.internal$"]')
    for k in ("include", "exclude", "integration"):
        _str_list(cfg["services"].get(k, []), f"services.{k}")
    for k in ("display", "aliases", "node_groups", "operations"):
        _str_dict(cfg["services"].get(k, {}), f"services.{k}")
    _str_dict(cfg["columns"].get("aliases", {}), "columns.aliases")
    from .readers import ALIASES
    for hdr, target in cfg["columns"].get("aliases", {}).items():
        if target not in ALIASES:
            raise ConfigError(f"columns.aliases['{hdr}'] maps to unknown column '{target}'{_nearest(target, sorted(ALIASES))}")
    _str_dict(cfg["columns"].get("units", {}), "columns.units")
    units = {}
    for hdr, u in cfg["columns"].get("units", {}).items():
        if u.strip().lower() not in UNIT_NAMES:
            raise ConfigError(f"columns.units['{hdr}'] must be one of ns, us, ms, s")
        units[hdr.strip().lower()] = UNIT_NAMES[u.strip().lower()]
        units[re.sub(r"[^A-Za-z0-9]+", "_", hdr.strip().lstrip("@")).strip("_").lower()] = UNIT_NAMES[u.strip().lower()]
    cfg["_units"] = units
    if cfg["columns"].get("date_format") is not None and not isinstance(cfg["columns"]["date_format"], str):
        raise ConfigError("columns.date_format must be a strptime format string")
    rx = {}
    for cls in ("health", "scanner", "static", "framework"):
        rx[cls] = [_rx(p, f"defaults.noise.{cls}") for p in d["noise"][cls]] + [_rx(p, f"noise.{cls}") for p in cfg["noise"].get(cls, [])]
    rx["auth"] = [_rx(p, "defaults.noise.auth") for p in d["noise"]["auth"]]
    rx["not_noise"] = [_rx(p, "noise.not_noise") for p in cfg["noise"].get("not_noise", [])]
    rx["by_design"] = []
    for i, item in enumerate(cfg["noise"].get("by_design", [])):
        _check_keys(item, {"service", "match", "why"}, f"noise.by_design[{i}]")
        if "match" not in item:
            raise ConfigError(f"noise.by_design[{i}] needs 'match' (and optionally 'service', 'why')")
        _opt_str(item.get("why"), f"noise.by_design[{i}].why")
        rx["by_design"].append((_rx(item.get("service", ".*"), "noise.by_design.service"), _rx(item["match"], "noise.by_design.match"), item.get("why", "")))
    rx["entry_ops"] = [_rx(p, "defaults.entry_operations") for p in d["entry_operations"]]
    rx["client_ops"] = [_rx(p, "defaults.client_operations") for p in d.get("client_operations", [])]
    rx["inprocess_ops"] = [_rx(p, "defaults.inprocess_operations") for p in d.get("inprocess_operations", [])]
    rx["consumer_ops"] = [_rx(p, "defaults.consumer_operations") for p in d.get("consumer_operations", [])]
    rx["secret_store"] = [_rx(p, "defaults.secret_store") for p in d["secret_store"]]
    rx["telemetry"] = [_rx(p, "hosts.telemetry") for p in d["hosts"]["telemetry"] + cfg["hosts"].get("telemetry", [])]
    rx["cloud"] = [_rx(p, "defaults.hosts.cloud") for p in d["hosts"]["cloud"]]
    rx["internal_suffixes"] = [_rx(p, "hosts.internal_suffixes") for p in d["hosts"]["internal_suffixes"] + cfg["hosts"].get("internal_suffixes", [])]
    rx["allowed_egress"] = [_rx(p, "hosts.allowed_egress") for p in d["hosts"].get("allowed_egress", []) + cfg["hosts"].get("allowed_egress", [])]
    rx["production_hosts"] = [_rx(p, "hosts.production") for p in d["hosts"].get("production", []) + cfg["hosts"].get("production", [])]
    markers = d["hosts"]["nonprod_markers"] + cfg["hosts"].get("nonprod_markers", [])
    rx["nonprod"] = re.compile(r"(?i)(^|[-._])(" + "|".join(re.escape(m) for m in markers) + r")(\d*)([-._]|$)")
    rx["include"] = [_rx(p, "services.include") for p in cfg["services"].get("include", [".*"])]
    rx["exclude"] = [_rx(p, "services.exclude") for p in cfg["services"].get("exclude", [])]
    rx["node_groups"] = {g: _rx(p, f"services.node_groups.{g}") for g, p in cfg["services"].get("node_groups", {}).items()}
    # an operation written as a metric name ('trace.express.request.hits') is the span operation 'express.request'
    norm_op = lambda v: re.sub(r"\.(hits(\.by_http_status)?|errors|duration|apdex)$", "", re.sub(r"^trace\.", "", v.strip())) if isinstance(v, str) and not v.startswith("_todo") else v
    cfg["services"]["operations"] = {k: norm_op(v) for k, v in (cfg["services"].get("operations") or {}).items()}
    if cfg["services"].get("default_operation"):
        cfg["services"]["default_operation"] = norm_op(cfg["services"]["default_operation"])
    # services.operations: exact service names first (aggregate looks them up directly), then full-match patterns, first match wins
    rx["operations_by_service"] = [(_safe_rx(s), op) for s, op in cfg["services"].get("operations", {}).items()]
    if cfg["window"].get("days") is not None and (isinstance(cfg["window"]["days"], bool) or not isinstance(cfg["window"]["days"], int) or cfg["window"]["days"] <= 0):
        raise ConfigError("window.days must be a positive whole number of days (or null)")
    rx["domains"] = []
    seen_keys, seen_slugs = set(), {}
    for i, dom in enumerate(cfg["domains"]):
        _check_keys(dom, DOMAIN_KEYS, f"domains[{i}]")
        if not dom.get("key") or not dom.get("label"):
            raise ConfigError(f"domains[{i}] needs 'key' and 'label'")
        if not isinstance(dom["key"], str) or not isinstance(dom["label"], str):
            raise ConfigError(f"domains[{i}].key and .label must be strings")
        _str_list(dom.get("services", []), f"domains[{i}].services", '["^checkout-api$"]')
        for k in ("icon", "audience", "description", "tier"):
            _opt_str(dom.get(k), f"domains[{i}].{k}")
        if dom["key"] in seen_keys:
            raise ConfigError(f"domain key '{dom['key']}' appears twice")
        if dom["key"].strip().lower() == "other":
            raise ConfigError("domain key 'other' is reserved for services outside the named domains — pick another key")
        seen_keys.add(dom["key"])
        sl = re.sub(r"[^a-z0-9]+", "_", dom["key"].lower()).strip("_")[:32]
        if sl in seen_slugs:
            raise ConfigError(f"domain keys '{seen_slugs[sl]}' and '{dom['key']}' are the same once simplified for diagram ids — rename one")
        seen_slugs[sl] = dom["key"]
        tier = dom.get("tier", "staff")
        if tier not in TIERS and not str(tier).startswith("_todo"):
            raise ConfigError(f"domains[{i}].tier must be one of {', '.join(TIERS)}")
        rx["domains"].append((dom, [_rx(p, f"domains[{i}].services") for p in dom.get("services", [])]))
    rx["kpis"] = []
    seen_ops = set()
    for i, op in enumerate(cfg["operations"]):
        if not isinstance(op, dict):
            raise ConfigError(f"operations[{i}] must be an object")
        _check_keys(op, OPERATION_KEYS, f"operations[{i}]")
        if not op.get("key") or "match" not in op:
            raise ConfigError(f"operations[{i}] needs 'key' and 'match'")
        _opt_str(op.get("key"), f"operations[{i}].key")
        _opt_str(op.get("label"), f"operations[{i}].label")
        _opt_bool(op.get("critical"), f"operations[{i}].critical")
        if op["key"] in seen_ops:
            raise ConfigError(f"operation key '{op['key']}' appears twice")
        seen_ops.add(op["key"])
        for num in ("slo_success_pct", "slo_p95_s"):
            if op.get(num) is not None and (isinstance(op[num], bool) or not isinstance(op[num], (int, float))):
                raise ConfigError(f"operations[{i}].{num} must be a number")
        if op.get("slo_success_pct") is not None and not 1 < op["slo_success_pct"] <= 100:
            raise ConfigError(f"operations[{i}].slo_success_pct is a percentage between 1 and 100 (e.g. 99.5 for 99.5 %), not {op['slo_success_pct']}")
        if op.get("slo_p95_s") is not None and op["slo_p95_s"] <= 0:
            raise ConfigError(f"operations[{i}].slo_p95_s must be above 0 seconds")
        if op.get("slo_p95_s") is not None and op["slo_p95_s"] > 120:
            raise ConfigError(f"operations[{i}].slo_p95_s is in seconds (e.g. 0.8 for 800 ms), not {op['slo_p95_s']:g}")
        rx["kpis"].append((op, _rx(op.get("service", ".*"), "operations.service"), _rx(op["match"], "operations.match")))
    rx["ratios"] = []
    for i, r in enumerate(cfg["ratios"]):
        try:
            _check_keys(r, RATIO_KEYS, f"ratios[{i}]")
            if not isinstance(r["label"], str) or not r["label"]:
                raise ConfigError(f"ratios[{i}].label must be a non-empty string")
            if r.get("expect_max") is not None and (isinstance(r["expect_max"], bool) or not isinstance(r["expect_max"], (int, float))):
                raise ConfigError(f"ratios[{i}].expect_max must be a number")
            _check_keys(r["numerator"], {"service", "match"}, f"ratios[{i}].numerator")
            _check_keys(r["denominator"], {"service", "match"}, f"ratios[{i}].denominator")
            twin = next((o["label"] for o, *_ in rx["ratios"] if slug(o["label"]) == slug(r["label"])), None)
            if twin is not None:
                raise ConfigError(f"ratios[{i}].label '{r['label']}' and '{twin}' are the same once shortened to a finding id ('{slug(r['label'])}') — make the first 40 characters differ")
            rx["ratios"].append((r, _rx(r["numerator"].get("service", ".*"), "ratios"), _rx(r["numerator"]["match"], "ratios"),
                                 _rx(r["denominator"].get("service", ".*"), "ratios"), _rx(r["denominator"]["match"], "ratios")))
        except (KeyError, TypeError):
            raise ConfigError(f"ratios[{i}] needs label, numerator{{service, match}} and denominator{{service, match}}")
    rx["shorten"] = [(re.compile(r["pattern"]), r["replace"]) for r in d["resource_shortening"]["rules"]]
    cfg["_rx"] = rx
    cfg["_integration"] = {s.lower() for s in d["integration_services"]} | {s.lower() for s in cfg["services"].get("integration", [])}
    cfg["_tzinfo"], cfg["_tz_note"], cfg["_tz_hint"] = resolve_timezone(cfg["org"].get("timezone"), cfg["org"].get("utc_offset"))
    return cfg


# zones with no daylight saving: their offset is right all year, so they need no tz database
FIXED_ZONES = {"Asia/Hong_Kong": "+08:00", "Asia/Macau": "+08:00", "Asia/Singapore": "+08:00", "Asia/Kuala_Lumpur": "+08:00", "Asia/Shanghai": "+08:00",
               "Asia/Taipei": "+08:00", "Asia/Manila": "+08:00", "Australia/Perth": "+08:00", "Asia/Tokyo": "+09:00", "Asia/Seoul": "+09:00",
               "Asia/Kolkata": "+05:30", "Asia/Calcutta": "+05:30", "Asia/Colombo": "+05:30", "Asia/Kathmandu": "+05:45", "Asia/Dhaka": "+06:00",
               "Asia/Yangon": "+06:30", "Asia/Jakarta": "+07:00", "Asia/Bangkok": "+07:00", "Asia/Ho_Chi_Minh": "+07:00", "Asia/Karachi": "+05:00",
               "Asia/Dubai": "+04:00", "Asia/Riyadh": "+03:00", "Asia/Qatar": "+03:00", "Africa/Nairobi": "+03:00", "Africa/Johannesburg": "+02:00",
               "Africa/Lagos": "+01:00", "Australia/Brisbane": "+10:00", "Pacific/Honolulu": "-10:00", "America/Phoenix": "-07:00"}
_UTC_NAMES = {"UTC", "ETC/UTC", "GMT", "ETC/GMT", "Z", "UNIVERSAL", "ZULU"}


def resolve_timezone(name, utc_offset):
    """zoneinfo when the tz database is available, else the zone's own offset when it has no daylight saving, else the fixed
    offset ('+08:00'). Returns (tzinfo, reader-facing note, operator hint)."""
    if name and name.upper() not in _UTC_NAMES:
        try:
            from zoneinfo import ZoneInfo
            return ZoneInfo(name), "", ""
        except Exception:
            pass
        if name in FIXED_ZONES and (not utc_offset or utc_offset.strip() in ("+00:00", FIXED_ZONES[name])):
            off = FIXED_ZONES[name]
            sign = 1 if off[0] == "+" else -1
            return timezone(sign * timedelta(hours=int(off[1:3]), minutes=int(off[4:6])), name=off), "", ""
        if not utc_offset or utc_offset.strip() in ("+00:00", "-00:00"):
            return (timezone.utc, f"times are shown in UTC (the timezone '{name}' could not be applied on the machine that built this review)",
                    f"timezone database for '{name}' not available on this machine and org.utc_offset is +00:00 (the drafted default), so times are in UTC — "
                    f"set org.utc_offset to {name}'s offset (for example '+08:00') or pip install tzdata")
    off = utc_offset or "+00:00"
    m = re.match(r"^([+-])(\d{2}):?(\d{2})$", off.strip())
    if not m:
        return timezone.utc, f"times are shown in UTC (the configured timezone '{name}' could not be applied)", f"timezone '{name}' unavailable and utc_offset '{off}' invalid; set org.utc_offset like '+08:00' or pip install tzdata"
    sign = 1 if m.group(1) == "+" else -1
    tz = timezone(sign * timedelta(hours=int(m.group(2)), minutes=int(m.group(3))), name=off)
    if not name or name.upper() in _UTC_NAMES:
        return tz, "", ""
    return tz, f"times are shown at the fixed offset {off} for {name} (no daylight-saving adjustment)", f"timezone database for '{name}' not available on this machine; using the fixed offset {off} — pip install tzdata to get daylight-saving handling"


def slug(text, maxlen=40):
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return s[:maxlen].strip("-")


def matches_any(patterns, text):
    return any(p.search(text) for p in patterns)


# ---------------------------------------------------------------- drafting (init)
def draft_config(F, org_name, existing=None):
    """Draft a config from facts computed with an empty config. Returns (config dict with _todo notes, list of todo lines)."""
    todos = []
    cfg = {
        "_comment": "Per-org configuration for ddreview. Keys starting with _ are ignored. Edit the _todo values, delete wrong guesses, keep the file under version control.",
        "org": {"name": org_name, "short": slug(org_name) or "org", "prepared_for": org_name, "prepared_by": "_todo: your team name",
                "timezone": F["meta"].get("timezone_guess") or "UTC", "utc_offset": F["meta"].get("utc_offset_guess") or "+00:00",
                "production_envs": ["prod", "prd", "production", "live"], "treat_missing_env_as_production": True,
                "internal_domains": [], "_internal_domains_to_confirm": F["meta"].get("internal_domains_guess", [])},
        "report": {"max_findings": 8, "diagrams": "both", "delivered_alongside": "", "closing_note": ""},
        "window": {"_comment": "the range you select when exporting the Endpoints list; a health check's cadence fits 7, 14 and 28 days equally, so the kit needs this to quote per-day figures", "days": None},
        "services": {"display": {}, "integration": [], "node_groups": {}, "operations": {}},
        "domains": [],
        "operations": [],
        "noise": {"health": [], "by_design": [], "not_noise": []},
        "hosts": {"allowed_egress": []},
        "thresholds": {"_comment": "Override any value from ddreview/defaults.json here, e.g. \"fail_red_pct\": 3.0"},
        "previous": "auto",
    }
    entries = {n: s for n, s in F["services"].items() if s["role"] == "entry"}
    stems = {}
    for name in sorted(entries):
        stems.setdefault(service_stem(name), []).append(name)
    families = sorted(stems.items(), key=lambda kv: (-sum(entries[n]["requests"]["all"] for n in kv[1]), kv[0]))
    if F["coverage"]["inputs"].get("endpoints"):
        cfg["window"]["_todo"] = "_todo: set days to the range the Endpoints list was exported over (7 if you followed the README); until set, no per-day figures are quoted"
        todos.append("window.days: the range of the Endpoints-list export (the health-check cadence cannot tell 7, 14 and 28 days apart)")
    if F["meta"].get("internal_domains_guess"):
        todos.append("org.internal_domains: move the client's own domains from org._internal_domains_to_confirm (moving a SaaS domain there hides its public-egress finding)")
    used_keys, used_simple = {"other"}, {"other"}
    simple = lambda k: re.sub(r"[^a-z0-9]+", "_", k.lower()).strip("_")[:32]
    for stem, members in families:
        key = (slug(stem) or "services")[:30].strip("-")
        if key == "other":
            key = "other-services"
        k, i = key, 2
        while k in used_keys or simple(k) in used_simple:
            k, i = f"{key[:28].strip('-')}-{i}", i + 1
        key = k
        used_keys.add(key)
        used_simple.add(simple(key))
        dom = {"key": key, "label": humanise(stem) or stem, "icon": "🧩", "audience": "_todo: who feels it (e.g. Customers, Clinic staff)",
               "tier": "_todo: staff — customer | clinical | staff | internal; red findings need customer or clinical",
               "services": ["^" + rx_escape(m) + "$" for m in members], "description": ""}
        cfg["domains"].append(dom)
        node_stem = re.sub(r"[-_]?\d+$", "", members[0])
        if len(members) > 1 and any(re.search(r"\d+$", m) for m in members) and all(re.sub(r"[-_]?\d+$", "", m) == node_stem for m in members) \
                and not any(re.search(r"(?i)(^|[-_.])v\d+$", m) for m in members):
            cfg["services"]["node_groups"][humanise(stem)] = "^" + rx_escape(node_stem) + r"([-_]?\d+)?$"
        todos.append(f"domain '{key}': name it, set audience and tier")
    for name, s in sorted(F["services"].items()):
        if s["role"] == "integration" and name not in F["meta"]["default_integration"]:
            cfg["services"]["integration"].append(name)
        if s.get("operation") and s.get("operation_source") == "column":
            cfg["services"].setdefault("_detected_operations", {})[name] = s["operation"]     # seen in the export; set services.operations only to override
        elif s["role"] == "entry":
            cfg["services"]["operations"][name] = ("_todo: span operation name, e.g. servlet.request — Metrics Explorer: type trace. and filter service:" + name
                                                   + ", then write the part between 'trace.' and '.hits'")
            todos.append(f"operation name for '{name}' (Metrics Explorer -> trace. -> service:{name})")
    seen_ops = set()
    for svc, rows in F["resources"].items():
        if svc not in entries:
            continue
        group = entries[svc].get("node_group")
        biz = [r for r in rows if r["class"] == "business"]
        for r in sorted(biz, key=lambda r: -r["calls"])[:3]:
            dedupe = (group or svc, r["resource"])
            if dedupe in seen_ops:
                continue
            seen_ops.add(dedupe)
            svc_rx = ("^" + rx_escape(re.sub(r"[-_]?\d+$", "", svc)) + r"([-_]?\d+)?$") if group else ("^" + rx_escape(svc) + "$")
            okey = slug(svc + "-" + r["short"], 60)
            base, k = okey, 2
            while okey in {o.get("key") for o in cfg["operations"]}:
                okey, k = f"{base}-{k}", k + 1
            cfg["operations"].append({"key": okey, "label": "_todo: plural business name (e.g. 'claim creations') for " + r["short"],
                                      "service": svc_rx, "match": "^" + rx_escape(r["resource"]) + "$", "critical": False})
            todos.append(f"KPI label for '{okey}': give it a plural business name, or delete the operation")
    for cand in F["noise"].get("by_design_candidates", []):
        cfg["noise"].setdefault("_by_design_to_confirm", []).append({"service": "^" + rx_escape(cand["service"]) + "$", "match": "^" + rx_escape(cand["resource"]) + "$",
                                                                     "why": "_todo: why this fails by design, as confirmed with the team (" + str(cand["calls"]) + " calls)"})
        todos.append(f"{cand['service']} {cand['resource']} fails on every call: if the team confirms it is by design, move its entry from "
                     "noise._by_design_to_confirm to noise.by_design (until then it is reported as a failure)")
    for probe in F["window"].get("aggregate", {}).get("proof", [])[:10]:
        pat = "^" + rx_escape(probe["resource"]) + "$"
        if pat not in cfg["noise"]["health"]:
            cfg["noise"]["health"].append(pat)
    if existing:
        existing = _drop_nulls(existing)
        merged = copy.deepcopy(existing)
        for k, v in cfg.items():
            if k not in merged and not str(k).startswith("_"):
                merged[k] = copy.deepcopy(v)          # a file that only held column aliases gets the full draft
            elif isinstance(v, dict) and isinstance(merged.get(k), dict):
                for kk, vv in v.items():
                    merged[k].setdefault(kk, copy.deepcopy(vv))
        if org_name and not merged.get("org", {}).get("name"):
            merged.setdefault("org", {})["name"] = org_name
            merged["org"]["short"] = merged["org"].get("short") or slug(org_name) or "org"
        new_services = [n for n in entries if not any(re.search(p, n) for dom in existing.get("domains", []) for p in dom.get("services", []))]
        todos = []
        if new_services:
            merged["_new_since_last_init"] = {"services": new_services, "drafted_domains": [d for d in cfg["domains"] if any(re.search(p, n) for p in d["services"] for n in new_services)]}
            todos.append("new services since last init: " + ", ".join(new_services))
        else:
            merged.pop("_new_since_last_init", None)
        org_m = merged.get("org") or {}
        if org_m.get("_internal_domains_to_confirm") is not None:
            left_ = [x for x in org_m["_internal_domains_to_confirm"] if x not in (org_m.get("internal_domains") or [])]
            if left_ and not org_m.get("internal_domains"):
                org_m["_internal_domains_to_confirm"] = left_
                todos.append("org.internal_domains: move the client's own domains from org._internal_domains_to_confirm (moving a SaaS domain there hides its public-egress finding)")
            else:
                org_m.pop("_internal_domains_to_confirm", None)
        if (merged.get("window") or {}).get("days"):
            merged["window"].pop("_todo", None)
        elif F["coverage"]["inputs"].get("endpoints"):
            todos.append("window.days: the range of the Endpoints-list export (the health-check cadence cannot tell 7, 14 and 28 days apart)")
        todos += [f"still _todo: {path}" for path in _todo_paths(merged)]
        return merged, todos
    return cfg, todos


def _todo_paths(obj, prefix=""):
    out = []
    if isinstance(obj, dict):
        filled = [vv for kk, vv in obj.items() if not str(kk).startswith("_")]
        filled = bool(filled) and all(vv is not None and not (isinstance(vv, str) and vv.startswith("_todo")) for vv in filled)
        for k, v in obj.items():
            if str(k).startswith("_") and k != "_todo":
                continue
            if k == "_todo" and filled:
                continue                  # the value it asked for has been given
            out += _todo_paths(v, f"{prefix}.{k}" if prefix else str(k))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            out += _todo_paths(v, f"{prefix}[{i}]")
    elif isinstance(obj, str) and obj.startswith("_todo"):
        out.append(prefix)
    return out


_ENV_TOKENS = {"prd", "prod", "production", "uat", "sit", "dev", "test", "stg", "staging", "qa", "live"}


def rx_escape(text):
    """re.escape without the noise: only characters that mean something in a regex are escaped (so '-' and ' ' stay readable)."""
    return re.sub(r"([.^$*+?{}\[\]\\|()])", r"\\\1", text)


def service_stem(name):
    """'prd-checkout-web-ui' -> 'checkout-web-ui'; 'orders-api01' -> 'orders-api'; 'shopfront_php_prd' -> 'shopfront-php'."""
    parts = [p for p in re.split(r"[-_.]+", name.lower()) if p and p not in _ENV_TOKENS and not p.isdigit()]
    parts = [re.sub(r"\d+$", "", p) for p in parts]
    parts = [p for p in parts if p]
    return "-".join(parts) or name.lower()


_ACRONYMS = {"api", "ui", "db", "emr", "flp", "sms", "crm", "erp", "hr", "cms", "iam", "sso", "etl", "bff", "ws", "svc", "ords", "efax", "eas", "lis", "ris", "aesb", "apim"}


def humanise(stem):
    return " ".join(w.upper() if w in _ACRONYMS else w.capitalize() for w in stem.replace("-", " ").split())
