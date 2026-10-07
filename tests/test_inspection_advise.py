from __future__ import annotations

import unittest
from unittest import mock

from netops_ai.api import dashboard
from netops_ai.inspection import advise


PAYLOAD = {
    "scanned_hosts": 2,
    "scanned_items": 2015,
    "items_with_data": 1796,
    "errors": [],
    "findings": [
        {
            "kind": "trend",
            "host_name": "V1-vios",
            "item_name": "ICMP ping",
            "item_key": "icmpping",
            "finding": {
                "kind": "trend",
                "direction": "rising",
                "health_impact": "improving",
                "first_segment_mean": 0.06210191082802548,
                "last_segment_mean": 1.0,
                "reason": "前段均值 0.0621 -> 后段均值 1（变化 +1510.3%），100% 的相邻点非降",
            },
        }
    ],
}


class TestCompact(unittest.TestCase):
    def test_喂给模型的是检测器写好的reason不是原始浮点(self):
        # 原始浮点（0.06210191082802548）既占 token 又没人读得懂，
        # 而且模型引用它的时候很容易自己重算一遍——**数字一重算就不是逐字引用了**。
        text = advise.compact(PAYLOAD)
        self.assertIn("前段均值 0.0621 -> 后段均值 1（变化 +1510.3%）", text)
        self.assertNotIn("0.06210191082802548", text)

    def test_条数封顶(self):
        # 不是省钱，是省注意力：几十条同质发现全灌进去，模型会把篇幅摊平，
        # 真正要紧的那一条反而被淹掉。
        big = {**PAYLOAD, "findings": PAYLOAD["findings"] * 100}
        rows = [l for l in advise.compact(big).splitlines() if l.startswith("[")]
        self.assertEqual(len(rows), advise.MAX_FINDINGS)


class TestCompactStatus(unittest.TestCase):
    STATUS = {"devices": [
        {"name": "R1", "checks": [
            {"check": "ospf", "status": "bad", "summary": "1 个 OSPF 邻居不是 FULL", "evidence": ["10.0.0.2  INIT/  -"]},
            {"check": "bgp", "status": "ok", "summary": "全部 Established", "evidence": []},
        ]},
        {"name": "R2", "checks": [{"check": "errors", "status": "warn", "summary": "没有上一次基线", "evidence": []}]},
    ]}

    def test_只带bad和warn_设备输出原样保留(self):
        text = advise.compact_status(self.STATUS)
        self.assertIn("R1 · 状态检查（ospf）", text)
        self.assertIn("设备输出：10.0.0.2  INIT/  -", text)
        self.assertIn("R2 · 状态检查（errors）", text)
        self.assertNotIn("bgp", text)

    def test_没有状态巡检就是空串(self):
        self.assertEqual(advise.compact_status(None), "")
        self.assertEqual(advise.compact_status({"devices": []}), "")


class TestThinkingSwitch(unittest.TestCase):
    """百炼思考模型要关思考，**收在 `LLMClient` 里统一做**，不靠各调用方记得。

    以前只写在巡检那一处；研判那次调用在百炼上同样两次都解析失败
    （8000 个 token 全烧在 reasoning 上），两处各写一份迟早漏第三处。
    """

    def _payload(self, base_url, **kw):
        import json as _json
        from netops_ai.llm.client import LLMClient

        seen = {}

        def fake_urlopen(req, timeout=None):
            seen.update(_json.loads(req.data))
            resp = mock.MagicMock()
            resp.__enter__.return_value.read.return_value = _json.dumps(
                {"choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}], "usage": {}}
            ).encode()
            return resp

        with mock.patch("urllib.request.urlopen", fake_urlopen), \
             mock.patch("netops_ai.llm.client.record_usage"):
            LLMClient(base_url=base_url, api_key="k", model="m").complete([{"role": "user", "content": "x"}], **kw)
        return seen

    def test_百炼默认关思考(self):
        self.assertIs(self._payload("https://ws-x.ap-southeast-1.maas.aliyuncs.com/compatible-mode/v1")["enable_thinking"], False)

    def test_别家不发这个参数(self):
        # 厂商专有参数，OpenAI 这类会直接 400。
        self.assertNotIn("enable_thinking", self._payload("https://api.openai.com/v1"))

    def test_调用方显式要开就听调用方的(self):
        self.assertIs(self._payload("https://x.aliyuncs.com/v1", extra_body={"enable_thinking": True})["enable_thinking"], True)


class TestAdvise(unittest.TestCase):
    def _run(self, parsed):
        resp = mock.MagicMock(parsed=parsed)
        with mock.patch.object(advise, "LLMClient") as client:
            client.return_value.complete.return_value = resp
            return advise.advise(PAYLOAD, topology="（测试不带拓扑）")

    def test_引文逐字对得上才算核过(self):
        out = self._run({
            "summary": "s",
            "needs_attention": [{
                "host": "V1-vios", "item_key": "icmpping", "severity": "medium",
                "why": "w", "suggestion": "去 V1 看 Gi0/1 日志",
                "evidence": "前段均值 0.0621 -> 后段均值 1（变化 +1510.3%），100% 的相邻点非降",
            }],
            "ignorable": [], "cannot_tell": [],
        })
        self.assertTrue(out["needs_attention"][0]["evidence_verified"])
        self.assertEqual(out["verification"]["unverified"], 0)

    def test_引文自己编的要标出来不能静默通过(self):
        # **这一条是这层存在的意义**：建议可以是模型写的，支撑它的那句原话不许是。
        out = self._run({
            "summary": "s",
            "needs_attention": [{
                "host": "V1-vios", "item_key": "icmpping", "severity": "high",
                "why": "w", "suggestion": "s",
                "evidence": "丢包率从 99.9% 掉到 0%",  # 原文里没有这句
            }],
            "ignorable": [], "cannot_tell": [],
        })
        self.assertFalse(out["needs_attention"][0]["evidence_verified"])
        self.assertIn("evidence_problem", out["needs_attention"][0])
        self.assertEqual(out["verification"]["unverified"], 1)

    def test_记下喂了几条一共几条(self):
        out = self._run({"summary": "s", "needs_attention": [], "ignorable": [], "cannot_tell": []})
        self.assertEqual((out["findings_fed"], out["findings_total"]), (1, 1))


class TestInspectionViewShape(unittest.TestCase):
    """**后端一起来巡检页就白屏**那个 bug 的守护测试。

    `GET /api/inspection` 以前直接端原始 payload 出去，里面没有 `groups`，
    前端 `d.groups.map` 抛异常，而 React 没有错误边界，整个控制台一起白。
    分组逻辑当时只长在 `web/tools/export-fixtures.py` 里——
    "一份代码两个出口"这个坑的另一半。
    """

    def test_返回的形状必须带前端要的字段(self):
        view = dashboard.build_inspection_view(PAYLOAD)
        for key in ("scanned_hosts", "scanned_items", "items_with_data", "errors",
                    "finding_count", "groups", "report_markdown", "advice"):
            self.assertIn(key, view, f"巡检页要 {key}，少一个就是白屏")
        self.assertEqual(view["finding_count"], 1)
        self.assertTrue(view["groups"])


if __name__ == "__main__":
    unittest.main()
