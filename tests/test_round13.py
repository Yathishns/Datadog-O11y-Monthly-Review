"""Tests for the thirteenth review round: probe cadences on real periods and per service, framework scrapes and slash-less
.NET probes, workers seen only through probes, the slow share's base, entry-only exports, fan-out per call target, one edge
per call, exact live-graph tags, closes within noise judged on noise tests only, slow operations that were reported inside
the slow-service finding, scanner findings on a short sample, KPIs whose operations changed, the noise class of a
reclassified operation, region-suffixed production envs, overlapping KPIs, probe verbs, CRLF fenced answers, dependency
shapes and built-in hosts.
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
from ddreview import aggregate, compare, config, mermaid, recipes  # noqa: E402
from ddreview.classify import host_class, resource_class  # noqa: E402

T0 = datetime(2026, 11, 19, 10, 0, tzinfo=timezone.utc)
OCT0 = datetime(2026, 10, 19, 10, 0, tzinfo=timezone.utc)
BASE = ["Date", "Service", "Resource", "Duration", "Method", "Status Code"]
OPS = BASE + ["Operation Name", "Type"]
TREE = OPS + ["Trace ID", "Span ID", "Parent ID"]
EP_HEADER = ["Resource", "Service", "Requests", "Error Rate", "P95 Latency", "Monitor Counts"]
SL_HEADER = "favorite\tType\tName\tHealth\tRequests\tError Rate\tp95 Latency (ns)\tMonitors\tSLOS\tEnvs\tLast Deploy\n"
SHORT = "round-thirteen"


def write(path, header, rows):
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        w.writerows(rows)


def ts(i, base=T0):
    return (base + timedelta(seconds=i)).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def ops(service, resource, n, ms, base=T0, method="GET", fails=0):
    return [[ts(i % 600, base), service, resource, f"{ms} ms", method, 500 if i < fails else 200] for i in range(n)]


class Round13(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ddreview-r13t-")
        self.out = os.path.join(self.tmp, "out")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def folder(self, name):
        p = os.path.join(self.tmp, name)
        os.makedirs(p, exist_ok=True)
        return p

    def config(self, name, **extra):
        cfg = {"org": {"name": "Round Thirteen", "short": SHORT}}
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
            args += ["--config", self.config("plain.json")]
        if "--previous" not in extra:
            args += ["--previous", "none"]
        return self.run_cli(args)

    def facts(self, key="2026-11"):
        p = os.path.join(self.out, SHORT, key)
        with open(os.path.join(p, "facts.json"), encoding="utf-8") as fh:
            F = json.load(fh)
        names = [x for x in os.listdir(p) if x.endswith(".md") and "review" in x]
        with open(os.path.join(p, names[0]), encoding="utf-8") as fh:
            return F, fh.read(), os.path.join(p, names[0])

    def two_months(self, oct_rows, nov_rows, header, cfg_oct=None, cfg_nov=None):
        fo, fn = self.folder("oct"), self.folder("nov")
        write(os.path.join(fo, "spans.csv"), header, oct_rows)
        write(os.path.join(fn, "spans.csv"), header, nov_rows)
        self.assertEqual(self.build(fo, "--config", cfg_oct or self.config("plain.json"), month="October 2026")[0], 0)
        prev = os.path.join(self.out, SHORT, "2026-10", "facts.json")
        self.assertEqual(self.build(fn, "--config", cfg_nov or cfg_oct or self.config("plain.json"), "--previous", prev)[0], 0)
        return self.facts()

    @staticmethod
    def statuses(F):
        fm = F["mom"]["findings"]
        return {r["id"]: r.get("status", k) for k in ("closed", "unmeasured", "persisting", "new", "first_measured", "demoted", "promoted") for r in fm.get(k, [])}

    # ------------------------------------------------------------ inputs
    def test_probe_cadence_needs_a_real_period_and_two_services(self):
        cfg = config.load_config(None)
        exact = [{"service": "catalog-api", "resource": "GET /actuator/health/liveness", "calls": 181440},
                 {"service": "catalog-api", "resource": "GET /actuator/health/readiness", "calls": 181440}]
        p7 = aggregate._probes(exact, 7, cfg)
        self.assertEqual([(p["service"], p["probers"], p["period_s"]) for p in p7], [("catalog-api", 3, 10)])
        self.assertFalse(aggregate._probes(exact, 30, cfg))
        drifted = [{"service": "catalog-api", "resource": "GET /actuator/health/liveness", "calls": 181895}]
        self.assertFalse(aggregate._probes(drifted, 30, cfg))

    def test_framework_scrapes_and_slashless_probes_are_health(self):
        cfg = config.load_config(None)
        for res in ("GET /q/metrics", "GET /prometheus", "GET /management/prometheus", "GET /q/metrics/application", "GET api/v1/health", "GET api/health/ready"):
            self.assertEqual(resource_class("svc", res, cfg)[0], "health", res)
        self.assertEqual(resource_class("svc", "GET api/patients/{id}/health", cfg)[0], "business")

    def test_worker_seen_only_through_probes_uses_its_services_list_figures(self):
        f = self.folder("probe")
        rows = [[ts(i % 600), "order-api", "POST /api/orders", "90 ms", "POST", 200, "servlet.request", "web"] for i in range(600)]
        rows += [[ts(i % 600), "billing-worker", "GET /actuator/health", "2 ms", "GET", 200, "servlet.request", "web"] for i in range(55)]
        write(os.path.join(f, "spans.csv"), OPS, rows)
        with open(os.path.join(f, "services.tsv"), "w", encoding="utf-8", newline="") as fh:
            fh.write(SL_HEADER + "\tweb\torder-api\t\t600\t0\t90000000\t\t\tprod\t\n\tqueue\tbilling-worker\t\t350000\t0.06\t900000000\t\t\tprod\t\n")
        self.assertEqual(self.build(f)[0], 0)
        F, md, _ = self.facts()
        self.assertEqual(F["services"]["billing-worker"]["requests"]["tier"], "services_list")
        self.assertIn("21,000 of 350,000 (6.00 %)", md)

    def test_one_call_into_a_traced_service_is_one_edge(self):
        f = self.folder("dns")
        rows = []
        for i in range(400):
            rows.append([ts(i % 600), "web-bff", "GET /home", "120 ms", "GET", 200, "express.request", "web", f"t{i}", f"r{i}", "", ""])
            for k, b in enumerate(("catalog-api", "cart-api", "user-api")):
                rows.append([ts(i % 600), "web-bff", "GET", "20 ms", "GET", 200, "http.request", "http", f"t{i}", f"c{i}-{k}", f"r{i}", f"{b}.shop.svc.cluster.local"])
                rows.append([ts(i % 600), b, "GET /items", "15 ms", "GET", 200, "express.request", "web", f"t{i}", f"s{i}-{k}", f"c{i}-{k}", ""])
        write(os.path.join(f, "spans.csv"), TREE + ["peer.service"], rows)
        self.assertEqual(self.build(f)[0], 0)
        F, _, _ = self.facts()
        self.assertEqual(sorted(e["to"] for e in F["edges"] if e["from"] == "web-bff"), ["cart-api", "catalog-api", "user-api"])

    def test_fan_out_wording_needs_each_target_once(self):
        f = self.folder("tri")
        rows = []
        for i in range(400):
            rows.append([ts(i % 600), "web-bff", "GET /home", "190 ms", "GET", 200, "express.request", "web", f"t{i}", f"r{i}", ""])
            calls = ["pricing-svc"] * 3 + ["a-svc", "b-svc", "c-svc"]
            for k, b in enumerate(calls):
                rows.append([ts(i % 600), "web-bff", "GET", "15 ms", "GET", 200, "http.request", "http", f"t{i}", f"c{i}-{k}", f"r{i}"])
                rows.append([ts(i % 600), b, "GET /x", "10 ms", "GET", 200, "express.request", "web", f"t{i}", f"s{i}-{k}", f"c{i}-{k}"])
        rows += [[ts(i % 600), "web-bff", "GET /healthz", "1 ms", "GET", 200, "express.request", "web", f"h{i}", f"hz{i}", ""] for i in range(300)]
        write(os.path.join(f, "spans.csv"), TREE, rows)
        self.assertEqual(self.build(f)[0], 0)
        _, md, _ = self.facts()
        self.assertNotIn("once each", md)

    def test_slow_share_counts_small_operations_in_its_base(self):
        f = self.folder("share")
        rows = ops("portal-api", "GET /api/dashboard", 300, 3000) + ops("portal-api", "GET /api/profile", 300, 50) + ops("portal-api", "GET /api/messages", 300, 50)
        for k in range(60):
            rows += ops("portal-api", f"GET /api/x{k}", 15, 40)
        write(os.path.join(f, "spans.csv"), BASE, rows)
        self.assertEqual(self.build(f)[0], 0)
        F, _, _ = self.facts()
        self.assertFalse([x for x in F["findings"] if x["detector"] == "slow_hot_paths"])

    def test_entry_only_export_makes_no_claim_about_child_spans(self):
        f = self.folder("entry")
        rows = [[ts(i % 600), "orders-api", "GET /api/orders", f"{3000 + i % 7} ms", "GET", 200, "servlet.request", "web", f"t{i}", f"r{i}", ""] for i in range(300)]
        rows += [[ts(i % 600), "orders-api", f"GET /api/y{i % 3}", "40 ms", "GET", 200, "servlet.request", "web", f"u{i}", f"q{i}", ""] for i in range(600)]
        write(os.path.join(f, "spans.csv"), TREE, rows)
        self.assertEqual(self.build(f)[0], 0)
        _, md, _ = self.facts()
        self.assertNotIn("no database, HTTP or cache spans inside them", md)

    def test_small_helpers(self):
        self.assertEqual(aggregate.match_key("PROPFIND /", "PROPFIND"), "propfind /")
        self.assertTrue(aggregate._is_prod_env("prod-eu", {"prod"}))
        self.assertFalse(aggregate._is_prod_env("preprod", {"prod"}))
        self.assertEqual(recipes.resource_wildcard("GET /api/cart/?"), "get_/api/cart/")
        self.assertEqual(recipes.resource_wildcard("/inventory.v1.Inventory/GetStock"), "/inventory.v1.inventory/getstock")
        cfg = config.load_config(None)
        self.assertTrue(config.matches_any(cfg["_rx"]["allowed_egress"], "browser-intake-us5-datadoghq.com"))
        self.assertEqual(host_class("contoso.visualstudio.com", cfg), "cloud")
        self.assertEqual(host_class("dc.services.visualstudio.com", cfg), "telemetry")
        F = {"services": {"requests": {"role": "integration"}}}
        self.assertEqual(mermaid.dep_shape(F, "requests", {"type": ""}), "host")
        self.assertEqual(mermaid.dep_shape(F, "database calls of x", {"type": "db"}), "db")

    def test_overlapping_kpis_are_reported(self):
        f = self.folder("ov")
        write(os.path.join(f, "spans.csv"), BASE, ops("pay", "POST /pay/authorise", 2000, 40, method="POST", fails=200))
        cfg = self.config("ov.json", operations=[{"key": "auth", "label": "card authorisations", "service": "^pay$", "match": "^POST /pay/authorise$", "slo_success_pct": 99.5},
                                                 {"key": "all", "label": "payment calls", "service": "^pay$", "match": "^POST /pay/"}])
        code, out = self.build(f, "--config", cfg)
        self.assertEqual(code, 0)
        F, md, path = self.facts()
        self.assertIn("card authorisations", md)
        vpath = os.path.join(os.path.dirname(path), "validation.txt")
        text = out
        if os.path.exists(vpath):
            with open(vpath, encoding="utf-8") as fh:
                text += fh.read()
        self.assertIn("both match", text)

    # ------------------------------------------------------------ month over month
    def test_close_noise_is_a_noise_test(self):
        T = {"mom_min_failures": 10, "mom_rate_z": 2.0}
        self.assertTrue(compare._significant((20, 4000), (2, 4000), T))
        self.assertFalse(compare._significant((6, 445), (3, 518), T))
        self.assertFalse(compare._significant((565, 2813), (552, 2789), T))

    def test_slow_operation_reported_inside_slow_service_persists(self):
        def rows(base, report_ms):
            return (ops("shop-api", "GET /api/search", 400, 3340, base=base) + ops("shop-api", "GET /api/report", 300, report_ms, base=base)
                    + ops("shop-api", "GET /a", 800, 50, base=base) + ops("shop-api", "GET /b", 800, 50, base=base))
        F, _, _ = self.two_months(rows(OCT0, 2600), rows(T0, 200), BASE)
        st = self.statuses(F)
        self.assertEqual({v for k, v in st.items() if k.startswith("slow_operations")}, {"persisting"})
        self.assertEqual({v for k, v in st.items() if k.startswith("slow_hot_paths")}, {"superseded"})

    def test_scanner_finding_not_closed_by_a_quiet_sample(self):
        oct_rows = ops("shop-api", "GET /manager/html", 120, 5, base=OCT0) + ops("shop-api", "GET /api/x", 2000, 40, base=OCT0)
        nov_rows = ops("shop-api", "GET /api/x", 2000, 40)
        F, _, _ = self.two_months(oct_rows, nov_rows, BASE)
        self.assertEqual({v for k, v in self.statuses(F).items() if k.startswith("scanner_exposure")}, {"too_little_traffic"})

    def test_kpi_with_an_operation_moved_to_noise_does_not_move(self):
        def rows(base):
            return ops("pay-api", "POST /checkout", 1000, 40, base=base, method="POST", fails=10) + ops("pay-api", "POST /checkout/legacy", 300, 40, base=base, method="POST", fails=300)
        kpi = [{"key": "c", "label": "checkouts", "service": "^pay-api$", "match": "^POST /checkout"}]
        F, _, _ = self.two_months(rows(OCT0), rows(T0), BASE, cfg_oct=self.config("o.json", operations=kpi),
                                  cfg_nov=self.config("n.json", operations=kpi, noise={"by_design": [{"service": "^pay-api$", "match": "legacy$", "why": "retired"}]}))
        self.assertFalse([r for r in F["mom"]["moved"] if "checkouts" in r["where"]])

    def test_reclassified_health_operation_says_health(self):
        def rows(base):
            return ops("shop-api", "GET /api/v2/system/probe", 600, 5, base=base, fails=30) + ops("shop-api", "GET /api/x", 2000, 40, base=base)
        F, md, _ = self.two_months(rows(OCT0), rows(T0), BASE, cfg_nov=self.config("n.json", noise={"health": ["^GET /api/v2/system/probe$"]}))
        self.assertIn("reclassified as health-check noise", md)
        self.assertNotIn("reclassified as by-design noise", md)

    # ------------------------------------------------------------ validate
    def test_crlf_fenced_answer_is_unwrapped(self):
        f = self.folder("val")
        write(os.path.join(f, "spans.csv"), BASE, ops("shop-api", "POST /api/cart", 3000, 40, method="POST", fails=100) + ops("shop-api", "GET /x", 3000, 30))
        self.assertEqual(self.build(f)[0], 0)
        F, md, path = self.facts()
        answer = os.path.join(self.tmp, "a.md")
        with open(answer, "w", encoding="utf-8", newline="") as fh:
            fh.write(("```markdown\n" + md + "\n```\n").replace("\n", "\r\n"))
        code, out = self.run_cli(["validate", answer, "--facts", os.path.join(os.path.dirname(path), "facts.json")])
        self.assertIn("wrapped in a code fence", out)


if __name__ == "__main__":
    unittest.main()
