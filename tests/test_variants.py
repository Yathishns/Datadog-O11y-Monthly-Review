"""Export-variant and edge-case tests: the shapes the adversarial review found. Each case writes a tiny export set into a
temp folder and runs the real CLI. python -m unittest discover -s tests"""
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
from ddreview import readers  # noqa: E402

T0 = datetime(2026, 11, 19, 10, 0, tzinfo=timezone.utc)


def write_csv(path, header, rows, delimiter=",", bom=False, encoding="utf-8"):
    with open(path, "w", newline="", encoding=encoding) as fh:
        if bom:
            fh.write("﻿")
        w = csv.writer(fh, delimiter=delimiter)
        w.writerow(header)
        w.writerows(rows)


def spans(n=600, failing_every=10, dur=lambda i: f"{50 + (i % 7) * 10} ms", status=lambda i: 500 if i % 10 == 0 else 200, extra_cols=(), extra=lambda i: ()):
    header = ["Date", "Service", "Resource", "Duration", "Method", "Status Code"] + list(extra_cols)
    rows = []
    for i in range(n):
        ts = (T0 + timedelta(seconds=i)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        rows.append([ts, "orders-api", "/api/orders" if i % 3 else "/api/orders/{id}", dur(i), "POST" if i % 3 else "GET", status(i)] + list(extra(i)))
    return header, rows


class Variants(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ddreview-var-")
        self.out = os.path.join(self.tmp, "out")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def folder(self, name):
        p = os.path.join(self.tmp, name)
        os.makedirs(p, exist_ok=True)
        return p

    def build(self, folder, *extra):
        return cli.main(["build", folder, "--out", self.out, "--org", "Variant Org", "--month", "November 2026", "--previous", "none", "--force"] + list(extra))

    def notebook(self):
        p = os.path.join(self.out, "variant-org", "2026-11")
        name = next(f for f in os.listdir(p) if f.endswith(".md") and "review" in f)
        return open(os.path.join(p, name), encoding="utf-8").read(), json.load(open(os.path.join(p, "facts.json"), encoding="utf-8")), name

    def test_header_only_spans_file_exits_2(self):
        f = self.folder("empty")
        write_csv(os.path.join(f, "spans.csv"), ["Date", "Service", "Resource", "Duration", "Method", "Status Code"], [])
        self.assertEqual(self.build(f), 2)

    def test_bare_nanosecond_durations_are_read_as_ns(self):
        f = self.folder("ns")
        h, rows = spans(dur=lambda i: str((50 + (i % 7) * 10) * 1_000_000))     # 50-110 ms in ns
        write_csv(os.path.join(f, "spans.csv"), h, rows)
        self.assertEqual(self.build(f), 0)
        md, facts, name = self.notebook()
        self.assertFalse(name.endswith(".INVALID.md"))
        p95 = facts["services"]["orders-api"]["p95_s"]
        self.assertTrue(0.05 <= p95 <= 0.2, p95)
        self.assertIn("ns", json.dumps(facts["meta"]["inputs"][0]["units"]))

    def test_status_column_with_ok_error_words(self):
        f = self.folder("status")
        h, rows = spans(extra_cols=("Status",), extra=lambda i: ("error" if i % 10 == 0 else "ok",), status=lambda i: "")
        h[5] = "Http Code"                                                       # no numeric status column at all
        write_csv(os.path.join(f, "spans.csv"), h, rows)
        self.assertEqual(self.build(f), 0)
        md, facts, _ = self.notebook()
        self.assertTrue(facts["coverage"]["columns"]["error"])
        self.assertGreater(facts["services"]["orders-api"]["failed"]["all"], 0)

    def test_semicolon_delimiter_bom_and_at_headers(self):
        f = self.folder("semi")
        h, rows = spans(extra_cols=("trace_id", "span_id", "parent_id", "operation_name"), extra=lambda i: (f"t{i}", f"s{i}", "", "servlet.request"))
        h = ["Date", "service_name", "resource_name", "@duration", "http_method", "@http.status_code", "trace_id", "span_id", "parent_id", "operation_name"]
        write_csv(os.path.join(f, "spans.csv"), h, rows, delimiter=";", bom=True)
        self.assertEqual(self.build(f), 0)
        md, facts, _ = self.notebook()
        self.assertEqual(facts["services"]["orders-api"]["operation"], "servlet.request")
        self.assertTrue(facts["coverage"]["columns"]["status"])

    def test_two_identical_span_files_count_once(self):
        f = self.folder("dup")
        h, rows = spans(extra_cols=("Span ID",), extra=lambda i: (f"s{i}",))
        write_csv(os.path.join(f, "spans-a.csv"), h, rows)
        write_csv(os.path.join(f, "spans-b.csv"), h, rows)
        self.assertEqual(self.build(f), 0)
        md, facts, _ = self.notebook()
        self.assertEqual(facts["services"]["orders-api"]["spans"]["rows"], len(rows))

    def test_overlapping_exports_dedupe_by_span_id(self):
        f = self.folder("overlap")
        h, rows = spans(extra_cols=("Span ID",), extra=lambda i: (f"s{i}",))
        write_csv(os.path.join(f, "spans-a.csv"), h, rows[:400])
        write_csv(os.path.join(f, "spans-b.csv"), h, rows[200:])
        self.assertEqual(self.build(f), 0)
        md, facts, _ = self.notebook()
        self.assertEqual(facts["services"]["orders-api"]["spans"]["rows"], len(rows))

    def test_no_timestamp_column_needs_as_of(self):
        f = self.folder("nodate")
        h, rows = spans(extra_cols=("Trace ID",), extra=lambda i: (f"t{i}",))
        h2 = h[1:]                                   # Service, Resource, Duration, Method, Status Code, Trace ID — recognisable, but undated
        rows2 = [r[1:] for r in rows]
        write_csv(os.path.join(f, "spans.csv"), h2, rows2)
        self.assertEqual(cli.main(["build", f, "--out", self.out, "--org", "Variant Org", "--previous", "none", "--force"]), 2)
        self.assertEqual(cli.main(["build", f, "--out", self.out, "--org", "Variant Org", "--previous", "none", "--force", "--as-of", "2026-11-19"]), 0)

    def test_cp1252_resave_keeps_microseconds(self):
        f = self.folder("cp1252")
        h, rows = spans(dur=lambda i: f"{400 + i} µs")
        write_csv(os.path.join(f, "spans.csv"), h, rows, encoding="cp1252")
        self.assertEqual(self.build(f), 0)
        md, facts, _ = self.notebook()
        self.assertEqual(facts["meta"]["inputs"][0]["encoding"], "cp1252")
        self.assertEqual(facts["meta"]["inputs"][0]["skipped"]["unreadable_duration"], 0)

    def test_host_named_service_is_a_dependency_not_an_entry(self):
        f = self.folder("hostlike")
        h, rows = spans()
        rows += [[r[0], "api.stripe.com", "POST /v1/charges", "120 ms", "POST", 200] for r in rows[:100]]
        write_csv(os.path.join(f, "spans.csv"), h, rows)
        self.assertEqual(self.build(f), 0)
        md, facts, _ = self.notebook()
        self.assertNotEqual(facts["services"]["api.stripe.com"]["role"], "entry")

    def test_empty_service_rows_are_skipped_with_a_warning(self):
        f = self.folder("nosvc")
        h, rows = spans()
        for r in rows[:50]:
            r[1] = ""
        write_csv(os.path.join(f, "spans.csv"), h, rows)
        self.assertEqual(self.build(f), 0)
        md, facts, _ = self.notebook()
        self.assertEqual(facts["meta"]["inputs"][0]["skipped"]["empty_service"], 50)
        self.assertNotIn("| `` |", md)
        self.assertNotIn("on `` ", md)

    def test_service_named_end_and_unicode_survive_mermaid_and_validation(self):
        f = self.folder("names")
        h, rows = spans()
        for r in rows[::2]:
            r[1] = "end"
        for r in rows[1::4]:
            r[1] = "café-api"
        write_csv(os.path.join(f, "spans.csv"), h, rows)
        self.assertEqual(self.build(f), 0)
        md, facts, name = self.notebook()
        self.assertFalse(name.endswith(".INVALID.md"))
        self.assertIn("```mermaid", md)

    def test_rename_across_months_is_persisting_not_closed(self):
        prev = self.folder("prev")
        cur = self.folder("cur")
        h, rows = spans()
        write_csv(os.path.join(prev, "spans.csv"), h, rows)
        rows2 = [[(T0 + timedelta(days=30, seconds=i)).strftime("%Y-%m-%dT%H:%M:%S.000Z"), "orders-v2", r[2], r[3], r[4], r[5]] for i, r in enumerate(rows)]
        write_csv(os.path.join(cur, "spans.csv"), h, rows2)
        cfgp = os.path.join(self.tmp, "org.json")
        json.dump({"org": {"name": "Variant Org", "short": "variant-org"}, "services": {"aliases": {"orders-api": "orders-v2"}}}, open(cfgp, "w", encoding="utf-8"))
        self.assertEqual(cli.main(["build", prev, "--out", self.out, "--config", cfgp, "--month", "October 2026", "--previous", "none"]), 0)
        self.assertEqual(cli.main(["build", cur, "--out", self.out, "--config", cfgp, "--month", "November 2026"]), 0)
        p = os.path.join(self.out, "variant-org", "2026-11", "facts.json")
        facts = json.load(open(p, encoding="utf-8"))
        self.assertTrue(facts["mom"]["findings"]["persisting"], facts["mom"]["findings"])
        self.assertFalse([c for c in facts["mom"]["findings"]["closed"] if c.get("status") == "closed"])

    def test_unknown_config_key_inside_domain_is_an_error(self):
        f = self.folder("cfgkeys")
        h, rows = spans()
        write_csv(os.path.join(f, "spans.csv"), h, rows)
        cfgp = os.path.join(self.tmp, "bad.json")
        json.dump({"org": {"name": "X"}, "domains": [{"key": "a", "label": "A", "serivces": ["orders"]}]}, open(cfgp, "w", encoding="utf-8"))
        with self.assertRaises(SystemExit) as ctx:
            cli.main(["build", f, "--out", self.out, "--config", cfgp, "--month", "November 2026"])
        self.assertEqual(ctx.exception.code, 3)

    def test_name_column_never_overrides_resource(self):
        normalised = [readers.norm_header(h) for h in ["Date", "Name", "Resource", "Service", "Duration"]]
        cmap = readers.column_map(normalised, "spans")
        self.assertEqual(cmap["resource"], 2)


if __name__ == "__main__":
    unittest.main()
