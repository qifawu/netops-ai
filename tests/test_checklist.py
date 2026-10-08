"""Inspection checklist prototype: format validation, the read-only whitelist, one run, stored results, trend prompt."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from netops_ai.inspection import checklist as cl

BRIEF = """Interface                  IP-Address      OK? Method Status                Protocol
GigabitEthernet0/0         192.0.2.10      YES NVRAM  up                    up
GigabitEthernet0/1         10.0.1.2        YES NVRAM  administratively down down
"""
IF_OUT = "GigabitEthernet0/1 is up, line protocol is up\n     5 input errors, 2 CRC, 0 frame\n     0 output errors, 0 collisions\n"

CHECKLIST = {
    "name": "t1",
    "vendor": "cisco",
    "devices": [{"name": "D1", "host": "192.0.2.10"}, {"name": "D2", "host": "192.0.2.11"}],
    "checks": [
        {"id": "brief", "command": "show ip interface brief",
         "expect": [{"type": "not_contains", "value": "administratively down", "severity": "warning"}]},
        {"id": "errs", "command": "show interfaces GigabitEthernet0/1",
         "expect": [{"type": "contains", "value": "line protocol is up", "severity": "critical"}],
         "extract": [{"name": "input_errors", "regex": r"(\d+) input errors", "cast": "int"}]},
    ],
}


class FakeAdapter:
    def __init__(self, outputs):
        self.outputs, self.sent, self.closed = outputs, [], False

    def run(self, command):
        # The real adapter checks the whitelist first; mirror that so the test proves refused commands are not sent.
        from netops_ai.devices.whitelist import check

        v = check("cisco", command)
        if not v.allowed:
            return SimpleNamespace(allowed=False, denial_reason=v.reason, error="", output="")
        self.sent.append(command)
        return SimpleNamespace(allowed=True, denial_reason="", error="", output=self.outputs.get(command, ""))

    def close(self):
        self.closed = True


class TestValidate(unittest.TestCase):
    def test_valid_checklist_has_no_problems(self):
        self.assertEqual(cl.validate_checklist(CHECKLIST), [])

    def test_write_command_is_refused_at_authoring_time(self):
        bad = json.loads(json.dumps(CHECKLIST))
        bad["checks"][0]["command"] = "configure terminal"
        problems = cl.validate_checklist(bad)
        self.assertTrue(any("refused by the read-only whitelist" in p for p in problems), problems)

    def test_format_errors_are_reported(self):
        bad = {"name": "x y", "devices": [], "checks": [{"id": "a", "command": "show version", "expect": [{"type": "nope", "value": "["}]}]}
        text = "\n".join(cl.validate_checklist(bad))
        self.assertIn("name:", text)
        self.assertIn("devices:", text)
        self.assertIn("expect[0].type", text)

    def test_example_file_is_valid(self):
        root = Path(__file__).resolve().parents[1]
        data = cl.load_checklist(root / "examples" / "checklists" / "core-health.yaml")
        self.assertEqual(cl.validate_checklist(data), [])


class TestRun(unittest.TestCase):
    def _factory(self, adapters):
        def make(dev):
            a = FakeAdapter({"show ip interface brief": BRIEF, "show interfaces GigabitEthernet0/1": IF_OUT})
            adapters[dev.name] = a
            return a
        return make

    def test_run_evaluates_expectations_and_extracts_metrics(self):
        adapters: dict = {}
        res = cl.run_checklist(CHECKLIST, self._factory(adapters))
        d1 = res["devices"][0]
        brief, errs = d1["checks"]
        self.assertEqual(brief["status"], "fail")
        self.assertEqual(brief["severity"], "warning")
        self.assertEqual(errs["status"], "pass")
        self.assertEqual(errs["metrics"], {"input_errors": 5})
        self.assertEqual(res["summary"]["fail"], 2)  # one per device
        self.assertTrue(all(a.closed for a in adapters.values()))

    def test_unreachable_device_does_not_stop_the_run(self):
        calls = []

        def make(dev):
            calls.append(dev.name)
            if dev.name == "D1":
                raise OSError("connection refused")
            return FakeAdapter({"show ip interface brief": BRIEF, "show interfaces GigabitEthernet0/1": IF_OUT})

        res = cl.run_checklist(CHECKLIST, make)
        self.assertEqual(calls, ["D1", "D2"])
        self.assertIn("connection refused", res["devices"][0]["error"])
        self.assertEqual(res["summary"]["unreachable_devices"], 1)

    def test_invalid_checklist_is_not_executed(self):
        bad = json.loads(json.dumps(CHECKLIST))
        bad["checks"][0]["command"] = "reload"
        with self.assertRaises(ValueError):
            cl.run_checklist(bad, lambda dev: FakeAdapter({}))


class TestStoreAndTrend(unittest.TestCase):
    def test_results_are_stored_and_the_trend_prompt_lists_every_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            for n in (3, 5, 9):
                out = IF_OUT.replace("5 input errors", f"{n} input errors")
                res = cl.run_checklist(
                    CHECKLIST,
                    lambda dev, out=out: FakeAdapter({"show ip interface brief": BRIEF, "show interfaces GigabitEthernet0/1": out}),
                )
                path = cl.save_result(res, tmp)
                self.assertTrue(path.exists())
            history = cl.load_history(tmp, "t1", 12)
            self.assertEqual(len(history), 3)
            table = cl.trend_table(history)
            self.assertIn("D1/errs input_errors: 3 | 5 | 9", table)
            msgs = cl.build_trend_messages(history)
            self.assertEqual([m["role"] for m in msgs], ["system", "user"])
            self.assertIn("Cite run ids", msgs[0]["content"])


if __name__ == "__main__":
    unittest.main()
