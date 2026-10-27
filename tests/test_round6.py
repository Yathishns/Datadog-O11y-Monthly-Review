"""Tests for the sixth review round: queue consumers named after their library, in-process spans without Operation Name,
the richer re-export kept, per-row joins across pooled files, Services-list consumers, the anatomy floor on examined
requests, configured ratios (own wording, tiers, ids), services filters, the configured failure rule, unrecorded failures,
API versions, number words under --against, month-over-month (absent operations, floors, merges, aliases), and diagrams
(budget search, production callers, long names).
python -m unittest discover -s tests"""
import csv
import json
import os
import shutil
import sys
import tempfile
import unittest
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


class Round6(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ddreview-r6t-")
        self.out = os.path.join(self.tmp, "out")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def folder(self, name):
        p = os.path.join(self.tmp, name)
        os.makedirs(p, exist_ok=True)
        return p

    def config(self, name, **extra):
        cfg = {"org": {"name": "Round Six", "short": "round-six"}}
        cfg.update(extra)
        p = os.path.join(self.tmp, name)
        with open(p, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh)
        return p

    def build(self, folder, *extra, month="November 2026"):
        args = ["build", folder, "--out", self.out, "--month", month, "--force"] + list(extra)
        if "--config" not in extra:
            args += ["--org", "Round Six"]
        if "--previous" not in extra:
            args += ["--previous", "none"]
        try:
            return cli.main(args)
        except SystemExit as e:
            return e.code

    def facts(self, key="2026-11", short="round-six"):
        p = os.path.join(self.out, short, key)
        with open(os.path.join(p, "facts.json"), encoding="utf-8") as fh:
            F = json.load(fh)
        names = [x for x in os.listdir(p) if x.endswith(".md") and "review" in x]
        with open(os.path.join(p, names[0]), encoding="utf-8") as fh:
            return F, fh.read(), p

    def two_months(self, oct_rows, nov_rows, header, cfg_oct=None, cfg_nov=None, nov_extra=()):
        fo, fn = self.folder("oct"), self.folder("nov")
        write(os.path.join(fo, "spans.csv"), header, oct_rows)
        write(os.path.join(fn, "spans.csv"), header, nov_rows)
        co = ["--config", cfg_oct] if cfg_oct else []
        cn = ["--config", cfg_nov or cfg_oct] if (cfg_nov or cfg_oct) else []
        self.assertEqual(self.build(fo, *co, month="October 2026"), 0)
        prev = os.path.join(self.out, "round-six", "2026-10", "facts.json")
        self.assertEqual(self.build(fn, *cn, "--previous", prev, *nov_extra), 0)
        return self.facts()

    @staticmethod
    def statuses(F):
        fm = F["mom"]["findings"]
        return {r["id"]: r.get("status", k) for k in ("closed", "unmeasured", "persisting", "new", "first_measured", "demoted", "promoted") for r in fm.get(k, [])}

    # ------------------------------------------------------------ inputs
    def test_consumer_named_after_its_queue_library_is_the_worker(self):
        f = self.folder("kafka")
        rows = []
        for i in range(300):
            rows.append([ts(i), "orders-api", "POST /orders", "90 ms", "POST", 200, "aspnet_core.request", "web", f"t{i}", f"r{i}", ""])
            rows.append([ts(i), "orders-api-kafka", "Produce Topic orders", "4 ms", "", "", "kafka.produce", "queue", f"t{i}", f"p{i}", f"r{i}"])
            rows.append([ts(i), "fulfilment-worker-kafka", "Consume Topic orders", "60 ms", "", "", "kafka.consume", "queue", f"t{i}", f"c{i}", f"p{i}"])
            for k in range(8):
                rows.append([ts(i), "fulfilment-worker-sql-server", "UPDATE Shipments SET Status = @p0", "3 ms", "", "", "sql-server.query", "sql", f"t{i}", f"q{i}-{k}", f"c{i}"])
        write(os.path.join(f, "spans.csv"), TREE, rows)
        self.assertEqual(self.build(f), 0)
        F, _, _ = self.facts()
        self.assertEqual(F["services"]["fulfilment-worker-kafka"]["role"], "entry")
        self.assertAlmostEqual(F["anatomy"]["orders-api"]["children_per_request"]["db"]["value"] if "db" in F["anatomy"]["orders-api"]["children_per_request"] else 0, 0.0)
        self.assertAlmostEqual(F["anatomy"]["fulfilment-worker-kafka"]["children_per_request"]["db"]["value"], 8.0)

    def test_in_process_span_with_type_but_no_operation_is_not_a_request(self):
        f = self.folder("spring")
        rows = []
        for i in range(600):
            rows.append([ts(i), "orders-api", "GET /api/orders", "120 ms", "GET", 500 if i % 10 == 0 else 200, "web"])
            rows.append([ts(i), "orders-api", "OrderController.list", "100 ms", "", "", "web"])
        write(os.path.join(f, "spans.csv"), BASE + ["Type"], rows)
        self.assertEqual(self.build(f), 0)
        F, _, _ = self.facts()
        self.assertEqual(F["services"]["orders-api"]["requests"]["all"], 600)
        self.assertEqual(F["services"]["orders-api"]["err"]["all"]["pct"], 10.0)

    def test_richer_reexport_is_kept_whatever_the_file_order(self):
        f = self.folder("reexport")
        rich = []
        for i in range(600):
            rich.append([ts(i), "tracking-api", "GET /track", "40 ms", "GET", 200, "express.request", "web"])
            rich.append([ts(i), "tracking-api", "GET /carrier", "30 ms", "GET", 503 if i % 3 == 0 else 200, "http.request", "http"])
        write(os.path.join(f, "extract-1.csv"), BASE, [r[:6] for r in rich])
        write(os.path.join(f, "extract-2.csv"), OPS + ["Type"], rich)
        self.assertEqual(self.build(f), 0)
        F, _, _ = self.facts()
        self.assertEqual(F["services"]["tracking-api"]["requests"]["all"], 600)
        self.assertEqual(F["services"]["tracking-api"]["failed"]["all"], 0)

    def test_pooled_file_without_parent_ids_keeps_its_peer_calls(self):
        f = self.folder("pooled")
        a, b = [], []
        for i in range(600):
            a.append([ts(i), "checkout-api", "POST /checkout", "120 ms", "POST", 200, "servlet.request", "web", f"a{i}", f"r{i}", "", ""])
            a.append([ts(i), "checkout-api", "POST /pay", "60 ms", "POST", 200, "okhttp.request", "http", f"a{i}", f"o{i}", f"r{i}", "api.payprovider.com"])
            b.append([ts(3600 + i), "checkout-api", "POST /checkout", "120 ms", "POST", 200, "servlet.request", "web", f"b{i}", ""])
            b.append([ts(3600 + i), "checkout-api", "POST /pay", "60 ms", "POST", 502 if i % 5 < 2 else 200, "okhttp.request", "http", f"b{i}", "api.payprovider.com"])
        write(os.path.join(f, "spans-1000.csv"), TREE + ["peer.service"], a)
        write(os.path.join(f, "spans-1100.csv"), OPS + ["Type", "Trace ID", "peer.service"], b)
        self.assertEqual(self.build(f), 0)
        F, _, _ = self.facts()
        d = F["dependencies"]["api.payprovider.com"]
        self.assertEqual((d["calls"], d["failed"]), (1200, 240))

    def test_services_list_only_queue_consumer_is_an_entry(self):
        f = self.folder("sl")
        for name in os.listdir(os.path.join(ACME, "exports")):
            shutil.copy(os.path.join(ACME, "exports", name), f)
        with open(os.path.join(f, "services.tsv"), "a", encoding="utf-8", newline="") as fh:
            fh.write("\tqueue\tfulfilment-consumer\t\t800000\t0.12\t4000000000\t\t\tprod\t\n")
        self.assertEqual(self.build(f, "--config", os.path.join(ACME, "org.json")), 0)
        F, md, _ = self.facts(short="acme-retail")
        self.assertEqual(F["services"]["fulfilment-consumer"]["role"], "entry")
        self.assertIn("fulfilment-consumer", md)

    def test_anatomy_floor_counts_examined_requests(self):
        f = self.folder("anat")
        rows = []
        for i in range(1000):
            t = f"t{i}"
            rows.append([ts(i % 540), "orders-api", "GET /orders", "200 ms", "GET", 200, "servlet.request", "web", t])
            if i < 20:
                rows += [[ts(i), "postgres", "SELECT 1", "3 ms", "", "", "postgresql.query", "sql", t]] * 20
            else:
                rows.append([ts(i % 540), "stock-api", "GET /stock", "20 ms", "GET", 200, "servlet.request", "web", t])
        write(os.path.join(f, "spans.csv"), OPS + ["Type", "Trace ID"], rows)
        self.assertEqual(self.build(f), 0)
        F, md, _ = self.facts()
        self.assertEqual(F["anatomy"]["orders-api"]["requests_with_tree"], 20)
        self.assertFalse([x for x in F["findings"] if x["detector"] == "chatty_requests"])

    def test_sample_service_without_failure_signal_is_not_recorded(self):
        f = self.folder("nosig")
        rows = [[ts(i), "web-api", "GET /x", "50 ms", "GET", 200, "servlet.request", "web"] for i in range(400)]
        rows += [[ts(i), "worker", "process_order", "80 ms", "", "", "celery.run", "worker"] for i in range(400)]
        write(os.path.join(f, "spans.csv"), OPS + ["Type"], rows)
        self.assertEqual(self.build(f), 0)
        F, md, _ = self.facts()
        self.assertFalse(F["services"]["worker"]["failure_known"])
        self.assertNotIn("Worker — healthy", md)

    def test_api_versions_are_not_nodes(self):
        f = self.folder("versions")
        rows = [[ts(i % 600), "orders-v1", "GET /api/orders", "50 ms", "GET", 200] for i in range(120)]
        rows += [[ts(i % 600), "orders-v2", "GET /api/orders", "50 ms", "GET", 200] for i in range(900)]
        write(os.path.join(f, "spans.csv"), BASE, rows)
        self.assertEqual(self.build(f), 0)
        F, _, _ = self.facts()
        self.assertEqual(F["node_groups"], {})

    # ------------------------------------------------------------ config, ratios, wording
    def test_configured_ratio_merged_keeps_its_own_words(self):
        f = self.folder("ratio")
        rows = [[ts(i % 600), "orders-api", "POST /orders", "50 ms", "POST", 500 if i % 33 == 0 else 200] for i in range(1000)]
        rows += [[ts(i % 600), "orders-api", "GET /inventory/check", "20 ms", "GET", 200] for i in range(4000)]
        write(os.path.join(f, "spans.csv"), BASE, rows)
        cfg = self.config("r.json", ratios=[{"label": "inventory checks per order", "numerator": {"service": "^orders-api$", "match": "^GET /inventory/check$"},
                                              "denominator": {"service": "^orders-api$", "match": "^POST /orders$"}, "expect_max": 2.0}])
        self.assertEqual(self.build(f, "--config", cfg), 0)
        _, md, _ = self.facts()
        self.assertIn("inventory checks per order", md)
        self.assertNotIn("token", md.lower())

    def test_two_ratio_labels_with_one_id_are_a_config_error(self):
        f = self.folder("dup")
        write(os.path.join(f, "spans.csv"), BASE, [[ts(i), "a-api", "GET /x", "5 ms", "GET", 200] for i in range(100)])
        r = lambda lab: {"label": lab, "numerator": {"service": ".*", "match": "x"}, "denominator": {"service": ".*", "match": "x"}}
        cfg = self.config("d.json", ratios=[r("price lookups per product search call from the web shop"), r("price lookups per product search call from the mobile app")])
        self.assertEqual(self.build(f, "--config", cfg), 3)

    def test_services_filter_that_drops_everything_is_a_config_error(self):
        f = self.folder("incl")
        write(os.path.join(f, "spans.csv"), BASE, [[ts(i), "a-api", "GET /x", "5 ms", "GET", 200] for i in range(100)])
        cfg = self.config("i.json", services={"include": ["^prod-"]})
        self.assertEqual(self.build(f, "--config", cfg), 3)

    def test_failure_rule_is_stated_as_configured(self):
        f = self.folder("rule")
        write(os.path.join(f, "spans.csv"), BASE, [[ts(i % 600), "orders-api", "GET /v1/orders/{id}", "5 ms", "GET", 404 if i % 9 == 0 else 200] for i in range(1000)])
        cfg = self.config("rule.json", failure={"rule": "non_2xx"})
        self.assertEqual(self.build(f, "--config", cfg), 0)
        _, md, _ = self.facts()
        self.assertNotIn("4xx responses are not failures", md)
        self.assertIn("4xx included", md)

    def test_against_catches_a_changed_number_word(self):
        orig = "## Finding 2 — One in twelve card authorisations fails\n\nText.\n"
        errors, _ = validate.check_against(orig.replace("One in twelve", "One in two"), orig)
        self.assertTrue(errors)
        errors, _ = validate.check_against(orig.replace("Two", "Two"), orig)
        self.assertFalse(errors)

    # ------------------------------------------------------------ month over month
    def test_operation_absent_from_the_sample_is_too_little_traffic(self):
        oct_rows = [[ts(i % 600, OCT0), "shop-api", "POST /orders", "50 ms", "POST", 500 if i % 4 == 0 else 200] for i in range(400)]
        oct_rows += [[ts(i % 600, OCT0), "shop-api", "GET /b", "20 ms", "GET", 200] for i in range(3000)]
        nov_rows = [[ts(i % 600), "shop-api", "GET /b", "20 ms", "GET", 200] for i in range(3000)]
        F, _, _ = self.two_months(oct_rows, nov_rows, BASE)
        self.assertIn("too_little_traffic", self.statuses(F).values())
        self.assertNotIn("closed", self.statuses(F).values())

    def test_raised_floor_with_a_healthy_figure_is_closed(self):
        oct_rows = [[ts(i % 600, OCT0), "shop-api", "GET /a", "50 ms", "GET", 500 if i % 20 == 0 else 200] for i in range(1000)]
        oct_rows += [[ts(i % 600, OCT0), "shop-api", "GET /b", "20 ms", "GET", 200] for i in range(1000)]
        nov_rows = [[ts(i % 600), "shop-api", "GET /a", "50 ms", "GET", 200] for i in range(150)]
        nov_rows += [[ts(i % 600), "shop-api", "GET /b", "20 ms", "GET", 200] for i in range(1000)]
        cfg_o = self.config("o.json")
        cfg_n = self.config("n.json", thresholds={"min_calls_sample": 200})
        F, _, _ = self.two_months(oct_rows, nov_rows, BASE, cfg_oct=cfg_o, cfg_nov=cfg_n)
        self.assertEqual(set(self.statuses(F).values()), {"closed"})

    def test_alias_joining_two_names_recomputes_the_trend(self):
        oct_rows = [[ts(i % 600, OCT0), "shop-api", "GET /a", "50 ms", "GET", 500 if i < 50 else 200] for i in range(2000)]
        oct_rows += [[ts(i % 600, OCT0), "shop-svc", "GET /a", "50 ms", "GET", 200] for i in range(2000)]
        nov_rows = [[ts(i % 600), "shop-svc", "GET /a", "50 ms", "GET", 500 if i < 100 else 200] for i in range(4000)]
        cfg = self.config("alias.json", services={"aliases": {"shop-api": "shop-svc"}})
        F, _, _ = self.two_months(oct_rows, nov_rows, BASE, cfg_oct=cfg)
        rows = [r for r in F["mom"]["findings"]["persisting"] if r["id"].startswith("failing_operations")]
        self.assertTrue(rows)
        self.assertEqual(rows[0]["then_text"], "1.25 %")

    # ------------------------------------------------------------ diagrams
    def test_long_names_stay_distinct_and_keep_the_count(self):
        a = mermaid.fit("northbank.onlinebanking.web.api.backoffice.customers-retail")
        b = mermaid.fit("northbank.onlinebanking.web.api.backoffice.customers-business")
        self.assertNotEqual(a, b)
        self.assertLessEqual(len(a), mermaid.MAX_LABEL)
        g = mermaid.fit("northbank.onlinebanking.web.api.statements-01", mermaid.MAX_LABEL - 4) + " × 3"
        self.assertIn("× 3", mermaid.label(g))

    def test_non_production_host_counts_production_callers_only(self):
        F = {"coverage": {"env_scope": {"column": True, "production_services": ["orders-api"], "nonprod_services": ["pricing-ui"]}}}
        dep = {"class": "nonprod", "callers": [{"service": "pricing-ui"}, {"service": "orders-api"}]}
        self.assertEqual([c["service"] for c in mermaid.counted_callers(F, dep)], ["orders-api"])
        F2 = {"meta": {"thresholds": {"dep_slow_s": 3}}}
        self.assertEqual(mermaid._edge_text(F2, {"class": "nonprod", "marker": "UAT", "err": {"pct": 0}}, {"pct": 0}, None, False), "production → UAT")

    def test_many_domains_and_flagged_hosts_still_draw_services(self):
        f = self.folder("degen")
        names = ["alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf", "hotel", "india", "juliet", "kilo", "lima", "mike", "november"]
        hosts = ["ledger", "billing", "pricing", "stock", "loyalty", "ratings"]
        rows, domains = [], []
        for k, d in enumerate(names):
            domains.append({"key": d, "label": d.capitalize(), "services": [f"^{d}-"]})
            for s in ("orders", "web", "admin"):
                for i in range(100):
                    rows.append([ts(i), f"{d}-{s}", "GET /x", "20 ms", "GET", 200, "servlet.request", "web", ""])
            if k < len(hosts):
                for i in range(300):
                    rows.append([ts(i), f"{d}-orders", "GET /h", "30 ms", "GET", 503 if i % 3 == 0 else 200, "okhttp.request", "http", f"{hosts[k]}.corp.example.org"])
        write(os.path.join(f, "spans.csv"), OPS + ["Type", "peer.service"], rows)
        cfg = self.config("degen.json", domains=domains)
        self.assertEqual(self.build(f, "--config", cfg), 0)
        _, _, p = self.facts()
        with open(os.path.join(p, "mermaid", "01-landscape.mmd"), encoding="utf-8") as fh:
            mmd = fh.read()
        self.assertGreaterEqual(mmd.count('    S_'), 6)
        self.assertEqual(mermaid.lint(mmd), [])


if __name__ == "__main__":
    unittest.main()
