"""Classification rules shared by the aggregator and the detectors.

- resource_class: business | health | scanner | static | framework | by_design — noise classes are removed from business totals
  and every exclusion is listed in the appendix with its rule.
- child_kind: db | http | cache | queue | secret_store | framework | other — from the span Type column, the service name, or the resource shape.
- host_class: internal | cloud | public | telemetry | nonprod | unknown — for dependency hosts.
- is_entry: the entry-span rule ladder (parent_id empty -> type web/server -> span.kind server/consumer -> operation pattern ->
  method + resource shape), the rule actually used is recorded so the appendix can print it.
"""
import re

from .config import matches_any

_SQL = re.compile(r"(?i)^\s*(select|insert|update|delete|begin|commit|rollback|with|create|alter|drop|exec|execute|call|merge)\b")
_HOSTLIKE = re.compile(r"(?i)^(https?://)?([a-z0-9-]+\.)+[a-z]{2,}(:\d+)?(/|$)")
_VERB_PATH = re.compile(r"^(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\s+\S")
_CONTROLLER = re.compile(r"[A-Za-z0-9_]+@[A-Za-z0-9_]+")
_IP = re.compile(r"^(\d{1,3}\.){3}\d{1,3}$")
_RFC1918 = re.compile(r"^(10\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.|127\.)")


def resource_class(service, resource, cfg, auto_by_design=None):
    """Return (class, rule) where rule names the pattern that matched ('config:not_noise', 'builtin:health', 'config:by_design', 'auto:by_design')."""
    rx = cfg["_rx"]
    if matches_any(rx["not_noise"], resource):
        return "business", "config:not_noise"
    for svc_rx, res_rx, why in rx["by_design"]:
        if svc_rx.search(service) and res_rx.search(resource):
            return "by_design", f"config:by_design:{res_rx.pattern}"
    for cls in ("scanner", "health", "static", "framework"):
        for p in rx[cls]:
            if p.search(resource):
                return cls, f"{'builtin' if p.pattern in cfg['_defaults']['noise'][cls] else 'config'}:{cls}:{p.pattern}"
    if auto_by_design and (service, resource) in auto_by_design:
        return "by_design", "auto:by_design"
    return "business", ""


def is_auth_resource(resource, cfg):
    return matches_any(cfg["_rx"]["auth"], resource)


def is_secret_store(text, cfg):
    return bool(text) and matches_any(cfg["_rx"]["secret_store"], text)


def child_kind(service, resource, span_type, cfg):
    d = cfg["_defaults"]
    t = (span_type or "").lower()
    if is_secret_store(resource, cfg) or is_secret_store(service, cfg):
        return "secret_store"                    # a Key Vault read over HTTP is a secret read, whatever its Type says
    if t:
        for kind, names in d["child_kinds"].items():
            if t in names:
                return kind
    s = (service or "").lower()
    if is_secret_store(resource, cfg) or is_secret_store(service, cfg):
        return "secret_store"
    if s in d["db_services"] or any(s.endswith(x) for x in ("-db", "_db", "sql", "oracle", "postgres", "mysql", "mongodb")):
        return "db"
    if re.search(r"(redis|memcache)", s):
        return "cache"
    if re.search(r"(elastic|opensearch|postgres|mysql|mariadb|mongo|mssql|oracle|cassandra|dynamo)", s):
        return "db"
    for kind, rx in _INTEG_KIND:
        if rx.search(s):
            return kind
    if s in d["cache_services"] or "redis" in s or "memcache" in s:
        return "cache"
    if s in d["queue_services"] or any(x in s for x in ("kafka", "rabbit", "queue", "sqs", "servicebus")):
        return "queue"
    if s in d["http_services"] or t == "http":
        return "http"
    r = resource or ""
    if matches_any(cfg["_rx"]["framework"], r):
        return "framework"
    if _SQL.match(r):
        return "db"
    if is_hostlike(r.split("/", 1)[0].split(" ")[-1]) or _VERB_PATH.match(r):
        return "http"
    return "other"


_CCTLD = set("""ac ad ae af ag ai al am ao aq ar as at au aw ax az ba bb bd be bf bg bh bi bj bm bn bo br bs bt bw by bz ca cc cd cf cg
ch ci ck cl cm cn co cr cu cv cw cx cy cz de dj dk dm do dz ec ee eg er es et eu fi fj fk fm fo fr ga gd ge gf gg gh gi gl gm gn gp gq gr gs
gt gu gw gy hk hm hn hr ht hu id ie il im in io iq ir is it je jm jo jp ke kg kh ki km kn kp kr kw ky kz la lb lc li lk lr ls lt lu lv ly
ma mc md me mg mh mk ml mm mn mo mp mq mr ms mt mu mv mw mx my mz na nc ne nf ng ni nl no np nr nu nz om pa pe pf pg ph pk pl pm pn pr ps pt
pw py qa re ro rs ru rw sa sb sc sd se sg sh si sk sl sm sn so sr ss st su sv sx sy sz tc td tf tg th tj tk tl tm tn to tr tt tv tw tz ua
ug uk us uy uz va vc ve vg vi vn vu wf ws ye yt za zm zw""".split())
_GTLD = set("""com net org edu gov mil int info biz name pro aero coop museum mobi asia tel travel jobs dev cloud xyz ai io co me tv cc""".split())
_INTERNAL_TLD = {"internal", "local", "lan", "corp", "intra", "intranet", "home", "localdomain", "localhost", "svc", "cluster", "private"}


def is_hostlike(name):
    """'api.stripe.com', 'uat-apim.qhms.com', 'erp.acme.internal', '10.0.0.5' — a dependency named after its host, never an
    entry service. The last label must be a real top-level domain or an internal suffix, so dotted service names such as
    'northbank.onlinebanking.web' (the .NET default) and integration names such as 'postgres.db' or 'redis.client' (the Go
    tracer's defaults) are not hosts."""
    n = (name or "").strip().lower()
    if not n:
        return False
    if _IP.match(n):
        return True
    if not _HOSTLIKE.match(n):
        return False
    host = re.sub(r"^https?://", "", n).split("/", 1)[0].split(":", 1)[0]
    last = host.rsplit(".", 1)[-1]
    return last in _CCTLD or last in _GTLD or last in _INTERNAL_TLD


_INTEG_SUFFIX = re.compile(r"(^|[-_.])(sql-?server|mssql|http-?client|postgres(ql)?|mysql2?|mariadb|redis|mongo(db)?|elasticsearch|opensearch|"
                           r"memcached?|oracle|cassandra|dynamodb|aws-?sdk|grpc-?client|jdbc|pdo|curl|guzzle|okhttp|axios|"
                           r"kafka|rabbitmq|amqp(lib)?|aws-?sqs|aws-?sns|sqs|sns|msmq|(azure-?)?servicebus)$")
_DOT_CLIENT = re.compile(r"\.(db|client|sql|cache)$")
_AWS_CLIENT = re.compile(r"^aws\.[a-z0-9-]+$|.[-_]aws$")         # botocore 'aws.s3', Node aws-sdk '<app>-aws'
_CONSUME_RESOURCE = re.compile(r"(?i)^basic\.(deliver|get)\b")
_QUEUE_SUFFIX = re.compile(r"(^|.[-_.])(kafka|rabbitmq|amqp(lib)?|jms|aws-?sqs|aws-?sns|sqs|sns|msmq|(azure-?)?servicebus)$")
_BARE_QUEUE = re.compile(r"^(kafka|rabbitmq|amqp(lib)?|jms|aws-?sqs|aws-?sns|sqs|sns|msmq|(azure-?)?servicebus)$")


def is_bare_queue(service):
    """The tracer's own name for a messaging library ('kafka'), not an app's ('orders-kafka')."""
    return bool(_BARE_QUEUE.match((service or "").strip().lower()))
_INTEG_KIND = (("db", re.compile(r"(sql-?server|mssql|postgres(ql)?|mysql2?|mariadb|mongo(db)?|oracle|cassandra|dynamodb|jdbc|pdo|elasticsearch|opensearch|\.db|\.sql)$")),
               ("cache", re.compile(r"(redis|memcached?|\.cache)$")),
               ("http", re.compile(r"(http-?client|curl|guzzle|okhttp|axios|grpc-?client|aws-?sdk|\.client)$")))


def is_integration_name(service, cfg):
    """An integration library's pseudo-service: a configured or built-in name ('pdo', 'http-client'), or a name that ends in
    one, as tracers name them after the app ('online-banking-web-sql-server', 'orders-redis', 'postgres.db')."""
    s = (service or "").strip().lower()
    return bool(s) and (s in cfg["_integration"] or bool(_INTEG_SUFFIX.search(s)) or bool(_DOT_CLIENT.search(s)) or bool(_AWS_CLIENT.search(s)))


CLIENT_TYPES = {"db", "sql", "cache", "redis", "memcached", "http", "web_client", "client", "grpc_client", "queue", "kafka", "rabbitmq", "sqs", "sns", "mongodb", "cassandra", "elasticsearch"}
CLIENT_KINDS = {"client", "producer"}
SERVER_KINDS = {"server", "consumer"}
SERVER_TYPES = {"web", "server", "http_server"}


def consumes(row, cfg):
    """The span receives work from a queue: span.kind consumer, or a consumer operation (kafka.consume, celery.run) on a queue
    or worker span (or with no Type column)."""
    t = (row.get("type") or "").lower()
    op = row.get("operation") or ""
    return (row.get("kind") or "").lower() == "consumer" or (t in ("queue", "worker", "") and (
        (bool(op) and matches_any(cfg["_rx"].get("consumer_ops", []), op)) or bool(_CONSUME_RESOURCE.match(row.get("resource") or ""))))


def queue_consumer_name(service, row, cfg):
    """'<app>-kafka' (a tracer's name for the app's queue library) is that app's consumer when its span consumes."""
    return bool(_QUEUE_SUFFIX.search((service or "").lower())) and consumes(row, cfg)


def server_evidence(row, caps=None):
    """The span says it serves: Type web/server, span.kind server/consumer, or (with a Parent ID column) it is a root."""
    t = (row.get("type") or "").lower()
    k = (row.get("kind") or "").lower()
    return t in SERVER_TYPES or k in SERVER_KINDS or bool(caps and caps.get("parent_id") and not row.get("parent_id"))


def is_entry_candidate(row, cfg, caps=None):
    """A span that could be a service's entry at all: not an integration library, not a client-kind type or span.kind, and not a
    host name — unless the span itself says it serves (a dotted .NET name such as 'contoso.payments.services' with root
    server spans is a service, not a host)."""
    svc = (row.get("service") or "").lower()
    t = (row.get("type") or "").lower()
    k = (row.get("kind") or "").lower()
    if not svc or k in CLIENT_KINDS or (is_integration_name(svc, cfg) and not queue_consumer_name(svc, row, cfg)):
        return False
    consumer = consumes(row, cfg) and t in ("queue", "worker", "")
    if t in CLIENT_TYPES and k not in SERVER_KINDS and not consumer:
        return False
    return not is_hostlike(svc) or server_evidence(row, caps)


def is_entry(row, caps, cfg):
    """First-pass rule ladder on one span row (before parents are resolved). ``caps`` says which columns exist.
    Integration libraries and host-named services are never entries. With a Parent ID column, aggregate.py runs a second
    pass that also treats a span whose parent belongs to another service as that service's entry. Returns (bool, rule)."""
    svc = (row.get("service") or "").lower()
    candidate = is_entry_candidate(row, cfg, caps)
    if caps.get("parent_id"):
        return (row.get("parent_id", "") == "" and candidate), "a root span, or a span whose parent belongs to another service (integration libraries and host names excluded)"
    if caps.get("type"):
        t = (row.get("type") or "").lower()
        k = (row.get("kind") or "").lower()
        op_entry = bool(row.get("operation")) and matches_any(cfg["_rx"]["entry_ops"], row["operation"])
        served = t in SERVER_TYPES or k in SERVER_KINDS or (t in ("rpc", "worker", "serverless", "queue") and op_entry)
        return (served and candidate), "type is web/server (or span.kind server/consumer)"
    if caps.get("kind"):
        return ((row.get("kind") or "").lower() in ("server", "consumer") and candidate), "span.kind is server or consumer"
    peer = (row.get("peer") or "").lower()
    if peer and peer != svc:
        return False, "rows carrying a peer tag are outbound calls; other rows by operation name, method or resource shape"
    if caps.get("operation") and row.get("operation"):
        return (matches_any(cfg["_rx"]["entry_ops"], row["operation"]) and candidate), "operation name matches an entry pattern"
    if not caps.get("method"):
        return candidate, "no Method column: every service that is neither an integration library nor a host name is an entry service"
    rule = "method or a VERB /path resource present, and the service is neither an integration library nor a host name"
    if not candidate:
        return False, rule
    m = row.get("method", "")
    r = row.get("resource", "")
    return bool(m) or bool(_VERB_PATH.match(r)) or bool(_CONTROLLER.search(r)), rule


def _sans_tld(h):
    """'api.uat.acme.com' -> 'api.uat.acme'; 'myapp.dev' -> 'myapp' (.dev and .qa are top-level domains, not markers)."""
    head, dot, tail = h.rpartition(".")
    return head if dot and head and tail.isalpha() else h


def host_class(host, cfg):
    """internal | cloud | public | telemetry | nonprod | unknown. nonprod wins over the others because it is always a finding."""
    h = (host or "").strip().lower()
    if not h:
        return "unknown"
    rx = cfg["_rx"]
    if rx["nonprod"].search(_sans_tld(h)) and not matches_any(rx.get("production_hosts", []), h):
        return "nonprod"
    if matches_any(rx["telemetry"], h):
        return "telemetry"
    if "." not in h or _RFC1918.match(h) or h == "blocked-ip-address" or matches_any(rx["internal_suffixes"], h) or not is_hostlike(h):
        return "internal"
    for dom in cfg["org"].get("internal_domains", []):
        dom = dom.lower().lstrip("*").lstrip(".")
        if h == dom or h.endswith("." + dom):
            return "internal"
    if matches_any(rx["cloud"], h):
        return "cloud"
    if _IP.match(h):
        return "public"
    return "public"


def nonprod_marker(host, cfg):
    """'UAT', 'SIT', 'QA' for short markers; longer ones ('staging', 'sandbox') stay lower case so they read as words."""
    m = cfg["_rx"]["nonprod"].search(_sans_tld((host or "").strip().lower()))
    if not m or matches_any(cfg["_rx"].get("production_hosts", []), (host or "").lower()):
        return ""
    w = m.group(2)
    return w.upper() if len(w) <= 4 else w


def registrable_domain(host):
    parts = (host or "").lower().split(".")
    if len(parts) < 2 or _IP.match(host or ""):
        return ""
    if len(parts) >= 3 and parts[-2] in ("com", "co", "org", "net", "gov", "edu", "ac") and len(parts[-1]) == 2:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def short_resource(resource, cfg, maxlen=60):
    """Trim a resource name to what a reader recognises: Laravel Controller@method, long SQL to its first clause, long paths kept whole up to maxlen."""
    r = (resource or "").strip()
    for rx, rep in cfg["_rx"]["shorten"]:
        if rx.search(r):
            r = rx.sub(rep, r)
            break
    if _SQL.match(r):
        r = re.sub(r"\s+", " ", r)
        if len(r) > maxlen:
            r = r[: maxlen - 1] + "…"
        return r
    if len(r) > maxlen:
        r = r[: maxlen - 1] + "…"
    return r


_MASK = [(re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"), "{id}"),
         (re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"), "{email}"),
         (re.compile(r"\b[0-9a-fA-F]{16,}\b"), "{id}"),
         (re.compile(r"\d{6,}"), "{id}"),
         (re.compile(r"'[^']{0,80}'"), "'?'")]


_CUT_FRAGMENT = [(re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]*…"), "{email}…"), (re.compile(r"'[^']*…"), "'?'…"),
                 (re.compile(r"[0-9a-fA-F]{4,}-[0-9a-fA-F-]*…"), "{id}…"), (re.compile(r"(?<![\w])[0-9a-fA-F]*\d[0-9a-fA-F]{3,}…"), "{id}…"),
                 (re.compile(r"(?<![\w])\d{3,}…"), "{id}…")]


def redact(text):
    t = text or ""
    for rx, rep in _MASK + _CUT_FRAGMENT:
        t = rx.sub(rep, t)
    return t
