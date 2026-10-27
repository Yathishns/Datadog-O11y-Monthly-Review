"""Tests for the twelfth review round: metrics scrapes and long intervals that never prove a range, framework and gRPC
health probes, HTTP fan-out told apart by callee, cache clusters that still show N+1, agreed targets judged beyond
sampling noise and per service with business operations only, the target-miss lead and budget, slow wording that names
agreed operations, live-graph tags, Datadog's own hosts, SaaS hosts named 'dev.', short Services-list ranges, month over
month (targets relaxed, removed or renamed, allow-listed and reclassified hosts, closes within noise), fenced answers and
leftover diagram markers.
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
from ddreview import aggregate, config, mermaid, narrative, recipes  # noqa: E402
from ddreview.classify import host_class, resource_class  # noqa: E402

T0 = datetime(2026, 11, 19, 10, 0, tzinfo=timezone.utc)
OCT0 = datetime(2026, 10, 19, 10, 0, tzinfo=timezone.utc)
BASE = ["Date", "Service", "Resource", "Duration", "Method", "Status Code"]
OPS = BASE + ["Operation Name", "Type"]
TREE = OPS + ["Trace ID", "Span ID", "Parent ID"]
SHORT = "round-twelve"


def write(path, header, rows):
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        w.writerows(rows)


def ts(i, base=T0):
    return (base + timedelta(seconds=i)).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def ops(service, resource, n, ms, fail_every=0, base=T0, method="GET", fails=None):
    out = []
    for i in range(n):
        failed = (i < fails) if fails is not None else bool(fail_every and i % fail_every == 0)
        out.append([ts(i % 600, base), service, resource, f"{ms} ms", method, 500 if failed else 200])
    return out


class Round12(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ddreview-r12t-")
        self.out = os.path.join(self.tmp, "out")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def folder(self, name):
        p = os.path.join(self.tmp, name)
        os.makedirs(p, exist_ok=True)
        return p

    def config(self, name, **extra):
        cfg = {"org": {"name": "Round Twelve", "short": SHORT}}
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
        return self.run_cli(args)[0]

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
        self.assertEqual(self.build(fo, "--config", cfg_oct or self.config("plain.json"), month="October 2026"), 0)
        prev = os.path.join(self.out, SHORT, "2026-10", "facts.json")
        self.assertEqual(self.build(fn, "--config", cfg_nov or cfg_oct or self.config("plain.json"), "--previous", prev), 0)
        return self.facts()

    @staticmethod
    def statuses(F):
        fm = F["mom"]["findings"]
        return {r["id"]: r.get("status", k) for k in ("closed", "unmeasured", "persisting", "new", "first_measured", "demoted", "promoted") for r in fm.get(k, [])}

    # ------------------------------------------------------------ inputs and classes
    def test_scrapes_and_long_random_intervals_never_prove_a_range(self):
        cfg = config.load_config(None)
        eps = [{"service": "orders-api", "resource": "GET /healthz", "calls": 120960}, {"service": "orders-api", "resource": "GET /metrics", "calls": 25000},
               {"service": "orders-api", "resource": "GET /readyz", "calls": 25000}]
        found = {p["resource"] for p in aggregate._probes(eps, 28, cfg)}
        self.assertNotIn("GET /metrics", found)
        self.assertNotIn("GET /readyz", found)              # 96.768 s is not a whole-second cadence
        self.assertIn("GET /healthz", found)

    def test_framework_and_grpc_probes_are_health(self):
        cfg = config.load_config(None)
        for res in ("GET /q/health/live", "GET /q/health/ready", "GET /api/v1/orders/health", "GET /orders-service/api/v1/health", "/grpc.health.v1.Health/Check"):
            self.assertEqual(resource_class("svc", res, cfg)[0], "health", res)
        self.assertEqual(resource_class("svc", "GET /api/patients/{id}/health", cfg)[0], "business")

    def test_saas_hosts_and_datadog_itself(self):
        cfg = config.load_config(None)
        for host in ("dev.azure.com", "vsrm.dev.azure.com", "contoso.dev.azuresynapse.net"):
            self.assertNotEqual(host_class(host, cfg), "nonprod", host)
        self.assertNotEqual(host_class("http-intake.logs.datadoghq.com", cfg), "telemetry")
        self.assertTrue(config.matches_any(cfg["_rx"]["allowed_egress"], "http-intake.logs.datadoghq.com"))

    def test_bff_fan_out_without_peer_is_not_n_plus_one(self):
        f = self.folder("bff")
        rows = []
        backends = ("catalog-api", "cart-api", "user-api", "reco-api", "promo-api", "stock-api")
        for i in range(400):
            rows.append([ts(i % 600), "web-bff", "GET /home", "190 ms", "GET", 200, "express.request", "web", f"t{i}", f"r{i}", ""])
            for k, b in enumerate(backends):
                rows.append([ts(i % 600), "web-bff", "GET", "25 ms", "GET", 200, "http.request", "http", f"t{i}", f"c{i}-{k}", f"r{i}"])
                rows.append([ts(i % 600), b, "GET /items", "20 ms", "GET", 200, "express.request", "web", f"t{i}", f"s{i}-{k}", f"c{i}-{k}"])
        write(os.path.join(f, "spans.csv"), TREE, rows)
        self.assertEqual(self.build(f), 0)
        F, md, _ = self.facts()
        self.assertFalse(F["anatomy"]["web-bff"]["repeated_children"])
        self.assertNotIn("an N+1 pattern", md)
        self.assertIn("fan-out, not a loop", md)

    def test_cache_cluster_repeats_are_still_n_plus_one(self):
        f = self.folder("redis")
        rows = []
        for i in range(300):
            rows.append([ts(i % 600), "profile-api", "GET /profile", "60 ms", "GET", 200, "servlet.request", "web", f"t{i}", f"r{i}", "", ""])
            for k in range(6):
                rows.append([ts(i % 600), "redis", "GET", "1 ms", "", "", "redis.command", "redis", f"t{i}", f"c{i}-{k}", f"r{i}", f"10.0.4.{11 + k % 3}"])
        write(os.path.join(f, "spans.csv"), TREE + ["peer.service"], rows)
        self.assertEqual(self.build(f), 0)
        F, _, _ = self.facts()
        self.assertTrue(F["anatomy"]["profile-api"]["repeated_children"])

    # ------------------------------------------------------------ agreed targets
    def kpi(self, name, **extra):
        k = {"key": "k", "label": "order placements", "service": ".*", "match": "^POST /api/orders$", "slo_success_pct": 99.5}
        k.update(extra)
        return self.config(name, operations=[k])

    def test_a_single_sampled_failure_is_not_a_target_miss(self):
        f = self.folder("one")
        write(os.path.join(f, "spans.csv"), BASE, ops("tracking-api", "POST /api/orders", 600, 40, fails=1, method="POST") + ops("tracking-api", "GET /x", 600, 40))
        self.assertEqual(self.build(f, "--config", self.kpi("one.json", slo_success_pct=99.9)), 0)
        F, _, _ = self.facts()
        self.assertFalse([x for x in F["findings"] if x["detector"] == "failing_operations"])

    def test_a_kpi_on_two_services_judges_each_service(self):
        f = self.folder("gw")
        rows = ops("web-bff", "POST /api/orders", 3000, 40, fails=1, method="POST") + ops("orders-api", "POST /api/orders", 3000, 40, fails=28, method="POST")
        rows += ops("web-bff", "GET /a", 600, 30) + ops("orders-api", "GET /b", 600, 30)
        write(os.path.join(f, "spans.csv"), BASE, rows)
        self.assertEqual(self.build(f, "--config", self.kpi("gw.json")), 0)
        F, md, _ = self.facts()
        subjects = {x["subject"]["service"] for x in F["findings"] if x["detector"] == "failing_operations"}
        self.assertEqual(subjects, {"orders-api"})
        self.assertIn("× the 0.5 % of calls the target allows to fail", md)

    def test_a_kpi_counts_business_operations_only(self):
        f = self.folder("bd")
        write(os.path.join(f, "spans.csv"), BASE, ops("claims-api", "POST /api/claims", 3000, 40, fails=15, method="POST")
              + ops("claims-api", "POST /api/claims/legacy-sync", 300, 40, fail_every=1, method="POST"))
        cfg = self.config("bd.json", operations=[{"key": "c", "label": "claims", "service": "^claims-api$", "match": "^POST /api/claims"}],
                          noise={"by_design": [{"service": "^claims-api$", "match": "legacy-sync$", "why": "retired"}]})
        self.assertEqual(self.build(f, "--config", cfg), 0)
        F, _, _ = self.facts()
        self.assertEqual(F["kpis"]["c"]["calls"], 3000)

    def test_target_miss_lead_when_small_operations_lift_the_service(self):
        f = self.folder("small")
        rows = ops("orders-api", "POST /api/orders", 3000, 40, fails=28, method="POST")
        for k in range(4):
            rows += ops("orders-api", f"POST /api/x{k}", 40, 40, fails=8, method="POST")
        write(os.path.join(f, "spans.csv"), BASE, rows)
        self.assertEqual(self.build(f, "--config", self.kpi("small.json")), 0)
        _, md, _ = self.facts()
        self.assertIn("Each operation with at least", md)
        self.assertIn("Smaller operations fail more", md)
        self.assertNotIn("— under the review's 1 % bar", md)

    def test_slow_wording_names_agreed_operations_and_targets(self):
        f = self.folder("slow")
        rows = ops("search-api", "GET /api/search", 600, 3500) + ops("search-api", "GET /api/suggest", 250, 2500) + ops("search-api", "GET /api/item", 150, 300)
        rows += ops("track-api", "GET /v1/track", 700, 222) + ops("track-api", "GET /a", 700, 20) + ops("track-api", "GET /b", 700, 20)
        write(os.path.join(f, "spans.csv"), BASE, rows)
        cfg = self.config("slow.json", operations=[{"key": "s", "label": "searches", "service": "^search-api$", "match": "^GET /api/search$", "slo_p95_s": 4.0},
                                                   {"key": "t", "label": "parcel lookups", "service": "^track-api$", "match": "^GET /v1/track$", "slo_p95_s": 0.15}])
        self.assertEqual(self.build(f, "--config", cfg), 0)
        F, md, _ = self.facts()
        self.assertIn("leaving out `GET /api/search`", md)
        summ = [x["summary"]["finding"] for x in F["findings"] if x["detector"] == "slow_operations"]
        self.assertTrue(summ and "misses its agreed p95 target" in summ[0] and "150 ms" in summ[0], summ)

    def test_noise_min_failed_zero_does_not_crash(self):
        f = self.folder("zero")
        write(os.path.join(f, "spans.csv"), OPS, [[ts(i % 600), "worker", "job", "20 ms", "", "", "celery.run", "worker"] for i in range(200)])
        self.assertEqual(self.build(f, "--config", self.config("z.json", thresholds={"noise_min_failed": 0})), 0)

    def test_live_graph_selects_the_operation(self):
        self.assertEqual(recipes.resource_wildcard("POST /api/claims"), "post_/api/claims")
        self.assertEqual(recipes.resource_wildcard("GET /api/claims/?"), "get_/api/claims/")

    def test_short_ranges_read_in_hours_and_days(self):
        self.assertEqual(narrative.range_words(0.042), "about 1 hour")
        self.assertEqual(narrative.range_words(1.0), "about 1 day")
        self.assertEqual(narrative.range_words(7.2), "about 7 days")

    def test_http_client_pseudo_services_are_hosts(self):
        F = {"services": {"portal-http-client": {"role": "integration"}, "hibernate": {"role": "integration"}}}
        self.assertEqual(mermaid.dep_shape(F, "portal-http-client", {"type": "http"}), "host")
        self.assertEqual(mermaid.dep_shape(F, "orders-db", {"type": "sql"}), "db")
        self.assertEqual(mermaid.dep_shape(F, "kafka", {"type": "queue"}), "queue")

    # ------------------------------------------------------------ month over month
    def test_relaxed_success_target_closes_within_target(self):
        def rows(base):
            return ops("shop-api", "POST /api/cart", 30000, 40, fails=210, base=base, method="POST") + ops("shop-api", "GET /x", 3000, 30, base=base)
        F, md, _ = self.two_months(rows(OCT0), rows(T0), BASE, cfg_oct=self.kpi("o.json", match="^POST /api/cart$", label="cart updates"),
                                   cfg_nov=self.kpi("n.json", match="^POST /api/cart$", label="cart updates", slo_success_pct=99.0))
        self.assertEqual({v for k, v in self.statuses(F).items() if k.startswith("failing_operations")}, {"within_target"})
        self.assertIn("closed — within its agreed success target", md)

    def test_removed_target_is_listed(self):
        def rows(base):
            return ops("shop-api", "GET /api/search", 1000, 3200, base=base) + ops("shop-api", "GET /a", 1000, 50, base=base) + ops("shop-api", "GET /b", 1000, 50, base=base)
        cfg_oct = self.config("o.json", operations=[{"key": "s", "label": "searches", "service": "^shop-api$", "match": "^GET /api/search$", "slo_p95_s": 4.0}])
        F, _, _ = self.two_months(rows(OCT0), rows(T0), BASE, cfg_oct=cfg_oct, cfg_nov=self.config("n.json"))
        self.assertIn(("slo_p95_s", 4.0, None), [(c["field"], c["then"], c["now"]) for c in F["mom"]["targets_changed"]])

    def test_failure_close_within_sampling_noise(self):
        oct_rows = ops("shop-api", "POST /api/cart", 445, 40, fails=6, base=OCT0, method="POST") + ops("shop-api", "GET /x", 1000, 30, base=OCT0)
        nov_rows = ops("shop-api", "POST /api/cart", 518, 40, fails=3, method="POST") + ops("shop-api", "GET /x", 1000, 30)
        F, md, _ = self.two_months(oct_rows, nov_rows, BASE)
        self.assertEqual({v for k, v in self.statuses(F).items() if k.startswith("failing_operations")}, {"within_noise"})
        self.assertIn("under the bar, within sampling noise", md)

    def test_allow_listed_host_is_no_comparable_change(self):
        def rows(base):
            out = []
            for i in range(1200):
                out.append([ts(i % 600, base), "shop-api", "GET /x", "90 ms", "GET", 200, "servlet.request", "web", f"t{i}", f"r{i}", "", ""])
                h = ("api.stripe.com", "hooks.slack.com", "api.sendgrid.com")[i % 3]
                out.append([ts(i % 600, base), "shop-api", "POST /v1", "40 ms", "POST", 200, "okhttp.request", "http", f"t{i}", f"c{i}", f"r{i}", h])
            return out
        cfg_nov = self.config("n.json", hosts={"allowed_egress": ["^api\\.stripe\\.com$"]})
        F, _, _ = self.two_months(rows(OCT0), rows(T0), TREE + ["peer.service"], cfg_nov=cfg_nov)
        row = [r for r in F["mom"]["findings"]["persisting"] if r["id"].startswith("egress_and_telemetry")]
        self.assertTrue(row)
        self.assertEqual(row[0]["direction"], "new")              # 'no comparable figure', never 'better'
        self.assertTrue(F["dependencies"]["api.stripe.com"]["allowed"])

    # ------------------------------------------------------------ validate
    def test_fenced_answer_is_checked_and_markers_are_warned(self):
        f = self.folder("val")
        write(os.path.join(f, "spans.csv"), BASE, ops("shop-api", "POST /api/cart", 3000, 40, fail_every=30, method="POST") + ops("shop-api", "GET /x", 3000, 30))
        self.assertEqual(self.build(f, "--diagrams", "prompt"), 0)
        F, md, path = self.facts()
        facts = os.path.join(os.path.dirname(path), "facts.json")
        code, out = self.run_cli(["validate", path, "--facts", facts])
        self.assertIn("a diagram marker is still in the notebook", out)
        sec = md[md.index("## 🔴 Finding 1") if "## 🔴 Finding 1" in md else md.index("## 🟠 Finding 1"):]
        sec = sec[:sec.index("\n---")]
        changed = sec.replace("3,000", "3,500", 1)
        fenced = os.path.join(self.tmp, "answer.md")
        with open(fenced, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("```markdown\n" + changed + "\n```\n")
        code, out = self.run_cli(["validate", fenced, "--facts", facts, "--text", "--against", path])
        self.assertNotEqual(code, 0, out)


if __name__ == "__main__":
    unittest.main()
