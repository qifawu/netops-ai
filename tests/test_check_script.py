"""判定脚本沙箱：能跑对的脚本；越界写法在静态检查或运行时被拦；超时、超大输出、脏返回值都有人话报错；自测用例。"""
from __future__ import annotations

import unittest

from netops_ai.inspection import check_script as S

DIFF_SCRIPT = '''
import re
def evaluate(ctx):
    cur = set(l.strip() for l in ctx["output"].splitlines() if re.match(r"^[A-Z*]\\s", l.strip()))
    if ctx["previous"] is None:
        return {"status": "baseline", "summary": "first run: %d routes" % len(cur), "metrics": {"routes": len(cur)}}
    old = set(l.strip() for l in ctx["previous"].splitlines() if re.match(r"^[A-Z*]\\s", l.strip()))
    gone, new = sorted(old - cur), sorted(cur - old)
    st = "fail" if gone else "pass"
    return {"status": st, "summary": "-%d +%d" % (len(gone), len(new)), "metrics": {"routes": len(cur)},
            "findings": ["- " + x for x in gone] + ["+ " + x for x in new]}
'''
SAMPLE = "C   198.51.100.0/30 is directly connected, Gi0/1\nO   198.51.100.4/30 [110/2] via 192.0.2.2, Gi0/1\nS   203.0.113.0/25 [1/0] via 192.0.2.2\n"


class TestStatic(unittest.TestCase):
    def test_正常脚本通过(self):
        self.assertEqual(S.validate_source(DIFF_SCRIPT), [])

    def test_越界写法被静态拦下(self):
        for bad in ("import os\ndef evaluate(ctx):\n    return {}", "import subprocess\ndef evaluate(ctx):\n    return {}",
                    "def evaluate(ctx):\n    return open('/etc/passwd').read()",
                    "def evaluate(ctx):\n    return eval('1')",
                    "def evaluate(ctx):\n    return ().__class__.__bases__",
                    "from socket import socket\ndef evaluate(ctx):\n    return {}",
                    "def evaluate(ctx):\n    return __import__('os')"):
            self.assertTrue(S.validate_source(bad), bad)

    def test_没有evaluate或语法错(self):
        self.assertTrue(any("evaluate" in p for p in S.validate_source("x = 1")))
        self.assertTrue(any("syntax" in p for p in S.validate_source("def evaluate(ctx:\n  pass")))
        self.assertTrue(S.validate_source(""))


class TestRun(unittest.TestCase):
    def ctx(self, **kw):
        return {"output": SAMPLE, "previous": None, "previous_run": None, "device": "D", "params": {}, **kw}

    def test_首次是基线_持平通过_少一条判失败(self):
        self.assertEqual(S.run_script(DIFF_SCRIPT, self.ctx())["status"], "baseline")
        self.assertEqual(S.run_script(DIFF_SCRIPT, self.ctx(previous=SAMPLE))["status"], "pass")
        less = "\n".join(SAMPLE.splitlines()[:2])
        r = S.run_script(DIFF_SCRIPT, self.ctx(output=less, previous=SAMPLE))
        self.assertEqual(r["status"], "fail")
        self.assertTrue(any("203.0.113.0/25" in f for f in r["findings"]))

    def test_运行时越界也拦_绕过静态检查的写法(self):
        # 静态检查能看到的名字都被拦；这里直接验证运行时的 import 守卫
        runner_bad = "def evaluate(ctx):\n    import json\n    return {'status': 'pass', 'summary': json.dumps({'a': 1})}"
        self.assertEqual(S.run_script(runner_bad, self.ctx())["summary"], '{"a": 1}')

    def test_超时(self):
        loop = "def evaluate(ctx):\n    while True:\n        pass"
        with self.assertRaises(S.ScriptError) as cm:
            S.run_script(loop, self.ctx(), timeout=1.0)
        self.assertIn("timed out", str(cm.exception))

    def test_脚本抛异常_报人话(self):
        boom = "def evaluate(ctx):\n    return 1 / 0"
        with self.assertRaises(S.ScriptError) as cm:
            S.run_script(boom, self.ctx())
        self.assertIn("ZeroDivisionError", str(cm.exception))

    def test_返回值不合规(self):
        for body, needle in (("return 5", "dict"), ("return {'status': 'maybe'}", "status"),
                             ("return {'status': 'pass', 'metrics': {'a': 'x'}}", "number")):
            with self.assertRaises(S.ScriptError) as cm:
                S.run_script(f"def evaluate(ctx):\n    {body}", self.ctx())
            self.assertIn(needle, str(cm.exception))

    def test_findings_有上限(self):
        many = "def evaluate(ctx):\n    return {'status': 'fail', 'findings': ['x'] * 500}"
        self.assertLessEqual(len(S.run_script(many, self.ctx())["findings"]), S.MAX_FINDINGS)

    def test_sha256对换行不敏感(self):
        self.assertEqual(S.sha256("a\r\nb\n"), S.sha256("a\nb"))


from tests.script_fixtures import GOAL_TESTS  # noqa: E402


class TestSelfTest(unittest.TestCase):
    def test_好脚本自测通过_且目标用例全部符合期望(self):
        r = S.self_test(DIFF_SCRIPT, SAMPLE, GOAL_TESTS)
        self.assertTrue(r["ok"], r)
        self.assertTrue(r["has_goal_tests"])
        self.assertTrue(r["plausible"], r["cases"])

    def test_总是通过的脚本_目标用例里期望fail的那条暴露它(self):
        lazy = "def evaluate(ctx):" + chr(10) + "    return {'status': 'pass', 'summary': 'ok'}"
        r = S.self_test(lazy, SAMPLE, GOAL_TESTS)
        self.assertTrue(r["ok"])  # 没崩、确定性
        self.assertFalse(r["plausible"])  # 但没有按用户目标报警
        gone = next(c for c in r["cases"] if c["name"] == "一条路由消失")
        self.assertEqual((gone["status"], gone["plausible"]), ("pass", False))

    def test_没有目标用例_不算合理(self):
        r = S.self_test(DIFF_SCRIPT, SAMPLE, None)
        self.assertTrue(r["ok"])
        self.assertFalse(r["plausible"])
        self.assertFalse(r["has_goal_tests"])

    def test_find不在样本里_用例作废并报告(self):
        r = S.self_test(DIFF_SCRIPT, SAMPLE, [{"name": "x", "edits": [{"find": "not in sample", "replace": "y"}], "expect": "fail"}])
        self.assertTrue(r["goal_problems"])
        self.assertFalse(r["plausible"])

    def test_忽略新增的脚本_只要目标用例符合就是合理的(self):
        r = S.self_test(DIFF_SCRIPT, SAMPLE, GOAL_TESTS)  # DIFF_SCRIPT 只盯消失，新增不报警
        self.assertTrue(r["plausible"])

    def test_崩溃的脚本自测不过(self):
        boom = "def evaluate(ctx):" + chr(10) + "    return ctx['nope']"
        r = S.self_test(boom, SAMPLE, GOAL_TESTS)
        self.assertFalse(r["ok"])
        self.assertTrue(r["problems"])

    def test_静态不过直接返回(self):
        evil = "import os" + chr(10) + "def evaluate(ctx):" + chr(10) + "    return {}"
        r = S.self_test(evil, SAMPLE)
        self.assertFalse(r["source_ok"])


class TestInChecklist(unittest.TestCase):
    """判定脚本接进巡检清单：连跑几次拿上一次输出比；未审核 / 源码被改 / 脚本出错都不会静默通过。"""

    def _cl(self, **kw):
        e = {"type": "script", "name": "route-diff", "source": DIFF_SCRIPT, "sha256": S.sha256(DIFF_SCRIPT), "approved": True,
             "severity": "critical"}
        e.update(kw)
        return {"name": "rt", "vendor": "cisco", "devices": [{"name": "V1", "host": "192.0.2.50"}],
                "checks": [{"id": "table", "command": "show ip route | begin Gateway", "expect": [e]}]}

    def _run(self, cl, outputs):
        from types import SimpleNamespace

        from netops_ai.inspection import checklist as CL

        hist, res = [], []
        for o in outputs:
            class A:
                def run(self, command, o=o):
                    return SimpleNamespace(allowed=True, denial_reason="", error="", output=o)

                def close(self):
                    pass
            r = CL.run_checklist(cl, lambda dev: A(), history=hist)
            hist.append(r)
            res.append(r)
        return res

    def _ev(self, r):
        return r["devices"][0]["checks"][0]["evaluations"][0]

    def test_三次运行_基线_持平_少一条(self):
        less = "\n".join(SAMPLE.splitlines()[:2])
        r = self._run(self._cl(), [SAMPLE, SAMPLE, less])
        self.assertEqual([self._ev(x)["state"] for x in r], ["baseline", "pass", "fail"])
        self.assertTrue(any("203.0.113.0/25" in f for f in self._ev(r[2])["findings"]))
        self.assertEqual(r[2]["devices"][0]["checks"][0]["status"], "fail")
        self.assertEqual(r[2]["devices"][0]["checks"][0]["metrics"]["routes"], 2)  # 脚本算的数进趋势表

    def test_未审核的脚本_整份清单拒绝运行(self):
        with self.assertRaises(ValueError) as cm:
            self._run(self._cl(approved=False), [SAMPLE])
        self.assertIn("not been reviewed", str(cm.exception))

    def test_源码被改_sha不对_整份清单拒绝运行(self):
        with self.assertRaises(ValueError) as cm:
            self._run(self._cl(sha256="0" * 64), [SAMPLE])
        self.assertIn("sha256", str(cm.exception))

    def test_运行时再防一道_直接评估未审核的规则也不执行(self):
        from netops_ai.inspection import checklist as CL

        e = {"type": "script", "name": "x", "source": DIFF_SCRIPT, "sha256": "bad", "approved": True}
        r = CL.evaluate_script_rule(e, SAMPLE, False, "V1", "table", CL.MetricHistory())
        self.assertEqual((r["state"], r["passed"]), ("error", False))
        self.assertIn("not executed", r["detail"])

    def test_脚本出错_判失败_不静默通过(self):
        boom = "def evaluate(ctx):\n    return ctx['nope']"
        r = self._run(self._cl(source=boom, sha256=S.sha256(boom)), [SAMPLE])
        self.assertEqual(self._ev(r[0])["state"], "error")
        self.assertEqual(r[0]["devices"][0]["checks"][0]["status"], "fail")

    def test_校验_没审核过的脚本保存前就被拦(self):
        from netops_ai.inspection import checklist as CL

        problems = " ".join(CL.validate_checklist(self._cl(approved=False)))
        self.assertIn("not been reviewed", problems)
        self.assertEqual(CL.validate_checklist(self._cl()), [])
        bad = self._cl(source="import os\ndef evaluate(ctx):\n    return {}", sha256=S.sha256("import os\ndef evaluate(ctx):\n    return {}"))
        self.assertTrue(any("not allowed" in p for p in CL.validate_checklist(bad)))
