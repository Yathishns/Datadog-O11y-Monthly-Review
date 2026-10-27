"""Tests for the eighth review round: production judged by the calling span's env, bare 'kafka' consumers, downstream gRPC
services, failure signal from entry spans, small failing operations, dependency p95 over all callers, orphaned client spans,
the HTTP operation of a service that also runs jobs, month over month (integration-named hosts, failure-rule changes, hosts
the export stops naming), count words, operation names written as metrics, and agreed p95 targets.
python -m unittest discover -s tests"""
import csv
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from ddreview import __main__ as cli  # noqa: E402
from ddreview import config, validate  # noqa: E402

T0 = datetime(2026, 11, 19, 10, 0, tzinfo=timezone.utc)
OCT0 = datetime(2026, 10, 19, 10, 0, tzinfo=timezone.utc)
BASE = ["Date", "Service", "Resource", "Duration", "Method", "Status Code"]
OPS = BASE + ["Operation Name"]
TREE = OPS + ["Type", "Trace ID", "Span ID", "Parent ID"]


def write(path, header, rows):
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        w.writerows(rows)


def ts(i, base=T0):
    return (base + timedelta(seconds=i)).strftime("%Y-%m-%dT%H:%M:%S.000Z")


class Round8(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ddreview-r8t-")
        self.out = os.path.join(self.tmp, "out")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def folder(self, name):
        p = os.path.join(self.tmp, name)
        os.makedirs(p, exist_ok=True)
        return p

    def config(self, name, **extra):
        cfg = {"org": {"name": "Round Eight", "short": "round-eight"}}
        cfg.update(extra)
        p = os.path.join(self.tmp, name)
        with open(p, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh)
        return p

    def build(self, folder, *extra, month="November 2026"):
        args = ["build", folder, "--out", self.out, "--month", month, "--force"] + list(extra)
        if "--config" not in extra:
            args += ["--org", "Round Eight"]
        if "--previous" not in extra:
            args += ["--previous", "none"]
        buf = io.StringIO()
        with redirect_stdout(buf):
            try:
                return cli.main(args)
            except SystemExit as e:
                return e.code

    def facts(self, key="2026-11", short="round-eight"):
        p = os.path.join(self.out, short, key)
        with open(os.path.join(p, "facts.json"), encoding="utf-8") as fh:
            F = json.load(fh)
        names = [x for x in os.listdir(p) if x.endswith(".md") and "review" in x]
        with open(os.path.join(p, names[0]), encoding="utf-8") as fh:
            return F, fh.read(), p

    def two_months(self, oct_rows, nov_rows, header, header_nov=None, cfg_oct=None, cfg_nov=None):
        fo, fn = self.folder("oct"), self.folder("nov")
        write(os.path.join(fo, "spans.csv"), header, oct_rows)
        write(os.path.join(fn, "spans.csv"), header_nov or header, nov_rows)
        co = ["--config", cfg_oct] if cfg_oct else []
        cn = ["--config", cfg_nov or cfg_oct] if (cfg_nov or cfg_oct) else []
        self.assertEqual(self.build(fo, *co, month="October 2026"), 0)
        prev = os.path.join(self.out, "round-eight", "2026-10", "facts.json")
        self.assertEqual(self.build(fn, *cn, "--previous", prev), 0)
        return self.facts()

    @staticmethod
    def statuses(F):
        fm = F["mom"]["findings"]
        return {r["id"]: r.get("status", k) for k in ("closed", "unmeasured", "persisting", "new", "first_measured", "demoted", "promoted") for r in fm.get(k, [])}

    # ------------------------------------------------------------ environments
    def test_uat_calls_from_the_uat_instances_of_a_prod_service_are_not_production(self):
        f = self.folder("env")
        rows = []
        for i in range(400):
            rows.append([ts(i), "catalog-api", "GET /items", "50 ms", "GET", 200, "servlet.request", "web", "", "prod"])
        for i in range(60):
            rows.append([ts(i), "catalog-api", "GET /items", "50 ms", "GET", 200, "servlet.request", "web", "", "uat"])
            rows.append([ts(i), "catalog-api", "GET /price", "40 ms", "GET", 200, "okhttp.request", "http", "pricing.uat.example.net", "uat"])
        write(os.path.join(f, "spans.csv"), OPS + ["Type", "peer.service", "Env"], rows)
        self.assertEqual(self.build(f), 0)
        F, _, _ = self.facts()
        self.assertFalse([x for x in F["findings"] if x["detector"] == "nonprod_from_prod"])

    def test_uat_host_reached_through_an_unresolved_library_stays_a_finding(self):
        f = self.folder("lib")
        rows = []
        for i in range(600):
            rows.append([ts(i), "orders-api", "POST /orders", "90 ms", "POST", 200, "aspnet_core.request", "web", "", "prod"])
            rows.append([ts(i), "orders-api-http-client", "POST /quote", "40 ms", "POST", 500 if i % 4 == 0 else 200, "http.request", "http", "api.uat.partner.com", "prod"])
        write(os.path.join(f, "spans.csv"), OPS + ["Type", "peer.service", "Env"], rows)
        self.assertEqual(self.build(f), 0)
        F, _, _ = self.facts()
        self.assertEqual(F["dependencies"]["api.uat.partner.com"]["calls"], 600)
        self.assertTrue([x for x in F["findings"] if x["detector"] == "nonprod_from_prod"])

    # ------------------------------------------------------------ entries and joins
    def test_bare_kafka_consumer_runs_the_worker_app(self):
        f = self.folder("kafka")
        rows = []
        for i in range(600):
            rows.append([ts(i), "order-api", "POST /orders", "90 ms", "POST", 200, "servlet.request", "web", f"t{i}", f"r{i}", ""])
            rows.append([ts(i), "kafka", "Produce Topic orders", "3 ms", "", "", "kafka.produce", "queue", f"t{i}", f"p{i}", f"r{i}"])
            rows.append([ts(i), "kafka", "Consume Topic orders", "200 ms", "", "", "kafka.consume", "queue", f"t{i}", f"c{i}", f"p{i}"])
            for k in range(4):
                rows.append([ts(i), "postgresql", "UPDATE stock", "3 ms", "", "", "postgresql.query", "sql", f"t{i}", f"q{i}-{k}", f"c{i}"])
            rows.append([ts(i), "order-processor", "POST /v1/shipments", "50 ms", "POST", 200, "okhttp.request", "http", f"t{i}", f"h{i}", f"c{i}"])
        write(os.path.join(f, "spans.csv"), TREE, rows)
        self.assertEqual(self.build(f), 0)
        F, _, _ = self.facts()
        self.assertEqual(F["services"]["order-processor"]["role"], "entry")
        self.assertNotIn("db", F["anatomy"]["order-api"]["children_per_request"])

    def test_downstream_grpc_service_below_a_client_call_is_promoted(self):
        f = self.folder("grpc")
        rows = []
        for i in range(600):
            rows.append([ts(i), "checkout", "GET /checkout", "90 ms", "GET", 200, "http.request", "web", f"t{i}", f"r{i}", ""])
            rows.append([ts(i), "grpc.client", "/recs.Recs/Get", "40 ms", "", "", "grpc.client", "rpc", f"t{i}", f"g{i}", f"r{i}"])
            rows.append([ts(i), "recommendation", "/recs.Recs/Get", "35 ms", "", "", "grpc", "grpc", f"t{i}", f"s{i}", f"g{i}"])
            for k in range(2):
                rows.append([ts(i), "postgres", "SELECT 1", "3 ms", "", "", "postgres.query", "sql", f"t{i}", f"q{i}-{k}", f"s{i}"])
        write(os.path.join(f, "spans.csv"), TREE, rows)
        self.assertEqual(self.build(f), 0)
        F, _, _ = self.facts()
        self.assertEqual(F["services"]["recommendation"]["role"], "entry")
        self.assertNotIn("db", F["anatomy"]["checkout"]["children_per_request"])

    def test_a_workers_own_client_spans_do_not_record_its_failures(self):
        f = self.folder("sig")
        rows = []
        for i in range(300):
            rows.append([ts(i), "orders-api", "GET /x", "50 ms", "GET", 500 if i < 12 else 200, "servlet.request", "web", f"a{i}", f"r{i}", ""])
        for i in range(200):
            rows.append([ts(i), "notifier", "orders-topic", "80 ms", "", "", "kafka.consume", "queue", f"b{i}", f"k{i}", ""])
            rows.append([ts(i), "notifier", "POST /notify", "30 ms", "POST", 503 if i < 40 else 200, "http.request", "http", f"b{i}", f"h{i}", f"k{i}"])
        write(os.path.join(f, "spans.csv"), TREE, rows)
        self.assertEqual(self.build(f), 0)
        F, _, _ = self.facts()
        self.assertFalse(F["services"]["notifier"]["failure_known"])

    def test_small_failing_operations_are_named(self):
        f = self.folder("small")
        rows = [[ts(i % 600), "shop-web", "GET /api/products", "40 ms", "GET", 200] for i in range(2000)]
        rows += [[ts(i), "shop-web", "DELETE /api/cart/{id}", "40 ms", "DELETE", 500 if i < 6 else 200] for i in range(40)]
        write(os.path.join(f, "spans.csv"), BASE, rows)
        self.assertEqual(self.build(f), 0)
        _, md, _ = self.facts()
        self.assertIn("DELETE /api/cart/{id}", md)
        self.assertNotIn("no business operation on `shop-web` fails more than", md)

    def test_dependency_p95_covers_every_caller_and_failing_callers_come_first(self):
        f = self.folder("p95")
        rows = []
        for i in range(2000):
            rows.append([ts(i % 600), "orders-api", "GET /stock", "40 ms", "GET", 200, "okhttp.request", "http", "inventory.prod.acme.internal"])
        for i in range(1200):
            rows.append([ts(i % 600), "invoice-api", "GET /stock", "5000 ms", "GET", 503, "okhttp.request", "http", "inventory.prod.acme.internal"])
        for svc in ("orders-api", "invoice-api"):
            rows += [[ts(i), svc, "GET /x", "50 ms", "GET", 200, "servlet.request", "web", ""] for i in range(300)]
        write(os.path.join(f, "spans.csv"), OPS + ["Type", "peer.service"], rows)
        self.assertEqual(self.build(f), 0)
        F, _, _ = self.facts()
        d = F["dependencies"]["inventory.prod.acme.internal"]
        self.assertEqual(d["p95_s"], 5.0)
        self.assertEqual(d["callers"][0]["service"], "invoice-api")

    def test_orphaned_client_span_counts_toward_its_peer_host(self):
        f = self.folder("orphan")
        rows = []
        for i in range(2000):
            root_in = i % 20 != 0
            if root_in:
                rows.append([ts(i % 600), "payment-api", "POST /pay", "90 ms", "POST", 200, "aspnet_core.request", "web", f"t{i}", f"r{i}", "", ""])
            rows.append([ts(i % 600), "payment-api-http-client", "POST /v1/charges", "60 ms", "POST", 503 if i % 5 == 0 else 200, "http.request", "http", f"t{i}", f"h{i}", f"r{i}",
                         "api.stripe.com"])
        write(os.path.join(f, "spans.csv"), TREE + ["peer.service"], rows)
        self.assertEqual(self.build(f), 0)
        F, _, _ = self.facts()
        self.assertEqual(F["dependencies"]["api.stripe.com"]["calls"], 2000)
        self.assertNotIn("payment-api-http-client", F["dependencies"])

    def test_http_operation_names_the_web_endpoints_of_a_service_that_runs_jobs(self):
        f = self.folder("laravel")
        rows = []
        for i in range(600):
            rows.append([ts(i), "shop", "POST /checkout", "120 ms", "POST", 200, "laravel.request", "web", f"t{i}", f"r{i}", ""])
            for j in range(2):
                rows.append([ts(i), "shop", "App\\Jobs\\Ship", "3 ms", "", "", "laravel.queue.push", "queue", f"t{i}", f"p{i}{j}", f"r{i}"])
                rows.append([ts(i), "shop", "App\\Jobs\\Ship", "90 ms", "", "", "laravel.queue.process", "queue", f"t{i}", f"j{i}{j}", f"p{i}{j}"])
        write(os.path.join(f, "spans.csv"), TREE, rows)
        self.assertEqual(self.build(f), 0)
        F, _, _ = self.facts()
        self.assertEqual(F["services"]["shop"]["operation"], "laravel.request")

    # ------------------------------------------------------------ month over month
    def test_integration_named_dependency_then_a_named_host_persists(self):
        def rows(base, peer):
            out = []
            for i in range(1500):
                out.append([ts(i % 600, base), "orders-web", "POST Orders/Create", "100 ms", "POST", 200, "aspnet_core.request", "web", f"t{i}", f"r{i}", ""] + ([""] if peer else []))
                out.append([ts(i % 600, base), "orders-web-http-client", "POST /pay", "60 ms", "POST", 502 if i % 5 == 0 else 200, "http.request", "http", f"t{i}", f"h{i}", f"r{i}"]
                           + (["api.payprovider.com"] if peer else []))
            return out
        F, _, _ = self.two_months(rows(OCT0, False), rows(T0, True), TREE, header_nov=TREE + ["peer.service"])
        st = self.statuses(F)
        self.assertIn("persisting", [v for k, v in st.items() if k.startswith("failing_dependencies")])

    def test_a_changed_failure_rule_is_not_compared(self):
        rows = lambda base: [[ts(i % 600, base), "shop-api", "GET /orders/{id}", "40 ms", "GET", 404 if i % 3 == 0 else 200] for i in range(1500)]
        F, md, _ = self.two_months(rows(OCT0), rows(T0), BASE, cfg_oct=self.config("o.json"), cfg_nov=self.config("n.json", failure={"rule": "non_2xx"}))
        self.assertFalse(F["mom"]["comparable"]["rates"])
        self.assertNotIn("closed", self.statuses(F).values())

    def test_uat_finding_is_not_checked_when_the_export_stops_naming_hosts(self):
        def rows(base, peer):
            out = []
            for i in range(1500):
                out.append([ts(i % 600, base), "shop-api", "GET /items", "50 ms", "GET", 200, "servlet.request", "web", f"t{i}", f"r{i}", ""] + ([""] if peer else []))
                out.append([ts(i % 600, base), "shop-api", "GET /prices", "40 ms", "GET", 200, "okhttp.request", "http", f"t{i}", f"h{i}", f"r{i}"] + (["uat-pricing.shop.internal"] if peer else []))
            return out
        F, _, _ = self.two_months(rows(OCT0, True), rows(T0, False), TREE + ["peer.service"], header_nov=TREE)
        self.assertEqual({v for k, v in self.statuses(F).items() if k.startswith("nonprod_from_prod")}, {"unmeasured"})

    # ------------------------------------------------------------ validate, config
    def test_pronoun_one_is_not_a_count(self):
        orig = "## Since last month\n\nOne is new and seven persist.\n"
        errors, _ = validate.check_against(orig + "Each one is tracked; no one was missed.\n", orig + "\n")
        self.assertFalse(errors)

    def test_operation_written_as_a_metric_is_normalised(self):
        cfg = config.load_config(self.config("op.json", services={"operations": {"shop": "trace.express.request.hits"}}))
        self.assertEqual(cfg["services"]["operations"]["shop"], "express.request")

    def test_operation_within_its_agreed_p95_is_not_slow(self):
        f = self.folder("slo")
        rows = [[ts(i % 600), "report-api", "GET /reports/monthly", "12000 ms", "GET", 200] for i in range(300)]
        rows += [[ts(i % 600), "report-api", "GET /x", "50 ms", "GET", 200] for i in range(2000)]
        write(os.path.join(f, "spans.csv"), BASE, rows)
        cfg = self.config("slo.json", operations=[{"key": "monthly", "label": "monthly reports", "service": "^report-api$", "match": "monthly", "slo_p95_s": 30}])
        self.assertEqual(self.build(f, "--config", cfg), 0)
        F, _, _ = self.facts()
        self.assertFalse([x for x in F["findings"] if x["detector"] == "slow_operations"])


if __name__ == "__main__":
    unittest.main()
