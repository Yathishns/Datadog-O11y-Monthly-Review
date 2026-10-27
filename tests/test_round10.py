"""Tests for the tenth review round: page extensions that are the app's own pages, library pseudo-services, production hosts
declared in the config, agreed p95 and success targets (slow tables, KPI subjects, month-over-month 'within target'),
KPI rates with no calls, p95 sampling noise, the figure a closed chatty finding tracks, detectors last month could not
run, the identity caveat, service-map labels, the previous-month wording, fixed-offset zones, by-design rules and the
landscape prompt's colour table.
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
from ddreview import compare, config, mermaid, stats  # noqa: E402
from ddreview.classify import host_class, is_integration_name, resource_class  # noqa: E402

T0 = datetime(2026, 11, 19, 10, 0, tzinfo=timezone.utc)
OCT0 = datetime(2026, 10, 19, 10, 0, tzinfo=timezone.utc)
BASE = ["Date", "Service", "Resource", "Duration", "Method", "Status Code"]
OPS = BASE + ["Operation Name", "Type"]
TREE = OPS + ["Trace ID", "Span ID", "Parent ID"]
SHORT = "round-ten"


def write(path, header, rows):
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        w.writerows(rows)


def ts(i, base=T0):
    return (base + timedelta(seconds=i)).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def ops(service, resource, n, ms, fail_every=0, base=T0, method="GET"):
    return [[ts(i % 600, base), service, resource, f"{ms} ms", method, 500 if fail_every and i % fail_every == 0 else 200] for i in range(n)]


class Round10(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ddreview-r10t-")
        self.out = os.path.join(self.tmp, "out")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def folder(self, name):
        p = os.path.join(self.tmp, name)
        os.makedirs(p, exist_ok=True)
        return p

    def config(self, name, **extra):
        cfg = {"org": {"name": "Round Ten", "short": SHORT}}
        cfg.update(extra)
        p = os.path.join(self.tmp, name)
        with open(p, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh)
        return p

    def build(self, folder, *extra, month="November 2026"):
        args = ["build", folder, "--out", self.out, "--month", month, "--force"] + list(extra)
        if "--config" not in extra:
            args += ["--config", self.config("plain.json")]
        if "--previous" not in extra:
            args += ["--previous", "none"]
        buf = io.StringIO()
        with redirect_stdout(buf):
            try:
                return cli.main(args)
            except SystemExit as e:
                return e.code

    def facts(self, key="2026-11"):
        p = os.path.join(self.out, SHORT, key)
        with open(os.path.join(p, "facts.json"), encoding="utf-8") as fh:
            F = json.load(fh)
        names = [x for x in os.listdir(p) if x.endswith(".md") and "review" in x]
        with open(os.path.join(p, names[0]), encoding="utf-8") as fh:
            return F, fh.read(), p

    def two_months(self, oct_rows, nov_rows, header, header_nov=None, cfg_oct=None, cfg_nov=None):
        fo, fn = self.folder("oct"), self.folder("nov")
        write(os.path.join(fo, "spans.csv"), header, oct_rows)
        write(os.path.join(fn, "spans.csv"), header_nov or header, nov_rows)
        self.assertEqual(self.build(fo, "--config", cfg_oct or self.config("plain.json"), month="October 2026"), 0)
        prev = os.path.join(self.out, SHORT, "2026-10", "facts.json")
        self.assertEqual(self.build(fn, "--config", cfg_nov or cfg_oct or self.config("plain.json"), "--previous", prev), 0)
        return self.facts()

    @staticmethod
    def statuses(F):
        fm = F["mom"]["findings"]
        return {r["id"]: r.get("status", k) for k in ("closed", "unmeasured", "persisting", "new", "first_measured", "demoted", "promoted") for r in fm.get(k, [])}

    # ------------------------------------------------------------ inputs
    def test_page_extension_is_the_apps_own_page(self):
        cfg = config.load_config(None)
        self.assertEqual(resource_class("shop-web", "POST /Checkout.aspx", cfg)[0], "business")
        self.assertEqual(resource_class("shop-web", "GET /phpinfo.php", cfg)[0], "scanner")

    def test_library_pseudo_services_are_integrations(self):
        cfg = config.load_config(None)
        for name in ("aws.s3", "orders-aws", "celery-producer"):
            self.assertTrue(is_integration_name(name, cfg), name)
        self.assertFalse(is_integration_name("orders-api", cfg))

    def test_declared_production_host_is_never_nonprod(self):
        p = self.config("hosts.json", hosts={"production": ["^uat\\.payments\\.partner\\.com$"]})
        cfg = config.load_config(p)
        self.assertNotEqual(host_class("uat.payments.partner.com", cfg), "nonprod")
        self.assertEqual(host_class("uat.pricing.partner.com", cfg), "nonprod")

    # ------------------------------------------------------------ agreed targets and KPIs
    def test_slow_table_leaves_out_operations_within_their_target(self):
        f = self.folder("slo")
        rows = ops("catalog-api", "GET /api/search", 200, 4400) + ops("catalog-api", "GET /api/cart", 100, 2750)
        rows += ops("catalog-api", "GET /api/products", 100, 110) + ops("catalog-api", "GET /api/reviews", 40, 220)
        write(os.path.join(f, "spans.csv"), BASE, rows)
        cfg = self.config("slo.json", operations=[{"key": "search", "label": "product searches", "service": "^catalog-api$", "match": "^GET /api/search$", "slo_p95_s": 5.0}])
        self.assertEqual(self.build(f, "--config", cfg), 0)
        F, md, _ = self.facts()
        slow = [x for x in F["findings"] if x["detector"] == "slow_hot_paths"]
        self.assertEqual(len(slow), 1)
        self.assertIn("one of its features", slow[0]["title"])
        self.assertFalse(any("search" in str(r[0]) for r in slow[0]["table"]["rows"]))
        self.assertIn("`GET /api/cart` at 2.75 s", md)

    def test_missed_success_target_is_reported_when_another_operation_fails_more(self):
        f = self.folder("kpi")
        rows = ops("orders-api", "POST /api/orders", 3000, 80, fail_every=33, method="POST") + ops("orders-api", "GET /api/orders/legacy", 200, 30, fail_every=1)
        rows += ops("orders-api", "GET /api/orders/{id}", 3000, 40)
        write(os.path.join(f, "spans.csv"), BASE, rows)
        cfg = self.config("kpi.json", operations=[{"key": "create", "label": "order creations", "service": "^orders-api$", "match": "^POST /api/orders$",
                                                   "critical": True, "slo_success_pct": 99.5}])
        self.assertEqual(self.build(f, "--config", cfg), 0)
        _, md, _ = self.facts()
        self.assertIn("The agreed target for order creations is 99.5 % success", md)
        self.assertIn("Track order creations against the agreed 99.5 % success target", md)
        self.assertNotIn("Agree a success-rate SLO for `GET /api/orders/legacy`", md)

    def test_kpi_label_names_only_its_own_rate(self):
        f = self.folder("multi")
        rows = ops("clinic-web", "GET /api/appointments/", 9000, 50, fail_every=500) + ops("clinic-web", "POST /api/appointments/", 2400, 90, fail_every=33, method="POST")
        write(os.path.join(f, "spans.csv"), BASE, rows)
        cfg = self.config("multi.json", operations=[{"key": "appts", "label": "appointment requests", "service": "^clinic-web$", "match": "/api/appointments/"}])
        self.assertEqual(self.build(f, "--config", cfg), 0)
        F, _, _ = self.facts()
        fail = [x for x in F["findings"] if x["detector"] == "failing_operations"]
        self.assertTrue(fail)
        self.assertIn("`POST /api/appointments/` calls", fail[0]["title"])
        self.assertNotIn("appointment requests fail", fail[0]["title"])

    def test_newly_agreed_target_closes_within_target(self):
        def rows(base):
            return (ops("shop-api", "GET /search", 1000, 3000, base=base) + ops("shop-api", "GET /a", 600, 100, base=base)
                    + ops("shop-api", "GET /b", 500, 100, base=base) + ops("shop-api", "GET /c", 500, 100, base=base))
        cfg_nov = self.config("nov.json", operations=[{"key": "search", "label": "searches", "service": "^shop-api$", "match": "^GET /search$", "slo_p95_s": 5.0}])
        F, md, _ = self.two_months(rows(OCT0), rows(T0), BASE, cfg_nov=cfg_nov)
        st = {k: v for k, v in self.statuses(F).items() if k.startswith("slow_hot_paths")}
        self.assertEqual(set(st.values()), {"within_target"})
        self.assertIn("closed — within its agreed p95 target", md)
        self.assertEqual([(c["field"], c["then"], c["now"]) for c in F["mom"]["targets_changed"]], [("slo_p95_s", None, 5.0)])
        self.assertIn("agreed p95 target for searches none → 5.00 s", md)

    # ------------------------------------------------------------ month over month
    def test_kpi_without_calls_is_not_a_move(self):
        oct_rows = ops("payment-api", "POST /pay/refund", 60, 80, fail_every=4, base=OCT0, method="POST") + ops("payment-api", "POST /pay", 2000, 80, base=OCT0, method="POST")
        nov_rows = ops("payment-api", "POST /pay", 2000, 80, method="POST")
        cfg = self.config("refund.json", operations=[{"key": "refunds", "label": "refunds", "service": "^payment-api$", "match": "^POST /pay/refund$"}])
        F, _, _ = self.two_months(oct_rows, nov_rows, BASE, cfg_oct=cfg)
        self.assertNotIn("refunds", F["mom"]["kpis"])
        self.assertFalse([r for r in F["mom"]["moved"] if "refunds" in r["where"]])

    def test_sample_p95_interval_marks_noise(self):
        lo, hi = stats.p95_interval_s(list(range(1000)))
        self.assertLess(lo, 0.95)
        self.assertGreater(hi, 0.95)
        self.assertIsNone(stats.p95_interval_s(list(range(10))))
        self.assertTrue(compare._ci_overlap([3.38, 5.88], [2.46, 4.87]))
        self.assertFalse(compare._ci_overlap([1.0, 1.2], [1.5, 1.9]))

    def test_closed_chatty_finding_tracks_its_own_figure(self):
        F = {"anatomy": {"shop-api": {"children_per_request": {"http": {"value": 0.89}, "db": {"value": 2.67}}, "repeated_children": []}}, "kpis": {}, "meta": {"thresholds": {}}}
        p = {"detector": "chatty_requests", "subject": {"service": "shop-api"},
             "metrics": {"primary": {"label": "outbound HTTP calls per request on shop-api", "value": 6.22, "unit": "ratio", "subject": "shop-api"}}}
        self.assertEqual(compare._current_value(F, p, {}), 0.89)
        p["metrics"]["primary"] = {"label": "runs of SELECT items per request on shop-api", "value": 12, "unit": "ratio", "subject": ["shop-api", "SELECT items"]}
        self.assertIsNone(compare._current_value(F, p, {}))

    def test_detector_last_month_could_not_run_is_first_measured(self):
        def rows(base, tree):
            out = []
            for i in range(400):
                out.append([ts(i % 600, base), "shop-api", "GET /orders", "300 ms", "GET", 200, "servlet.request", "web"] + ([f"t{i}", f"r{i}", ""] if tree else []))
                for k in range(7):
                    out.append([ts(i % 600, base), "shop-api", "GET /stock", "20 ms", "GET", 200, "okhttp.request", "http"] + ([f"t{i}", f"h{i}-{k}", f"r{i}"] if tree else []))
            return out
        F, md, _ = self.two_months(rows(OCT0, False), rows(T0, True), OPS, header_nov=TREE)
        st = {k: v for k, v in self.statuses(F).items() if k.startswith("chatty_requests")}
        self.assertEqual(set(st.values()), {"first_measured"})
        self.assertIn("last month's export had no parent and child spans", md)
        self.assertFalse([ln for ln in md.splitlines() if ln.strip() == "**Not covered this month.**"])

    def test_one_close_and_one_new_problem_is_not_an_identity_change(self):
        def rows(base, bad_a):
            return (ops("a-api", "GET /a", 1000, 50, fail_every=10 if bad_a else 0, base=base) + ops("b-api", "GET /b", 1000, 50, fail_every=0 if bad_a else 10, base=base)
                    + ops("c-api", "GET /c", 1000, 50, fail_every=20, base=base))
        F, _, _ = self.two_months(rows(OCT0, True), rows(T0, False), BASE)
        st = self.statuses(F)
        self.assertIn("closed", st.values())
        self.assertIn("new", st.values())
        self.assertFalse([c for c in F["caveats"] if "changed identity" in c])

    # ------------------------------------------------------------ notebook text
    def test_service_map_labels_keep_whole_words(self):
        self.assertEqual(mermaid.tech_line(".NET (ASP.NET Core)", " · 800 req in the sample"), ".NET · 800 req in the sample")
        self.assertEqual(mermaid.tech_line("", " · 900 k req (Services list)"), "900 k req (Services list)")
        self.assertEqual(mermaid.tech_line("Java servlet", " · 3.5 M req"), "Java servlet · 3.5 M req")

    def test_previous_none_does_not_claim_a_first_review(self):
        f = self.folder("none")
        write(os.path.join(f, "spans.csv"), BASE, ops("web", "GET /", 500, 50))
        self.assertEqual(self.build(f), 0)
        _, md, _ = self.facts()
        self.assertIn("no earlier month was compared in this build", md)
        self.assertNotIn("first review on record", md)

    def test_zone_without_daylight_saving_keeps_its_offset(self):
        tz, note, hint = config.resolve_timezone("Asia/Hong_Kong", "+00:00")
        self.assertEqual(tz.utcoffset(datetime(2026, 11, 19)), timedelta(hours=8))
        self.assertEqual(note, "")

    def test_by_design_rule_names_its_pattern_and_reason(self):
        f = self.folder("bd")
        write(os.path.join(f, "spans.csv"), BASE, ops("clinic-web", "POST /api/legacy/fax", 300, 40, fail_every=1, method="POST") + ops("clinic-web", "GET /api/x", 3000, 40))
        cfg = self.config("bd.json", noise={"by_design": [{"service": "^clinic-web$", "match": "^POST /api/legacy/fax$", "why": "the fax gateway was retired"}]})
        self.assertEqual(self.build(f, "--config", cfg), 0)
        _, md, _ = self.facts()
        self.assertIn("`^POST /api/legacy/fax$` — the fax gateway was retired", md)

    def test_landscape_prompt_folds_only_what_the_map_folds(self):
        f = self.folder("hosts")
        rows = []
        for i in range(2000):
            rows.append([ts(i % 600), "shop-api", "GET /x", "90 ms", "GET", 200, "servlet.request", "web", f"t{i}", f"r{i}", "", ""])
            h = f"api.h{i % 8}.com"
            rows.append([ts(i % 600), "shop-api", f"GET {h}/v1", "40 ms", "GET", 500 if i % 2 else 200, "okhttp.request", "http", f"t{i}", f"c{i}", f"r{i}", h])
        write(os.path.join(f, "spans.csv"), TREE + ["peer.service"], rows)
        self.assertEqual(self.build(f), 0)
        F, _, p = self.facts()
        with open(os.path.join(p, "prompts", "01-diagram-landscape.md"), encoding="utf-8") as fh:
            prompt = fh.read()
        self.assertEqual(F["meta"]["landscape_folded_hosts"], [])
        for k in range(8):                    # odd hosts fail (red), even ones are public egress only (amber)
            self.assertIn(f"| api.h{k}.com | {'red' if k % 2 else 'amber'} |", prompt)
        self.assertNotIn("more flagged hosts |", prompt)


if __name__ == "__main__":
    unittest.main()
