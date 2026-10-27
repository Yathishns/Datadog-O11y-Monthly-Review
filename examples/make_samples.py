"""Write synthetic Datadog exports for three fictional orgs, so the kit can be demonstrated and tested
without any real client data.

    python examples/make_samples.py [--out examples] [--previous]

Each org gets  examples/<org>/exports/  with files in the exact shapes Datadog produces:
- spans-*.csv ........ Trace Explorer export (Date, Service, Resource, Duration, Method, Status Code [, Trace ID, Span ID, Parent ID, Operation Name, Env, Host, Type])
- endpoints.csv ...... APM Software Catalog > Endpoints export (Name, Service, Request Count, Error Rate, P95 Duration, Monitor Counts)
- services.tsv ....... APM Software Catalog > Services export, tab-separated (favorite, Type, Name, Health, Requests, Error Rate, p95 Latency (ns), Monitors, SLOS, Envs, ...)
- inferred.tsv ....... the same list with *inferred services* (Intg column)

The three orgs deliberately differ in tech stack and in which exports exist:
- acme-retail ....... Java/Spring microservices (servlet.request), full trace IDs, all four exports
- northbank ......... .NET monolith (aspnet_core.request) + batch jobs, no trace IDs, spans + endpoints only
- quickship ......... Node/Express (express.request), spans only, default six columns
"""
import argparse
import csv
import json
import os
import random
import uuid
from datetime import datetime, timedelta, timezone


def dur_text(ms):
    """Datadog writes durations the way the UI shows them: '1.23 s', '456 ms', '789 µs'."""
    if ms >= 1000:
        return f"{ms / 1000:.2f} s"
    if ms >= 1:
        return f"{ms:.2f} ms"
    return f"{ms * 1000:.0f} µs"


class Org:
    def __init__(self, name, seed, operation, env, with_ids, start, minutes, services):
        self.name, self.rng, self.operation, self.env = name, random.Random(seed), operation, env
        self.with_ids, self.start, self.minutes, self.services = with_ids, start, minutes, services

    def spans(self):
        rows, t = [], self.start
        end = self.start + timedelta(minutes=self.minutes)
        while t < end:
            for svc in self.services:
                for res in svc["resources"]:
                    per_min = res["per_min"]
                    k = self.rng.randint(max(0, per_min - 2), per_min + 2) if per_min < 50 else int(self.rng.gauss(per_min, per_min * 0.1))
                    for _ in range(max(0, k)):
                        ts = t + timedelta(seconds=self.rng.uniform(0, 60))
                        ok = self.rng.random() >= res["err"]
                        ms = max(0.2, self.rng.lognormvariate(res["mu"], res["sigma"]))
                        status = res.get("ok_status", 200) if ok else self.rng.choice(res.get("err_status", [500, 502, 503]))
                        trace = uuid.UUID(int=self.rng.getrandbits(128)).hex if self.with_ids else None
                        root = uuid.UUID(int=self.rng.getrandbits(128)).hex[:16] if self.with_ids else None
                        rows.append((ts, svc["name"], res["name"], ms, res["method"], status, trace, root, "", self.operation, svc.get("host", ""), "web"))
                        for child in res.get("children", []):
                            cms = max(0.1, self.rng.lognormvariate(child["mu"], child["sigma"]))
                            cok = self.rng.random() >= child.get("err", 0)
                            cstatus = "" if not child.get("status") else (child["status"] if cok else 500)
                            rows.append((ts + timedelta(milliseconds=self.rng.uniform(0, ms)), child["service"], child["resource"], cms,
                                         "", cstatus, trace,
                                         uuid.UUID(int=self.rng.getrandbits(128)).hex[:16] if self.with_ids else None, root,
                                         child.get("operation", ""), "", child.get("type", "db")))
            t += timedelta(minutes=1)
        rows.sort(key=lambda r: r[0])
        return rows

    def write(self, out, sub="exports"):
        folder = os.path.join(out, self.name, sub)
        os.makedirs(folder, exist_ok=True)
        rows = self.spans()
        head = ["Date", "Service", "Resource", "Duration", "Method", "Status Code"]
        if self.with_ids:
            head += ["Trace ID", "Span ID", "Parent ID", "Operation Name", "Env", "Host", "Type"]
        with open(os.path.join(folder, f"spans-{self.start:%Y-%m-%d}.csv"), "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(head)
            for ts, svc, res, ms, meth, status, trace, span, parent, op, host, typ in rows:
                base = [ts.strftime("%Y-%m-%dT%H:%M:%S.") + f"{ts.microsecond // 1000:03d}Z", svc, res, dur_text(ms), meth, status]
                if self.with_ids:
                    base += [trace, span, parent, op, self.env, host, typ]
                w.writerow(base)
        return folder, len(rows)

    def write_endpoints(self, folder):
        """Weekly aggregates consistent with the span rates (requests = per_min x 10080)."""
        with open(os.path.join(folder, "endpoints.csv"), "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["Name", "Service", "Request Count", "Error Rate", "P95 Duration", "Monitor Counts"])
            for svc in self.services:
                for res in svc["resources"]:
                    if res.get("cadence_s"):
                        req = int(round(7 * 86400 / res["cadence_s"]))      # a real health check leaves exactly window / interval requests
                    else:
                        req = int(res["per_min"] * 10080 * self.rng.uniform(0.97, 1.03))
                    p95 = 2.718 ** (res["mu"] + 1.645 * res["sigma"]) / 1000
                    w.writerow([f"{res['method']} {res['name']}".strip(), svc["name"], req, f"{res['err']:.6f}", f"{p95:.6f}",
                                json.dumps({"non-synthetics": {"0": svc.get("monitors", 0)}})])
                for probe in svc.get("probes", []):
                    w.writerow([probe, svc["name"], self.rng.randint(3, 60), "1.000000", "0.012000", json.dumps({"non-synthetics": {}})])

    def write_services(self, folder, inferred):
        cols = ["favorite", "Type", "Name", "Health", "Requests", "Error Rate", "p95 Latency (ns)", "Monitors", "SLOS", "Envs", "Last Deploy"]
        with open(os.path.join(folder, "services.tsv"), "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh, delimiter="\t")
            w.writerow(cols)
            for svc in self.services:
                req = sum(int(r["per_min"] * 8640) for r in svc["resources"])
                err = sum(r["per_min"] * r["err"] for r in svc["resources"]) / max(1, sum(r["per_min"] for r in svc["resources"]))
                p95 = max(2.718 ** (r["mu"] + 1.645 * r["sigma"]) for r in svc["resources"]) * 1e6
                w.writerow(["", "web", svc["name"], "", req, f"{err:.6f}", int(p95),
                            json.dumps({"non-synthetics": {"0": svc.get("monitors", 0)}}), "", svc.get("envs", ""), ""])
        with open(os.path.join(folder, "inferred.tsv"), "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh, delimiter="\t")
            w.writerow(["favorite", "Type", "Name", "Intg", "Requests", "Error Rate", "p95 Latency (ns)"])
            for name, typ, req, err, p95_ms in inferred:
                w.writerow(["", typ, name, "", req, f"{err:.6f}", int(p95_ms * 1e6)])


def acme():
    pg = {"service": "postgres", "resource": "SELECT * FROM orders WHERE customer_id = ?", "mu": 1.2, "sigma": 0.6, "type": "sql", "operation": "postgresql.query"}
    redis = {"service": "redis", "resource": "GET session:*", "mu": -1.0, "sigma": 0.4, "type": "cache", "operation": "redis.command"}
    stripe = {"service": "api.stripe.com", "resource": "POST /v1/payment_intents", "mu": 6.2, "sigma": 0.5, "err": 0.08, "status": 200, "type": "http", "operation": "http.request"}
    uat = {"service": "uat-pricing.acme.internal", "resource": "GET /price", "mu": 5.5, "sigma": 0.7, "err": 0.0, "status": 200, "type": "http", "operation": "http.request"}
    services = [
        {"name": "checkout-api", "host": "chk-01", "monitors": 2, "envs": "prod", "resources": [
            {"name": "/api/checkout", "method": "POST", "per_min": 40, "err": 0.045, "mu": 6.6, "sigma": 0.5, "children": [pg, pg, redis, stripe]},
            {"name": "/api/cart/{id}", "method": "GET", "per_min": 120, "err": 0.002, "mu": 4.8, "sigma": 0.4, "children": [redis, pg]},
            {"name": "/actuator/health", "method": "GET", "per_min": 20, "err": 0.0, "mu": 1.0, "sigma": 0.2, "cadence_s": 3.0},
        ], "probes": ["GET /manager/html", "GET /.env", "PROPFIND /", "GET /wp-login.php"]},
        {"name": "catalog-api", "host": "cat-01", "monitors": 0, "envs": "prod", "resources": [
            {"name": "/api/products/{id}", "method": "GET", "per_min": 300, "err": 0.001, "mu": 5.9, "sigma": 0.5, "children": [pg, redis]},
            {"name": "/api/search", "method": "GET", "per_min": 90, "err": 0.003, "mu": 7.4, "sigma": 0.6, "children": [pg, pg, pg]},
            {"name": "/api/price", "method": "GET", "per_min": 60, "err": 0.0, "mu": 6.0, "sigma": 0.5, "children": [uat]},
            {"name": "/actuator/health", "method": "GET", "per_min": 20, "err": 0.0, "mu": 1.0, "sigma": 0.2, "cadence_s": 3.0},
        ]},
        {"name": "payment-gateway", "host": "pay-01", "monitors": 1, "envs": "prod", "resources": [
            {"name": "/pay/authorise", "method": "POST", "per_min": 35, "err": 0.09, "mu": 6.9, "sigma": 0.6, "children": [stripe, pg]},
            {"name": "/pay/refund", "method": "POST", "per_min": 3, "err": 0.33, "mu": 7.0, "sigma": 0.5, "children": [stripe]},
            {"name": "/oauth/token", "method": "POST", "per_min": 110, "err": 0.0, "mu": 4.0, "sigma": 0.3},
        ]},
        {"name": "notify-worker", "host": "ntf-01", "monitors": 0, "envs": "", "resources": [
            {"name": "send_email", "method": "", "per_min": 25, "err": 0.012, "mu": 5.3, "sigma": 0.8},
        ]},
    ]
    inferred = [("api.stripe.com", "http", 520000, 0.081, 520), ("uat-pricing.acme.internal", "http", 603000, 0.0, 250),
                ("postgres", "db", 9800000, 0.0004, 4), ("redis", "cache", 6100000, 0.0, 0.4),
                ("legacy-erp.acme.internal", "http", 1440, 1.0, 3100), ("www.youtube.com", "http", 300, 0.0, 800)]
    return Org("acme-retail", 1, "servlet.request", "prod", True, datetime(2026, 11, 19, 2, 0, tzinfo=timezone.utc), 9, services), inferred


def northbank():
    sql = {"service": "sql-server", "resource": "SELECT TOP 1 * FROM Accounts WHERE Id = @p0", "mu": 0.9, "sigma": 0.5, "type": "sql"}
    services = [
        {"name": "online-banking-web", "monitors": 3, "envs": "", "resources": [
            {"name": "Account/Summary", "method": "GET", "per_min": 180, "err": 0.0015, "mu": 6.3, "sigma": 0.5, "children": [sql, sql, sql]},
            {"name": "Payments/Transfer", "method": "POST", "per_min": 25, "err": 0.021, "mu": 7.1, "sigma": 0.6, "children": [sql, sql]},
            {"name": "Statements/Download", "method": "GET", "per_min": 12, "err": 0.004, "mu": 9.2, "sigma": 0.7, "children": [sql]},
            {"name": "health", "method": "GET", "per_min": 12, "err": 0.0, "mu": 0.7, "sigma": 0.2, "cadence_s": 5.0},
        ], "probes": ["GET /manager/status", "BADMETHOD /QuAlYs", "GET /phpinfo.php"]},
        {"name": "batch-posting-01", "monitors": 0, "envs": "", "resources": [
            {"name": "Batch/PostLedger", "method": "POST", "per_min": 4, "err": 0.31, "mu": 8.5, "sigma": 0.4, "children": [sql]},
            {"name": "Batch/Reconcile", "method": "POST", "per_min": 4, "err": 0.30, "mu": 8.3, "sigma": 0.4, "children": [sql]},
        ]},
        {"name": "online-banking-web-02", "monitors": 3, "envs": "", "resources": [
            {"name": "Account/Summary", "method": "GET", "per_min": 100, "err": 0.0017, "mu": 6.4, "sigma": 0.5, "children": [sql, sql, sql]},
            {"name": "Payments/Transfer", "method": "POST", "per_min": 14, "err": 0.024, "mu": 7.1, "sigma": 0.6, "children": [sql, sql]},
            {"name": "health", "method": "GET", "per_min": 12, "err": 0.0, "mu": 0.7, "sigma": 0.2, "cadence_s": 5.0},
        ]},
    ]
    return Org("northbank", 2, "aspnet_core.request", "", False, datetime(2026, 11, 19, 9, 30, tzinfo=timezone.utc), 12, services), None


def quickship():
    services = [
        {"name": "shipping-api", "resources": [
            {"name": "GET /v1/rates", "method": "GET", "per_min": 150, "err": 0.006, "mu": 5.0, "sigma": 0.5},
            {"name": "POST /v1/labels", "method": "POST", "per_min": 40, "err": 0.07, "mu": 7.6, "sigma": 0.6},
            {"name": "GET /healthz", "method": "GET", "per_min": 30, "err": 0.0, "mu": 0.5, "sigma": 0.2},
        ]},
        {"name": "tracking-api", "resources": [
            {"name": "GET /v1/track/:id", "method": "GET", "per_min": 400, "err": 0.0008, "mu": 4.6, "sigma": 0.5},
            {"name": "GET /healthz", "method": "GET", "per_min": 30, "err": 0.0, "mu": 0.5, "sigma": 0.2},
        ]},
    ]
    return Org("quickship", 3, "express.request", "", False, datetime(2026, 11, 19, 14, 0, tzinfo=timezone.utc), 10, services), None


def previous_month(org, inferred):
    """A plausible October for the same org: one month earlier, refunds failing worse, the ERP still alive, no UAT reference yet."""
    org.name = org.name
    org.start = org.start.replace(month=org.start.month - 1, day=22)
    org.rng = random.Random(org.rng.random())
    for svc in org.services:
        for res in svc["resources"]:
            if res["name"] == "/pay/refund":
                res["err"] = 0.45
            if res["name"] == "/api/checkout":
                res["err"] = 0.021
            if res["name"] == "Batch/PostLedger":
                res["err"] = 0.05
            if res["name"] == "/api/price":
                res["children"] = []
    if inferred:
        inferred = [(n, t, r, (0.02 if n == "legacy-erp.acme.internal" else e), p) for n, t, r, e, p in inferred if n != "uat-pricing.acme.internal"]
    return org, inferred


def main():
    ap = argparse.ArgumentParser(description="Write synthetic Datadog exports for the example orgs.")
    ap.add_argument("--out", default=os.path.dirname(os.path.abspath(__file__)))
    ap.add_argument("--previous", action="store_true", help="also write exports-previous/ (one month earlier) for month-over-month demos")
    a = ap.parse_args()
    for build in (acme, northbank, quickship):
        for variant in (("exports", False), ("exports-previous", True)) if a.previous else (("exports", False),):
            org, inferred = build()
            if variant[1]:
                org, inferred = previous_month(org, inferred)
            folder, n = org.write(a.out, variant[0])
            if org.name != "quickship":
                org.write_endpoints(folder)
            if inferred:
                org.write_services(folder, inferred)
            print(f"{org.name:12} {variant[0]:16} {n:>7,} span rows -> {folder}")


if __name__ == "__main__":
    main()
