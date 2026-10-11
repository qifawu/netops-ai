"""助手写判定脚本：按真实样本写 → 沙箱自测 → 不过就带着问题让模型改 → 交给用户审核（草案里是未批准的脚本检查项）。"""
from __future__ import annotations

import json
import unittest
from types import SimpleNamespace

from netops_ai.inspection import check_script as CS
from netops_ai.inspection import script_author as SA
from tests.test_check_script import DIFF_SCRIPT, SAMPLE

LAZY = "def evaluate(ctx):\n    return {'status': 'pass', 'summary': 'ok'}"
CRASH = "def evaluate(ctx):\n    return ctx['nope']"
EVIL = "import os\ndef evaluate(ctx):\n    return {'status': 'pass'}"


from tests.script_fixtures import TESTS  # noqa: E402


class FakeLLM:
    def __init__(self, sources):
        self.sources, self.calls = list(sources), []

    def complete(self, messages, **kw):
        self.calls.append(messages)
        src = self.sources.pop(0) if len(self.sources) > 1 else self.sources[0]
        if isinstance(src, Exception):
            raise src
        return SimpleNamespace(parsed=None, content=json.dumps({"source": src, "explanation": "对比两次输出的条目。", "tests": TESTS},
                                                               ensure_ascii=False))


class TestRedact(unittest.TestCase):
    def test_口令和团体字行被脱敏_其余行与换行原样(self):
        nl = chr(10)
        text = "snmp-server community abc RO" + nl + "enable secret 5 xyz" + nl + "S 203.0.113.0/25 via 192.0.2.2" + nl
        out = SA.redact_secrets(text)
        self.assertNotIn("abc", out)
        self.assertNotIn("xyz", out)
        self.assertTrue(out.endswith("S 203.0.113.0/25 via 192.0.2.2" + nl))


class TestAuthor(unittest.TestCase):
    def test_一次写对(self):
        llm = FakeLLM([DIFF_SCRIPT])
        r = SA.author_script("路由表和上次比，少了哪些路由要报警", "show ip route", SAMPLE, llm)
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["rounds"], 1)
        self.assertEqual(r["sha256"], CS.sha256(DIFF_SCRIPT))
        # 给模型的提示里带了真实样本和用户的目标
        user = llm.calls[0][1]["content"]
        self.assertIn("203.0.113.0/25", user)
        self.assertIn("路由表和上次比", user)

    def test_静态不过_带着问题让模型改(self):
        llm = FakeLLM([EVIL, DIFF_SCRIPT])
        r = SA.author_script("g", "show ip route", SAMPLE, llm)
        self.assertTrue(r["ok"])
        self.assertEqual(r["rounds"], 2)
        self.assertIn("not allowed", llm.calls[1][-1]["content"])

    def test_崩溃_再改(self):
        r = SA.author_script("g", "show ip route", SAMPLE, FakeLLM([CRASH, DIFF_SCRIPT]))
        self.assertTrue(r["ok"])
        self.assertEqual(r["rounds"], 2)

    def test_总是通过的脚本_少一行也判通过_不合理_被打回(self):
        llm = FakeLLM([LAZY, DIFF_SCRIPT])
        r = SA.author_script("g", "show ip route", SAMPLE, llm)
        self.assertTrue(r["ok"])
        self.assertEqual(r["rounds"], 2)
        self.assertIn("一条路由消失", llm.calls[1][-1]["content"])

    def test_几轮都写不对_ok为False_保留最后一版给用户看(self):
        r = SA.author_script("g", "show ip route", SAMPLE, FakeLLM([LAZY]), max_rounds=2)
        self.assertFalse(r["ok"])
        self.assertEqual(r["source"], LAZY)
        self.assertTrue(r["tests"])
        self.assertTrue(r["error"])

    def test_模型不可用_样本为空_不抛异常(self):
        self.assertFalse(SA.author_script("g", "c", SAMPLE, None)["ok"])
        self.assertFalse(SA.author_script("g", "c", "  \n", FakeLLM([DIFF_SCRIPT]))["ok"])
        r = SA.author_script("g", "c", SAMPLE, FakeLLM([RuntimeError("boom")]))
        self.assertFalse(r["ok"])
        self.assertIn("model call failed", r["error"])

    def test_草案里的脚本检查项默认未批准_校验不过(self):
        from netops_ai.inspection import checklist as CL

        r = SA.author_script("g", "show ip route | begin Gateway", SAMPLE, FakeLLM([DIFF_SCRIPT]))
        chk = SA.pending_check("route_diff", "路由表明细对比", "show ip route | begin Gateway", r)
        cl = {"name": "p", "vendor": "cisco", "devices": [{"name": "V1", "host": "192.0.2.50"}], "checks": [chk]}
        self.assertTrue(any("not been reviewed" in p for p in CL.validate_checklist(cl)))
        chk["expect"][0]["approved"] = True
        self.assertTrue(any("signature" in p for p in CL.validate_checklist(cl)))  # 只改 approved 不行，得有应用签发的签名
        chk["expect"][0]["signature"] = CS.sign(chk["expect"][0]["source"])
        self.assertEqual(CL.validate_checklist(cl), [])


if __name__ == "__main__":
    unittest.main()
