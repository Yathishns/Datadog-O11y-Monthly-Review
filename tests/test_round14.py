"""Tests for the fourteenth review round: cloud-region production envs, KPIs that overlap, UAT callees, verb-only resources,
workers seen only through probes or unmarked spans, services with no business request in the sample, store-named client
services, verb-only calls to unknown hosts, fan-out that explains the figure, the tracer's peer.service hint, single-caller
edges, p95 targets in milliseconds, fenced answers with prose, cross-references, and month over month (Services-list
workers, slow operations moved to noise, detector floors for client errors, noise, nodes and scanners, noise findings that
follow a reclassification, slow operations listed on another service's finding).
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
from ddreview import aggregate, config, mermaid, validate  # noqa: E402
from ddreview.classify import child_kind  # noqa: E402

T0 = datetime(2026, 11, 19, 10, 0, tzinfo=timezone.utc)
OCT0 = datetime(2026, 10, 19, 10, 0, tzinfo=timezone.utc)
BASE = ["Date", "Service", "Resource", "Duration", "Method", "Status Code"]
OPS = BASE + ["Operation Name", "Type"]
TREE = OPS + ["Trace ID", "Span ID", "Parent ID"]
SL_HEADER = "favorite\tType\tName\tHealth\tRequests\tError Rate\tp95 Latency (ns)\tMonitors\tSLOS\tEnvs\tLast Deploy\n"
SHORT = "round-fourteen"


def write(path, header, rows):
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        w.writerows(rows)


def ts(i, base=T0):
    return (base + timedelta(seconds=i)).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def ops(service, resource, n, ms, base=T0, method="GET", fails=0, status=500):
    return [[ts(i % 600, base), service, resource, f"{ms} ms", method, status if i < fails else 200] for i in range(n)]


class Round14(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ddreview-r14t-")
        self.out = os.path.join(self.tmp, "out")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def folder(self, name):
        p = os.path.join(self.tmp, name)
        os.makedirs(p, exist_ok=True)
        return p

    def config(self, name, **extra):
        cfg = {"org": {"name": "Round Fourteen", "short": SHORT}}
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

    def two_months(self, oct_rows, nov_rows, header, cfg_oct=None, cfg_nov=None, files=None):
        fo, fn = self.folder("oct"), self.folder("nov")
        write(os.path.join(fo, "spans.csv"), header, oct_rows)
        write(os.path.join(fn, "spans.csv"), header, nov_rows)
        for name, text in (files or {}).items():
            for f in (fo, fn):
                with open(os.path.join(f, name), "w", encoding="utf-8", newline="") as fh:
                    fh.write(text)
        self.assertEqual(self.build(fo, "--config", cfg_oct or self.config("plain.json"), month="October 2026")[0], 0)
        prev = os.path.join(self.out, SHORT, "2026-10", "facts.json")
        self.assertEqual(self.build(fn, "--config", cfg_nov or cfg_oct or self.config("plain.json"), "--previous", prev)[0], 0)
        return self.facts()

    @staticmethod
    def statuses(F):
        fm = F["mom"]["findings"]
        return {r["id"]: r.get("status", k) for k in ("closed", "unmeasured", "persisting", "new", "first_measured", "demoted", "promoted") for r in fm.get(k, [])}

    # ------------------------------------------------------------ helpers
    def test_small_helpers(self):
        cfg = config.load_config(None)
        prod = {"prod", "production"}
        for env in ("prod-us-east-1", "prod-eu-west-1", "production-ap-southeast-2", "prod-eu"):
            self.assertTrue(aggregate._is_prod_env(env, prod, cfg["_rx"]["nonprod"]), env)
        for env in ("prod-staging", "prod-uat", "preprod"):
            self.assertFalse(aggregate._is_prod_env(env, prod, cfg["_rx"]["nonprod"]), env)
        self.assertEqual(aggregate.match_key("GET", "GET"), "get")
        self.assertEqual(child_kind("redis.client", "get", "", cfg), "cache")
        self.assertEqual(child_kind("elastic.client", "search", "", cfg), "db")
        F = {"meta": {"thresholds": {"dep_slow_s": 3}}}
        dep = {"err": {"pct": 1.2, "num": 12, "den": 1000}, "class": "public", "marker": ""}
        self.assertEqual(mermaid._edge_text(F, dep, {"pct": 0.0, "num": 0, "den": 200}, None, True, only_caller=True), "1.20 % fail")
        self.assertEqual(mermaid._edge_text(F, dep, {"pct": 0.0, "num": 0, "den": 200}, None, True), "no failures")
        secs = validate._sections("# T\n\n## Finding 1 — x\n\n*Dependency map for Findings 1 and 6:* failed 3 of 9\n")
        self.assertNotIn("6", "".join(secs.values()).replace("of 9", ""))

    def test_p95_target_in_milliseconds_is_a_config_error(self):
        p = self.config("ms.json", operations=[{"key": "a", "label": "as", "match": "^GET /a$", "slo_p95_s": 800}])
        with self.assertRaises(config.ConfigError):
            config.load_config(p)

    # ------------------------------------------------------------ inputs
    def test_overlapping_kpis_count_once(self):
        f = self.folder("ov")
        write(os.path.join(f, "spans.csv"), BASE, ops("checkout-api", "POST /api/checkout", 4000, 40, method="POST", fails=38)
              + ops("checkout-api", "POST /api/orders", 4000, 40, method="POST", fails=4))
        cfg = self.config("ov.json", operations=[{"key": "checkouts", "label": "checkouts", "service": ".*", "match": "^POST /api/checkout$", "slo_success_pct": 99.0},
                                                 {"key": "order_writes", "label": "order writes", "service": ".*", "match": "^POST /api/", "slo_success_pct": 99.5}])
        self.assertEqual(self.build(f, "--config", cfg)[0], 0)
        F, md, _ = self.facts()
        self.assertEqual(F["kpis"]["order_writes"]["calls"], 4000)
        self.assertNotIn("Order writes miss", md)

    def test_uat_callee_keeps_the_nonprod_host(self):
        f = self.folder("uat")
        rows = []
        for i in range(500):
            rows.append([ts(i % 600), "web-bff", "GET /home", "90 ms", "GET", 200, "express.request", "web", f"t{i}", f"r{i}", "", "", "prod"])
            rows.append([ts(i % 600), "web-bff", "GET", "40 ms", "GET", 200, "http.request", "http", f"t{i}", f"c{i}", f"r{i}", "orders-api.uat.shop.internal", "prod"])
            rows.append([ts(i % 600), "orders-api", "GET /orders", "30 ms", "GET", 200, "express.request", "web", f"t{i}", f"s{i}", f"c{i}", "", "uat"])
        write(os.path.join(f, "spans.csv"), TREE + ["peer.service", "Env"], rows)
        self.assertEqual(self.build(f)[0], 0)
        F, _, _ = self.facts()
        self.assertTrue([x for x in F["findings"] if x["detector"] == "nonprod_from_prod"])

    def test_probe_only_worker_reads_as_sampled_through_probes(self):
        f = self.folder("probe")
        rows = [[ts(i % 600), "order-api", "POST /api/orders", "90 ms", "POST", 200, "servlet.request", "web"] for i in range(600)]
        rows += [[ts(i % 600), "email-worker", "GET /health", "2 ms", "GET", 200, "flask.request", "web"] for i in range(120)]
        write(os.path.join(f, "spans.csv"), OPS, rows)
        with open(os.path.join(f, "services.tsv"), "w", encoding="utf-8", newline="") as fh:
            fh.write(SL_HEADER + "\tweb\torder-api\t\t600\t0\t90000000\t\t\tprod\t\n\tqueue\temail-worker\t\t300000\t0.021\t500000000\t\t\tprod\t\n")
        self.assertEqual(self.build(f)[0], 0)
        F, md, _ = self.facts()
        self.assertEqual(F["services"]["email-worker"]["list_reason"], "probes_sampled")
        self.assertIn("in the span sample only through health probes", md)
        self.assertNotIn("Not in the span sample", md)

    def test_service_with_only_probes_and_no_services_list_is_not_unrecorded(self):
        f = self.folder("stock")
        write(os.path.join(f, "spans.csv"), OPS, [[ts(i % 600), "shop-api", "GET /x", "40 ms", "GET", 200, "servlet.request", "web"] for i in range(600)]
              + [[ts(i % 600), "stock-api", "GET /healthz", "1 ms", "GET", 200, "servlet.request", "web"] for i in range(40)])
        self.assertEqual(self.build(f)[0], 0)
        F, _, _ = self.facts()
        self.assertTrue(F["services"]["stock-api"]["failure_known"])

    def test_verb_only_calls_to_unknown_hosts_are_not_n_plus_one(self):
        f = self.folder("ext")
        rows = []
        for i in range(400):
            rows.append([ts(i % 600), "web-bff", "GET /home", "190 ms", "GET", 200, "express.request", "web", f"t{i}", f"r{i}", ""])
            for k in range(6):
                rows.append([ts(i % 600), "web-bff", "GET", "25 ms", "GET", 200, "http.request", "http", f"t{i}", f"c{i}-{k}", f"r{i}"])
        write(os.path.join(f, "spans.csv"), TREE, rows)
        self.assertEqual(self.build(f)[0], 0)
        F, md, _ = self.facts()
        self.assertFalse(F["anatomy"]["web-bff"]["repeated_children"])
        self.assertNotIn("an N+1 pattern", md)

    def test_fan_out_must_explain_the_figure(self):
        f = self.folder("reco")
        rows = []
        for i in range(300):
            rows.append([ts(i % 600), "web-bff", "GET /api/home", "190 ms", "GET", 200, "express.request", "web", f"t{i}", f"r{i}", "", ""])
            targets = ["cart", "catalog", "user"] + (["reco"] * 6 if i % 5 < 2 else [])
            for k, h in enumerate(targets):
                rows.append([ts(i % 600), "web-bff", "GET", "15 ms", "GET", 200, "http.request", "http", f"t{i}", f"c{i}-{k}", f"r{i}", f"{h}.svc.cluster.local"])
        write(os.path.join(f, "spans.csv"), TREE + ["peer.service"], rows)
        self.assertEqual(self.build(f)[0], 0)
        _, md, _ = self.facts()
        self.assertNotIn("once each", md)

    def test_tracer_peer_service_hint_when_the_column_exists(self):
        f = self.folder("peer")
        rows = []
        for i in range(600):
            rows.append([ts(i % 600), "cart-api", "GET /cart", "90 ms", "GET", 200, "servlet.request", "web", f"t{i}", f"r{i}", "", ""])
            rows.append([ts(i % 600), "cart-api", "GET /price", "40 ms", "GET", 503 if i % 4 == 0 else 200, "okhttp.request", "http", f"t{i}", f"c{i}", f"r{i}", ""])
            rows.append([ts(i % 600), "cart-api", "GET /stock", "40 ms", "GET", 200, "okhttp.request", "http", f"t{i}", f"d{i}", f"r{i}", "stock.internal.example.com"])
        write(os.path.join(f, "spans.csv"), TREE + ["peer.service"], rows)
        self.assertEqual(self.build(f)[0], 0)
        _, md, _ = self.facts()
        self.assertIn("Have the tracer set peer.service", md)
        self.assertNotIn("Add peer.service to the spans export", md)

    # ------------------------------------------------------------ month over month
    def test_worker_failure_not_closed_when_the_sample_shows_only_probes(self):
        sl = SL_HEADER + "\tweb\tweb-api\t\t1000\t0\t90000000\t\t\tprod\t\n\tqueue\treport-worker\t\t463680\t0.045\t900000000\t\t\tprod\t\n"
        oct_rows = ops("report-worker", "POST /jobs/run", 400, 200, base=OCT0, method="POST", fails=20) + ops("report-worker", "GET /health", 60, 2, base=OCT0)
        oct_rows += ops("web-api", "GET /x", 1000, 40, base=OCT0)
        nov_rows = ops("report-worker", "GET /health", 60, 2) + ops("web-api", "GET /x", 1000, 40)
        F, _, _ = self.two_months(oct_rows, nov_rows, BASE, files={"services.tsv": sl})
        self.assertEqual({v for k, v in self.statuses(F).items() if k.startswith("failing_operations")}, {"too_little_traffic"})

    def test_slow_operation_moved_to_health_noise_is_reclassified(self):
        def rows(base):
            return ops("pay-api", "GET /pay/status", 700, 120, base=base) + ops("pay-api", "GET /internal/healthcheck/deep", 600, 3500, base=base)
        F, md, _ = self.two_months(rows(OCT0), rows(T0), BASE, cfg_nov=self.config("n.json", noise={"health": ["^GET /internal/healthcheck/deep$"]}))
        st = self.statuses(F)
        self.assertIn("reclassified", st.values())
        self.assertFalse([r for r in F["mom"]["moved"] if r["metric"] == "p95"])

    def test_detector_floors_keep_client_error_findings_open(self):
        from ddreview import compare
        cfg = config.load_config(None)
        F = {"services": {"shop-api": {"requests": {"tier": "sample"}, "failed": {"all": 0, "business": 0}}},
             "resources": {"shop-api": [{"resource": "GET /api/session", "status_mix": {"2xx": 51, "4xx": 39}, "tier": "sample"}]},
             "window": {"volumes_tier": "sample"}, "meta": {"thresholds": cfg["thresholds"]}}
        p = {"detector": "client_errors", "subject": {"service": "shop-api"},
             "metrics": {"primary": {"subject": ["shop-api", "GET /api/session"], "unit": "pct", "num": 131, "den": 300, "value": 43.7}}}
        calls, floor_now, _ = compare._calls_and_floors(F, {"meta": {"thresholds": {}}}, p, cfg)
        self.assertEqual(calls, 90)
        self.assertLess(calls, floor_now)
        F["resources"]["shop-api"][0]["status_mix"] = {"2xx": 411, "4xx": 89}
        p["metrics"]["primary"].update({"num": 107, "den": 500, "value": 21.4})
        self.assertTrue(compare._within_noise(F, {}, p, 17.8, cfg["thresholds"]))

    def test_noise_finding_after_a_by_design_move_is_first_measured(self):
        def rows(base):
            return ops("pay-api", "POST /checkout/legacy", 300, 40, base=base, method="POST", fails=300) + ops("pay-api", "POST /checkout", 3000, 40, base=base, method="POST", fails=6)
        F, _, _ = self.two_months(rows(OCT0), rows(T0), BASE,
                                  cfg_nov=self.config("n.json", noise={"by_design": [{"service": "^pay-api$", "match": "legacy$", "why": "retired"}]}))
        self.assertEqual({v for k, v in self.statuses(F).items() if k.startswith("noise_masks_signal")}, {"first_measured"})

    def test_scanner_below_its_floor_is_too_little_traffic(self):
        oct_rows = ops("shop-api", "GET /manager/html", 90, 5, base=OCT0) + ops("shop-api", "GET /phpmyadmin/index.php", 60, 5, base=OCT0, fails=60, status=404)
        nov_rows = ops("shop-api", "GET /manager/html", 25, 5) + ops("shop-api", "GET /phpmyadmin/index.php", 15, 5, fails=15, status=404)
        for k in range(6):
            oct_rows += ops("shop-api", f"GET /api/x{k}", 400, 40, base=OCT0)
            nov_rows += ops("shop-api", f"GET /api/x{k}", 400, 40)
        F, _, _ = self.two_months(oct_rows, nov_rows, BASE)
        self.assertEqual({v for k, v in self.statuses(F).items() if k.startswith("scanner_exposure")}, {"too_little_traffic"})

    # ------------------------------------------------------------ validate
    def test_fenced_answer_with_prose_is_unwrapped(self):
        f = self.folder("val")
        write(os.path.join(f, "spans.csv"), BASE, ops("shop-api", "POST /api/cart", 3000, 40, method="POST", fails=100) + ops("shop-api", "GET /x", 3000, 30))
        self.assertEqual(self.build(f)[0], 0)
        F, md, path = self.facts()
        answer = os.path.join(self.tmp, "a.md")
        with open(answer, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("Here is the polished notebook:\n\n```markdown\n" + md + "\n```\n\nLet me know if you want changes.\n")
        code, out = self.run_cli(["validate", answer, "--facts", os.path.join(os.path.dirname(path), "facts.json")])
        self.assertIn("other text around it", out)


if __name__ == "__main__":
    unittest.main()
