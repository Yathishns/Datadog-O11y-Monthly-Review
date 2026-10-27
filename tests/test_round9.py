"""Tests for the ninth review round: Endpoints-list failures without a sample signal, workers judged on their Services-list
rate, health probes that do not speak for a worker, gRPC and queue spans that are not client calls, consumers named after an
app in their own process (per topic, with the tracer's tag for live graphs), method spans that are not HTTP calls, the
business operation of a service, agreed p95 targets, production-env narrowing, orphans into traced services, and month over
month (per-operation failure signal, the figure that fired, outlier trends, unnamed callers, rule changes for dependencies).
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
from ddreview import compare  # noqa: E402

T0 = datetime(2026, 11, 19, 10, 0, tzinfo=timezone.utc)
OCT0 = datetime(2026, 10, 19, 10, 0, tzinfo=timezone.utc)
BASE = ["Date", "Service", "Resource", "Duration", "Method", "Status Code"]
OPS = BASE + ["Operation Name"]
TREE = OPS + ["Type", "Trace ID", "Span ID", "Parent ID"]
EP_HEADER = ["Resource", "Service", "Requests", "Error Rate", "P95 Latency", "Monitor Counts"]


def write(path, header, rows):
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        w.writerows(rows)


def ts(i, base=T0):
    return (base + timedelta(seconds=i)).strftime("%Y-%m-%dT%H:%M:%S.000Z")


class Round9(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ddreview-r9t-")
        self.out = os.path.join(self.tmp, "out")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def folder(self, name):
        p = os.path.join(self.tmp, name)
        os.makedirs(p, exist_ok=True)
        return p

    def config(self, name, **extra):
        cfg = {"org": {"name": "Round Nine", "short": "round-nine"}}
        cfg.update(extra)
        p = os.path.join(self.tmp, name)
        with open(p, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh)
        return p

    def build(self, folder, *extra, month="November 2026"):
        args = ["build", folder, "--out", self.out, "--month", month, "--force"] + list(extra)
        if "--config" not in extra:
            args += ["--org", "Round Nine"]
        if "--previous" not in extra:
            args += ["--previous", "none"]
        buf = io.StringIO()
        with redirect_stdout(buf):
            try:
                return cli.main(args)
            except SystemExit as e:
                return e.code

    def facts(self, key="2026-11", short="round-nine"):
        p = os.path.join(self.out, short, key)
        with open(os.path.join(p, "facts.json"), encoding="utf-8") as fh:
            F = json.load(fh)
        names = [x for x in os.listdir(p) if x.endswith(".md") and "review" in x]
        with open(os.path.join(p, names[0]), encoding="utf-8") as fh:
            return F, fh.read(), p

    def two_months(self, oct_rows, nov_rows, header, header_nov=None):
        fo, fn = self.folder("oct"), self.folder("nov")
        write(os.path.join(fo, "spans.csv"), header, oct_rows)
        write(os.path.join(fn, "spans.csv"), header_nov or header, nov_rows)
        self.assertEqual(self.build(fo, month="October 2026"), 0)
        prev = os.path.join(self.out, "round-nine", "2026-10", "facts.json")
        self.assertEqual(self.build(fn, "--previous", prev), 0)
        return self.facts()

    @staticmethod
    def statuses(F):
        fm = F["mom"]["findings"]
        return {r["id"]: r.get("status", k) for k in ("closed", "unmeasured", "persisting", "new", "first_measured", "demoted", "promoted") for r in fm.get(k, [])}

    # ------------------------------------------------------------ failure signal
    def test_endpoints_list_failures_survive_a_sample_without_status(self):
        f = self.folder("ep")
        rows = [[ts(i % 600), "checkout-api", "POST /api/checkout", "90 ms", "POST", "servlet.request", "web"] for i in range(300)]
        rows += [[ts(i % 600), "checkout-api", "GET /api/cart", "40 ms", "GET", "servlet.request", "web"] for i in range(600)]
        write(os.path.join(f, "spans.csv"), ["Date", "Service", "Resource", "Duration", "Method", "Operation Name", "Type"], rows)
        write(os.path.join(f, "endpoints.csv"), EP_HEADER,
              [["POST /api/checkout", "checkout-api", 400000, 0.08, 0.5, "{}"], ["GET /api/cart", "checkout-api", 800000, 0.001, 0.2, "{}"]])
        self.assertEqual(self.build(f, "--window-days", "7"), 0)
        F, _, _ = self.facts()
        self.assertTrue(F["services"]["checkout-api"]["failure_known"])
        self.assertTrue([x for x in F["findings"] if x["detector"] == "failing_operations"])

    def test_health_probe_does_not_make_a_worker_judged(self):
        f = self.folder("probe")
        rows = [[ts(i % 600), "notify-worker", "orders", "80 ms", "", "", "kafka.consume", "queue"] for i in range(300)]
        rows += [[ts(i % 600), "notify-worker", "GET /actuator/health", "2 ms", "GET", 200, "servlet.request", "web"] for i in range(30)]
        write(os.path.join(f, "spans.csv"), OPS + ["Type"], rows)
        self.assertEqual(self.build(f), 0)
        F, md, _ = self.facts()
        self.assertFalse(F["services"]["notify-worker"]["failure_known"])
        self.assertNotIn("0 of 0", md)

    def test_worker_alone_in_its_domain_uses_its_services_list_rate(self):
        f = self.folder("sl")
        write(os.path.join(f, "spans.csv"), OPS + ["Type"], [[ts(i % 600), "billing-worker", "charge", "200 ms", "", "", "celery.run", "worker"] for i in range(150)])
        with open(os.path.join(f, "services.tsv"), "w", encoding="utf-8", newline="") as fh:
            fh.write("favorite\tType\tName\tHealth\tRequests\tError Rate\tp95 Latency (ns)\tMonitors\tSLOS\tEnvs\tLast Deploy\n")
            fh.write("\tworker\tbilling-worker\t\t1000000\t0.04\t300000000\t\t\tprod\t\n")
        cfg = self.config("sl.json", domains=[{"key": "billing", "label": "Billing", "services": ["^billing-"]}])
        self.assertEqual(self.build(f, "--config", cfg), 0)
        _, md, _ = self.facts()
        self.assertIn("40,000 of 1,000,000 (4.00 %)", md)
        self.assertIn("◇", md)

    # ------------------------------------------------------------ entries
    def test_library_span_below_a_grpc_server_is_not_a_service(self):
        f = self.folder("grpc")
        rows = []
        for i in range(600):
            rows.append([ts(i), "orders-api", "POST /orders", "120 ms", "POST", 200, "servlet.request", "web", f"t{i}", f"r{i}", ""])
            rows.append([ts(i), "orders-api", "/inventory/Reserve", "60 ms", "", "", "grpc.client", "rpc", f"t{i}", f"c{i}", f"r{i}"])
            rows.append([ts(i), "inventory", "/inventory/Reserve", "50 ms", "", "", "grpc.server", "rpc", f"t{i}", f"s{i}", f"c{i}"])
            rows.append([ts(i), "hibernate", "Session", "30 ms", "", "", "hibernate.session", "hibernate", f"t{i}", f"h{i}", f"s{i}"])
            rows.append([ts(i), "postgresql", "UPDATE stock", "5 ms", "", "", "postgresql.query", "sql", f"t{i}", f"q{i}", f"h{i}"])
        write(os.path.join(f, "spans.csv"), TREE, rows)
        self.assertEqual(self.build(f), 0)
        F, _, _ = self.facts()
        self.assertEqual(F["totals"]["requests"], 1200)
        self.assertNotEqual(F["services"].get("hibernate", {}).get("role"), "entry")
        self.assertIn("db", F["anatomy"]["inventory"]["children_per_request"])

    def test_kafka_consumer_named_per_topic_keeps_the_tracer_tag(self):
        f = self.folder("kafka")
        rows = []
        for i in range(1000):
            rows.append([ts(i % 600), "orders-api", "POST /orders", "90 ms", "POST", 200, "servlet.request", "web", f"t{i}", f"r{i}", ""])
            rows.append([ts(i % 600), "kafka", "Produce Topic orders", "3 ms", "", "", "kafka.produce", "queue", f"t{i}", f"p{i}", f"r{i}"])
            rows.append([ts(i % 600), "kafka", "Consume Topic orders", "200 ms", "", "", "kafka.consume", "queue", f"t{i}", f"c{i}", f"p{i}"])
            rows.append([ts(i % 600), "postgresql", "UPDATE stock", "3 ms", "", "", "postgresql.query", "sql", f"t{i}", f"q{i}", f"c{i}"])
            if i % 5 < 3:
                rows.append([ts(i % 600), "fulfilment-worker", "POST /v1/ship", "50 ms", "POST", 200, "okhttp.request", "http", f"t{i}", f"h{i}", f"c{i}"])
        write(os.path.join(f, "spans.csv"), TREE, rows)
        self.assertEqual(self.build(f), 0)
        F, md, _ = self.facts()
        self.assertEqual(F["services"]["fulfilment-worker"]["requests"]["all"], 1000)
        self.assertNotIn("kafka", {k for k, v in F["services"].items() if v["role"] == "entry"})
        self.assertEqual(F["services"]["fulfilment-worker"].get("metric_service"), "kafka")

    def test_method_spans_are_not_http_calls(self):
        f = self.folder("annot")
        rows = []
        for i in range(900):
            rows.append([ts(i % 600), "catalog-api", "GET /catalog", "300 ms", "GET", 200, "servlet.request", "web", f"t{i}", f"r{i}", ""])
            for k in range(4):
                rows.append([ts(i % 600), "catalog-api", f"CatalogService.step{k}", "20 ms", "", "", "trace.annotation", "", f"t{i}", f"a{i}-{k}", f"r{i}"])
            for k in range(3):
                rows.append([ts(i % 600), "catalog-api", f"ProductRepository.find{k}", "10 ms", "", "", "repository.operation", "", f"t{i}", f"b{i}-{k}", f"r{i}"])
            rows.append([ts(i % 600), "catalog-api", "GET /prices", "40 ms", "GET", 200, "okhttp.request", "http", f"t{i}", f"h{i}", f"r{i}"])
        write(os.path.join(f, "spans.csv"), TREE, rows)
        self.assertEqual(self.build(f), 0)
        F, _, _ = self.facts()
        self.assertAlmostEqual(F["anatomy"]["catalog-api"]["children_per_request"]["http"]["value"], 1.0)
        self.assertFalse([x for x in F["findings"] if x["detector"] == "chatty_requests"])

    def test_service_operation_is_never_a_health_probe(self):
        f = self.folder("op")
        rows = [[ts(i % 600), "inventory", "/inventory/Reserve", "3500 ms", "", "", "grpc.server", "rpc"] for i in range(2000)]
        rows += [[ts(i % 600), "inventory", "GET /actuator/health", "2 ms", "GET", 200, "servlet.request", "web"] for i in range(100)]
        write(os.path.join(f, "spans.csv"), OPS + ["Type"], rows)
        self.assertEqual(self.build(f), 0)
        F, _, _ = self.facts()
        self.assertEqual(F["services"]["inventory"]["operation"], "grpc.server")

    def test_slow_service_respects_agreed_targets(self):
        f = self.folder("slo")
        rows = [[ts(i % 600), "payments-api", "POST /payments", "4000 ms", "POST", 200] for i in range(1200)]
        rows += [[ts(i % 600), "payments-api", "GET /payments/{id}", "200 ms", "GET", 200] for i in range(800)]
        rows += [[ts(i % 600), "payments-api", "POST /refunds", "500 ms", "POST", 200] for i in range(100)]
        write(os.path.join(f, "spans.csv"), BASE, rows)
        cfg = self.config("slo.json", operations=[{"key": "pay", "label": "payments", "service": "^payments-api$", "match": "^POST /payments$", "slo_p95_s": 5.0}])
        self.assertEqual(self.build(f, "--config", cfg), 0)
        F, _, _ = self.facts()
        self.assertFalse([x for x in F["findings"] if x["detector"] in ("slow_hot_paths", "slow_operations")])

    def test_orphaned_call_into_a_traced_service_is_not_a_dependency(self):
        f = self.folder("orph")
        rows = []
        for i in range(1000):
            if i % 10:
                rows.append([ts(i % 600), "shop-api", "GET /cart", "90 ms", "GET", 200, "servlet.request", "web", f"t{i}", f"r{i}", "", ""])
            rows.append([ts(i % 600), "shop-api", "GET /prices", "40 ms", "GET", 500 if i % 4 == 0 else 200, "okhttp.request", "http", f"t{i}", f"h{i}", f"r{i}", "pricing-svc"])
            rows.append([ts(i % 600), "pricing-svc", "GET /prices", "30 ms", "GET", 200, "servlet.request", "web", f"t{i}", f"s{i}", f"h{i}", ""])
        write(os.path.join(f, "spans.csv"), TREE + ["peer.service"], rows)
        self.assertEqual(self.build(f), 0)
        F, _, _ = self.facts()
        self.assertNotIn("pricing-svc", F["dependencies"])

    # ------------------------------------------------------------ month over month
    def test_zero_to_zero_is_flat(self):
        T = {"mom_volume_pct": 10}
        self.assertEqual(compare._direction(0.0, 0.0, "ratio", T)[0], "flat")
        self.assertEqual(compare._direction(5.38, 5.43, "ratio", T)[0], "flat")

    def test_operation_whose_signal_is_gone_is_not_checked(self):
        hdr_oct = OPS + ["Type", "Error"]
        oct_rows = [[ts(i % 600, OCT0), "order-svc", "Consume Topic orders", "80 ms", "", "", "kafka.consume", "queue", "true" if i % 12 == 0 else "false"] for i in range(1200)]
        oct_rows += [[ts(i % 600, OCT0), "order-svc", "GET /orders", "40 ms", "GET", 200, "servlet.request", "web", "false"] for i in range(1200)]
        nov_rows = [[ts(i % 600)] + r[1:8] for i, r in enumerate(oct_rows)]
        F, _, _ = self.two_months(oct_rows, nov_rows, hdr_oct, header_nov=OPS + ["Type"])
        self.assertEqual({v for k, v in self.statuses(F).items() if k.startswith("failing_operations")}, {"unmeasured"})

    def test_outlier_only_finding_follows_its_operation(self):
        def rows(base, ms):
            out = [[ts(i % 600, base), "report-api", "GET /report", f"{ms} ms", "GET", 200] for i in range(1200)]
            out += [[ts(i % 600, base), "report-api", "GET /x", "50 ms", "GET", 200] for i in range(30000)]
            out += [[ts(i % 600, base), "report-api", "GET /y", "60 ms", "GET", 200] for i in range(20000)]
            return out
        F, _, _ = self.two_months(rows(OCT0, 12000), rows(T0, 25000), BASE)
        rows_ = [r for r in F["mom"]["findings"]["persisting"] if r["id"].startswith("slow_hot_paths")]
        self.assertTrue(rows_)
        self.assertEqual(rows_[0]["direction"], "worse")


if __name__ == "__main__":
    unittest.main()
