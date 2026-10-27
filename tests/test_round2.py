"""Tests for the second review round: failure-signal gating, unit inference, month-over-month subjects and statistics,
environment mixing, validate --against, --force clean-up, prompt-mode markers and config type checks.
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
from ddreview import compare, config, validate  # noqa: E402

T0 = datetime(2026, 11, 19, 10, 0, tzinfo=timezone.utc)
THRESH = config.load_config(None)["thresholds"]


def write_csv(path, header, rows):
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        w.writerows(rows)


def span_rows(n=600, services=("orders-api",), cols=("Date", "Service", "Resource", "Duration", "Method", "Status Code"), cell=None):
    rows = []
    for i in range(n):
        svc = services[i % len(services)]
        base = {"Date": (T0 + timedelta(seconds=i)).strftime("%Y-%m-%dT%H:%M:%S.000Z"), "Service": svc,
                "Resource": "POST /api/orders" if i % 3 else "GET /api/orders/{id}", "Duration": f"{50 + (i % 7) * 10} ms",
                "Method": "POST" if i % 3 else "GET", "Status Code": 500 if i % 10 == 0 else 200}
        if cell:
            base.update(cell(i, svc))
        rows.append([base.get(c, "") for c in cols])
    return list(cols), rows


class Round2(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ddreview-r2-")
        self.out = os.path.join(self.tmp, "out")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def folder(self, name):
        p = os.path.join(self.tmp, name)
        os.makedirs(p, exist_ok=True)
        return p

    def build(self, folder, *extra, month="November 2026"):
        return cli.main(["build", folder, "--out", self.out, "--org", "Round Two", "--month", month, "--force"] + (["--previous", "none"] if "--previous" not in extra else []) + list(extra))

    def month_dir(self, key="2026-11"):
        return os.path.join(self.out, "round-two", key)

    def notebook(self, key="2026-11"):
        p = self.month_dir(key)
        names = [f for f in os.listdir(p) if f.endswith(".md") and "review" in f]
        self.assertEqual(len(names), 1, names)
        with open(os.path.join(p, names[0]), encoding="utf-8") as fh:
            md = fh.read()
        with open(os.path.join(p, "facts.json"), encoding="utf-8") as fh:
            facts = json.load(fh)
        return md, facts, names[0]

    # ------------------------------------------------------------ readers
    def test_error_column_with_0_and_1(self):
        f = self.folder("err01")
        h, rows = span_rows(cols=("Date", "Service", "Resource", "Duration", "Method", "Error"), cell=lambda i, s: {"Error": "1" if i % 20 == 0 else "0"})
        write_csv(os.path.join(f, "spans.csv"), h, rows)
        self.assertEqual(self.build(f), 0)
        md, facts, _ = self.notebook()
        s = facts["services"]["orders-api"]
        self.assertEqual(s["failed"]["all"], 30)                     # 600 / 20: '0' is not an error
        self.assertTrue(facts["coverage"]["failure_signal"])

    def test_bare_microsecond_durations(self):
        f = self.folder("us")
        h, rows = span_rows(cell=lambda i, s: {"Duration": str((50 + (i % 7) * 10) * 1000)})      # 50-110 ms written in µs
        write_csv(os.path.join(f, "spans.csv"), h, rows)
        self.assertEqual(self.build(f), 0)
        md, facts, _ = self.notebook()
        p95 = facts["services"]["orders-api"]["p95_s"]
        self.assertTrue(0.05 <= p95 <= 0.2, p95)
        self.assertIn("bare numbers", md)                              # the inference is a caveat the reader sees

    # ------------------------------------------------------------ failure-signal gating
    def test_no_failure_signal_prints_no_failure_figures(self):
        f = self.folder("nofail")
        h, rows = span_rows(cols=("Date", "Service", "Resource", "Duration", "Method"))
        write_csv(os.path.join(f, "spans.csv"), h, rows)
        self.assertEqual(self.build(f), 0)
        md, facts, _ = self.notebook()
        self.assertFalse(facts["coverage"]["failure_signal"])
        self.assertIn("not recorded", md)
        self.assertNotIn("0 % failed", md)
        self.assertNotIn("keeping the monitors", md)
        self.assertTrue(any(h_.get("status") in ("unmeasured", "quiet") for h_ in facts["healthy"]))
        self.assertNotIn("no monitors", md)                            # monitor coverage is unknown without a Services list

    # ------------------------------------------------------------ month over month
    def test_then_for_looks_up_this_months_subject(self):
        F = {"mom": {"comparable": {"volumes": True, "rates": True}}, "resources": {}, "coverage": {"failure_signal": True},
             "dependencies": {"b.example.com": {"failure_known": True}}}
        prev = {"dependencies": {"b.example.com": {"err": {"num": 2, "den": 100, "pct": 2.0}, "failure_known": True, "calls": 100}}}
        p = {"metrics": {"primary": {"label": "failure rate of a.example.com", "value": 50.0, "unit": "pct", "subject": "a.example.com"}}}
        f = {"detector": "failing_dependencies", "subject": {"service": None}, "metrics": {"primary": {"label": "failure rate of b.example.com", "value": 60.0, "unit": "pct", "subject": "b.example.com"},
                                                                                  "subject_host": "b.example.com"}}
        value, text, nd = compare._then_for(F, prev, p, f)
        self.assertEqual(value, 2.0)
        self.assertEqual(nd, (2, 100))
        del prev["dependencies"]["b.example.com"]
        value, text, nd = compare._then_for(F, prev, p, f)
        self.assertIsNone(value)
        self.assertEqual(compare._direction(None, 60.0, "pct", THRESH)[0], "new")

    def test_rate_change_within_sampling_noise_is_flat(self):
        d, _ = compare._direction(2.0, 4.0, "pct", THRESH, then_nd=(2, 100), now_nd=(4, 100))
        self.assertEqual(d, "flat")                                    # +2 pt but 4 failures and z < 2
        d, _ = compare._direction(2.0, 4.0, "pct", THRESH, then_nd=(2000, 100000), now_nd=(4000, 100000))
        self.assertEqual(d, "worse")

    def test_volumes_not_compared_when_ranges_are_unproved(self):
        F = {"window": {"volumes_tier": "aggregate", "aggregate": {"days": 7, "source": "assumed", "proof": []}, "spans": {"minutes": 9}},
             "coverage": {"failure_signal": True}, "meta": {}, "services": {}, "domains": {}, "kpis": {}, "dependencies": {}}
        prev = {"window": {"volumes_tier": "aggregate", "aggregate": {"days": 7, "source": "assumed", "proof": []}}, "meta": {"month": "October 2026"}, "coverage": {"failure_signal": True}}
        mom = compare.compare_facts(F, prev, {"thresholds": THRESH})
        self.assertFalse(mom["comparable"]["volumes"])
        F["window"]["aggregate"]["proof"] = [{"service": "a", "resource": "GET /health", "requests": 100800, "implied_interval_s": 6.0}]
        prev["window"]["aggregate"]["proof"] = [{"service": "a", "resource": "GET /health", "requests": 201600, "implied_interval_s": 3.0}]
        mom = compare.compare_facts(F, prev, {"thresholds": THRESH})
        self.assertFalse(mom["comparable"]["volumes"])
        self.assertIn("health-check count", mom["comparable"]["note"])

    # ------------------------------------------------------------ environments
    def test_mixed_environment_export_is_flagged(self):
        f = self.folder("env")
        h, rows = span_rows(cols=("Date", "Service", "Resource", "Duration", "Method", "Status Code", "Env"), cell=lambda i, s: {"Env": "prod" if i % 2 else "staging"})
        write_csv(os.path.join(f, "spans.csv"), h, rows)
        self.assertEqual(self.build(f), 0)
        md, facts, _ = self.notebook()
        self.assertIn("mixes environments", " ".join(facts["caveats"]))

    # ------------------------------------------------------------ validate
    def test_validate_text_needs_facts(self):
        f = self.folder("v")
        h, rows = span_rows()
        write_csv(os.path.join(f, "spans.csv"), h, rows)
        self.assertEqual(self.build(f), 0)
        _, _, name = self.notebook()
        self.assertEqual(cli.main(["validate", os.path.join(self.month_dir(), name), "--text"]), 2)

    def test_validate_against_catches_a_changed_number(self):
        f = self.folder("against")
        h, rows = span_rows()
        write_csv(os.path.join(f, "spans.csv"), h, rows)
        self.assertEqual(self.build(f), 0)
        md, facts, name = self.notebook()
        orig = os.path.join(self.month_dir(), name)
        same = os.path.join(self.tmp, "same.md")
        with open(same, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(md)
        self.assertEqual(cli.main(["validate", same, "--facts", os.path.join(self.month_dir(), "facts.json"), "--against", orig]), 0)
        e, w = validate.check_against(md.replace("600", "601", 1), md)
        self.assertTrue(e, "a changed number must be an error")

    def test_unedited_notebook_with_other_pie_slice_passes_text_check(self):
        f = self.folder("many")
        names = tuple(f"{w}-api" for w in ("alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf", "hotel", "india", "juliet"))
        h, rows = span_rows(n=1200, services=names)
        write_csv(os.path.join(f, "spans.csv"), h, rows)
        self.assertEqual(self.build(f), 0)
        md, facts, name = self.notebook()
        self.assertIn('"Other" :', md)
        self.assertIn("other systems", md.split("\n")[2])
        self.assertNotIn("domains systems", md)
        self.assertEqual(cli.main(["validate", os.path.join(self.month_dir(), name), "--facts", os.path.join(self.month_dir(), "facts.json"), "--text"]), 0)

    # ------------------------------------------------------------ build options
    def test_force_rebuild_clears_stale_outputs(self):
        f = self.folder("force")
        h, rows = span_rows()
        write_csv(os.path.join(f, "spans.csv"), h, rows)
        self.assertEqual(self.build(f), 0)
        self.assertTrue(os.listdir(os.path.join(self.month_dir(), "mermaid")))
        self.assertEqual(self.build(f, "--diagrams", "prompt", "--no-prompts"), 2)   # prompt-only diagrams need the prompt files
        self.assertEqual(self.build(f, "--no-prompts"), 0)
        self.assertFalse(os.path.exists(os.path.join(self.month_dir(), "prompts")))
        self.assertEqual(self.build(f, "--diagrams", "prompt"), 0)
        md, _, _ = self.notebook()                                     # exactly one notebook file is left
        self.assertFalse(os.path.exists(os.path.join(self.month_dir(), "mermaid")))
        self.assertNotIn("```mermaid", md)
        self.assertIn("prompts/01-diagram-landscape.md", md)
        self.assertTrue(os.path.exists(os.path.join(self.month_dir(), "prompts", "01-diagram-landscape.md")))

    def test_previous_keyword_is_case_insensitive(self):
        f = self.folder("prevcase")
        h, rows = span_rows()
        write_csv(os.path.join(f, "spans.csv"), h, rows)
        self.assertEqual(self.build(f, "--previous", "NONE"), 0)

    def test_prompt_mode_readme_says_paste_in_place_of_marker(self):
        f = self.folder("pm")
        h, rows = span_rows()
        write_csv(os.path.join(f, "spans.csv"), h, rows)
        self.assertEqual(self.build(f, "--diagrams", "prompt"), 0)
        with open(os.path.join(self.month_dir(), "prompts", "00-README.md"), encoding="utf-8") as fh:
            readme = fh.read()
        self.assertIn("in place of the *Service map* marker", readme)

    # ------------------------------------------------------------ config
    def test_pattern_list_written_as_a_string_is_a_config_error(self):
        f = self.folder("cfg")
        h, rows = span_rows()
        write_csv(os.path.join(f, "spans.csv"), h, rows)
        cfgp = os.path.join(self.tmp, "org.json")
        with open(cfgp, "w", encoding="utf-8") as fh:
            json.dump({"org": {"name": "Round Two", "short": "round-two"}, "noise": {"health": "^GET /health$"}}, fh)
        with self.assertRaises(SystemExit) as cm:
            cli.main(["build", f, "--config", cfgp, "--out", self.out, "--month", "November 2026", "--previous", "none"])
        self.assertEqual(cm.exception.code, 3)

    def test_overlapping_endpoints_lists_are_refused(self):
        f = self.folder("ep")
        h, rows = span_rows()
        write_csv(os.path.join(f, "spans.csv"), h, rows)
        ep_h = ["Name", "Service", "Request Count", "Error Rate", "P95 Duration"]
        ep_rows = [["POST /api/orders", "orders-api", "40000", "0.01", "0.08"]]
        write_csv(os.path.join(f, "endpoints.csv"), ep_h, ep_rows)
        write_csv(os.path.join(f, "endpoints-old.csv"), ep_h, ep_rows + [["GET /api/orders/{id}", "orders-api", "20000", "0.0", "0.05"]])
        self.assertEqual(self.build(f), 2)


if __name__ == "__main__":
    unittest.main()
