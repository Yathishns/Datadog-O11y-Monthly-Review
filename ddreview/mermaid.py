"""Mermaid diagrams drawn from facts, in the conservative subset proven in Datadog notebooks.

Proven: ``flowchart TB`` / ``flowchart LR``, ``subgraph Id["title"]`` with its own ``direction``, quoted labels, ``[("db")]`` cylinders,
``(["host"])`` stadiums, ``{"x"}`` rhombus, ``-->|"label"|`` edges, ``-.->`` dashed edges, ``~~~`` invisible links, ``style`` lines,
``<br/>`` inside labels, and ``pie showData``. Unproven there (not generated): xychart, timeline, mindmap, classDef, frontmatter.

Ids are generated (upper-case prefix + slug) so none is a reserved word, none starts with o or x, and real names only ever
appear inside quoted labels. Budgets (20 nodes, with lint tolerating 22 so the dependencies map can keep a host's callers; 40 edges, 8 pie slices) are
enforced while drawing, with a visible
"+N more" node when something was left out. A small builder assembles the text — no f-string templating of braces.
"""
import re

from .narrative import LEDGER, pct, sec

RED = "fill:#ffe0e0,stroke:#c62828,color:#000"
AMBER = "fill:#fff3cd,stroke:#b8860b,color:#000"
GREEN = "fill:#e6f4ea,stroke:#2e7d32,color:#000"
MAX_NODES, MAX_EDGES, MAX_LABEL, MAX_TITLE, MAX_SLICES = 20, 40, 40, 80, 8
_ENTITY_HASH = re.compile(r"#(?=\w+;)")        # Mermaid decodes '#35;' / '#quot;' as entity codes, so a literal '#1;' needs escaping


def node_id(prefix, name, used):
    slug = re.sub(r"[^a-z0-9]+", "_", (name or "").lower()).strip("_")[:32].rstrip("_")
    base = prefix + "_" + (slug or "n")
    nid, k = base, 2
    while nid in used:
        nid, k = f"{base}_{k}", k + 1
    used.add(nid)
    return nid


def label(text, line2=None, limit=MAX_LABEL, line3=None):
    def clean(t):
        t = re.sub(r"[\r\n\t]+", " ", str(t)).replace("|", "·").replace("`", "")
        if len(t) > limit:
            t = t[: limit - 1] + "…"                       # cut the raw text, so an escape is never cut in half
        t = _ENTITY_HASH.sub("#35;", t)
        t = t.replace('"', "#quot;").replace("<", "#lt;").replace(">", "#gt;")
        return t or "(unnamed)"
    parts = [clean(text)] + ([clean(line2)] if line2 else []) + ([clean(line3)] if line3 else [])
    return '"' + "<br/>".join(parts) + '"'


class Diagram:
    def __init__(self, kind="flowchart TB"):
        self.kind, self.nodes, self.edges, self.links, self.styles, self.used = kind, {}, [], [], [], set()
        self.subgraphs = []

    def add_node(self, nid, text, shape="box", line2=None, where=None, line3=None):
        lab = label(text, line2, line3=line3)
        shapes = {"box": f"[{lab}]", "db": f"[({lab})]", "host": f"([{lab}])", "queue": f"[[{lab}]]", "diamond": f"{{{lab}}}"}
        self.nodes[nid] = (shapes[shape], where)
        return nid

    def add_edge(self, a, b, text=None, dashed=False):
        arrow = "-.->" if dashed else "-->"
        self.edges.append(f"  {a} {arrow}" + (f"|{label(text)}|" if text else "") + f" {b}")

    def add_link(self, a, b):
        """Invisible link: layout only, never counted as an edge."""
        self.links.append(f"  {a} ~~~ {b}")

    def style(self, nid, colour):
        if colour:
            self.styles.append(f"  style {nid} {colour}")

    def subgraph(self, sid, title, direction="LR"):
        self.subgraphs.append((sid, title, direction))
        return sid

    def text(self):
        out = [self.kind]
        placed, seen_titles = set(), {}
        for sid, title, direction in self.subgraphs:
            t = label(title, limit=MAX_TITLE)
            if t in seen_titles:
                t = label(title[: MAX_TITLE - 17] + "…" + title[-15:], limit=MAX_TITLE + 4)
            seen_titles[t] = sid
            out.append(f"  subgraph {sid}[{t}]")
            out.append(f"    direction {direction}")
            for nid, (shape, where) in self.nodes.items():
                if where == sid:
                    out.append(f"    {nid}{shape}")
                    placed.add(nid)
            out.append("  end")
        for nid, (shape, where) in self.nodes.items():
            if nid not in placed:
                out.append(f"  {nid}{shape}")
        out.extend(self.edges)
        out.extend(self.links)
        out.extend(self.styles)
        return "\n".join(out) + "\n"


def flagged_hosts(findings):
    """host or service -> worst severity across the findings that name it. An estate-wide finding about many hosts gives each
    host the severity that host earns on its own figures (subject.host_severity), so a healthy host it lists is never red."""
    rank = {"red": 2, "amber": 1, "info": 0}
    out = {}
    for f in findings:
        names = [f["subject"].get("service")] + list(f["subject"].get("hosts", [])) + list(f["subject"].get("nodes", [])) + list(f["subject"].get("services", []))
        own = f["subject"].get("host_severity") or {}
        for h in names:
            sev = own.get(h, f["severity"])
            if h and rank.get(sev, 0) > rank.get(out.get(h), -1):
                out[h] = sev
    return out


def counted_callers(F, dep):
    """The callers of a host that the review counts: for a non-production host, only production services (a UAT service
    calling a UAT host is not production reaching UAT)."""
    scope = F["coverage"].get("env_scope") or {}
    if dep.get("class") == "nonprod" and scope.get("column"):
        nonprod = set(scope.get("nonprod_services", []))
        return [c for c in dep["callers"] if c["service"] not in nonprod]
    return list(dep["callers"])


def fit(name, room=MAX_LABEL):
    """A long name cut in the middle, so two names that share a long prefix stay different ('northbank.onlinebanking.…customers-business')."""
    name = str(name)
    if len(name) <= room:
        return name
    tail = min(15, room // 2)
    return name[: room - tail - 1] + "…" + name[-tail:]


def dep_shape(F, name, dep):
    """A dependency's node shape: databases and caches are cylinders, queues are queues, everything else (HTTP clients and
    hosts) is a stadium; an integration with no type is a database."""
    t = (dep.get("type") or "").lower()
    if not t:
        from .classify import child_kind
        t = child_kind(name, "", "", _defaults_cfg())
    if t in ("db", "sql", "database", "cache", "redis", "memcached", "mongodb", "elasticsearch"):
        return "db"
    if t in ("queue", "kafka", "rabbitmq", "sqs", "sns", "pubsub"):
        return "queue"
    if t in ("other", "") and F["services"].get(name, {}).get("role") == "integration" and not re.search(r"(?i)(http|client|request|api|rest|grpc|soap)", name):
        return "db"                              # an ORM or driver named after its library
    return "host"


_DEFAULTS_CFG = []


def _defaults_cfg():
    if not _DEFAULTS_CFG:
        from .config import load_config
        _DEFAULTS_CFG.append(load_config(None))
    return _DEFAULTS_CFG[0]


def tech_line(tech, vol_part):
    """The second line of a service node: the stack word and the volume. A stack word that does not fit is shortened at a word
    boundary ('.NET (ASP.NET Core)' -> '.NET', 'Java servlet' -> 'Java') or left out, and 'entry service' is kept only whole;
    fit() is for names only."""
    room = MAX_LABEL - len(vol_part)
    t = str(tech or "")
    if not t:
        t = "entry service" if len("entry service") <= room else ""
    elif len(t) > room:
        t = t.split(" (")[0]
        while len(t) > room and " " in t:
            t = t.rsplit(" ", 1)[0]
        if len(t) > room:
            t = ""
    if not t:
        return vol_part[3:] if vol_part else "entry service"
    return t + vol_part


def fit_all(names, room=MAX_LABEL):
    """name -> label for every name a diagram may draw: fit() each, and where two cut names collide keep a window around the
    first character in which they differ ('…prod-01.database.windows.net' vs '…prod-02.database.windows.net')."""
    names = list(dict.fromkeys(str(x) for x in names if x))
    out = {nm: fit(nm, room) for nm in names}
    by_label = {}
    for nm, lab in out.items():
        by_label.setdefault(lab, []).append(nm)
    for lab, group in by_label.items():
        if len(group) < 2:
            continue
        for nm in group:
            i = min(next((j for j, (a, b) in enumerate(zip(nm, o)) if a != b), min(len(nm), len(o))) for o in group if o != nm)
            start = max(0, min(i - 6, len(nm) - (room - 1)))
            body = nm[start:start + room - (1 if start else 0) - (1 if start + room - 1 < len(nm) else 0)]
            out[nm] = ("…" if start else "") + body + ("…" if start + len(body) < len(nm) else "")
    return out


def _colour(sev):
    return RED if sev == "red" else (AMBER if sev == "amber" else None)


# ---------------------------------------------------------------- generators
def _worst(sevs):
    rank = {"red": 2, "amber": 1, "info": 0}
    sevs = [s for s in sevs if s]
    return max(sevs, key=lambda x: rank.get(x, 0)) if sevs else None


def _vol_suffix(F, tier):
    """A node's volume is on the review's tier, or says where it comes from (never a sample count beside weekly counts unmarked)."""
    if tier == F["window"]["volumes_tier"]:
        return ""
    return " in the sample" if tier == "sample" else " (Services list)"


def landscape(F, findings, cfg):
    """One subgraph per domain (direction LR), entry services as boxes (node groups collapsed), dependencies placed in the
    subgraph of their top caller, service-to-service calls where the data shows them. The node budget is shared across
    domains round-robin so every domain shows its busiest services; nodes are reserved first for the dependencies a finding
    names and for a '+N more' node in every domain that cannot show all its services, so what was left out is always said."""
    flagged = flagged_hosts(findings)
    domains = [(k, v) for k, v in F["domains"].items() if v["services"]]
    if not domains:
        return None
    rank = {"red": 2, "amber": 1}
    flagged_dep_callers = {c["service"] for name, dep in F["dependencies"].items() if name in flagged for c in counted_callers(F, dep)}
    domains.sort(key=lambda kv: (-max((rank.get(flagged.get(m), 0) for m in kv[1]["services"]), default=0),
                                 not any(m in flagged_dep_callers for m in kv[1]["services"])))     # stable: volume order within each rank
    # one node per service, or per node group within a domain (members in another domain are drawn there); flagged first
    queues, members_of = {}, {}
    vol_of = lambda svcs: sum(F["services"][m]["requests"]["all"] for m in svcs)
    vt = F["window"]["volumes_tier"]
    for dkey, dom in domains:
        heads, group_head = [], {}
        for svc in sorted(dom["services"], key=lambda x: (F["services"][x]["requests"].get("tier") != vt, -F["services"][x]["requests"]["all"], x)):
            g = F["services"][svc]["node_group"]
            if g and g in group_head:
                members_of[(dkey, group_head[g])].append(svc)
                continue
            if g:
                group_head[g] = svc
            members_of[(dkey, svc)] = [svc]
            heads.append(svc)
        heads.sort(key=lambda h: (not any(flagged.get(m) for m in members_of[(dkey, h)]), not any(m in flagged_dep_callers for m in members_of[(dkey, h)]),
                                  -vol_of(members_of[(dkey, h)]), h))
        queues[dkey] = heads
    sev_rank = {"red": 2, "amber": 1}
    deps_ranked = sorted(F["dependencies"].items(), key=lambda kv: (kv[0] not in flagged, -sev_rank.get(flagged.get(kv[0]), 0), -kv[1]["calls"], kv[0]))
    flagged_deps = [name for name, _ in deps_ranked if name in flagged]
    dep_reserve = min(len(flagged_deps), 6)

    def place(service_budget):
        """Round-robin over domains; returns the services shown per domain and those left over."""
        shown = {dkey: [] for dkey, _ in domains}
        left = {dkey: list(q) for dkey, q in queues.items()}
        budget = service_budget
        while budget > 0 and any(left.values()):
            for dkey, _ in domains:
                if budget <= 0:
                    break
                if left[dkey]:
                    shown[dkey].append(left[dkey].pop(0))
                    budget -= 1
        return shown, left

    room = MAX_NODES - dep_reserve
    for b in range(room, -1, -1):            # the most services that still leave a slot for every '+N more' node
        shown, left = place(b)
        overflow = sum(1 for dkey, _ in domains if left[dkey] and shown[dkey]) + (1 if any(not shown[dkey] for dkey, _ in domains) else 0)
        if sum(len(v) for v in shown.values()) + overflow <= room:
            break
    d = Diagram("flowchart TB")
    sg_by_key, svc_ids = {}, {}
    fits = fit_all([F["services"][m]["display"] for _, dom in domains for m in dom["services"]] + list(F["dependencies"]))
    grp = {F["services"][h]["display"]: len(ms) for (dk, h), ms in members_of.items() if len(ms) > 1}
    gfits = fit_all(list(grp), MAX_LABEL - max((len(f" × {k}") for k in grp.values()), default=4))
    for dkey, dom in domains:
        if not shown[dkey]:
            continue
        sg_by_key[dkey] = d.subgraph(node_id("B", dkey, d.used), f"{dom.get('icon', '')} {dom['label']}".strip())
        for svc in shown[dkey]:
            s = F["services"][svc]
            members = members_of[(dkey, svc)]
            line3 = None
            if len(members) > 1:
                ht = s["requests"].get("tier")
                same = [m for m in members if F["services"][m]["requests"].get("tier") == (vt if ht == vt else ht)]
                vol = vol_of(same)
                apart = len(members) - len(same)
                suffix = "" if ht == vt else _vol_suffix(F, ht)
                line3 = f"(+{apart} counted apart)" if apart else None
                times = f" × {len(members)}"
                vol_part = f" · {_short_vol(vol)}{suffix}"
                text, line2 = gfits.get(s["display"], fit(s["display"], MAX_LABEL - len(times))) + times, tech_line(s["tech"], vol_part)
            else:
                text = fits.get(s["display"]) or fit(s["display"])
                vol_part = f" · {_short_vol(s['requests']['all'])}{_vol_suffix(F, s['requests'].get('tier'))}" if s["requests"]["all"] else ""
                line2 = tech_line(s["tech"], vol_part)
            nid = node_id("S", svc, d.used)
            d.add_node(nid, text, "box", line2, where=sg_by_key[dkey], line3=line3)
            for m in members:
                svc_ids[m] = nid
            d.style(nid, _colour(_worst([flagged.get(m) for m in members])))
        hidden = [m for h in left[dkey] for m in members_of[(dkey, h)]]
        if hidden:
            mid = node_id("M", dkey, d.used)
            d.add_node(mid, f"+{len(hidden)} more service{'s' if len(hidden) != 1 else ''}", "box", where=sg_by_key[dkey])
            d.style(mid, _colour(_worst([flagged.get(m) for m in hidden])))
    unplaced = [dom for dkey, dom in domains if dkey not in sg_by_key]
    if unplaced:
        mid = d.add_node(node_id("M", "domains", d.used), f"+{len(unplaced)} more domain{'s' if len(unplaced) != 1 else ''}", "box")
        d.style(mid, _colour(_worst([flagged.get(m) for dom in unplaced for m in dom["services"]])))
    # edges: service -> service calls the data shows
    edge_count, drawn = 0, set()
    for e in sorted(F["edges"], key=lambda e: (-e["calls"], e["from"], e["to"])):
        a, b = svc_ids.get(e["from"]), svc_ids.get(e["to"])
        if not a or not b or a == b or (a, b) in drawn:
            continue
        if edge_count >= MAX_EDGES:
            break
        d.add_edge(a, b, None)
        drawn.add((a, b))
        edge_count += 1
    # dependencies: the ones a finding names first, then the busiest; placed in the subgraph of their busiest caller
    dep_budget = MAX_NODES - len(d.nodes)
    flagged_left, dropped_flagged, dropped_sevs, dropped_names = len(flagged_deps), 0, [], []
    for name, dep in deps_ranked:
        is_flagged = name in flagged
        if name in svc_ids:                           # the consuming app's box already carries these calls
            if is_flagged:
                d.style(svc_ids[name], _colour(_worst([flagged.get(name)] + [flagged.get(m) for m, i in svc_ids.items() if i == svc_ids[name]])))
            continue
        callers = [c for c in counted_callers(F, dep) if c["service"] in svc_ids]
        if not callers and not is_flagged:
            continue
        if is_flagged:
            room = dep_budget - (1 if (flagged_left > dep_budget or dropped_flagged) else 0)     # keep a slot for '+N more flagged hosts'
            flagged_left -= 1
            if room <= 0:
                dropped_flagged += 1
                dropped_sevs.append(flagged.get(name))
                dropped_names.append(name)
                continue
        elif dep_budget - (1 if dropped_flagged else 0) <= 0:
            continue
        take, seen_ids = [], set()
        for c in callers:
            cid = svc_ids[c["service"]]
            if cid in seen_ids or (cid, name) in drawn:
                continue
            seen_ids.add(cid)
            take.append(cid)
            if len(take) == 3:
                break
        if edge_count + len(take) > MAX_EDGES:
            if name in flagged:
                dropped_flagged += 1
                dropped_sevs.append(flagged.get(name))
                dropped_names.append(name)
            continue
        shape = dep_shape(F, name, dep)
        where = sg_by_key.get(F["services"][callers[0]["service"]]["domain"]) if callers else None
        nid = node_id("D", name, d.used)
        d.add_node(nid, fits.get(name) or fit(name), shape, _dep_line2(dep), where=where)
        dep_budget -= 1
        d.style(nid, _colour(flagged.get(name)))
        for cid in take:
            d.add_edge(cid, nid, None)
            drawn.add((cid, name))
            edge_count += 1
    F["meta"]["landscape_folded_hosts"] = dropped_names      # the landscape prompt folds exactly these
    if dropped_flagged:
        mid = d.add_node(node_id("M", "hosts", d.used), f"+{dropped_flagged} more flagged host{'s' if dropped_flagged != 1 else ''}", "host")
        d.style(mid, _colour(_worst(dropped_sevs)) or AMBER)
    # layout: chain unlinked nodes inside a subgraph so 'direction LR' lays them side by side; pair subgraphs into two columns
    linked = {x for e in d.edges for x in re.findall(r"[A-Z]_[A-Za-z0-9_]+", e)}
    for sid in sg_by_key.values():
        loose = [nid for nid, (_, where) in d.nodes.items() if where == sid and nid not in linked]
        for a, b in zip(loose, loose[1:]):
            d.add_link(a, b)
    sg_ids = list(sg_by_key.values())
    if len(sg_ids) >= 4:
        for i in range(0, len(sg_ids) - 2, 2):
            d.add_link(sg_ids[i], sg_ids[i + 2])
    return d.text() if d.nodes else None


def _short_vol(v):
    if v >= 1e6:
        return f"{v / 1e6:.1f} M req"
    if v >= 1e3:
        return f"{v / 1e3:.0f} k req"
    return f"{v} req"


def _dep_line2(dep):
    bits = []
    if dep.get("failure_known", True) and dep["err"]["pct"] >= 1:
        bits.append(f"{pct(dep['err'])} fail")
    if dep.get("p95_s") and dep["p95_s"] >= 1:
        bits.append(f"p95 {sec(dep['p95_s'])}")
    if dep["class"] == "nonprod":
        bits.append(f"{dep['marker']} host")
    return " · ".join(bits) or None


def request_share(F):
    """pie showData of requests by domain (all requests on the review's volume tier, the same base as the totals; top slices + Other)."""
    items = sorted(((k, v["label"], v["requests"]["all"]) for k, v in F["domains"].items()
                    if v["requests"]["all"] > 0 and v.get("volume_tier", F["window"]["volumes_tier"]) == F["window"]["volumes_tier"]), key=lambda t: (-t[2], t[0]))
    if not items:
        return None
    top, rest = items[:MAX_SLICES], items[MAX_SLICES:]
    lines = ["pie showData", f"  title Requests by domain, {F['window']['phrase']['volumes'].replace('in the ', '')}"]
    seen = set()
    for key, lbl, v in top:
        text = _pie_label(lbl)
        if text in seen or text.lower() == "other":
            text = _pie_label(f"{lbl[:MAX_LABEL - len(key) - 4]} ({key})")
        n = 2
        while text in seen:
            text, n = _pie_label(f"{lbl[:MAX_LABEL - 4]} {n}"), n + 1
        seen.add(text)
        lines.append(f'  "{text}" : {int(v)}')
    if rest:
        other = int(sum(v for _, _, v in rest))
        LEDGER.add(str(other))
        lines.append(f'  "Other" : {other}')
    return "\n".join(lines) + "\n"


def _pie_label(t):
    return re.sub(r"[:\"\r\n\t|<>]", " ", str(t)).strip()[:MAX_LABEL] or "(unnamed)"


def dependencies(F, findings):
    """flowchart LR: callers on the left, flagged hosts on the right; colours agree with the landscape (worst finding severity).
    Every host keeps a node: callers are drawn while the budget allows, then routed through one 'Production services' node,
    and hosts beyond twelve are summarised in a '+N more hosts' node."""
    hosts = []
    for f in findings:
        if f["detector"] in ("failing_dependencies", "nonprod_from_prod", "egress_and_telemetry"):
            for h in f["subject"].get("hosts", []):
                if h not in hosts and h in F["dependencies"]:
                    hosts.append(h)
    if not hosts:
        return None
    more = len(hosts) - 12
    hidden = hosts[12:]
    hosts = hosts[:12]
    flagged = flagged_hosts(findings)
    fits = fit_all(list(F["dependencies"]) + [s for dom in F["domains"].values() for s in dom["services"]] + hosts
                   + [c["service"] for h in hosts for c in counted_callers(F, F["dependencies"][h])])
    d = Diagram("flowchart LR")
    svc_ids, prod_nodes = {}, {}
    budget_total = MAX_NODES - (1 if more > 0 else 0)
    for i, h in enumerate(hosts):
        dep = F["dependencies"][h]
        nid = node_id("D", h, d.used)
        shape = dep_shape(F, h, dep)
        d.add_node(nid, fits.get(h) or fit(h), shape, None)
        d.style(nid, host_colour(dep, flagged.get(h)))
        text = _edge_text(F, dep, dep["err"], dep.get("p95_s"), dep.get("failure_known", True))
        callers = [c for c in counted_callers(F, dep) if F["services"].get(c["service"], {}).get("role") == "entry"][:3]
        hosts_left = len(hosts) - i - 1
        new_callers = [c for c in callers if c["service"] not in svc_ids]
        if callers and len(d.nodes) + hosts_left + len(new_callers) + (0 if prod_nodes.get("known") else 1) <= budget_total:
            for c in callers:
                cid = svc_ids.get(c["service"])
                if not cid:
                    cid = svc_ids[c["service"]] = node_id("S", c["service"], d.used)
                    d.add_node(cid, fits.get(c["service"]) or fit(c["service"]), "box")
                d.add_edge(cid, nid, _edge_text(F, dep, c["err"], c.get("p95_s"), c.get("failure_known", dep.get("failure_known", True)), only_caller=len(counted_callers(F, dep)) <= 1))
        else:
            key = "known" if callers else "unknown"
            if key not in prod_nodes:
                prod_nodes[key] = node_id("P", "production " + key, d.used)
                d.add_node(prod_nodes[key], "Production services" if callers else "Production services (caller unknown)", "box")
            d.add_edge(prod_nodes[key], nid, text)
    if more > 0:
        mid = d.add_node(node_id("M", "hosts", d.used), f"+{more} more host{'s' if more != 1 else ''}", "host")
        cols = [host_colour(F["dependencies"][h], flagged.get(h)) for h in hidden]
        d.style(mid, RED if RED in cols else (AMBER if AMBER in cols else None))
    return d.text()


def host_colour(dep, sev):
    """The dependencies map and its prompt colour a host as the landscape does (its own severity), and amber for any
    non-production, public or telemetry host a finding lists."""
    return RED if sev == "red" else (AMBER if (sev or dep["class"] in ("nonprod", "public", "telemetry")) else None)


def _edge_text(F, dep, err, p95, failure_known, only_caller=False):
    """Label of one caller's edge to a host, from that caller's own calls (the host-wide figures on the single 'Production
    services' edge): failures first, then the latency above the configured slow bar, then what kind of host it is."""
    slow = F["meta"]["thresholds"].get("dep_slow_s", 3)
    late = f"p95 {sec(p95)}" if p95 and p95 >= slow else None
    if not failure_known:
        kind = f"production → {dep['marker']}" if dep["class"] == "nonprod" else ("telemetry" if dep["class"] == "telemetry" else None)
        return " · ".join(x for x in (kind, late) if x) or "called"
    if err["pct"] >= 99.5:
        return "fails every call"
    if err["pct"] >= 1 or (err["pct"] > 0 and dep["err"]["pct"] >= 1):
        return f"{pct(err)} fail" + (f" · {late}" if late else "")
    if dep["err"]["pct"] >= 1 and only_caller:
        return f"{pct(dep['err'])} fail" + (f" · {late}" if late else "")   # the host's own figure (its list counts what the sample rule does not)
    if dep["err"]["pct"] >= 1:
        return "no failures" + (f" · {late}" if late else "")       # other callers' calls fail; this caller's do not
    if dep["class"] == "nonprod":
        return f"production → {dep['marker']}"
    if dep["class"] == "telemetry":
        return "telemetry"
    if late:
        return late
    return "public host" if dep["class"] == "public" else "called"


def _per_request(v):
    return f"~{v:.2f} per request" if v < 0.1 else f"~{v:.1f} per request"


def inside(F, svc):
    """flowchart LR for one service: request -> framework -> child kinds with per-request ratios (sample counts). Secret-store
    reads the finding flags get their own node even when they travel as HTTP calls."""
    a = F["anatomy"].get(svc)
    s = F["services"].get(svc)
    if not a or not s or len(a["children_per_request"]) < 2:
        return None
    d = Diagram("flowchart LR")
    r = node_id("R", "request", d.used)
    d.add_node(r, f"{s['display']} request", "box")
    c = node_id("C", "framework", d.used)
    d.add_node(c, s["tech"] or "application", "diamond")
    d.add_edge(r, c)
    T = F["meta"]["thresholds"]
    kinds = {"db": ("database", "db", T["db_per_request"]), "http": ("HTTP calls", "host", T["http_per_request"]), "cache": ("cache", "db", T["cache_per_request"]),
             "queue": ("queue", "queue", 1e9), "secret_store": ("secret store", "host", 0.0), "framework": ("framework events", "box", 1e9), "other": ("other spans", "box", 1e9)}
    ratios = dict(a["children_per_request"])
    sr = (a.get("secret_reads_per_request") or {}).get("value")
    if sr and sr >= T["secret_reads_per_request"] and "secret_store" not in ratios:
        ratios["secret_store"] = {"value": sr}
    db_node = None
    for kind, rt in sorted(ratios.items(), key=lambda kv: (-(kv[1]["value"] or 0), kv[0])):
        if kind not in kinds or not rt.get("value"):
            continue
        text, shape, thr = kinds[kind]
        nid = node_id("K", kind, d.used)
        if kind == "db":
            db_node = nid
        line2 = "bursts every minute" if kind == "secret_store" and a["bursts"] else None
        d.add_node(nid, text, shape, line2)
        d.add_edge(c, nid, _per_request(rt["value"]))
        fired = (kind == "secret_store" and rt["value"] >= T["secret_reads_per_request"]) or (kind != "secret_store" and rt["value"] >= thr)
        if fired:
            d.style(nid, AMBER)
    if a["cache_table_queries"]:
        nid = node_id("K", "cache_tables", d.used)
        d.add_node(nid, "cache and lock tables in the database", "db", f"{a['cache_table_queries']} statements in the sample")
        d.add_edge(db_node or c, nid)
        d.style(nid, AMBER)
    return d.text()


# ---------------------------------------------------------------- lint
_ID = re.compile(r"^[A-Z][A-Za-z0-9_]{1,40}$")


def lint(text):
    """Static checks on a Mermaid block. Returns a list of problems (empty when clean)."""
    problems = []
    lines = [l.rstrip() for l in text.strip().split("\n")]
    if not lines:
        return ["empty diagram"]
    head = lines[0].strip()
    if head not in ("flowchart TB", "flowchart LR", "pie showData"):
        problems.append(f"unsupported first line: {head}")
    if head == "pie showData":
        n_slices, labels = 0, set()
        for l in lines[1:]:
            ls = l.strip()
            if ls.startswith("title "):
                continue
            m = re.match(r'^"([^":]+)"\s*:\s*\d+$', ls)
            if not m:
                problems.append(f"pie line not '\"label\" : integer': {ls}")
                continue
            if m.group(1) in labels:
                problems.append(f"duplicate pie label: {m.group(1)}")
            labels.add(m.group(1))
            n_slices += 1
        if n_slices > MAX_SLICES + 1:
            problems.append(f"{n_slices} slices (max {MAX_SLICES + 1})")
        return problems
    declared, depth, sub_dir = set(), 0, []
    node_rx = re.compile(r'^([A-Z][A-Za-z0-9_]*)(\[\(|\(\[|\[\[|\[|\{)("[^"]*")(\)\]|\]\)|\]\]|\]|\})$')
    edge_rx = re.compile(r'^([A-Z][A-Za-z0-9_]*)\s+(-->|-\.->|~~~)(\|"[^"|]*"\|)?\s+([A-Z][A-Za-z0-9_]*)$')
    style_rx = re.compile(r"^style ([A-Z][A-Za-z0-9_]*) fill:#[0-9a-f]{6},stroke:#[0-9a-f]{6},color:#[0-9a-f]{3}$")
    sub_rx = re.compile(r'^subgraph ([A-Z][A-Za-z0-9_]*)\["[^"]*"\]$')
    edges, nodes = 0, 0
    for raw in lines[1:]:
        ls = raw.strip().rstrip(";").rstrip()
        if not ls or ls.startswith("%%"):
            continue
        if ls == "end":
            depth -= 1
            if not sub_dir or not sub_dir.pop():
                problems.append("a subgraph has no direction line")
            continue
        m = sub_rx.match(ls)
        if m:
            depth += 1
            declared.add(m.group(1))
            sub_dir.append(False)
            continue
        if ls.startswith("direction"):
            if not re.match(r"^direction (TB|BT|LR|RL)$", ls):
                problems.append(f"bad direction (TB|BT|LR|RL only): {ls}")
            if sub_dir:
                sub_dir[-1] = True
            else:
                problems.append("direction outside a subgraph")
            continue
        m = node_rx.match(ls)
        if m:
            nid = m.group(1)
            if not _ID.match(nid) or nid.lower() == "end":
                problems.append(f"bad node id {nid}")
            if nid in declared:
                problems.append(f"duplicate id {nid}")
            declared.add(nid)
            nodes += 1
            if m.group(3) == '""':
                problems.append(f"empty label on {nid}")
            continue
        m = edge_rx.match(ls)
        if m:
            if m.group(2) != "~~~":
                edges += 1
            for end in (m.group(1), m.group(4)):
                if end not in declared:
                    problems.append(f"edge endpoint {end} not declared")
            continue
        m = style_rx.match(ls)
        if m:
            if m.group(1) not in declared:
                problems.append(f"style target {m.group(1)} not declared")
            continue
        problems.append(f"unrecognised line: {ls}")
    if depth != 0:
        problems.append("subgraph/end unbalanced")
    if nodes > MAX_NODES + 2:
        problems.append(f"{nodes} nodes (max {MAX_NODES + 2})")
    if edges > MAX_EDGES:
        problems.append(f"{edges} edges (max {MAX_EDGES})")
    return problems
