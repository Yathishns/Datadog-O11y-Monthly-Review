"""Tests for the eleventh review round: workers listed only through their health check, probe and scrape paths, scanner
patterns anchored at the path root, agreed success and p95 targets missed under the review's bars, slow shares and
severities on one basis across months, reclassified operations and hosts, the inferred services list on its own,
fan-out that is not N+1, several consumer groups, by-design wording, egress wording, top-level domains, config types,
missing nodes, alias joins, the landscape and dependencies prompts, and fences that must start a line.
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
from ddreview import compare, config, detectors  # noqa: E402
from ddreview.classify import host_class, resource_class  # noqa: E402

T0 = datetime(2026, 11, 19, 10, 0, tzinfo=timezone.utc)
OCT0 = datetime(2026, 10, 19, 10, 0, tzinfo=timezone.utc)
BASE = ["Date", "Service", "Resource", "Duration", "Method", "Status Code"]
OPS = BASE + ["Operation Name", "Type"]
TREE = OPS + ["Trace ID", "Span ID", "Parent ID"]
EP_HEADER = ["Resource", "Service", "Requests", "Error Rate", "P95 Latency", "Monitor Counts"]
SL_HEADER = "favorite\tType\tName\tHealth\tRequests\tError Rate\tp95 Latency (ns)\tMonitors\tSLOS\tEnvs\tLast Deploy\n"
SHORT = "round-eleven"


def write(path, header, rows):
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        w.writerows(rows)


def ts(i, base=T0):
    return (base + timedelta(seconds=i)).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def ops(service, resource, n, ms, fail_every=0, base=T0, method="GET"):
    return [[ts(i % 600, base), service, resource, f"{ms} ms", method, 500 if fail_every and i % fail_every == 0 else 200] for i in range(n)]


class Round11(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ddreview-r11t-")
        self.out = os.path.join(self.tmp, "out")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def folder(self, name):
        p = os.path.join(self.tmp, name)
        os.makedirs(p, exist_ok=True)
        return p

    def config(self, name, **extra):
        cfg = {"org": {"name": "Round Eleven", "short": SHORT}}
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

    def two_months(self, oct_rows, nov_rows, header, cfg_oct=None, cfg_nov=None, extra_files=None):
        fo, fn = self.folder("oct"), self.folder("nov")
        write(os.path.join(fo, "spans.csv"), header, oct_rows)
        write(os.path.join(fn, "spans.csv"), header, nov_rows)
        for name, text in (extra_files or {}).items():
            for f in (fo, fn):
                with open(os.path.join(f, name), "w", encoding="utf-8", newline="") as fh:
                    fh.write(text)
        self.assertEqual(self.build(fo, "--config", cfg_oct or self.config("plain.json"), month="October 2026"), 0)
        prev = os.path.join(self.out, SHORT, "2026-10", "facts.json")
        self.assertEqual(self.build(fn, "--config", cfg_nov or cfg_oct or self.config("plain.json"), "--previous", prev), 0)
        return self.facts()

    @staticmethod
    def statuses(F):
        fm = F["mom"]["findings"]
        return {r["id"]: r.get("status", k) for k in ("closed", "unmeasured", "persisting", "new", "first_measured", "demoted", "promoted") for r in fm.get(k, [])}

    # ------------------------------------------------------------ inputs and classes
    def test_worker_listed_only_by_its_health_check_counts_its_sample(self):
        f = self.folder("worker")
        rows = [[ts(i % 600), "order-api", "POST /api/orders", "90 ms", "POST", 200, "servlet.request", "web"] for i in range(600)]
        rows += [[ts(i % 600), "billing-worker", "orders", "80 ms", "", "", "kafka.consume", "queue"] for i in range(600)]
        rows += [[ts(i % 600), "billing-worker", "GET /actuator/health", "2 ms", "GET", 200, "servlet.request", "web"] for i in range(30)]
        write(os.path.join(f, "spans.csv"), OPS, rows)
        write(os.path.join(f, "endpoints.csv"), EP_HEADER, [["POST /api/orders", "order-api", 403200, 0.02, 0.24, "{}"], ["GET /actuator/health", "billing-worker", 60480, 0, 0.002, "{}"]])
        self.assertEqual(self.build(f, "--window-days", "7"), 0)
        F, md, _ = self.facts()
        self.assertEqual(F["services"]["billing-worker"]["requests"]["all"], 630)
        self.assertTrue([c for c in F["caveats"] if "only through health or probe endpoints" in c])
        self.assertNotIn("61 k req", md)

    def test_probe_and_scrape_paths_are_health_but_resource_health_is_business(self):
        cfg = config.load_config(None)
        for res in ("GET /readyz", "GET /livez", "GET /metrics", "GET /healthz/ready", "GET /health/startup", "GET /api/health/ready", "GET /orders-svc/health"):
            self.assertEqual(resource_class("svc", res, cfg)[0], "health", res)
        for res in ("GET /api/patients/{id}/health", "GET /api/devices/123/health", "GET /api/metrics/{id}"):
            self.assertEqual(resource_class("svc", res, cfg)[0], "business", res)

    def test_scanner_patterns_need_the_path_root_or_a_real_verb(self):
        cfg = config.load_config(None)
        for res in ("GET /api/policies/{id}/docs/{docId}", "POST /api/docs/upload", "GET /v1/examples/{id}", "GET /api/console/settings", "GET /api/manager/status",
                    "tracking.tasks.send_reminder", "TrackShipmentJob", "trace_export", "debug_dump"):
            self.assertEqual(resource_class("svc", res, cfg)[0], "business", res)
        for res in ("GET /manager/html", "GET /docs/", "GET /examples/servlets/index.html", "TRACE /", "PROPFIND /", "GET /phpmyadmin/index.php", "GET /.aws/credentials",
                    "GET /jmx-console/", "GET /.env.production"):
            self.assertEqual(resource_class("svc", res, cfg)[0], "scanner", res)

    def test_top_level_domain_is_not_an_environment_marker(self):
        cfg = config.load_config(None)
        for host in ("image-resizer.acme.workers.dev", "api.myapp.fly.dev", "api.payments.com.qa"):
            self.assertNotEqual(host_class(host, cfg), "nonprod", host)
        self.assertEqual(host_class("api.dev.acme.com", cfg), "nonprod")

    def test_hosts_production_must_be_a_list(self):
        p = self.config("bad.json", hosts={"production": "pricing.partner.com"})
        with self.assertRaises(config.ConfigError):
            config.load_config(p)

    def test_inferred_list_alone_unlocks_dependencies(self):
        f = self.folder("inf")
        write(os.path.join(f, "spans.csv"), BASE, ops("orders-api", "POST /orders", 2000, 80, method="POST"))
        with open(os.path.join(f, "inferred.tsv"), "w", encoding="utf-8", newline="") as fh:
            fh.write("favorite\tType\tName\tIntg\tRequests\tError Rate\tp95 Latency (ns)\n\thttp\tlegacy-erp.corp.internal\t\t250000\t1.0\t90000000\n")
        self.assertEqual(self.build(f), 0)
        F, md, _ = self.facts()
        self.assertNotEqual(F["coverage"]["capabilities"]["dependencies"], "none")
        self.assertTrue([x for x in F["findings"] if x["detector"] == "failing_dependencies"])
        self.assertNotIn("or export the inferred services list", md)

    def test_fan_out_to_several_hosts_is_not_n_plus_one(self):
        f = self.folder("bff")
        rows = []
        for i in range(4000):
            rows.append([ts(i % 600), "web-bff", "GET /home", "120 ms", "GET", 200, "express.request", "web", f"t{i}", f"r{i}", "", ""])
            if i >= 2500:
                continue                      # the rest of the requests call nothing: 3.75 calls per request, under the bar
            for k, h in enumerate(("profile", "cart", "offers", "stock", "reviews", "pricing")):
                rows.append([ts(i % 600), "web-bff", "GET", "15 ms", "GET", 200, "http.request", "http", f"t{i}", f"c{i}-{k}", f"r{i}", f"{h}.internal.example.com"])
        write(os.path.join(f, "spans.csv"), TREE + ["peer.service"], rows)
        self.assertEqual(self.build(f), 0)
        F, _, _ = self.facts()
        self.assertFalse(F["anatomy"]["web-bff"]["repeated_children"])
        self.assertFalse([x for x in F["findings"] if x["detector"] == "chatty_requests"])

    def test_two_consumer_groups_each_get_an_edge(self):
        f = self.folder("fan")
        rows = []
        for i in range(400):
            rows.append([ts(i % 600), "order-api", "POST /orders", "90 ms", "POST", 200, "servlet.request", "web", f"t{i}", f"r{i}", ""])
            rows.append([ts(i % 600), "kafka", "Produce Topic orders", "3 ms", "", "", "kafka.produce", "queue", f"t{i}", f"p{i}", f"r{i}"])
            rows.append([ts(i % 600), "fulfil-worker", "Consume Topic orders", "200 ms", "", "", "kafka.consume", "queue", f"t{i}", f"a{i}", f"p{i}"])
            rows.append([ts(i % 600), "notify-worker", "Consume Topic orders", "40 ms", "", "", "kafka.consume", "queue", f"t{i}", f"b{i}", f"p{i}"])
        write(os.path.join(f, "spans.csv"), TREE, rows)
        self.assertEqual(self.build(f), 0)
        F, _, _ = self.facts()
        targets = {e["to"] for e in F["edges"] if e["from"] == "order-api"}
        self.assertTrue({"fulfil-worker", "notify-worker"} <= targets, targets)

    # ------------------------------------------------------------ agreed targets
    def test_missed_success_target_under_the_bar_is_a_finding(self):
        f = self.folder("succ")
        write(os.path.join(f, "spans.csv"), BASE, ops("accounts-api", "POST /api/login", 4000, 60, fail_every=333, method="POST") + ops("accounts-api", "GET /api/me", 4000, 30))
        cfg = self.config("succ.json", operations=[{"key": "login", "label": "sign-ins", "service": "^accounts-api$", "match": "^POST /api/login$", "critical": True, "slo_success_pct": 99.95}])
        self.assertEqual(self.build(f, "--config", cfg), 0)
        F, md, _ = self.facts()
        fail = [x for x in F["findings"] if x["detector"] == "failing_operations"]
        self.assertTrue(fail)
        self.assertIn("Sign-ins miss the agreed 99.95 % success target", fail[0]["title"])
        self.assertEqual(fail[0]["severity"], "amber")

    def test_missed_p95_target_under_the_bar_is_slow(self):
        f = self.folder("p95")
        rows = ops("accounts-api", "POST /api/login", 700, 1400, method="POST") + ops("accounts-api", "GET /a", 700, 50) + ops("accounts-api", "GET /b", 700, 60)
        write(os.path.join(f, "spans.csv"), BASE, rows)
        cfg = self.config("p95.json", operations=[{"key": "login", "label": "sign-ins", "service": "^accounts-api$", "match": "^POST /api/login$", "slo_p95_s": 1.0}])
        self.assertEqual(self.build(f, "--config", cfg), 0)
        F, md, _ = self.facts()
        slow = [x for x in F["findings"] if x["detector"] == "slow_operations"]
        self.assertTrue(slow)
        self.assertIn("misses its agreed p95 target", slow[0]["title"])
        self.assertIn("The agreed p95 target for sign-ins is 1.00 s", md)

    def test_achieved_rate_never_rounds_onto_a_missed_target(self):
        self.assertEqual(detectors.achieved_text(99.478, 99.5), "99.48 %")
        self.assertEqual(detectors.achieved_text(98.96, 99.0), "98.96 %")

    def test_slow_operations_share_leaves_agreed_operations_out(self):
        f = self.folder("share")
        rows = ops("report-api", "GET /reports/list", 600, 200) + ops("report-api", "GET /reports/annual", 300, 4000) + ops("report-api", "GET /reports/monthly", 150, 3500)
        rows += ops("report-api", "POST /reports/export", 30, 800, method="POST")
        write(os.path.join(f, "spans.csv"), BASE, rows)
        cfg = self.config("share.json", operations=[{"key": "annual", "label": "annual reports", "service": "^report-api$", "match": "^GET /reports/annual$", "slo_p95_s": 5.0}])
        self.assertEqual(self.build(f, "--config", cfg), 0)
        _, md, _ = self.facts()
        self.assertNotIn("sits on slow operations", md)
        self.assertNotIn("`GET /reports/annual` takes", md)

    # ------------------------------------------------------------ month over month
    def test_slow_share_trend_and_severity_on_one_basis(self):
        def rows(base):
            return (ops("shop-api", "GET /api/search", 1000, 5100, base=base) + ops("shop-api", "GET /api/recs", 700, 3400, base=base)
                    + ops("shop-api", "GET /a", 500, 100, base=base) + ops("shop-api", "GET /b", 500, 100, base=base))
        cfg_nov = self.config("nov.json", operations=[{"key": "search", "label": "searches", "service": "^shop-api$", "match": "^GET /api/search$", "slo_p95_s": 6.0}])
        F, md, p = self.two_months(rows(OCT0), rows(T0), BASE, cfg_nov=cfg_nov)
        row = [r for r in F["mom"]["findings"]["persisting"] if r["id"].startswith("slow_hot_paths")]
        self.assertTrue(row)
        self.assertEqual(row[0]["direction"], "flat")
        self.assertIsNone(row[0]["then_severity"])
        with open(os.path.join(p, "prompts", "22-month-over-month.md"), encoding="utf-8") as fh:
            self.assertIn("The agreed targets changed since last month", fh.read())

    def test_operation_moved_to_by_design_does_not_move_the_service_rate(self):
        def rows(base):
            return ops("clinic-web", "POST /api/fax", 300, 40, fail_every=1, base=base, method="POST") + ops("clinic-web", "GET /api/x", 3000, 40, fail_every=100, base=base)
        cfg_nov = self.config("bd.json", noise={"by_design": [{"service": "^clinic-web$", "match": "^POST /api/fax$", "why": "the fax gateway was retired"}]})
        F, md, _ = self.two_months(rows(OCT0), rows(T0), BASE, cfg_nov=cfg_nov)
        self.assertFalse([r for r in F["mom"]["moved"] if "clinic-web" in r["where"] and r["metric"] == "failure rate"])
        self.assertNotIn("confirm each one with the team", md)
        self.assertNotIn("Fix or remove", md)

    def test_host_declared_production_reads_reclassified(self):
        def rows(base):
            out = []
            for i in range(1000):
                out.append([ts(i % 600, base), "web-bff", "GET /price", "90 ms", "GET", 200, "servlet.request", "web", f"t{i}", f"r{i}", "", ""])
                out.append([ts(i % 600, base), "web-bff", "GET /v1/price", "40 ms", "GET", 200, "okhttp.request", "http", f"t{i}", f"c{i}", f"r{i}", "pricing.partner-uat.com"])
            return out
        cfg_nov = self.config("prod.json", hosts={"production": ["^pricing\\.partner-uat\\.com$"]})
        F, md, _ = self.two_months(rows(OCT0), rows(T0), TREE + ["peer.service"], cfg_nov=cfg_nov)
        st = {k: v for k, v in self.statuses(F).items() if k.startswith("nonprod_from_prod")}
        self.assertEqual(set(st.values()), {"host_reclassified"})
        self.assertIn("reclassified by the hosts config", md)

    def test_missing_node_is_not_checked(self):
        F = {"coverage": {"detectors_skipped": [], "failure_signal": True}, "meta": {}, "services": {"web-01": {}}, "resources": {}, "dependencies": {}, "mom": {}}
        prev = {"node_groups": {"web": {"members": {"web-01": 900, "web-02": 400}}}}
        p = {"detector": "node_imbalance", "subject": {"service": "web-02", "key": "web", "group": "web"}, "metrics": {"primary": {"unit": "pct"}}}
        self.assertEqual(compare._unmeasured_reason(F, prev, p, True), "web-02 is not in this month's exports")

    def test_alias_join_adds_the_failure_base(self):
        a = {"requests": {"all": 1800, "business": 1800}, "failed": {"all": 46, "business": 46}, "err_base": {"all": 1800, "business": 1800}}
        b = {"requests": {"all": 1200, "business": 1200}, "failed": {"all": 7, "business": 7}, "err_base": {"all": 1200, "business": 1200}}
        out = compare._merge_prev("services", a, b)
        self.assertEqual(out["err_base"]["business"], 3000)

    # ------------------------------------------------------------ wording, prompts and fences
    def test_drafted_by_design_reason_is_a_todo(self):
        f = self.folder("draft")
        write(os.path.join(f, "spans.csv"), BASE, ops("clinic-web", "POST /api/fax", 300, 40, fail_every=1, method="POST") + ops("clinic-web", "GET /api/x", 3000, 40))
        cfg = self.config("todo.json", noise={"by_design": [{"service": "^clinic-web$", "match": "^POST /api/fax$", "why": "_todo: why this fails by design (300 calls)"}]})
        self.assertEqual(self.build(f, "--config", cfg), 0)
        _, md, _ = self.facts()
        self.assertNotIn("_todo", md)
        self.assertIn("`^POST /api/fax$`", md)

    def test_landscape_prompt_quotes_the_services_list_rate(self):
        f = self.folder("sl")
        write(os.path.join(f, "spans.csv"), OPS, [[ts(i % 600), "billing-worker", "charge", "200 ms", "", "", "celery.run", "worker"] for i in range(150)])
        with open(os.path.join(f, "services.tsv"), "w", encoding="utf-8", newline="") as fh:
            fh.write(SL_HEADER + "\tworker\tbilling-worker\t\t1000000\t0.04\t300000000\t\t\tprod\t\n")
        cfg = self.config("sl.json", domains=[{"key": "billing", "label": "Billing", "services": ["^billing-"]}])
        self.assertEqual(self.build(f, "--config", cfg), 0)
        _, _, p = self.facts()
        with open(os.path.join(p, "prompts", "01-diagram-landscape.md"), encoding="utf-8") as fh:
            prompt = fh.read()
        self.assertIn("4.00 % ◇", prompt)
        self.assertIn("◇ marks a failure rate from the Services list", prompt)

    def test_dependencies_prompt_names_each_marker(self):
        f = self.folder("stg")
        rows = []
        for i in range(1000):
            rows.append([ts(i % 600), "checkout-api", "POST /checkout", "90 ms", "POST", 200, "servlet.request", "web", f"t{i}", f"r{i}", "", ""])
            rows.append([ts(i % 600), "checkout-api", "POST /ledger", "40 ms", "POST", 200, "okhttp.request", "http", f"t{i}", f"c{i}", f"r{i}", "ledger.staging.acme.internal"])
        write(os.path.join(f, "spans.csv"), TREE + ["peer.service"], rows)
        self.assertEqual(self.build(f), 0)
        _, _, p = self.facts()
        with open(os.path.join(p, "prompts", "03-diagram-dependencies.md"), encoding="utf-8") as fh:
            prompt = fh.read()
        self.assertIn("nonprod (staging)", prompt)
        self.assertNotIn("`production → UAT` for non-production hosts", prompt)

    def test_fence_must_start_a_line(self):
        from ddreview import validate
        errs, _ = validate.check_markdown("# T\n\nsome text ```mermaid\nflowchart LR\n  A --> B\n```\n")
        self.assertTrue([e for e in errs if "does not start a line" in e])


if __name__ == "__main__":
    unittest.main()
