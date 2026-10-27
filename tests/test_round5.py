"""Tests for the fifth review round: the entry rule per row, outbound calls under the caller's own name, per-file columns,
anatomy without parents, queue client libraries, the failing-operations tail, unrounded thresholds, month-over-month statuses
(call floors, aliases joining two names, a host named after an action, every changed threshold), per-caller edge labels,
per-host colours, the configured slow bar on edges, the counted-apart note, config text with digits and drafted domain keys.
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
from ddreview import classify, mermaid, narrative  # noqa: E402

T0 = datetime(2026, 11, 19, 10, 0, tzinfo=timezone.utc)
OCT0 = datetime(2026, 10, 19, 10, 0, tzinfo=timezone.utc)
BASE = ["Date", "Service", "Resource", "Duration", "Method", "Status Code"]
OPS = BASE + ["Operation Name"]


def write(path, header, rows):
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        w.writerows(rows)


def ts(i, base=T0):
    return (base + timedelta(seconds=i)).strftime("%Y-%m-%dT%H:%M:%S.000Z")


class Round5(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ddreview-r5t-")
        self.out = os.path.join(self.tmp, "out")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def folder(self, name):
        p = os.path.join(self.tmp, name)
        os.makedirs(p, exist_ok=True)
        return p

    def config(self, name, **extra):
        cfg = {"org": {"name": "Round Five", "short": "round-five"}}
        cfg.update(extra)
        p = os.path.join(self.tmp, name)
        with open(p, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh)
        return p

    def build(self, folder, *extra, month="November 2026", out=None):
        args = ["build", folder, "--out", out or self.out, "--month", month, "--force"] + list(extra)
        if "--config" not in extra:
            args += ["--org", "Round Five"]
        if "--previous" not in extra:
            args += ["--previous", "none"]
        try:
            return cli.main(args)
        except SystemExit as e:
            return e.code

    def facts(self, key="2026-11", out=None):
        p = os.path.join(out or self.out, "round-five", key)
        with open(os.path.join(p, "facts.json"), encoding="utf-8") as fh:
            F = json.load(fh)
        names = [x for x in os.listdir(p) if x.endswith(".md") and "review" in x]
        with open(os.path.join(p, names[0]), encoding="utf-8") as fh:
            return F, fh.read(), p

    def two_months(self, oct_rows, nov_rows, header, cfg_oct=None, cfg_nov=None):
        fo, fn = self.folder("oct"), self.folder("nov")
        write(os.path.join(fo, "spans.csv"), header, oct_rows)
        write(os.path.join(fn, "spans.csv"), header, nov_rows)
        co = ["--config", cfg_oct] if cfg_oct else []
        cn = ["--config", cfg_nov or cfg_oct] if (cfg_nov or cfg_oct) else []
        self.assertEqual(self.build(fo, *co, month="October 2026"), 0)
        prev = os.path.join(self.out, "round-five", "2026-10", "facts.json")
        self.assertEqual(self.build(fn, *cn, "--previous", prev), 0)
        return self.facts()

    @staticmethod
    def closed_rows(F):
        return {r["id"]: r for r in F["mom"]["findings"]["closed"] + F["mom"]["findings"]["unmeasured"]}

    # ------------------------------------------------------------ inputs and numbers
    def test_entry_rule_does_not_depend_on_row_order(self):
        header = OPS + ["peer.service"]
        rows = [[ts(i), "ship-api", "POST /ship", "80 ms", "POST", 200, "express.request", ""] for i in range(300)]
        rows += [[ts(i), "ship-api", "POST /rates", "60 ms", "POST", 200, "http.request", "rates.carrier.com"] for i in range(100)]
        rows += [[ts(i), "ship-api", "POST /labels", "70 ms", "POST", 502 if i % 5 < 2 else 200, "http.request", ""] for i in range(200)]
        totals = []
        for name, order in (("a", rows), ("b", rows[300:] + rows[:300])):
            f = self.folder(name)
            write(os.path.join(f, "spans.csv"), header, order)
            out = os.path.join(self.tmp, "out-" + name)
            self.assertEqual(self.build(f, out=out), 0)
            F, _, _ = self.facts(out=out)
            totals.append(F["services"]["ship-api"]["requests"]["all"])
        self.assertEqual(totals, [300, 300])

    def test_outbound_calls_under_the_callers_name_survive_parent_ids(self):
        f = self.folder("outbound")
        rows = []
        for i in range(300):
            rows.append([ts(i), "ship-api", "POST /ship", "300 ms", "POST", 200, "express.request", f"t{i}", f"r{i}", ""])
            rows.append([ts(i), "ship-api", "POST /labels", "200 ms", "POST", 502 if i % 5 < 2 else 200, "http.request", f"t{i}", f"c{i}", f"r{i}"])
        write(os.path.join(f, "spans.csv"), OPS + ["Trace ID", "Span ID", "Parent ID"], rows)
        self.assertEqual(self.build(f), 0)
        F, md, _ = self.facts()
        dep = F["dependencies"].get("outbound calls of ship-api")
        self.assertIsNotNone(dep)
        self.assertEqual(dep["failed"], 120)
        self.assertEqual(F["services"]["ship-api"]["requests"]["all"], 300)
        self.assertNotIn("APM › Software Catalog › `outbound calls of", md)

    def test_each_file_is_judged_by_its_own_columns(self):
        f = self.folder("pooled")
        tree = OPS + ["Type", "Trace ID", "Span ID", "Parent ID"]
        with_ids, without = [], []
        for i in range(200):
            with_ids.append([ts(i), "orders-api", "GET /orders", "120 ms", "GET", 200, "servlet.request", "web", f"t{i}", f"r{i}", ""])
            with_ids.append([ts(i), "orders-api", "OrdersController.list", "100 ms", "", "", "spring.handler", "web", f"t{i}", f"h{i}", f"r{i}"])
            without.append([ts(3600 + i), "orders-api", "GET /orders", "120 ms", "GET", 200, "servlet.request", "web"])
            without.append([ts(3600 + i), "orders-api", "OrdersController.list", "100 ms", "", "", "spring.handler", "web"])
        write(os.path.join(f, "spans-0200.csv"), tree, with_ids)
        write(os.path.join(f, "spans-0300.csv"), tree[:9], without)
        self.assertEqual(self.build(f), 0)
        F, _, _ = self.facts()
        self.assertEqual(F["services"]["orders-api"]["requests"]["all"], 400)
        self.assertNotIn("OrdersController.list", {r["resource"] for r in F["resources"].get("orders-api", [])})

    def test_anatomy_without_parents_does_not_understate_children(self):
        f = self.folder("noparent")
        rows = []
        for i in range(300):
            t = f"t{i}"
            rows.append([ts(i), "front", "GET /page", "3000 ms" if i % 4 == 0 else "300 ms", "GET", 200, "servlet.request", "web", t, f"f{i}"])
            for k in range(4):
                rows.append([ts(i), "postgres", "SELECT * FROM t", "5 ms", "", "", "postgresql.query", "sql", t, f"q{i}-{k}"])
            if i % 5:
                rows.append([ts(i), "back", "GET /data", "50 ms", "GET", 200, "servlet.request", "web", t, f"b{i}"])
        write(os.path.join(f, "spans.csv"), OPS + ["Type", "Trace ID", "Span ID"], rows)
        self.assertEqual(self.build(f), 0)
        F, md, _ = self.facts()
        db = F["anatomy"]["front"]["children_per_request"]["db"]
        self.assertAlmostEqual(db["value"], 4.0, places=1)
        self.assertNotIn("no database, HTTP or cache spans inside them", md)

    def test_queue_client_library_is_not_an_entry(self):
        f = self.folder("kafka")
        rows = []
        for i in range(300):
            rows.append([ts(i), "orders-api", "POST /orders", "90 ms", "POST", 200, "aspnet_core.request", "web", f"t{i}", f"r{i}", ""])
            rows.append([ts(i), "orders-api-kafka", "Produce Topic orders", "4 ms", "", "", "kafka.produce", "queue", f"t{i}", f"k{i}", f"r{i}"])
        write(os.path.join(f, "spans.csv"), OPS + ["Type", "Trace ID", "Span ID", "Parent ID"], rows)
        self.assertEqual(self.build(f), 0)
        F, _, _ = self.facts()
        self.assertNotEqual(F["services"]["orders-api-kafka"]["role"], "entry")

    def test_failing_operations_tail_names_the_call_floor(self):
        f = self.folder("tail")
        rows = [[ts(i % 600), "acct-api", "GET /a", "50 ms", "GET", 500 if i < 30 else 200] for i in range(1000)]
        rows += [[ts(i), "acct-api", "POST /b", "50 ms", "POST", 500 if i < 20 else 200] for i in range(40)]
        rows += [[ts(i % 600), "acct-api", "GET /c", "50 ms", "GET", 500 if i < 15 else 200] for i in range(2000)]
        write(os.path.join(f, "spans.csv"), BASE, rows)
        self.assertEqual(self.build(f), 0)
        _, md, _ = self.facts()
        self.assertIn("POST /b", md)
        self.assertIn("too few calls", md)
        self.assertNotIn("`POST /b` fails 20 of 40 (50 %) — below the threshold", md)

    def test_non_integer_threshold_is_printed_as_given(self):
        f = self.folder("thr")
        rows = [[ts(i % 600), "acct-api", "GET /a", "50 ms", "GET", 500 if i < 17 else 200] for i in range(1000)]
        write(os.path.join(f, "spans.csv"), BASE, rows)
        cfg = self.config("thr.json", thresholds={"fail_amber_pct": 1.5})
        self.assertEqual(self.build(f, "--config", cfg), 0)
        _, md, _ = self.facts()
        self.assertNotIn("above 2 %", md)

    def test_digits_in_config_text_pass_strict(self):
        f = self.folder("digits")
        write(os.path.join(f, "spans.csv"), BASE, [[ts(i), "web-api", "GET /x", "50 ms", "GET", 200] for i in range(300)])
        cfg = self.config("digits.json", org={"name": "Team 42 Retail", "short": "round-five"})
        self.assertEqual(self.build(f, "--config", cfg, "--strict"), 0)

    def test_drafted_domain_keys_load(self):
        f = self.folder("init")
        names = ["membership-eligibility-verification-api", "membership-eligibility-verification-portal", "prd-other-01"]
        write(os.path.join(f, "spans.csv"), BASE, [[ts(i), names[i % 3], "GET /x", "50 ms", "GET", 200] for i in range(600)])
        cfg = os.path.join(self.tmp, "drafted.json")
        try:
            rc = cli.main(["init", f, "--org", "Round Five", "--config", cfg])
        except SystemExit as e:
            rc = e.code
        self.assertIn(rc, (0, None))
        with open(cfg, encoding="utf-8") as fh:
            drafted = json.load(fh)
        drafted["org"]["short"] = "round-five"
        with open(cfg, "w", encoding="utf-8") as fh:
            json.dump(drafted, fh)
        self.assertNotIn("other", [d["key"] for d in drafted.get("domains", [])])
        self.assertEqual(self.build(f, "--config", cfg), 0)

    def test_target_below_100_never_prints_as_100(self):
        self.assertNotEqual(narrative.pct(99.999).replace(" ", ""), "100%")
        self.assertIn("99.999", narrative.pct(99.999))

    def test_a_cut_email_is_masked(self):
        res = "GET /users/firstname.lastname@example-company.com/preferences/notifications"
        cut = res[:59] + "…"
        self.assertNotIn("lastname@", classify.redact(cut))

    # ------------------------------------------------------------ month over month
    def test_too_few_calls_this_month_is_not_closed(self):
        oct_rows = [[ts(i % 600, OCT0), "shop-api", "GET /a", "50 ms", "GET", 500 if i < 50 else 200] for i in range(1000)]
        nov_rows = [[ts(i), "shop-api", "GET /a", "50 ms", "GET", 500 if i < 1 else 200] for i in range(40)]
        nov_rows += [[ts(i % 600), "shop-api", "GET /b", "50 ms", "GET", 200] for i in range(1000)]
        F, _, _ = self.two_months(oct_rows, nov_rows, BASE)
        statuses = {r.get("status") for r in self.closed_rows(F).values()}
        self.assertIn("too_little_traffic", statuses)
        self.assertNotIn("closed", statuses)

    def test_alias_onto_an_existing_name_merges_last_month(self):
        oct_rows = [[ts(i % 600, OCT0), "shop-api", "GET /a", "50 ms", "GET", 500 if i < 50 else 200] for i in range(2000)]
        oct_rows += [[ts(i % 600, OCT0), "shop-svc", "GET /a", "50 ms", "GET", 200] for i in range(800)]
        nov_rows = [[ts(i % 600), "shop-svc", "GET /a", "50 ms", "GET", 500 if i < 50 else 200] for i in range(2000)]
        cfg = self.config("alias.json", services={"aliases": {"shop-api": "shop-svc"}})
        F, md, _ = self.two_months(oct_rows, nov_rows, BASE, cfg_oct=cfg)
        self.assertNotIn("| `shop-svc` | failure rate | 0 %", md)

    def test_every_changed_threshold_is_named(self):
        rows = lambda base: [[ts(i % 600, base), "shop-api", "GET /slow", "4000 ms", "GET", 200] for i in range(200)] + \
            [[ts(i % 600, base), "shop-api", "GET /fast", "40 ms", "GET", 200] for i in range(2000)]
        cfg_o = self.config("o.json")
        cfg_n = self.config("n.json", thresholds={"slow_op_min_calls": 300, "fail_red_pct": 4.0})
        F, md, _ = self.two_months(rows(OCT0), rows(T0), BASE, cfg_oct=cfg_o, cfg_nov=cfg_n)
        self.assertIn("calls a slow operation needs", md)

    def test_naming_the_host_keeps_the_outbound_finding(self):
        def rows(base, peer):
            out = []
            for i in range(1000):
                out.append([ts(i % 600, base), "shop-api", "POST /checkout", "120 ms", "POST", 200, "express.request"] + ([""] if peer else []))
                out.append([ts(i % 600, base), "shop-api", "POST /charge", "90 ms", "POST", 502 if i % 4 == 0 else 200, "http.request"] + (["api.pay.com"] if peer else []))
            return out
        fo, fn = self.folder("oct"), self.folder("nov")
        write(os.path.join(fo, "spans.csv"), OPS, rows(OCT0, False))
        write(os.path.join(fn, "spans.csv"), OPS + ["peer.service"], rows(T0, True))
        self.assertEqual(self.build(fo, month="October 2026"), 0)
        prev = os.path.join(self.out, "round-five", "2026-10", "facts.json")
        self.assertEqual(self.build(fn, "--previous", prev), 0)
        F, _, _ = self.facts()
        dep_rows = [r for r in F["mom"]["findings"]["persisting"] if r["id"].startswith("failing_dependencies")]
        self.assertTrue(dep_rows)
        self.assertFalse([r for r in F["mom"]["findings"]["closed"] if r["id"].startswith("failing_dependencies")])

    # ------------------------------------------------------------ diagrams
    def test_each_caller_edge_carries_its_own_rate(self):
        f = self.folder("edges")
        rows = []
        for i in range(600):
            rows.append([ts(i), "orders-api", "GET /orders", "90 ms", "GET", 200, "servlet.request", "web", ""])
            rows.append([ts(i), "orders-api", "GET /partner", "60 ms", "GET", 200, "http.request", "http", "partner.example.net"])
            rows.append([ts(i), "billing-api", "POST /bill", "90 ms", "POST", 200, "servlet.request", "web", ""])
            rows.append([ts(i), "billing-api", "POST /partner", "60 ms", "POST", 503, "http.request", "http", "partner.example.net"])
        write(os.path.join(f, "spans.csv"), OPS + ["Type", "peer.service"], rows)
        self.assertEqual(self.build(f), 0)
        _, _, p = self.facts()
        with open(os.path.join(p, "mermaid", "03-dependencies.mmd"), encoding="utf-8") as fh:
            mmd = fh.read()
        self.assertIn('S_orders_api -->|"no failures"| D_partner_example_net', mmd)
        self.assertIn('S_billing_api -->|"fails every call"| D_partner_example_net', mmd)
        self.assertEqual(mermaid.lint(mmd), [])

    def test_estate_finding_colours_each_host_by_its_own_figures(self):
        findings = [{"severity": "red", "subject": {"key": "estate", "hosts": ["bad.example.com", "fine.example.com"],
                                                    "host_severity": {"bad.example.com": "red", "fine.example.com": "amber"}}}]
        self.assertEqual(mermaid.flagged_hosts(findings), {"bad.example.com": "red", "fine.example.com": "amber"})

    def test_edge_uses_the_configured_slow_bar_and_no_false_public(self):
        F = {"meta": {"thresholds": {"dep_slow_s": 2.0}}}
        dep = {"class": "internal", "err": {"pct": 0.0, "num": 0, "den": 1100}, "marker": ""}
        self.assertEqual(mermaid._edge_text(F, dep, dep["err"], 2.5, True), "p95 2.50 s")
        self.assertEqual(mermaid._edge_text(F, dep, dep["err"], 0.2, True), "called")
        self.assertEqual(mermaid._edge_text(F, dict(dep, **{"class": "public"}), dep["err"], 0.2, True), "public host")

    def test_counted_apart_note_is_never_cut(self):
        lab = mermaid.label("web-01 × 2", "Java servlet · 600 k req", line3="(+1 counted apart)")
        self.assertIn("(+1 counted apart)", lab)


if __name__ == "__main__":
    unittest.main()
