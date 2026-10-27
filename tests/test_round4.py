"""Tests for the fourth review round: server evidence over host-like names, re-exports with ids kept, in-process framework
spans, span.kind over Type, unit words in headers, service-to-service edges, Services-list-only workers, anatomy without
parents, node groups across tiers, wildcard live graphs, outlier shares, every slow operation counted, {id} routes in
month-over-month rows, config nulls and reserved keys, CRLF answers, thousands-grouped European numbers, cut identifiers.
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
from ddreview import classify, readers, validate  # noqa: E402

T0 = datetime(2026, 11, 19, 10, 0, tzinfo=timezone.utc)
ACME = os.path.join(ROOT, "examples", "acme-retail")
TREE = ["Date", "Service", "Resource", "Duration", "Method", "Status Code", "Trace ID", "Span ID", "Parent ID"]


def write(path, header, rows, delimiter=","):
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, delimiter=delimiter)
        w.writerow(header)
        w.writerows(rows)


def ts(i, base=T0):
    return (base + timedelta(seconds=i)).strftime("%Y-%m-%dT%H:%M:%S.000Z")


class Round4(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ddreview-r4t-")
        self.out = os.path.join(self.tmp, "out")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def folder(self, name):
        p = os.path.join(self.tmp, name)
        os.makedirs(p, exist_ok=True)
        return p

    def build(self, folder, *extra, month="November 2026", org=True):
        args = ["build", folder, "--out", self.out, "--month", month, "--force"] + (["--org", "Round Four"] if org else []) + list(extra)
        if "--previous" not in extra:
            args += ["--previous", "none"]
        try:
            return cli.main(args)
        except SystemExit as e:
            return e.code

    def facts(self, short="round-four", key="2026-11"):
        p = os.path.join(self.out, short, key)
        with open(os.path.join(p, "facts.json"), encoding="utf-8") as fh:
            F = json.load(fh)
        names = [x for x in os.listdir(p) if x.endswith(".md") and "review" in x]
        with open(os.path.join(p, names[0]), encoding="utf-8") as fh:
            return F, fh.read(), names[0]

    # ------------------------------------------------------------ inputs
    def test_dotted_service_with_server_spans_is_an_entry(self):
        f = self.folder("dotted")
        rows = [[ts(i), "northbank.payments.services", "POST Payments/Transfer", "90 ms", "POST", 500 if i % 10 == 0 else 200, f"t{i}", f"s{i}", "0", "web"] for i in range(300)]
        write(os.path.join(f, "spans.csv"), TREE + ["Type"], rows)
        self.assertEqual(self.build(f), 0)
        F, md, _ = self.facts()
        self.assertEqual(F["services"]["northbank.payments.services"]["role"], "entry")
        self.assertNotIn("public internet", md)

    def test_reexport_with_ids_is_the_copy_kept(self):
        f = self.folder("reexport")
        full = []
        for i in range(300):
            full.append([ts(i), "orders-api", "GET /api/orders", "120 ms", "GET", 200, f"t{i}", f"r{i}", ""])
            full.append([ts(i), "postgres", "SELECT * FROM orders", "4 ms", "", "", f"t{i}", f"c{i}", f"r{i}"])
        write(os.path.join(f, "extract-1.csv"), TREE[:6], [r[:6] for r in full])
        write(os.path.join(f, "extract-2.csv"), TREE, full)
        self.assertEqual(self.build(f), 0)
        F, _, _ = self.facts()
        self.assertEqual(F["services"]["orders-api"]["requests"]["all"], 300)
        self.assertIn("orders-api", F["anatomy"])

    def test_in_process_framework_span_is_not_a_second_request(self):
        f = self.folder("spring")
        rows = []
        for i in range(300):
            rows.append([ts(i), "orders-api", "GET /api/orders", "120 ms", "GET", 500 if i % 20 == 0 else 200, "servlet.request", "web"])
            rows.append([ts(i), "orders-api", "OrderController.list", "100 ms", "", "", "spring.handler", "web"])
        write(os.path.join(f, "spans.csv"), ["Date", "Service", "Resource", "Duration", "Method", "Status Code", "Operation Name", "Type"], rows)
        self.assertEqual(self.build(f), 0)
        F, _, _ = self.facts()
        self.assertEqual(F["services"]["orders-api"]["requests"]["all"], 300)

    def test_queue_consumer_is_an_entry_even_with_a_client_type(self):
        f = self.folder("consumer")
        rows = [[ts(i), "fulfilment-worker", "consume orders", "40 ms", "", 500 if i % 5 == 0 else 200, f"t{i}", f"s{i}", "", "queue", "consumer"] for i in range(300)]
        write(os.path.join(f, "spans.csv"), TREE + ["Type", "span.kind"], rows)
        self.assertEqual(self.build(f), 0)
        F, _, _ = self.facts()
        self.assertEqual(F["services"]["fulfilment-worker"]["role"], "entry")

    def test_unit_word_header_is_still_recognised(self):
        f = self.folder("ms")
        write(os.path.join(f, "spans.csv"), ["Date", "Service", "Resource", "Duration (milliseconds)", "Method", "Status Code"],
              [[ts(i), "orders-api", "GET /x", "80", "GET", 200] for i in range(200)])
        self.assertEqual(self.build(f), 0)
        F, _, _ = self.facts()
        self.assertTrue(0.05 <= F["services"]["orders-api"]["p95_s"] <= 0.1)

    def test_call_between_traced_services_is_an_edge(self):
        f = self.folder("edge")
        rows = []
        for i in range(300):
            rows.append([ts(i), "orders-api", "GET /api/orders", "120 ms", "GET", 200, f"t{i}", f"r{i}", ""])
            rows.append([ts(i), "inventory-api", "GET /stock", "30 ms", "GET", 200, f"t{i}", f"i{i}", f"r{i}"])
        write(os.path.join(f, "spans.csv"), TREE, rows)
        self.assertEqual(self.build(f), 0)
        F, _, _ = self.facts()
        self.assertTrue(any(e["from"] == "orders-api" and e["to"] == "inventory-api" for e in F["edges"]))

    def test_services_list_only_worker_is_shown(self):
        f = self.folder("worker")
        for name in os.listdir(os.path.join(ACME, "exports")):
            shutil.copy(os.path.join(ACME, "exports", name), f)
        with open(os.path.join(f, "services.tsv"), "a", encoding="utf-8", newline="") as fh:
            fh.write("\tcustom\tbilling-worker\t\t50000\t0.30\t4000000000\t\t\tprod\t\n")
        self.assertEqual(self.build(f, "--config", os.path.join(ACME, "org.json"), org=False), 0)
        F, md, _ = self.facts(short="acme-retail")
        self.assertEqual(F["services"]["billing-worker"]["role"], "entry")
        self.assertIn("billing-worker", md)

    def test_anatomy_without_parents_does_not_borrow_another_services_children(self):
        f = self.folder("noparent")
        rows = []
        for t in range(300):
            rows.append([ts(t), "bff-api", "GET /page", "300 ms", "GET", 200, f"t{t}", f"b{t}"])
            rows.append([ts(t), "inventory-api", "GET /stock", "30 ms", "GET", 200, f"t{t}", f"i{t}"])
            for k in range(12):
                rows.append([ts(t), "postgres", "SELECT 1", "2 ms", "", "", f"t{t}", f"d{t}-{k}"])
        write(os.path.join(f, "spans.csv"), TREE[:8], rows)
        self.assertEqual(self.build(f), 0)
        F, md, _ = self.facts()
        self.assertNotIn("db", (F["anatomy"].get("bff-api") or {}).get("children_per_request", {}))

    def test_numbered_nodes_build_valid_and_compare_one_tier(self):
        f = self.folder("nodes")
        rows = [[ts(i), "web-01" if i % 3 else "web-02", "GET /home", "50 ms", "GET", 200] for i in range(900)]
        write(os.path.join(f, "spans.csv"), ["Date", "Service", "Resource", "Duration", "Method", "Status Code"], rows)
        self.assertEqual(self.build(f), 0)
        F, _, name = self.facts()
        self.assertFalse(name.endswith(".INVALID.md"))
        self.assertTrue(any(fd["detector"] == "node_imbalance" for fd in F["findings"]))

    # ------------------------------------------------------------ narrative
    def test_slow_operations_count_every_operation(self):
        f = self.folder("manyslow")
        rows = []
        for k in range(11):
            rows += [[ts(i), "batch-api", f"POST /job/{k}", "4.2 s", "POST", 200] for i in range(120)]
        rows += [[ts(i % 3000), "batch-api", "GET /api/items", "20 ms", "GET", 200] for i in range(20000)]     # most traffic is quick
        write(os.path.join(f, "spans.csv"), ["Date", "Service", "Resource", "Duration", "Method", "Status Code"], rows)
        self.assertEqual(self.build(f), 0)
        F, md, _ = self.facts()
        self.assertIn("Eleven slow operations", md)
        self.assertIn("Smaller items", md)

    def test_month_over_month_with_route_templates_stays_valid(self):
        prev, cur = self.folder("oct"), self.folder("nov")
        hdr = ["Date", "Service", "Resource", "Duration", "Method", "Status Code"]
        base = datetime(2026, 10, 22, 10, 0, tzinfo=timezone.utc)
        write(os.path.join(prev, "spans.csv"), hdr, [[ts(i, base), "orders-api", "GET /orders/{id}", "60 ms", "GET", 500 if i % 10 == 0 else 200] for i in range(800)])
        write(os.path.join(cur, "spans.csv"), hdr, [[ts(i), "orders-api", "GET /orders/{id}", "60 ms", "GET", 500 if i % 8 == 0 else 200] for i in range(800)])
        self.assertEqual(self.build(prev, month="October 2026"), 0)
        self.assertEqual(self.build(cur, "--previous", "auto", "--strict"), 0)
        _, md, name = self.facts()
        self.assertFalse(name.endswith(".INVALID.md"))
        self.assertIn("failure rate of `GET /orders/{id}`", md)

    # ------------------------------------------------------------ config, validate, readers
    def config_exit(self, mutate):
        with open(os.path.join(ACME, "org.json"), encoding="utf-8") as fh:
            cfg = json.load(fh)
        mutate(cfg)
        path = os.path.join(self.tmp, "org.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, ensure_ascii=False)
        return self.build(os.path.join(ACME, "exports"), "--config", path, org=False)

    def test_config_nulls_and_reserved_keys(self):
        self.assertEqual(self.config_exit(lambda c: c["domains"][0].update(key="other")), 3)

        def nulls(c):
            c["operations"][0]["label"] = None
            c["domains"][0]["icon"] = None
            c.setdefault("prompts", {})["mask_resources"] = None
            c["ratios"] = [{"label": "tokens per checkout", "numerator": {"service": "^checkout-api$", "match": "search"},
                            "denominator": {"service": "^checkout-api$", "match": "^POST /api/checkout$"}, "expect_max": None}]
        self.assertEqual(self.config_exit(nulls), 0)
        _, md, _ = self.facts(short="acme-retail")
        self.assertNotIn("None", md.replace("Nonetheless", ""))

    def test_crlf_answer_is_a_warning_not_a_broken_table(self):
        md = "# T\n\n| a | b |\n|---|---|\n| 1 | 2 |\n"
        e, w = validate.check_markdown(md.replace("\n", "\r\n"))
        self.assertEqual(e, [])
        self.assertTrue(any("CRLF" in x for x in w))

    def test_european_file_with_thousands_groups_is_decimal_comma(self):
        f = self.folder("euk")
        path = os.path.join(f, "endpoints.csv")
        write(path, ["Name", "Service", "Request Count", "Error Rate", "P95 Duration"],
              [["POST /api/checkout", "shop-api", "409.143", "0,045", "1,671924"], ["GET /api/cart", "shop-api", "1.194.360", "0,002", "0,2"]], delimiter=";")
        entry = [x for x in readers.discover(f) if x["file"] == "endpoints.csv"][0]
        self.assertTrue(entry["decimal_comma"])
        self.assertEqual(readers.parse_int("1.194.360", True), 1194360)

    def test_cut_identifiers_are_masked(self):
        self.assertNotIn("123e4567", classify.redact("GET /api/orders/123e4567-e89b-12…"))


if __name__ == "__main__":
    unittest.main()
