"""判定脚本相关测试共用的样本和「按用户目标给的用例」（模型在真实样本上做文本替换得到的 previous / current）。"""
from __future__ import annotations

NL = "\n"
GONE = "S   203.0.113.0/25 [1/0] via 192.0.2.2" + NL

GOAL_TESTS = [
    {"name": "一条路由消失", "edits": [{"find": GONE, "replace": ""}], "expect": "fail"},
    {"name": "新增一条路由不报警",
     "edits": [{"find": GONE, "replace": GONE + "S   203.0.113.128/25 [1/0] via 192.0.2.2" + NL}], "expect": "pass"},
]
TESTS = GOAL_TESTS
