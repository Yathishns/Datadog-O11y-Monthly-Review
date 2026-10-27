"""Smoke tests: python -m unittest discover -s tests   (run from the kit folder).

They build the three example orgs end to end, prove determinism (build twice, identical bytes), exercise the readers on
the value shapes Datadog writes, lint the Mermaid generators, and check that validate catches a changed number.
"""
import json
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from ddreview import __main__ as cli  # noqa: E402
from ddreview import mermaid, readers, validate  # noqa: E402
from ddreview.narrative import LEDGER  # noqa: E402

EXAMPLES = os.path.join(ROOT, "examples")


def ensure_samples():
    if not os.path.exists(os.path.join(EXAMPLES, "quickship", "exports")) or not os.path.exists(os.path.join(EXAMPLES, "acme-retail", "exports-previous")):
        import subprocess
        subprocess.check_call([sys.executable, "-I", os.path.join(EXAMPLES, "make_samples.py"), "--previous"])


class Readers(unittest.TestCase):
    def test_durations(self):
        self.assertAlmostEqual(readers.parse_duration_ms("1.23 s"), 1230.0)
        self.assertAlmostEqual(readers.parse_duration_ms("456 µs"), 0.456)
        self.assertAlmostEqual(readers.parse_duration_ms("45 μs"), 0.045)
        self.assertAlmostEqual(readers.parse_duration_ms("12 ns"), 1.2e-5)
        self.assertAlmostEqual(readers.parse_duration_ms("1m 2.5s"), 62500.0)
        self.assertAlmostEqual(readers.parse_duration_ms("1234", "ns"), 0.001234)
        self.assertIsNone(readers.parse_duration_ms("'-5 ms"))              # a negative duration is unreadable, never silently made positive
        self.assertAlmostEqual(readers.parse_duration_ms("1,23 ms"), 1.23)  # decimal comma from a spreadsheet re-save
        self.assertAlmostEqual(readers.parse_duration_ms("1,234 ms"), 1234.0)
        self.assertIsNone(readers.parse_duration_ms("n/a"))

    def test_timestamps(self):
        ts, assumed = readers.parse_ts("2026-11-19T02:00:00.089Z")
        self.assertEqual((ts.hour, ts.minute, assumed), (2, 0, False))
        ts, _ = readers.parse_ts("2026-11-19 10:00:03.123456789+08:00")
        self.assertEqual(ts.hour, 2)
        ts, assumed = readers.parse_ts("2026-11-19T10:00:03")
        self.assertTrue(assumed)
        ts, _ = readers.parse_ts("1763517600000")
        self.assertEqual(ts.year, 2025)

    def test_monitors_and_rates(self):
        self.assertEqual(readers.parse_monitors('{"non-synthetics": {"0": 2, "1": 1}}'), (3, {"0": 2, "1": 1}))
        self.assertEqual(readers.parse_monitors("3"), (3, {}))
        self.assertEqual(readers.parse_number("12.5%"), 12.5)
        self.assertEqual(readers.parse_int("1,234"), 1234)

    def test_kind_detection(self):
        self.assertEqual(readers.detect_kind([readers.norm_header(h) for h in ["Date", "Service", "Resource", "Duration", "Method", "Status Code"]]), "spans")
        self.assertEqual(readers.detect_kind([readers.norm_header(h) for h in ["Name", "Service", "Request Count", "Error Rate", "P95 Duration", "Monitor Counts"]]), "endpoints")
        self.assertEqual(readers.detect_kind([readers.norm_header(h) for h in ["favorite", "Type", "Name", "Health", "Requests", "Error Rate", "p95 Latency (ns)", "Monitors", "SLOS", "Envs"]]), "services")
        self.assertEqual(readers.detect_kind([readers.norm_header(h) for h in ["favorite", "Type", "Name", "Intg", "Requests", "Error Rate", "p95 Latency (ns)"]]), "inferred")
        self.assertEqual(readers.detect_kind([readers.norm_header(h) for h in ["account", "cloud_provider", "resource_type", "display_name", "env"]]), "catalog")
        self.assertEqual(readers.detect_kind([readers.norm_header(h) for h in ["@trace_id", "span_id", "service_name", "resource_name", "@duration", "@http.status_code"]]), "spans")
        self.assertEqual(readers.detect_kind(["foo", "bar"]), "unknown")


class MermaidLint(unittest.TestCase):
    def test_accepts_generated_shapes(self):
        good = 'flowchart TB\n  subgraph B_a["A"]\n    direction LR\n    S_x["x<br/>y"]\n  end\n  D_h(["h.example.com"])\n  S_x -->|"8 % fail"| D_h\n  style D_h fill:#ffe0e0,stroke:#c62828,color:#000\n'
        self.assertEqual(mermaid.lint(good), [])
        self.assertEqual(mermaid.lint('pie showData\n  title Requests\n  "A" : 10\n  "B" : 5\n'), [])

    def test_rejects_bad_ids_and_labels(self):
        self.assertTrue(mermaid.lint('flowchart LR\n  end["x"]\n'))
        self.assertTrue(mermaid.lint('flowchart LR\n  A["x"]\n  A --> B\n'))
        self.assertTrue(mermaid.lint('flowchart LR\n  subgraph S["t"]\n    A["x"]\n  end\n'))   # no direction
        self.assertTrue(mermaid.lint('pie showData\n  "A: b" : 10\n'))

    def test_label_escaping(self):
        self.assertEqual(mermaid.label('say "hi" | <b>'), '"say #quot;hi#quot; · #lt;b#gt;"')


class Builds(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        ensure_samples()
        cls.tmp = tempfile.mkdtemp(prefix="ddreview-test-")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def build(self, exports, month, out, config=None, extra=()):
        argv = ["build", exports, "--month", month, "--out", out] + (["--config", config] if config else []) + list(extra)
        return cli.main(argv)

    def test_spans_only_builds_valid(self):
        out = os.path.join(self.tmp, "q")
        self.assertEqual(self.build(os.path.join(EXAMPLES, "quickship", "exports"), "November 2026", out), 0)
        md = open(os.path.join(out, "quickship", "2026-11", "quickship-observability-review-2026-11.md"), encoding="utf-8").read()
        self.assertIn("10-minute sample", md)
        import re
        self.assertIsNone(re.search(r"\d[\d,.]*\s+(?:\w+\s+){0,2}(a day|per day|a week|per week)\b", md), "a sample figure was scaled to a day or week")
        self.assertIn("Not covered this month", md)

    def test_full_build_with_config_and_previous(self):
        out = os.path.join(self.tmp, "a")
        cfg = os.path.join(EXAMPLES, "acme-retail", "org.json")
        self.assertEqual(self.build(os.path.join(EXAMPLES, "acme-retail", "exports-previous"), "October 2026", out, cfg), 0)
        self.assertEqual(self.build(os.path.join(EXAMPLES, "acme-retail", "exports"), "November 2026", out, cfg), 0)
        md = open(os.path.join(out, "acme-retail", "2026-11", "acme-retail-observability-review-2026-11.md"), encoding="utf-8").read()
        self.assertIn("## 📈 Since last month", md)
        self.assertIn("Shoppers", md)
        facts = json.load(open(os.path.join(out, "acme-retail", "2026-11", "facts.json"), encoding="utf-8"))
        self.assertEqual(facts["window"]["aggregate"]["days"], 7)
        self.assertEqual(facts["window"]["aggregate"]["source"], "asserted")      # org.json says 7; the cadence alone fits 7, 14 and 28
        self.assertEqual(facts["window"]["aggregate"]["confidence"], "consistent_not_unique")   # the cadence fits 7, 14 and 28 days
        self.assertTrue(facts["window"]["per_day_allowed"])
        self.assertTrue(any(f["detector"] == "nonprod_from_prod" for f in facts["findings"]))
        self.assertTrue(facts["mom"]["findings"]["closed"] or facts["mom"]["findings"]["persisting"])

    def test_build_twice_is_byte_identical(self):
        out1, out2 = os.path.join(self.tmp, "d1"), os.path.join(self.tmp, "d2")
        src = os.path.join(EXAMPLES, "northbank", "exports")
        self.assertEqual(self.build(src, "November 2026", out1), 0)
        self.assertEqual(self.build(src, "November 2026", out2), 0)
        for name in ("northbank-observability-review-2026-11.md", "facts.json"):
            a = open(os.path.join(out1, "northbank", "2026-11", name), "rb").read()
            b = open(os.path.join(out2, "northbank", "2026-11", name), "rb").read()
            self.assertEqual(a, b, name)

    def test_validate_catches_a_changed_number(self):
        out = os.path.join(self.tmp, "v")
        self.assertEqual(self.build(os.path.join(EXAMPLES, "quickship", "exports"), "November 2026", out), 0)
        folder = os.path.join(out, "quickship", "2026-11")
        md_path = os.path.join(folder, "quickship-observability-review-2026-11.md")
        md = open(md_path, encoding="utf-8").read()
        tampered = md.replace("8.06 %", "3.06 %", 1)
        self.assertNotEqual(md, tampered)
        bad = os.path.join(folder, "tampered.md")
        open(bad, "w", encoding="utf-8", newline="\n").write(tampered)
        self.assertEqual(cli.main(["validate", md_path, "--facts", os.path.join(folder, "facts.json"), "--text"]), 0)
        self.assertEqual(cli.main(["validate", bad, "--facts", os.path.join(folder, "facts.json"), "--text"]), 1)

    def test_refuses_to_overwrite_a_month(self):
        out = os.path.join(self.tmp, "o")
        src = os.path.join(EXAMPLES, "quickship", "exports")
        self.assertEqual(self.build(src, "November 2026", out), 0)
        self.assertEqual(self.build(src, "November 2026", out), 2)
        self.assertEqual(self.build(src, "November 2026", out, extra=["--force"]), 0)


if __name__ == "__main__":
    unittest.main()
