"""Tests for the seventh review round: calls no join could attribute, same-service queue jobs, library-named spans under
Parent ID, an app that produces and consumes through one library name, files read by the fallback rule, amqplib
deliveries, Services-list-only integrations, left-out callers, failure signals per rule and per service, number words
under --against, init's domain guess, SLO ranges, inspect's config errors, distinct labels, production-only UAT callers,
and month over month (unrecorded failures, floors, named and unnamed hosts).
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
from ddreview import mermaid, validate  # noqa: E402

T0 = datetime(2026, 11, 19, 10, 0, tzinfo=timezone.utc)
OCT0 = datetime(2026, 10, 19, 10, 0, tzinfo=timezone.utc)
BASE = ["Date", "Service", "Resource", "Duration", "Method", "Status Code"]
OPS = BASE + ["Operation Name"]
TREE = OPS + ["Type", "Trace ID", "Span ID", "Parent ID"]
ACME = os.path.join(ROOT, "examples", "acme-retail")


def write(path, header, rows):
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        w.writerows(rows)


def ts(i, base=T0):
    return (base + timedelta(seconds=i)).strftime("%Y-%m-%dT%H:%M:%S.000Z")


class Round7(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ddreview-r7t-")
        self.out = os.path.join(self.tmp, "out")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def folder(self, name):
        p = os.path.join(self.tmp, name)
        os.makedirs(p, exist_ok=True)
        return p

    def config(self, name, **extra):
        cfg = {"org": {"name": "Round Seven", "short": "round-seven"}}
        cfg.update(extra)
        p = os.path.join(self.tmp, name)
        with open(p, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh)
        return p

    def run_cli(self, args):
        buf = io.StringIO()
        with redirect_stdout(buf):
            try:
                code = cli.main(args)
            except SystemExit as e:
                code = e.code
        return code, buf.getvalue()

    def build(self, folder, *extra, month="November 2026"):
        args = ["build", folder, "--out", self.out, "--month", month, "--force"] + list(extra)
        if "--config" not in extra:
            args += ["--org", "Round Seven"]
        if "--previous" not in extra:
            args += ["--previous", "none"]
        return self.run_cli(args)[0]

    def facts(self, key="2026-11", short="round-seven"):
        p = os.path.join(self.out, short, key)
        with open(os.path.join(p, "facts.json"), encoding="utf-8") as fh:
            F = json.load(fh)
        names = [x for x in os.listdir(p) if x.endswith(".md") and "review" in x]
        with open(os.path.join(p, names[0]), encoding="utf-8") as fh:
            return F, fh.read(), p

    def two_months(self, oct_rows, nov_rows, header, header_nov=None, cfg=None):
        fo, fn = self.folder("oct"), self.folder("nov")
        write(os.path.join(fo, "spans.csv"), header, oct_rows)
        write(os.path.join(fn, "spans.csv"), header_nov or header, nov_rows)
        c = ["--config", cfg] if cfg else []
        self.assertEqual(self.build(fo, *c, month="October 2026"), 0)
        prev = os.path.join(self.out, "round-seven", "2026-10", "facts.json")
        self.assertEqual(self.build(fn, *c, "--previous", prev), 0)
        return self.facts()

    @staticmethod
    def statuses(F):
        fm = F["mom"]["findings"]
        return {r["id"]: r.get("status", k) for k in ("closed", "unmeasured", "persisting", "new", "first_measured", "demoted", "promoted") for r in fm.get(k, [])}

    # ------------------------------------------------------------ inputs and joins
    def test_unjoined_database_calls_are_kept(self):
        f = self.folder("tid")
        rows = []
        for i in range(600):
            t = f"t{i}"
            rows.append([ts(i), "web-bff", "GET /orders", "200 ms", "GET", 200, "express.request", "web", t])
            rows.append([ts(i), "orders-api", "GET /orders", "120 ms", "GET", 200, "express.request", "web", t])
            for k in range(3):
                rows.append([ts(i), "orders-api-postgres", "SELECT * FROM orders", "4 ms", "", 500 if (i * 3 + k) % 10 < 3 else "", "pg.query", "sql", t])
        write(os.path.join(f, "spans.csv"), OPS + ["Type", "Trace ID"], rows)
        self.assertEqual(self.build(f), 0)
        F, _, _ = self.facts()
        d = F["dependencies"]["orders-api-postgres"]
        self.assertEqual((d["calls"], d["failed"]), (1800, 540))

    def test_queue_job_of_the_same_service_is_an_entry(self):
        f = self.folder("laravel")
        rows = []
        for i in range(600):
            rows.append([ts(i), "shop", "POST /checkout", "120 ms", "POST", 200, "laravel.request", "web", f"t{i}", f"r{i}", ""])
            rows.append([ts(i), "shop", "App\\Jobs\\Ship", "3 ms", "", "", "laravel.queue.push", "queue", f"t{i}", f"p{i}", f"r{i}"])
            rows.append([ts(i), "shop", "App\\Jobs\\Ship", "90 ms", "", "", "laravel.queue.process", "queue", f"t{i}", f"j{i}", f"p{i}"])
            for k in range(9):
                rows.append([ts(i), "pdo", "UPDATE shipments SET x = ?", "2 ms", "", "", "PDO.execute", "sql", f"t{i}", f"q{i}-{k}", f"j{i}"])
        write(os.path.join(f, "spans.csv"), TREE, rows)
        self.assertEqual(self.build(f), 0)
        F, _, _ = self.facts()
        self.assertEqual(F["totals"]["requests"], 1200)
        self.assertNotIn("queue messages of shop", F["dependencies"])

    def test_library_named_span_under_a_parent_is_not_a_service(self):
        f = self.folder("graphql")
        rows = []
        for i in range(600):
            rows.append([ts(i), "storefront-api", "POST /graphql", "80 ms", "POST", 200, "aspnet_core.request", "web", f"t{i}", f"r{i}", ""])
            rows.append([ts(i), "storefront-api-graphql", "query Products", "60 ms", "", "", "graphql.execute", "graphql", f"t{i}", f"g{i}", f"r{i}"])
        write(os.path.join(f, "spans.csv"), TREE, rows)
        self.assertEqual(self.build(f), 0)
        F, _, _ = self.facts()
        self.assertEqual(F["totals"]["requests"], 600)
        self.assertNotEqual(F["services"]["storefront-api-graphql"]["role"], "entry")

    def test_app_that_produces_and_consumes_keeps_its_produce_calls(self):
        f = self.folder("both")
        rows = []
        for i in range(600):
            rows.append([ts(i), "orders-api", "POST Orders/Create", "90 ms", "POST", 200, "aspnet_core.request", "web", f"t{i}", f"r{i}", "", "server"])
            rows.append([ts(i), "orders-api-kafka", "Produce Topic orders", "4 ms", "", 500 if i % 5 == 0 else "", "kafka.produce", "queue", f"t{i}", f"p{i}", f"r{i}", "producer"])
        for i in range(300):
            rows.append([ts(i), "orders-api-kafka", "Consume Topic payments", "30 ms", "", "", "kafka.consume", "queue", f"c{i}", f"k{i}", "", "consumer"])
        write(os.path.join(f, "spans.csv"), TREE + ["Span Kind"], rows)
        self.assertEqual(self.build(f), 0)
        F, _, _ = self.facts()
        d = F["dependencies"]["orders-api-kafka"]
        self.assertEqual((d["calls"], d["failed"]), (600, 120))
        self.assertEqual([c["service"] for c in d["callers"]], ["orders-api"])
        self.assertNotIn("queue messages of orders-api-kafka", F["dependencies"])

    def test_amqplib_delivery_is_the_workers_entry(self):
        f = self.folder("amqp")
        rows = []
        for i in range(600):
            rows.append([ts(i), "orders-api", "POST /orders", "90 ms", "POST", 200, "express.request", "web", f"t{i}", f"r{i}", ""])
            rows.append([ts(i), "orders-api-amqp", "basic.publish orders", "2 ms", "", "", "amqp.command", "worker", f"t{i}", f"p{i}", f"r{i}"])
            rows.append([ts(i), "fulfilment-worker-amqp", "basic.deliver orders", "60 ms", "", "", "amqp.command", "worker", f"t{i}", f"d{i}", f"p{i}"])
            for k in range(8):
                rows.append([ts(i), "fulfilment-worker-postgres", "UPDATE shipments", "3 ms", "", "", "pg.query", "sql", f"t{i}", f"q{i}-{k}", f"d{i}"])
        write(os.path.join(f, "spans.csv"), TREE, rows)
        self.assertEqual(self.build(f), 0)
        F, _, _ = self.facts()
        self.assertEqual(F["services"]["fulfilment-worker-amqp"]["role"], "entry")
        self.assertNotIn("db", F["anatomy"]["orders-api"]["children_per_request"])

    def test_services_list_only_integration_named_database_is_kept(self):
        f = self.folder("sl")
        for name in os.listdir(os.path.join(ACME, "exports")):
            shutil.copy(os.path.join(ACME, "exports", name), f)
        with open(os.path.join(f, "services.tsv"), "a", encoding="utf-8", newline="") as fh:
            fh.write("\tdb\tmysql\t\t500000\t0.20\t4000000\t\t\tprod\t\n")
        self.assertEqual(self.build(f, "--config", os.path.join(ACME, "org.json")), 0)
        F, _, _ = self.facts(short="acme-retail")
        self.assertEqual((F["dependencies"]["mysql"]["calls"], F["dependencies"]["mysql"]["failed"]), (500000, 100000))

    def test_calls_of_a_left_out_service_are_not_counted(self):
        f = self.folder("excl")
        rows = []
        for i in range(600):
            rows.append([ts(i), "shop-web", "GET /pay", "50 ms", "GET", 200, "servlet.request", "web", f"a{i}", f"r{i}", ""])
            rows.append([ts(i), "shop-web", "POST /charge", "40 ms", "POST", 200, "okhttp.request", "http", f"a{i}", f"o{i}", f"r{i}", ])
        for i in range(300):
            rows.append([ts(i), "loadtest-runner", "GET /run", "50 ms", "GET", 200, "servlet.request", "web", f"b{i}", f"l{i}", ""])
            rows.append([ts(i), "loadtest-runner", "POST /charge", "40 ms", "POST", 503, "okhttp.request", "http", f"b{i}", f"m{i}", f"l{i}"])
        rows = [r + ["pay-gw.example.com" if r[6] == "okhttp.request" else ""] for r in rows]
        write(os.path.join(f, "spans.csv"), TREE + ["peer.service"], rows)
        cfg = self.config("excl.json", services={"exclude": ["^loadtest-"]})
        self.assertEqual(self.build(f, "--config", cfg), 0)
        F, _, _ = self.facts()
        d = F["dependencies"]["pay-gw.example.com"]
        self.assertEqual((d["calls"], d["failed"]), (600, 0))

    # ------------------------------------------------------------ failure signal
    def test_flag_only_without_an_error_column_records_no_failures(self):
        f = self.folder("flag")
        write(os.path.join(f, "spans.csv"), BASE, [[ts(i % 600), "orders-api", "GET /x", "20 ms", "GET", 500 if i % 10 == 0 else 200] for i in range(1000)])
        cfg = self.config("flag.json", failure={"rule": "flag_only"})
        self.assertEqual(self.build(f, "--config", cfg), 0)
        F, md, _ = self.facts()
        self.assertFalse(F["coverage"]["failure_signal"])
        self.assertNotIn("0 % failed", md)
        self.assertIn("Error column", md)

    def test_domain_names_members_whose_failures_are_not_recorded(self):
        f = self.folder("mixed")
        rows = [[ts(i % 600), "shop-web", "GET /x", "20 ms", "GET", 500 if i < 8 else 200, "servlet.request", "web"] for i in range(2000)]
        rows += [[ts(i % 600), "shop-worker", "process_order", "300 ms", "", "", "celery.run", "worker"] for i in range(3000)]
        write(os.path.join(f, "spans.csv"), OPS + ["Type"], rows)
        cfg = self.config("mixed.json", domains=[{"key": "shop", "label": "Shop", "services": ["^shop-"]}])
        self.assertEqual(self.build(f, "--config", cfg), 0)
        _, md, _ = self.facts()
        self.assertIn("8 of 2,000 requests with a failure signal", md)
        self.assertIn("failures are not recorded for `shop-worker`", md)

    # ------------------------------------------------------------ validate, config, CLI
    def test_against_catches_changed_count_words(self):
        orig = "## Since last month\n\nOne is new, seven persist, and `shop-api` is slow on two of its features.\n"
        for changed in ("Two are new, seven persist", "One is new, six persist"):
            errors, _ = validate.check_against(orig.replace("One is new, seven persist", changed), orig)
            self.assertTrue(errors, changed)
        errors, _ = validate.check_against(orig.replace("two of its features", "three of its features"), orig)
        self.assertTrue(errors)
        errors, _ = validate.check_against(orig.replace("One is new", "One finding is new"), orig)
        self.assertFalse(errors)

    def test_init_never_fills_internal_domains_itself(self):
        f = self.folder("egress")
        rows = []
        for i in range(400):
            rows.append([ts(i), "notify-api", "POST /notify", "50 ms", "POST", 200, "servlet.request", "web", ""])
            rows.append([ts(i), "notify-api", "POST /v1/send", "40 ms", "POST", 200, "okhttp.request", "http", "api.sendgrid.net"])
        write(os.path.join(f, "spans.csv"), OPS + ["Type", "peer.service"], rows)
        cfg_path = os.path.join(self.tmp, "drafted.json")
        code, _ = self.run_cli(["init", f, "--org", "Round Seven", "--config", cfg_path])
        self.assertIn(code, (0, None))
        with open(cfg_path, encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["org"]["internal_domains"], [])

    def test_success_target_must_be_a_percentage(self):
        f = self.folder("slo")
        write(os.path.join(f, "spans.csv"), BASE, [[ts(i), "a-api", "GET /x", "5 ms", "GET", 200] for i in range(100)])
        cfg = self.config("slo.json", operations=[{"key": "x", "label": "lookups", "match": "x", "slo_success_pct": 0.995}])
        self.assertEqual(self.build(f, "--config", cfg), 3)

    def test_inspect_reports_a_config_error_with_exit_3(self):
        f = self.folder("insp")
        write(os.path.join(f, "spans.csv"), BASE, [[ts(i), "a-api", "GET /x", "5 ms", "GET", 200] for i in range(100)])
        cfg = self.config("insp.json", services={"include": ["^prod-"]})
        code, out = self.run_cli(["inspect", f, "--config", cfg])
        self.assertEqual(code, 3)
        self.assertIn("config error", out)

    # ------------------------------------------------------------ diagrams
    def test_similar_long_names_get_distinct_labels(self):
        names = [f"sql-northbank-onlinebanking-prod-0{i}.database.windows.net" for i in (1, 2, 3)]
        labels = mermaid.fit_all(names)
        self.assertEqual(len(set(labels.values())), 3)
        self.assertTrue(all(len(v) <= mermaid.MAX_LABEL for v in labels.values()))

    def test_uat_host_called_only_by_uat_services_is_not_production(self):
        f = self.folder("uat")
        rows = []
        for i in range(300):
            rows.append([ts(i), "orders-api", "GET /o", "50 ms", "GET", 200, "servlet.request", "web", "", "prod"])
            rows.append([ts(i), "pricing-ui", "GET /p", "50 ms", "GET", 200, "servlet.request", "web", "", "uat"])
            rows.append([ts(i), "pricing-ui", "GET /price", "40 ms", "GET", 503, "okhttp.request", "http", "pricing.uat.example.net", "uat"])
        write(os.path.join(f, "spans.csv"), OPS + ["Type", "peer.service", "Env"], rows)
        self.assertEqual(self.build(f), 0)
        F, _, _ = self.facts()
        self.assertNotIn("pricing.uat.example.net", F["dependencies"])
        self.assertFalse([x for x in F["findings"] if x["detector"] in ("nonprod_from_prod", "failing_dependencies")])

    # ------------------------------------------------------------ month over month
    def test_unrecorded_failures_this_month_are_not_checked(self):
        hdr = OPS + ["Type", "Error"]
        oct_rows = [[ts(i % 600, OCT0), "shop-worker", "process_order", "80 ms", "", "", "celery.run", "worker", "true" if i % 15 == 0 else "false"] for i in range(1000)]
        oct_rows += [[ts(i % 600, OCT0), "shop-web", "GET /x", "20 ms", "GET", 200, "servlet.request", "web", "false"] for i in range(1000)]
        nov_rows = [r[:8] for r in oct_rows]
        nov_rows = [[ts(i % 600)] + r[1:] for i, r in enumerate(nov_rows)]
        F, md, _ = self.two_months(oct_rows, nov_rows, hdr, header_nov=OPS + ["Type"])
        self.assertIn("unmeasured", self.statuses(F).values())
        self.assertNotIn("closed", self.statuses(F).values())

    def test_slow_service_below_its_floor_is_too_little_traffic(self):
        mk = lambda base, k: [[ts(i % 600, base), "shop-worker", f"job_{j}", "2800 ms", "", 200, "celery.run", "worker"] for j in range(3 if k else 2) for i in range(150 if k else 24)]
        F, _, _ = self.two_months(mk(OCT0, True), mk(T0, False), OPS + ["Type"])
        self.assertEqual(set(self.statuses(F).values()), {"too_little_traffic"})

    def test_named_host_is_compared_with_the_same_kind_of_unnamed_calls(self):
        def rows(base, peer):
            out = []
            for i in range(1000):
                out.append([ts(i % 600, base), "shop-api", "POST /checkout", "120 ms", "POST", 200, "servlet.request", "web", f"t{i}", f"r{i}", ""] + ([""] if peer else []))
                for k in range(6):
                    out.append([ts(i % 600, base), "shop-api", "SELECT 1", "3 ms", "", "", "postgresql.query", "sql", f"t{i}", f"q{i}-{k}", f"r{i}"] + (["orders-db"] if peer else []))
                out.append([ts(i % 600, base), "shop-api", "POST /pay", "90 ms", "POST", 502 if i % 2 == 0 else 200, "okhttp.request", "http", f"t{i}", f"h{i}", f"r{i}"]
                           + (["pay.acme.internal"] if peer else []))
            return out
        F, _, _ = self.two_months(rows(OCT0, False), rows(T0, True), TREE, header_nov=TREE + ["peer.service"])
        rows_ = [r for r in F["mom"]["findings"]["persisting"] if r["id"].startswith("failing_dependencies")]
        self.assertTrue(rows_)
        self.assertEqual(rows_[0]["then_text"], "50 %")


if __name__ == "__main__":
    unittest.main()
