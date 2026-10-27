"""Readers for Datadog CSV / TSV exports.

Files are recognised by their header row, never by their name. Header cells are normalised (lower-case, a leading
'@' or sanitising apostrophe dropped, non-alphanumerics turned into '_') and mapped to canonical names through the
alias table below, so an export with extra or renamed columns still loads. Every value parser is tolerant of the
shapes Datadog writes: "1.23 ms" / "456 µs" durations (both mu code points and the double-encoded 'Âµs'), ISO-8601
timestamps with Z or an offset, rates as fractions or percentages, counts with thousands separators, decimal commas
from a spreadsheet re-save, and the leading single quote the CSV sanitiser adds before values that start with = + - @.
Files are decoded as UTF-8 and fall back to cp1252 (an Excel re-save) rather than mangling the micro sign.

This is the only module that knows Datadog's column names.
"""
import csv
import hashlib
import io
import json
import os
import re
from datetime import datetime, timezone

KINDS = ("spans", "endpoints", "services", "inferred", "catalog")
CAP_VALUES = (5000, 10000, 50000, 100000)
HOUR_MS = 3_600_000.0

# canonical <- normalised header names
ALIASES = {
    "ts": ["date", "timestamp", "time", "start", "start_time", "start_timestamp", "_timestamp", "datetime"],
    "service": ["service", "service_name"],
    "resource": ["resource", "resource_name"],
    "name": ["name"],
    "duration": ["duration", "_duration", "duration_ns", "duration_ms", "duration_us", "duration_s", "duration_sec", "duration_secs", "duration_seconds", "span_duration", "latency"],
    "method": ["method", "http_method"],
    "status": ["status_code", "http_status_code", "http_status", "statuscode"],
    "status_raw": ["status"],
    "error": ["error", "_error", "is_error", "error_flag", "error_type", "error_message", "error_msg", "error_stack"],
    "trace_id": ["trace_id", "traceid"],
    "span_id": ["span_id", "spanid"],
    "parent_id": ["parent_id", "parentid", "parent_span_id", "parent_span"],
    "operation": ["operation_name", "operation", "span_name", "name_operation", "op_name"],
    "env": ["env", "environment"],
    "host": ["host", "hostname", "host_name"],
    "version": ["version"],
    "type": ["type", "span_type"],
    "kind": ["span_kind", "kind"],
    # client-side peer tags only: http.host is left out because on a server span it holds the service's own Host header
    "peer": ["peer_service", "peer_hostname", "peer_host", "out_host", "net_peer_name", "db_instance", "peer_db_name", "network_destination_name"],
    "request_count": ["request_count", "requests", "hits", "request_counts", "total_requests"],
    "error_rate": ["error_rate", "errors_rate", "err_rate"],
    "p95": ["p95_duration", "p95_latency_ns", "p95_latency", "p95", "p95_duration_s", "p95_latency_ms"],
    "monitors": ["monitor_counts", "monitors"],
    "slos": ["slos", "slo"],
    "envs": ["envs"],
    "svc_type": ["type"],
    "intg": ["intg"],
    "resource_type": ["resource_type"],
    "account": ["account"],
    "cloud_provider": ["cloud_provider"],
    "display_name": ["display_name"],
    "team": ["team"],
    "region": ["region"],
    "resource_key": ["resource_key"],
}
_ALIAS_LOOKUP = {}
for _canon, _names in ALIASES.items():
    for _n in _names:
        _ALIAS_LOOKUP.setdefault(_n, []).append(_canon)


def norm_header(cell):
    """'@http.status_code' -> 'http_status_code'; 'P95 Duration' -> 'p95_duration'; 'p95 Latency (ns)' -> 'p95_latency_ns'."""
    s = cell.strip().lstrip("'﻿").strip()
    if s.startswith("@"):
        s = s[1:]
    s = re.sub(r"[^A-Za-z0-9]+", "_", s).strip("_").lower()
    return s


def unquote(value):
    """Drop the leading apostrophe Datadog's CSV sanitiser adds before values starting with = + - @, and surrounding spaces."""
    if value is None:
        return ""
    v = value.strip()
    if v.startswith("'") and len(v) > 1 and v[1] in "=+-@":
        v = v[1:]
    return v


# ---------------------------------------------------------------- numbers
_THOUSANDS = re.compile(r"^-?[1-9]\d{0,2}(,\d{3})+(\.\d+)?$")
_DECIMAL_COMMA = re.compile(r"^-?\d+,\d+$")
_DOT_DECIMAL = re.compile(r"^-?\d+\.\d+$")
_DOT_THOUSANDS = re.compile(r"^-?\d{1,3}(\.\d{3})+$")


def normalise_number_text(text, decimal_comma=False):
    """'1,234' -> '1234'; '1,23' -> '1.23' (decimal comma, never a thousands group); '0,045' -> '0.045' (a group never
    starts with 0); '1.234,5' -> '1234.5'. With ``decimal_comma`` (a ';'-delimited file whose numbers use the comma as the
    decimal mark) every comma is the decimal mark and every dot a thousands separator."""
    v = text.strip()
    if decimal_comma:
        return v.replace(".", "").replace(",", ".")
    if _THOUSANDS.match(v):
        return v.replace(",", "")
    if _DECIMAL_COMMA.match(v):
        return v.replace(",", ".")
    if "," in v and "." in v and v.rfind(",") > v.rfind("."):
        return v.replace(".", "").replace(",", ".")
    return v.replace(",", "")


_UNIT_MS = {"ns": 1e-6, "us": 1e-3, "µs": 1e-3, "μs": 1e-3, "âµs": 1e-3, "�s": 1e-3, "ms": 1.0, "s": 1e3, "sec": 1e3, "m": 6e4, "min": 6e4, "h": 3.6e6, "hr": 3.6e6}
_DUR_PART = re.compile(r"(-?[\d][\d,]*(?:[.,]\d+)?)\s*(ns|us|µs|μs|âµs|�s|ms|sec|s|min|m|hr|h)\b", re.I)
_NUMBER = re.compile(r"^-?[\d,]*[.,]?\d+(?:e[-+]?\d+)?$", re.I)


def parse_duration_ms(text, bare_unit="ms", decimal_comma=False):
    """'1.23 s' -> 1230.0; '456 µs' -> 0.456; '1m 2.5s' -> 62500.0; a bare number takes ``bare_unit`` (ns|us|ms|s).
    Negative durations are unreadable (None): a sign is never silently dropped."""
    v = unquote(text)
    if not v:
        return None
    parts = _DUR_PART.findall(v)
    if parts and sum(len(p[0]) + len(p[1]) for p in parts) >= len(re.sub(r"\s", "", v)) - 1:
        total = 0.0
        for num, unit in parts:
            x = float(normalise_number_text(num, decimal_comma))
            if x < 0:
                return None
            total += x * _UNIT_MS[unit.lower()]
        return total
    if _NUMBER.match(v):
        x = float(normalise_number_text(v, decimal_comma))
        return None if x < 0 else x * _UNIT_MS[bare_unit]
    return None


def parse_number(text, decimal_comma=False):
    v = unquote(text)
    if v.endswith("%"):
        v = v[:-1].strip()
    try:
        return float(normalise_number_text(v, decimal_comma))
    except ValueError:
        return None


def parse_int(text, decimal_comma=False):
    x = parse_number(text, decimal_comma)
    return int(round(x)) if x is not None else None


def parse_status(text):
    v = unquote(text)
    if not v:
        return None
    try:
        return int(float(v))
    except ValueError:
        return None


_TRUE = {"true", "1", "yes", "y", "error", "err", "failed", "fail"}
_FALSE = {"", "0", "false", "no", "n", "ok", "none", "null", "-", "n/a", "na", "unset", "success"}


def parse_flag(text):
    """A Datadog error flag or error.type cell: true/yes/error, any non-zero number (1, 1.0, an error code), or any error
    type text. 0, false, ok, n/a and blanks are not errors."""
    v = unquote(text).lower()
    if v in _TRUE:
        return True
    if v in _FALSE:
        return False
    if _NUMBER.match(v):
        try:
            return float(normalise_number_text(v)) != 0
        except ValueError:
            return False
    return len(v) > 0


def parse_monitors(text):
    """int, or JSON like {"non-synthetics": {"0": 1, "1": 0}} (summed), or blank -> 0. Returns (count, states)."""
    v = unquote(text)
    if not v:
        return 0, {}
    try:
        return int(float(v)), {}
    except ValueError:
        pass
    try:
        obj = json.loads(v)
        total, states = 0, {}
        if isinstance(obj, dict):
            for group in obj.values():
                if isinstance(group, dict):
                    for k, n in group.items():
                        try:
                            states[str(k)] = states.get(str(k), 0) + int(n)
                            total += int(n)
                        except (TypeError, ValueError):
                            pass
                elif isinstance(group, (int, float)):
                    total += int(group)
        return total, states
    except (ValueError, TypeError):
        return 0, {}


_ISO = re.compile(r"^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}(?::\d{2})?)(\.\d+)?\s*(Z|[+-]\d{2}:?\d{2}|UTC)?$", re.I)
_UI_FORMATS = ["%b %d, %Y %H:%M:%S.%f", "%b %d, %Y %H:%M:%S", "%b %d %Y %H:%M:%S", "%d %b %Y %H:%M:%S", "%Y/%m/%d %H:%M:%S", "%d/%m/%Y %H:%M:%S", "%m/%d/%Y %H:%M:%S"]


def parse_ts(text, date_format=None):
    """Return (aware UTC datetime, assumed_utc flag) or (None, False). Accepts ISO 8601 with Z/offset, epoch s/ms/us/ns, a few UI shapes."""
    v = unquote(text)
    if not v:
        return None, False
    if date_format:
        try:
            dt = datetime.strptime(v, date_format)
            return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).astimezone(timezone.utc), dt.tzinfo is None
        except ValueError:
            pass
    m = _ISO.match(v)
    if m:
        date, clock, frac, zone = m.groups()
        if len(clock) == 5:
            clock += ":00"
        frac = (frac or "")[:7]
        try:
            if zone is None:
                dt = datetime.fromisoformat(f"{date}T{clock}{frac}")
                return dt.replace(tzinfo=timezone.utc), True
            if zone.upper() in ("Z", "UTC"):
                off = "+00:00"
            else:
                off = zone if ":" in zone else zone[:3] + ":" + zone[3:]
            dt = datetime.fromisoformat(f"{date}T{clock}{frac}{off}")
            return dt.astimezone(timezone.utc), False
        except ValueError:
            return None, False
    m = re.match(r"^(\d{10,19})(?:\.(\d+))?$", v)
    if m:
        scale = {10: 1, 13: 1e3, 16: 1e6, 19: 1e9}.get(len(m.group(1)))
        if scale:
            try:
                return datetime.fromtimestamp(float(v) / scale, tz=timezone.utc), False
            except (OverflowError, OSError, ValueError):
                return None, False
    for fmt in _UI_FORMATS:
        try:
            return datetime.strptime(v, fmt).replace(tzinfo=timezone.utc), True
        except ValueError:
            continue
    return None, False


# ---------------------------------------------------------------- files
def read_text(path):
    """Decode an export: UTF-8 (with or without BOM) first; a file that is not valid UTF-8 is read as cp1252 (a spreadsheet
    re-save), so 'µs' survives instead of becoming an unreadable duration. Returns (text, encoding)."""
    with open(path, "rb") as fh:
        raw = fh.read()
    try:
        return raw.decode("utf-8-sig"), "utf-8"
    except UnicodeDecodeError:
        return raw.decode("cp1252", errors="replace"), "cp1252"


def sniff(path):
    """Return (delimiter, raw header cells, normalised header cells, encoding)."""
    text, enc = read_text(path)
    head = text.split("\n", 1)[0].rstrip("\r")
    if not head.strip():
        return ",", [], [], enc
    delim = "\t" if "\t" in head else ("," if "," in head else ";")
    cells = next(csv.reader([head], delimiter=delim))
    return delim, cells, [norm_header(c) for c in cells], enc


_UNIT_SUFFIX = re.compile(r"_(in_)?(ns|nanos?|nanoseconds?|us|µs|μs|micros?|microseconds?|ms|millis?|milliseconds?|s|secs?|seconds?)$")


def _aliases_of(n, extra=None):
    """Canonical names for a normalised header: configured aliases, the alias table, or the table entry for the header
    without a trailing unit word ('duration_milliseconds' -> duration, 'p95_duration_ms' -> p95)."""
    extra = extra or {}
    if n in extra:
        return [extra[n]]
    if n in _ALIAS_LOOKUP:
        return _ALIAS_LOOKUP[n]
    bare = _UNIT_SUFFIX.sub("", n)
    return _ALIAS_LOOKUP.get(bare, []) if bare != n else []


def _canon_set(normalised, extra=None):
    canon = set()
    for n in set(normalised):
        canon.update(_aliases_of(n, extra))
    return canon


def detect_kind(normalised, extra=None):
    """Recognise an export by its header. ``extra`` maps normalised headers to canonical names (columns.aliases), so a
    configured alias can rescue a renamed column."""
    s = set(normalised)
    canon = _canon_set(normalised, extra)
    if "resource_type" in canon and ("account" in canon or "cloud_provider" in canon):
        return "catalog"
    if "intg" in canon and "request_count" in canon:
        return "inferred"
    if "service" in canon and "request_count" in canon and "error_rate" in canon and ("resource" in canon or "name" in canon):
        return "endpoints"
    if "service" not in canon and ({"request_count", "monitor_counts", "p95_duration"} & s) and not ({"requests", "monitors", "p95_latency_ns", "slos", "envs"} & s):
        return "unknown"          # an Endpoints export without its Service column is unusable, not a Services list
    if "name" in canon and "request_count" in canon and ("monitors" in canon or "slos" in canon) and "service" not in canon:
        return "services"
    if "service" in canon and ("resource" in canon or "name" in canon) and "duration" in canon and ({"ts", "trace_id", "span_id"} & canon):
        return "spans"
    return "unknown"


def column_map(normalised, kind, extra_aliases=None):
    """Map canonical name -> original column index for this kind. extra_aliases: {original or normalised header: canonical}."""
    cmap = {}
    extra = {norm_header(k): v for k, v in (extra_aliases or {}).items()}
    present = set(normalised)
    for i, n in enumerate(normalised):
        canon_list = _aliases_of(n, extra)
        for canon in canon_list:
            if canon == "svc_type" and kind not in ("services", "inferred"):
                continue
            if canon == "type" and kind in ("services", "inferred"):
                continue
            if canon == "name":
                target = "resource" if kind in ("spans", "endpoints") else "service"
                if target in cmap or any(h in present for h in ALIASES[target]):
                    continue              # a real Resource / Service header exists somewhere: Name never overrides it
                canon = target
            cmap.setdefault(canon, i)
    if "status_raw" in cmap:
        cmap["status_pending"] = cmap.pop("status_raw")     # resolved by content (numeric codes, or ok/error flags) when the file is read
    return cmap


def file_sha256(path):
    """Digest of the whole file, read in 1 MiB blocks."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def discover(folder, extra_aliases=None):
    """Inventory every *.csv / *.tsv / *.txt in ``folder``: kind, delimiter, columns. A file whose header, size and digest
    repeat an earlier file is marked duplicate; an empty file is reported as such."""
    files, seen = [], set()
    extra = {norm_header(k): v for k, v in (extra_aliases or {}).items()}
    for name in sorted(os.listdir(folder)):
        path = os.path.join(folder, name)
        if not os.path.isfile(path) or not name.lower().endswith((".csv", ".tsv", ".txt")):
            continue
        delim, raw, normalised, enc = sniff(path)
        digest = file_sha256(path)
        if not raw:
            files.append({"file": name, "path": path, "kind": "unknown", "empty": True, "reason": "empty file (0 bytes or a blank first line) — the export did not download; re-export it",
                          "delimiter": ",", "sha256": digest, "encoding": enc, "columns": {}, "headers": [], "normalised": [], "duplicate": False, "rows": 0})
            continue
        kind = detect_kind(normalised, extra)
        key = (tuple(normalised), os.path.getsize(path), digest)
        entry = {"file": name, "path": path, "kind": kind, "delimiter": "tab" if delim == "\t" else delim, "sha256": digest, "encoding": enc,
                 "columns": column_map(normalised, kind, extra_aliases) if kind != "unknown" else {},
                 "headers": raw, "normalised": normalised, "duplicate": key in seen, "rows": None, "empty": False}
        entry["decimal_comma"] = decimal_comma_file(entry) if kind != "unknown" else False
        canon = _canon_set(normalised, extra)
        if kind == "unknown" and "service" not in canon and ({"request_count", "monitor_counts", "p95_duration"} & set(normalised)):
            entry["reason"] = "looks like an APM Endpoints export without its Service column — re-export with the Service column shown"
        elif kind == "unknown" and "service" in canon and "duration" in canon and ({"resource", "name"} & canon):
            entry["reason"] = "looks like a spans export without a Date (or Trace ID / Span ID) column — re-export with the Date column shown"
        seen.add(key)
        files.append(entry)
    return files


def iter_rows(entry):
    """Yield raw cell lists for a discovered file (header skipped)."""
    delim = "\t" if entry["delimiter"] == "tab" else entry["delimiter"]
    text, _ = read_text(entry["path"])
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=delim)
    next(reader, None)
    for row in reader:
        if row and any(c.strip() for c in row):
            yield row


_UNIT_TOKENS = (("ns", r"ns|nanos?|nanoseconds?"), ("us", r"us|µs|μs|micros?|microseconds?"), ("ms", r"ms|millis?|milliseconds?"), ("s", r"s|secs?|seconds?"))


def unit_from_header(raw_header):
    """The unit a header names: 'Duration (ms)', 'duration_us', 'Duration (sec)', 'Duration [seconds]', 'p95 Latency (ns)'.
    The unit is the last word of the header, or a word in brackets anywhere in it."""
    h = raw_header.lower()
    bracketed = re.findall(r"[(\[]\s*([^)\]]+?)\s*[)\]]", h)
    words_in = re.sub(r"[^a-z0-9µμ]+", "_", h).strip("_").split("_")
    tail = words_in[-1] if len(words_in) > 1 else ""
    for unit, words in _UNIT_TOKENS:
        rx = re.compile(r"^(" + words + r")$")
        if any(rx.match(b) for b in bracketed) or (tail and rx.match(tail)):
            return unit
    return None


def decimal_comma_file(entry, sample_rows=2000):
    """A ';'-delimited file whose numeric cells use the comma as the decimal mark (a European spreadsheet re-save): at least
    one '0,045'-style cell and no '0.045'-style cell."""
    if entry.get("delimiter") != ";":
        return False
    comma = dot = 0
    for i, row in enumerate(iter_rows(entry)):
        if i >= sample_rows:
            break
        for c in row:
            v = c.strip().strip('"').rstrip("%").strip()
            if _DECIMAL_COMMA.match(v) and not _THOUSANDS.match(v):
                comma += 1
            elif _DOT_DECIMAL.match(v) and not _DOT_THOUSANDS.match(v):
                dot += 1                      # '409.143' in a ';' file is a thousands-grouped integer, not a dot decimal
    return comma > 0 and dot == 0


DAY_MS = 24 * HOUR_MS
TYPICAL_SPAN_MS = 10.0
PLAUSIBLE_MEDIAN_MS = (0.02, 5000.0)


def decide_bare_unit(values, header_unit, default, header_name="", source="header"):
    """For a column of bare numbers: a unit from the header or from columns.units wins. Otherwise keep the units (ms, µs,
    ns) under which the longest span still fits in a day, and pick the one that puts the median span closest (in log
    space) to a typical 10 ms span. Returns (unit, evidence, inferred, low_confidence) — low confidence when the chosen
    median falls outside 0.02 ms – 5 s, which the build reports as a warning and a notebook caveat."""
    if header_unit:
        return header_unit, ("set in columns.units" if source == "config" else "unit from the column header"), False, False
    if not values:
        return default, "no bare numbers seen", False, False
    vals = sorted(values)
    mx, med = vals[-1], vals[len(vals) // 2] if len(vals) % 2 else (vals[len(vals) // 2 - 1] + vals[len(vals) // 2]) / 2.0
    cands = [u for u in ("ms", "us", "ns") if mx * _UNIT_MS[u] <= DAY_MS]
    if not cands:
        return default, f"largest bare value {mx:.0f} is implausible in any unit; read as {default}", True, True
    import math
    score = lambda u: abs(math.log10(max(med * _UNIT_MS[u], 1e-12) / TYPICAL_SPAN_MS))
    unit = min(cands, key=score)
    med_ms = med * _UNIT_MS[unit]
    names = {"ms": "milliseconds", "us": "microseconds", "ns": "nanoseconds"}
    low = not (PLAUSIBLE_MEDIAN_MS[0] <= med_ms <= PLAUSIBLE_MEDIAN_MS[1])
    why = f"median bare value {med:g} read as {names[unit]} gives a typical span of {med_ms:.3g} ms; the longest is {mx * _UNIT_MS[unit] / 1000:.3g} s"
    return unit, why, True, low
