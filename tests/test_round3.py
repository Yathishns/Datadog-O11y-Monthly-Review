"""Tests for the third review round: European number formats, root spans with Parent ID 0, dotted .NET service names,
client operations, unit words in headers, integration names, span.kind, re-exports with ids, per-request anatomy, outages
that stay visible, narrower health checks, per-service monitor knowledge, Services-list-only services, validate on
fragments, config type checks, month over month without a failure signal, diagram budgets and prompt names.
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
from ddreview import mermaid, prompts  # noqa: E402
from ddreview.narrative import pct  # noqa: E402

T0 = datetime(2026, 11, 19, 10, 0, tzinfo=timezone.utc)
ACME = os.path.join(ROOT, "examples", "acme-retail")


def write(path, header, rows, delimiter=","):
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, delimiter=delimiter)
        w.writerow(header)
        w.writerows(rows)


def ts(i):
    return (T0 + timedelta(seconds=i)).strftime("%Y-%m-%dT%H:%M:%S.000Z")


class Round3(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ddreview-r3t-")
        self.out = os.path.join(self.tmp, "out")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def folder(self, name):
        p = os.path.join(self.tmp, name)
        os.makedirs(p, exist_ok=True)
        return p

    def build(self, folder, *extra, month="November 2026", org=True):
        args = ["build", folder, "--out", self.out, "--month", month, "--force"] + (["--org", "Round Three"] if org else []) + list(extra)
        if "--previous" not in extra:
            args += ["--previous", "none"]
        try:
            return cli.main(args)
        except SystemExit as e:
            return e.code

    def facts(self, short="round-three", key="2026-11"):
        p = os.path.join(self.out, short, key)
        with open(os.path.join(p, "facts.json"), encoding="utf-8") as fh:
            F = json.load(fh)
        name = [x for x in os.listdir(p) if x.endswith(".md") and "review" in x][0]
        with open(os.path.join(p, name), encoding="utf-8") as fh:
            return F, fh.read()

    # ------------------------------------------------------------ inputs
    def test_semicolon_decimal_comma_endpoints_list(self):
        f = self.folder("eu")
        write(os.path.join(f, "spans.csv"), ["Date", "Service", "Resource", "Duration", "Method", "Status Code"],
              [[ts(i), "shop-api", "POST /api/checkout", "80 ms", "POST", 500 if i % 20 == 0 else 200] for i in range(300)])
        write(os.path.join(f, "endpoints.csv"), ["Name", "Service", "Request Count", "Error Rate", "P95 Duration"],
              [["POST /api/checkout", "shop-api", "409143", "0,045", "1,671924"], ["GET /scan", "shop-api", "12", "1", "0,012"]], delimiter=";")
        self.assertEqual(self.build(f, "--window-days", "7"), 0)
        F, _ = self.facts()
        row = next(r for r in F["resources"]["shop-api"] if r["short"] == "POST /api/checkout")
        self.assertAlmostEqual(row["err"]["pct"], 4.5, places=1)
        self.assertAlmostEqual(row["p95_s"], 1.672, places=2)

    def test_parent_id_zero_is_a_root_span(self):
        f = self.folder("p0")
        rows = []
        for i in range(300):
            rows.append([ts(i), "orders-api", "GET /api/orders", "120 ms", "GET", 500 if i % 20 == 0 else 200, f"t{i}", f"r{i}", "0"])
            rows.append([ts(i), "postgres", "SELECT 1", "5 ms", "", "", f"t{i}", f"c{i}", f"r{i}"])
        write(os.path.join(f, "spans.csv"), ["Date", "Service", "Resource", "Duration", "Method", "Status Code", "Trace ID", "Span ID", "Parent ID"], rows)
        self.assertEqual(self.build(f), 0)
        F, _ = self.facts()
        self.assertEqual(F["services"]["orders-api"]["role"], "entry")
        self.assertEqual(F["services"]["orders-api"]["requests"]["all"], 300)

    def test_dotted_dotnet_service_on_the_endpoints_list_is_an_entry(self):
        f = self.folder("dotnet")
        write(os.path.join(f, "spans.csv"), ["Date", "Service", "Resource", "Duration", "Method", "Status Code"],
              [[ts(i), "northbank.onlinebanking.web", "GET Account/Summary", "90 ms", "GET", 200] for i in range(200)])
        write(os.path.join(f, "endpoints.csv"), ["Name", "Service", "Request Count", "Error Rate", "P95 Duration"],
              [["GET Account/Summary", "northbank.onlinebanking.web", "1795964", "0.0015", "1.2"]])
        self.assertEqual(self.build(f, "--window-days", "7"), 0)
        F, _ = self.facts()
        self.assertEqual(F["services"]["northbank.onlinebanking.web"]["role"], "entry")
        self.assertEqual(F["totals"]["requests"], 1795964)

    def test_client_operation_beside_a_server_operation_is_outbound(self):
        f = self.folder("opc")
        rows = []
        for i in range(300):
            rows.append([ts(i), "orders-api", "POST /api/orders", "150 ms", "POST", 200, "express.request"])
            rows.append([ts(i), "orders-api", "POST /v1/charges", "60 ms", "POST", 502 if i % 2 else 200, "http.request"])
        write(os.path.join(f, "spans.csv"), ["Date", "Service", "Resource", "Duration", "Method", "Status Code", "Operation Name"], rows)
        self.assertEqual(self.build(f), 0)
        F, md = self.facts()
        self.assertEqual(F["services"]["orders-api"]["requests"]["all"], 300)
        self.assertTrue(any("outbound calls" in c for c in F["caveats"]))

    def test_unit_words_in_the_duration_header(self):
        f = self.folder("sec")
        write(os.path.join(f, "spans.csv"), ["Date", "Service", "Resource", "Duration (sec)", "Method", "Status Code"],
              [[ts(i), "orders-api", "GET /api/orders", f"{0.08 + (i % 4) * 0.01:.3f}", "GET", 200] for i in range(300)])
        self.assertEqual(self.build(f), 0)
        F, _ = self.facts()
        self.assertTrue(0.08 <= F["services"]["orders-api"]["p95_s"] <= 0.12)

    def test_integration_names_never_become_entry_services(self):
        f = self.folder("integ")
        rows = []
        for i in range(300):
            rows += [[ts(i), "online-banking-web", "GET /accounts", "100 ms", "GET", 200, f"t{i}", f"r{i}", ""],
                     [ts(i), "online-banking-web-sql-server", "SELECT * FROM Accounts WHERE Id = @p0", "4 ms", "", "", f"t{i}", f"a{i}", f"r{i}"],
                     [ts(i), "postgres.db", "SELECT 1", "3 ms", "", "", f"t{i}", f"b{i}", f"r{i}"],
                     [ts(i), "redis.client", "GET", "1 ms", "", "", f"t{i}", f"c{i}", f"r{i}"]]
        write(os.path.join(f, "spans.csv"), ["Date", "Service", "Resource", "Duration", "Method", "Status Code", "Trace ID", "Span ID", "Parent ID"], rows)
        self.assertEqual(self.build(f), 0)
        F, md = self.facts()
        self.assertEqual(F["totals"]["services_entry"], 1)
        self.assertEqual(F["totals"]["requests"], 300)
        self.assertNotIn("public internet", md)
        for host in ("postgres.db", "redis.client"):
            self.assertEqual(F["dependencies"][host]["class"], "internal")

    def test_span_kind_is_not_a_span_type(self):
        f = self.folder("kind")
        rows = []
        for i in range(300):
            rows.append([ts(i), "orders-api", "GET /api/orders", "100 ms", "GET", 200, f"t{i}", f"r{i}", "", "server"])
            for k in range(12):
                rows.append([ts(i), "postgres", "SELECT * FROM items WHERE order_id = ?", "2 ms", "", "", f"t{i}", f"c{i}-{k}", f"r{i}", "client"])
        write(os.path.join(f, "spans.csv"), ["Date", "Service", "Resource", "Duration", "Method", "Status Code", "Trace ID", "Span ID", "Parent ID", "span.kind"], rows)
        self.assertEqual(self.build(f), 0)
        F, _ = self.facts()
        self.assertIn("db", F["anatomy"]["orders-api"]["children_per_request"])
        self.assertNotIn("http", F["anatomy"]["orders-api"]["children_per_request"])

    def test_export_and_its_reexport_with_ids_count_once(self):
        f = self.folder("twice")
        base = [[ts(i), "orders-api", "GET /api/orders", "100 ms", "GET", 500 if i % 20 == 0 else 200] for i in range(300)]
        write(os.path.join(f, "a.csv"), ["Date", "Service", "Resource", "Duration", "Method", "Status Code"], base)
        write(os.path.join(f, "b.csv"), ["Date", "Service", "Resource", "Duration", "Method", "Status Code", "Trace ID", "Span ID"],
              [r + [f"t{i}", f"s{i}"] for i, r in enumerate(base)])
        self.assertEqual(self.build(f), 0)
        F, _ = self.facts()
        self.assertEqual(F["totals"]["requests"], 300)

    def test_anatomy_counts_per_request_not_per_trace(self):
        f = self.folder("fan")
        rows = []
        for t in range(30):
            rows.append([ts(t), "bff-api", "GET /page", "300 ms", "GET", 200, f"t{t}", f"root{t}", ""])
            for k in range(12):
                rows.append([ts(t), "inventory-api", "GET /stock", "20 ms", "GET", 200, f"t{t}", f"inv{t}-{k}", f"root{t}"])
                rows.append([ts(t), "postgres", "SELECT * FROM stock WHERE sku = ?", "2 ms", "", "", f"t{t}", f"db{t}-{k}", f"inv{t}-{k}"])
        write(os.path.join(f, "spans.csv"), ["Date", "Service", "Resource", "Duration", "Method", "Status Code", "Trace ID", "Span ID", "Parent ID"], rows)
        self.assertEqual(self.build(f), 0)
        F, _ = self.facts()
        a = F["anatomy"]["inventory-api"]
        self.assertAlmostEqual(a["children_per_request"]["db"]["value"], 1.0)
        self.assertEqual(a["repeated_children"], [])

    # ------------------------------------------------------------ notebook
    def test_pct_never_rounds_a_target_up_to_100(self):
        self.assertEqual(pct(99.95), "99.95 %")
        self.assertEqual(pct(99.5), "99.5 %")
        self.assertEqual(pct(100), "100 %")

    def test_an_operation_failing_on_every_call_stays_a_failure(self):
        f = self.folder("dead")
        rows = [[ts(i), "pay-api", "POST /pay/refund", "90 ms", "POST", 500] for i in range(200)]
        rows += [[ts(i), "pay-api", "POST /pay/authorise", "90 ms", "POST", 200] for i in range(400)]
        write(os.path.join(f, "spans.csv"), ["Date", "Service", "Resource", "Duration", "Method", "Status Code"], rows)
        self.assertEqual(self.build(f), 0)
        F, md = self.facts()
        self.assertTrue(any(fd["detector"] == "failing_operations" for fd in F["findings"]))
        self.assertIn("fails on every call", md)

    def test_a_business_path_ending_in_status_is_not_a_health_check(self):
        f = self.folder("status")
        rows = [[ts(i), "orders-api", "GET /api/orders/{id}/status", "60 ms", "GET", 500 if i % 10 == 0 else 200] for i in range(300)]
        rows += [[ts(i), "orders-api", "GET /healthz", "1 ms", "GET", 200] for i in range(100)]
        write(os.path.join(f, "spans.csv"), ["Date", "Service", "Resource", "Duration", "Method", "Status Code"], rows)
        self.assertEqual(self.build(f), 0)
        F, _ = self.facts()
        cls = {r["short"]: r["class"] for r in F["resources"]["orders-api"]}
        self.assertEqual(cls["GET /api/orders/{id}/status"], "business")
        self.assertEqual(cls["GET /healthz"], "health")

    def test_services_list_only_service_shows_its_services_list_figures(self):
        f = self.folder("slonly")
        for name in os.listdir(os.path.join(ACME, "exports")):
            shutil.copy(os.path.join(ACME, "exports", name), f)
        with open(os.path.join(f, "services.tsv"), "a", encoding="utf-8", newline="") as fh:
            fh.write("\tweb\tlegacy-portal\t\t86400\t0.25\t9100000000\t\t\tprod\t\n")
        self.assertEqual(self.build(f, "--config", os.path.join(ACME, "org.json"), org=False), 0)
        F, md = self.facts(short="acme-retail")
        s = F["services"]["legacy-portal"]
        self.assertEqual(s["requests"]["tier"], "services_list")
        self.assertAlmostEqual(s["err"]["business"]["pct"], 25.0, places=1)
        self.assertIn("¶", md)

    def test_sample_only_service_is_not_called_unmonitored(self):
        f = self.folder("mon")
        for name in os.listdir(os.path.join(ACME, "exports")):
            if name != "services.tsv":
                shutil.copy(os.path.join(ACME, "exports", name), f)
        with open(os.path.join(f, "spans-extra.csv"), "w", encoding="utf-8", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["Date", "Service", "Resource", "Duration", "Method", "Status Code"])
            for i in range(300):
                w.writerow([(datetime(2026, 11, 19, 2, 0, tzinfo=timezone.utc) + timedelta(seconds=i)).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                            "card-services", "POST Cards/Block", "80 ms", "POST", 500 if i % 10 == 0 else 200])
        self.assertEqual(self.build(f, "--config", os.path.join(ACME, "org.json"), org=False), 0)
        F, md = self.facts(short="acme-retail")
        self.assertFalse(F["services"]["card-services"]["monitors_known"])
        self.assertNotIn("No monitor watches `card-services`", md)

    # ------------------------------------------------------------ validate and config
    def test_validate_against_checks_heading_numbers_and_accepts_fragments(self):
        self.assertEqual(self.build(os.path.join(ACME, "exports"), "--config", os.path.join(ACME, "org.json"), org=False), 0)
        _, md = self.facts(short="acme-retail")
        p = os.path.join(self.out, "acme-retail", "2026-11")
        nb = os.path.join(p, [x for x in os.listdir(p) if x.endswith(".md") and "review" in x][0])
        start = md.index("## 🔴 Finding 2")
        end = md.index("## ", start + 5)
        frag = os.path.join(self.tmp, "frag.md")
        with open(frag, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(md[start:end])
        facts = os.path.join(p, "facts.json")
        self.assertEqual(cli.main(["validate", frag, "--facts", facts, "--text", "--against", nb]), 0)
        with open(frag, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(md[start:end].replace("33.0 %", "3.0 %", 1))
        self.assertEqual(cli.main(["validate", frag, "--facts", facts, "--text", "--against", nb]), 1)

    def config_exit(self, mutate, encoding="utf-8"):
        with open(os.path.join(ACME, "org.json"), encoding="utf-8") as fh:
            cfg = json.load(fh)
        mutate(cfg)
        path = os.path.join(self.tmp, "org.json")
        with open(path, "w", encoding=encoding) as fh:
            json.dump(cfg, fh, ensure_ascii=False)
        return self.build(os.path.join(ACME, "exports"), "--config", path, org=False)

    def test_config_type_mistakes_are_config_errors(self):
        def dup_key(c):
            c["operations"][1]["key"] = c["operations"][0]["key"]
        self.assertEqual(self.config_exit(dup_key), 3)
        self.assertEqual(self.config_exit(lambda c: c["report"].update(title_template="{org} {month:%B}")), 3)
        self.assertEqual(self.config_exit(lambda c: c.setdefault("columns", {}).update(aliases={"Duration": "durration"})), 3)
        self.assertEqual(self.config_exit(lambda c: c["org"].update(utc_offset="+25:00")), 3)
        self.assertEqual(self.config_exit(lambda c: c["org"].update(prepared_for=["a"])), 3)
        self.assertEqual(self.config_exit(lambda c: c["operations"][0].update(critical="false")), 3)
        self.assertEqual(self.config_exit(lambda c: c["thresholds"].update(max_findings=2) if "thresholds" in c else c.update(thresholds={"max_findings": 2})), 3)
        self.assertEqual(self.config_exit(lambda c: c["org"].update(short="../escape")), 3)

    def test_config_encodings(self):
        self.assertEqual(self.config_exit(lambda c: None, encoding="utf-16"), 0)
        self.assertEqual(self.config_exit(lambda c: None, encoding="utf-8-sig"), 0)
        def ansi(c):                     # what Notepad's 'ANSI' save produces: an em dash as byte 0x97, no emoji possible
            for d in c["domains"]:
                d.pop("icon", None)
            c["org"]["name"] = "Acme — Retail"
            c["report"] = {k: v for k, v in c.get("report", {}).items() if k != "delivered_alongside"}
        self.assertEqual(self.config_exit(ansi, encoding="cp1252"), 3)

    # ------------------------------------------------------------ month over month
    def test_failure_finding_is_not_closed_when_this_month_cannot_measure_it(self):
        prev = self.folder("oct")
        cur = self.folder("nov")
        hdr = ["Date", "Service", "Resource", "Duration", "Method", "Status Code"]
        write(os.path.join(prev, "spans.csv"), hdr, [[(datetime(2026, 10, 22, 10, 0, tzinfo=timezone.utc) + timedelta(seconds=i)).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                                                     "ship-api", "POST /v1/labels", "90 ms", "POST", 500 if i % 10 == 0 else 200] for i in range(400)])
        write(os.path.join(cur, "spans.csv"), hdr[:5], [[ts(i), "ship-api", "POST /v1/labels", "90 ms", "POST"] for i in range(400)])
        self.assertEqual(self.build(prev, month="October 2026"), 0)
        self.assertEqual(self.build(cur, "--previous", "auto"), 0)
        F, md = self.facts()
        self.assertTrue(F["mom"]["findings"]["unmeasured"])
        self.assertFalse([c for c in F["mom"]["findings"]["closed"] if c["status"] == "closed"])
        self.assertIn("not checked this month", md)
        self.assertIn("no status code or error column", md)

    # ------------------------------------------------------------ diagrams and prompts
    def test_landscape_says_what_it_leaves_out_and_lints(self):
        f = self.folder("many")
        names = [f"svc{chr(97 + i)}-api" for i in range(25)]
        rows = [[ts(i), names[i % 25], "GET /x", "50 ms", "GET", 200] for i in range(2500)]
        write(os.path.join(f, "spans.csv"), ["Date", "Service", "Resource", "Duration", "Method", "Status Code"], rows)
        cfgp = os.path.join(self.tmp, "one-domain.json")
        with open(cfgp, "w", encoding="utf-8") as fh:
            json.dump({"org": {"name": "Round Three", "short": "round-three"}, "domains": [{"key": "all", "label": "All", "tier": "staff", "services": [".*"]}]}, fh)
        self.assertEqual(self.build(f, "--config", cfgp, org=False), 0)
        F, md = self.facts()
        land = open(os.path.join(self.out, "round-three", "2026-11", "mermaid", "01-landscape.mmd"), encoding="utf-8").read()
        self.assertEqual(mermaid.lint(land), [])
        self.assertIn("more services", land)

    def test_inside_prompt_names_never_collide(self):
        self.assertNotEqual(prompts.inside_prompt_name("claims_api"), prompts.inside_prompt_name("claims-api"))
        self.assertEqual(prompts.inside_prompt_name("claims-api"), "04-diagram-inside-claims-api.md")


if __name__ == "__main__":
    unittest.main()
