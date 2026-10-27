"""Live-graph recipes: Datadog metric queries a reader pastes into a notebook `/timeseries` or `/toplist` cell.

Trace metrics (trace.<operation>.hits / .errors / the trace.<operation> distribution) count 100 % of traffic regardless of
trace sampling, which is why a finding stays current when it carries one. The operation name comes from services.operations
in the config when a pattern matches, else the spans export's Operation Name column (most frequent among entry rows), else
services.default_operation, else a visible placeholder with the lookup instruction — never a hard-coded framework name.
"""
import re

PLACEHOLDER = "OPERATION"
LOOKUP = "replace OPERATION with the span operation name: Metrics Explorer → type trace. → filter service:{service}"


def operation(service_facts):
    op = service_facts.get("operation") or ""
    return op if op and not op.startswith("_todo") else PLACEHOLDER


def resource_wildcard(resource):
    """The resource_name tag value of one operation, as Datadog stores it: lower case, any character a tag cannot hold as '_',
    runs of '_' collapsed and trailing '_' trimmed ('POST /api/claims' -> 'post_/api/claims', 'GET /api/claims/?' ->
    'get_/api/claims/', '/inventory.v1.Inventory/GetStock' -> '/inventory.v1.inventory/getstock')."""
    v = re.sub(r"[^a-z0-9_:./-]", "_", resource.strip().lower())
    v = re.sub(r"_+", "_", v)[:200].rstrip("_")
    if v and (v[0].isalpha() or v[0] == "/"):
        return v
    return _old_wildcard(resource)


def _old_wildcard(resource):
    """A resource that does not start with a letter cannot be a tag value as it stands: fall back to its last path word."""
    r = re.sub(r"^(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\s+", "", resource.strip(), flags=re.I)
    r = r.split("?")[0].rstrip("/")
    tail = re.split(r"[/\\@]", r)[-1] if r else ""
    tail = re.sub(r"[^a-z0-9]+", "_", tail.lower()).strip("_")
    if not tail or len(tail) < 3 or tail in ("id", "api", "v1", "v2"):
        tail = re.sub(r"[^a-z0-9]+", "_", r.lower()).strip("_")[-40:]
    return f"*{tail}*"


def tag_value(service):
    """A service name as Datadog stores it in a tag: lower case, and any character a tag cannot hold becomes '_'."""
    return re.sub(r"[^a-z0-9_:./-]", "_", str(service).lower())


def scope(service, resource=None, env=None):
    parts = [f"service:{tag_value(service)}"]
    if env:
        parts.append(f"env:{env}")
    if resource:
        parts.append(f"resource_name:{resource_wildcard(resource)}")
    return "{" + ",".join(parts) + "}"


def hits(op, service, resource=None, env=None):
    return f"sum:trace.{op}.hits{scope(service, resource, env)}.as_count()"


def errors(op, service, resource=None, env=None):
    return f"sum:trace.{op}.errors{scope(service, resource, env)}.as_count()"


def failure_pct(op, service, resource=None, env=None):
    return f"({errors(op, service, resource, env)} / {hits(op, service, resource, env)}) * 100"


def p95(op, service, env=None):
    return f"p95:trace.{op}{scope(service, None, env)}"


def p95_by_resource(op, service, env=None):
    return f"p95:trace.{op}{scope(service, None, env)} by {{resource_name}}"


def node_balance(op, prefix, env=None):
    return f"sum:trace.{op}.hits{{service:{tag_value(prefix)}*{',env:' + env if env else ''}}} by {{service}}.as_count()"


def five_xx(op, service, env=None):
    return f"sum:trace.{op}.hits.by_http_status{{service:{tag_value(service)},http.status_class:5xx{',env:' + env if env else ''}}}.as_count()"


def recipe_line(items):
    """Render a list of (widget, query, note) as one blockquote line for the notebook."""
    parts = []
    for widget, query, note in items:
        parts.append(f"`/{widget}` · `{query}`" + (f" ({note})" if note else ""))
    return "> ➕ **live graph** — " + "; ".join(parts)


def placeholder_note(service):
    return LOOKUP.format(service=service)


def dependency_note(host):
    return f"open APM › Software Catalog › `{host}` › Dependencies for the callers and the live error rate (peer-tag metrics are not exposed on trace.* metrics)"
