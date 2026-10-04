"""schema 的跨字段业务规则校验——strict json_schema 保证不了「字段之间逻辑
对不对」，这条规则在代码里再校一遍，测试确保它真的拦得住。"""

from __future__ import annotations

import unittest

from netops_ai.analysis.schema import (
    ANALYSIS_JSON_SCHEMA,
    CONTRADICTION_ABSENCE,
    CONTRADICTION_DIRECT,
    HYPOTHESIS_CATEGORIES,
    ROLE_CONSEQUENCE,
    ROLE_INDEPENDENT,
    ROLE_ROOT,
    SOURCE_DEVICE,
    SOURCE_MONITOR,
    STATUS_CANNOT_DETERMINE,
    STATUS_RULED_OUT,
    STATUS_SUPPORTED,
    TIME_CURRENT_SNAPSHOT,
    TIME_FAULT_WINDOW,
    TIME_INVARIANT,
    analysis_json_schema,
    check_business_rules,
    derive_confidence,
    normalize_enum,
)


EXPANDED_ANALYSIS_JSON_SCHEMA = analysis_json_schema(use_refs=False)


_VALID_COUNTER_EVIDENCE = [
    {
        "claim": "x",
        "source": "x",
        "source_from": SOURCE_MONITOR,
        "contradiction": CONTRADICTION_DIRECT,
        "time_relevance": TIME_FAULT_WINDOW,
    }
]


def _checklist(**overrides):
    """默认全部 ruled_out（带着合法的 counter_evidence），覆盖某几类改成别的状态。"""
    base = {
        cat: {"status": STATUS_RULED_OUT, "reason": "x", "counter_evidence": _VALID_COUNTER_EVIDENCE}
        for cat in HYPOTHESIS_CATEGORIES
    }
    for cat, status in overrides.items():
        counter_evidence = _VALID_COUNTER_EVIDENCE if status == STATUS_RULED_OUT else []
        base[cat] = {"status": status, "reason": "x", "counter_evidence": counter_evidence}
    return base


class TestSchemaShape(unittest.TestCase):
    def test_必填字段包含checklist和候选(self):
        required = ANALYSIS_JSON_SCHEMA["json_schema"]["schema"]["required"]
        props = ANALYSIS_JSON_SCHEMA["json_schema"]["schema"]["properties"]
        self.assertIn("undistinguishable_candidates", required)
        self.assertIn("hypothesis_checklist", required)
        self.assertIn("alert_roles", required)
        self.assertIn("grouping", required)
        self.assertNotIn("confidence", required)
        self.assertNotIn("confidence", props)

    def test_alert_roles要求固定三种角色(self):
        alert_role_schema = ANALYSIS_JSON_SCHEMA["json_schema"]["schema"]["properties"]["alert_roles"][
            "items"
        ]
        self.assertEqual(
            set(alert_role_schema["properties"]["role"]["enum"]),
            {ROLE_ROOT, ROLE_CONSEQUENCE, ROLE_INDEPENDENT},
        )
        self.assertIn("caused_by_eventid", alert_role_schema["required"])

    def test_grouping要求events和why_same(self):
        grouping_schema = ANALYSIS_JSON_SCHEMA["json_schema"]["schema"]["properties"]["grouping"]["items"]
        self.assertEqual(set(grouping_schema["required"]), {"events", "why_same"})
        self.assertFalse(grouping_schema["additionalProperties"])

    def test_checklist要求全部5类都必填(self):
        checklist_schema = ANALYSIS_JSON_SCHEMA["json_schema"]["schema"]["properties"][
            "hypothesis_checklist"
        ]
        self.assertEqual(set(checklist_schema["required"]), set(HYPOTHESIS_CATEGORIES))
        self.assertFalse(checklist_schema["additionalProperties"])
        # "本端人为shutdown"这类必须是固定字段之一，不能被漏掉
        self.assertIn("local_action", checklist_schema["properties"])

    def test_refs版本把checklist条目放进defs(self):
        schema = ANALYSIS_JSON_SCHEMA["json_schema"]["schema"]
        self.assertIn("$defs", schema)
        self.assertIn("hypothesis_item", schema["$defs"])
        checklist_props = schema["properties"]["hypothesis_checklist"]["properties"]
        for category, description in HYPOTHESIS_CATEGORIES.items():
            self.assertEqual(checklist_props[category]["$ref"], "#/$defs/hypothesis_item")
            self.assertEqual(checklist_props[category]["description"], description)

    def test_展开版本不使用refs(self):
        schema = EXPANDED_ANALYSIS_JSON_SCHEMA["json_schema"]["schema"]
        self.assertNotIn("$defs", schema)
        checklist_props = schema["properties"]["hypothesis_checklist"]["properties"]
        for category, description in HYPOTHESIS_CATEGORIES.items():
            self.assertNotIn("$ref", checklist_props[category])
            self.assertEqual(checklist_props[category]["description"], description)
            self.assertIn("counter_evidence", checklist_props[category]["properties"])

    def test_evidence条目要求source_from(self):
        item_required = ANALYSIS_JSON_SCHEMA["json_schema"]["schema"]["properties"]["evidence"][
            "items"
        ]["required"]
        self.assertIn("source_from", item_required)
        source_from_enum = ANALYSIS_JSON_SCHEMA["json_schema"]["schema"]["properties"]["evidence"][
            "items"
        ]["properties"]["source_from"]["enum"]
        self.assertEqual(set(source_from_enum), {SOURCE_MONITOR, SOURCE_DEVICE})

    def test_counter_evidence条目要求contradiction(self):
        # M2.4：拦"引用一句无关真话当反证"，加的必填枚举字段
        checklist_schema = EXPANDED_ANALYSIS_JSON_SCHEMA["json_schema"]["schema"]["properties"][
            "hypothesis_checklist"
        ]["properties"]["local_action"]["properties"]["counter_evidence"]["items"]
        self.assertIn("contradiction", checklist_schema["required"])
        self.assertEqual(set(checklist_schema["properties"]["contradiction"]["enum"]), {CONTRADICTION_DIRECT, CONTRADICTION_ABSENCE})

    def test_counter_evidence条目要求time_relevance(self):
        checklist_schema = EXPANDED_ANALYSIS_JSON_SCHEMA["json_schema"]["schema"]["properties"][
            "hypothesis_checklist"
        ]["properties"]["local_action"]["properties"]["counter_evidence"]["items"]
        self.assertIn("time_relevance", checklist_schema["required"])
        self.assertEqual(
            set(checklist_schema["properties"]["time_relevance"]["enum"]),
            {TIME_FAULT_WINDOW, TIME_CURRENT_SNAPSHOT, TIME_INVARIANT},
        )


class TestBusinessRules(unittest.TestCase):
    def test_旧英文枚举会规范成中文(self):
        self.assertEqual(normalize_enum("supported"), STATUS_SUPPORTED)
        self.assertEqual(normalize_enum("ruled_out"), STATUS_RULED_OUT)
        self.assertEqual(normalize_enum("agent_can_retry"), "智能体可继续取证")
        self.assertEqual(normalize_enum("已经是中文"), "已经是中文")

    def test_截图形状没有支持证据且候选非空时置信度是low(self):
        parsed = {
            "undistinguishable_candidates": [
                {"candidates": ["a", "b"], "why_indistinguishable": "x", "what_data_would_help": "y"}
            ],
            "hypothesis_checklist": _checklist(
                local_action=STATUS_CANNOT_DETERMINE,
                remote_or_upstream=STATUS_CANNOT_DETERMINE,
            ),
        }
        self.assertEqual(derive_confidence(parsed), "low")

    def test_唯一支持且其余排除时置信度是high(self):
        parsed = {
            "undistinguishable_candidates": [],
            "hypothesis_checklist": _checklist(local_action=STATUS_SUPPORTED),
        }
        self.assertEqual(derive_confidence(parsed), "high")

    def test_唯一支持但还有方向没排除时置信度是medium(self):
        parsed = {
            "undistinguishable_candidates": [],
            "hypothesis_checklist": _checklist(
                local_action=STATUS_SUPPORTED, remote_or_upstream=STATUS_CANNOT_DETERMINE
            ),
        }
        self.assertEqual(derive_confidence(parsed), "medium")

    def test_多个支持方向时置信度是low(self):
        parsed = {
            "undistinguishable_candidates": [],
            "hypothesis_checklist": _checklist(
                local_action=STATUS_SUPPORTED, remote_or_upstream=STATUS_SUPPORTED
            ),
        }
        self.assertEqual(derive_confidence(parsed), "low")

    def test_候选非空会把high封顶到medium(self):
        parsed = {
            "undistinguishable_candidates": [
                {"candidates": ["a", "b"], "why_indistinguishable": "x", "what_data_would_help": "y"}
            ],
            "hypothesis_checklist": _checklist(local_action=STATUS_SUPPORTED),
        }
        self.assertEqual(derive_confidence(parsed), "medium")

    def test_两个类别未排除但candidates是空的算违规(self):
        # 这条专门防上一轮那个问题：checklist 里明明有歧义，但 candidates 没写全/没写
        parsed = {
            "undistinguishable_candidates": [],
            "hypothesis_checklist": _checklist(
                local_action=STATUS_SUPPORTED, remote_or_upstream=STATUS_CANNOT_DETERMINE
            ),
        }
        violations = check_business_rules(parsed)
        self.assertTrue(any("候选根因" in v for v in violations))

    def test_只有一个类别未排除时high没问题(self):
        parsed = {
            "undistinguishable_candidates": [],
            "hypothesis_checklist": _checklist(local_action=STATUS_SUPPORTED),
        }
        self.assertEqual(check_business_rules(parsed), [])

    def test_ruled_out但counter_evidence为空算违规(self):
        # M2.3：v3-spike-2-A-run1 把唯一正确的 local_action 标成 ruled_out，
        # 理由只是"没看到证据"——这不是正面反证，必须拦住
        checklist = _checklist()
        checklist["local_action"] = {"status": STATUS_RULED_OUT, "reason": "没有变更记录", "counter_evidence": []}
        parsed = {
            "confidence": "high",
            "undistinguishable_candidates": [],
            "hypothesis_checklist": checklist,
        }
        violations = check_business_rules(parsed)
        self.assertTrue(any("本端有人动过配置" in v and "没给出任何反证" in v for v in violations))

    def test_ruled_out且counter_evidence非空没问题(self):
        parsed = {
            "confidence": "high",
            "undistinguishable_candidates": [],
            "hypothesis_checklist": _checklist(),
        }
        self.assertEqual(check_business_rules(parsed), [])

    def test_cannot_determine不需要counter_evidence(self):
        parsed = {
            "confidence": "medium",
            "undistinguishable_candidates": [],
            "hypothesis_checklist": _checklist(local_action=STATUS_CANNOT_DETERMINE),
        }
        self.assertEqual(check_business_rules(parsed), [])

    def test_contradiction为absence算违规(self):
        # M2.4 核心回归用例：two-round-live-m2.3.json（spike-1）round2 的真实
        # 失败模式——claim「故障发生时没有对应的配置变更日志」，source 逐字
        # 为真，但这是 absence，不是反证
        checklist = _checklist()
        checklist["local_action"] = {
            "status": STATUS_RULED_OUT,
            "reason": "没有配置变更日志",
            "counter_evidence": [
                {
                    "claim": "故障发生时没有对应的配置变更日志",
                    "source": "Sep 18 13:15:20 ... %SYS-5-CONFIG_I: Configured from console by console",
                    "source_from": SOURCE_DEVICE,
                    "contradiction": CONTRADICTION_ABSENCE,
                    "time_relevance": TIME_FAULT_WINDOW,
                }
            ],
        }
        parsed = {
            "confidence": "high",
            "undistinguishable_candidates": [],
            "hypothesis_checklist": checklist,
        }
        violations = check_business_rules(parsed)
        self.assertTrue(any("没发现相关记录" in v and "本端有人动过配置" in v for v in violations))

    def test_claim带否定词即使标direct也算违规(self):
        # 防止把 absence 硬标成 direct 蒙混过关的兜底检查
        checklist = _checklist()
        checklist["local_action"] = {
            "status": STATUS_RULED_OUT,
            "reason": "x",
            "counter_evidence": [
                {
                    "claim": "没有发现任何配置变更记录",
                    "source": "x",
                    "source_from": SOURCE_DEVICE,
                    "contradiction": CONTRADICTION_DIRECT,
                    "time_relevance": TIME_FAULT_WINDOW,
                }
            ],
        }
        parsed = {
            "confidence": "high",
            "undistinguishable_candidates": [],
            "hypothesis_checklist": checklist,
        }
        violations = check_business_rules(parsed)
        self.assertTrue(any("没找到证据" in v and "本端有人动过配置" in v for v in violations))

    def test_direct且claim不带否定词没问题(self):
        checklist = _checklist()
        checklist["local_action"] = {
            "status": STATUS_RULED_OUT,
            "reason": "x",
            "counter_evidence": [
                {
                    "claim": "接口是本端 administratively down，跟对端故障直接矛盾",
                    "source": "GigabitEthernet0/0 is administratively down",
                    "source_from": SOURCE_DEVICE,
                    "contradiction": CONTRADICTION_DIRECT,
                    "time_relevance": TIME_FAULT_WINDOW,
                }
            ],
        }
        parsed = {
            "confidence": "high",
            "undistinguishable_candidates": [],
            "hypothesis_checklist": checklist,
        }
        self.assertEqual(check_business_rules(parsed), [])

    def test_current_snapshot_direct且故障已结束算违规(self):
        checklist = _checklist()
        checklist["local_action"] = {
            "status": STATUS_RULED_OUT,
            "reason": "当前接口 up",
            "counter_evidence": [
                {
                    "claim": "Gi0/0 接口当前处于正常 up 状态，接口没有被人为关闭",
                    "source": "GigabitEthernet0/0 is up, line protocol is up",
                    "source_from": SOURCE_DEVICE,
                    "contradiction": CONTRADICTION_DIRECT,
                    "time_relevance": TIME_CURRENT_SNAPSHOT,
                }
            ],
        }
        parsed = {
            "confidence": "high",
            "undistinguishable_candidates": [],
            "hypothesis_checklist": checklist,
        }
        source_text = '"clock": "1789694011"\n01:13:31 -> 0\n01:36:31 -> 0'
        violations = check_business_rules(parsed, source_text)
        self.assertTrue(any("当前快照" in v and "本端有人动过配置" in v for v in violations))

    def test_fault_window_direct且故障已结束不因时间相关性违规(self):
        checklist = _checklist()
        checklist["local_action"] = {
            "status": STATUS_RULED_OUT,
            "reason": "故障窗口日志直接排除",
            "counter_evidence": [
                {
                    "claim": "故障窗口内接口持续 up",
                    "source": "01:13:31 Gi0/0 line protocol is up",
                    "source_from": SOURCE_MONITOR,
                    "contradiction": CONTRADICTION_DIRECT,
                    "time_relevance": TIME_FAULT_WINDOW,
                }
            ],
        }
        parsed = {
            "confidence": "high",
            "undistinguishable_candidates": [],
            "hypothesis_checklist": checklist,
        }
        source_text = '"clock": "1789694011"\n01:13:31 -> 0\n01:36:31 -> 0'
        self.assertEqual(check_business_rules(parsed, source_text), [])

    def test_current_snapshot_direct但故障仍在进行不因时间相关性违规(self):
        checklist = _checklist()
        checklist["local_action"] = {
            "status": STATUS_RULED_OUT,
            "reason": "当前接口 up",
            "counter_evidence": [
                {
                    "claim": "Gi0/0 接口当前处于正常 up 状态，接口仍为启用状态",
                    "source": "GigabitEthernet0/0 is up, line protocol is up",
                    "source_from": SOURCE_DEVICE,
                    "contradiction": CONTRADICTION_DIRECT,
                    "time_relevance": TIME_CURRENT_SNAPSHOT,
                }
            ],
        }
        parsed = {
            "confidence": "high",
            "undistinguishable_candidates": [],
            "hypothesis_checklist": checklist,
        }
        source_text = '"r_eventid": "0"\n"c_eventid": "0"\n01:13:31 -> 0\n01:14:31 -> 0'
        self.assertEqual(check_business_rules(parsed, source_text), [])

    def test_三条协议级联告警角色和分组正例(self):
        parsed = {
            "confidence": "high",
            "undistinguishable_candidates": [],
            "hypothesis_checklist": _checklist(local_action=STATUS_SUPPORTED),
            "alert_roles": [
                {
                    "eventid": "E1",
                    "role": ROLE_ROOT,
                    "reason": "接口先 down，是后续协议邻居中断的触发点",
                    "caused_by_eventid": "",
                },
                {
                    "eventid": "E2",
                    "role": ROLE_CONSEQUENCE,
                    "reason": "OSPF 邻居依赖该接口承载，接口 down 后邻居 down",
                    "caused_by_eventid": "E1",
                },
                {
                    "eventid": "E3",
                    "role": ROLE_CONSEQUENCE,
                    "reason": "BGP 会话依赖底层连通性，接口 down 后会话中断",
                    "caused_by_eventid": "E1",
                },
            ],
            "grouping": [{"events": ["E1", "E2", "E3"], "why_same": "同一接口故障引发的协议级联"}],
        }
        self.assertEqual(check_business_rules(parsed), [])

    def test_consequence但caused_by_eventid为空算违规(self):
        parsed = {
            "confidence": "high",
            "undistinguishable_candidates": [],
            "hypothesis_checklist": _checklist(local_action=STATUS_SUPPORTED),
            "alert_roles": [
                {
                    "eventid": "E1",
                    "role": ROLE_ROOT,
                    "reason": "接口 down",
                    "caused_by_eventid": "",
                },
                {
                    "eventid": "E2",
                    "role": ROLE_CONSEQUENCE,
                    "reason": "OSPF down 是连带结果",
                    "caused_by_eventid": "",
                },
            ],
            "grouping": [{"events": ["E1", "E2"], "why_same": "同一故障"}],
        }
        violations = check_business_rules(parsed)
        self.assertTrue(any("被标成「连带」" in v and "没指出" in v for v in violations))

    def test_caused_by_eventid指向候选集合外算违规(self):
        parsed = {
            "confidence": "high",
            "undistinguishable_candidates": [],
            "hypothesis_checklist": _checklist(local_action=STATUS_SUPPORTED),
            "alert_roles": [
                {
                    "eventid": "E1",
                    "role": ROLE_ROOT,
                    "reason": "接口 down",
                    "caused_by_eventid": "",
                },
                {
                    "eventid": "E2",
                    "role": ROLE_CONSEQUENCE,
                    "reason": "OSPF down 是连带结果",
                    "caused_by_eventid": "E404",
                },
            ],
            "grouping": [{"events": ["E1", "E2"], "why_same": "同一故障"}],
        }
        violations = check_business_rules(parsed)
        self.assertTrue(any("不在本次候选告警里" in v for v in violations))

    def test_grouping漏掉候选eventid算违规(self):
        parsed = {
            "confidence": "high",
            "undistinguishable_candidates": [],
            "hypothesis_checklist": _checklist(local_action=STATUS_SUPPORTED),
            "alert_roles": [
                {
                    "eventid": "E1",
                    "role": ROLE_ROOT,
                    "reason": "接口 down",
                    "caused_by_eventid": "",
                },
                {
                    "eventid": "E2",
                    "role": ROLE_CONSEQUENCE,
                    "reason": "OSPF down 是连带结果",
                    "caused_by_eventid": "E1",
                },
            ],
            "grouping": [{"events": ["E1"], "why_same": "漏写了 E2"}],
        }
        violations = check_business_rules(parsed)
        self.assertTrue(any("必须刚好覆盖" in v and "E2" in v for v in violations))

    def test_单条告警也能填写角色和分组(self):
        parsed = {
            "confidence": "high",
            "undistinguishable_candidates": [],
            "hypothesis_checklist": _checklist(local_action=STATUS_SUPPORTED),
            "alert_roles": [
                {
                    "eventid": "E1",
                    "role": ROLE_ROOT,
                    "reason": "只有一条候选，作为本次分析的根告警",
                    "caused_by_eventid": "",
                }
            ],
            "grouping": [{"events": ["E1"], "why_same": "单条候选告警单独构成一次 incident"}],
        }
        self.assertEqual(check_business_rules(parsed), [])


class TestBusinessRuleMessages(unittest.TestCase):
    FORBIDDEN_FIELD_NAMES = (
        "alert_roles",
        "caused_by_eventid",
        "hypothesis_checklist",
        "undistinguishable_candidates",
        "counter_evidence",
        "related_alerts",
        "contradiction",
        "time_relevance",
    )

    def _assert_messages_clean(self, parsed, source_text=None):
        violations = check_business_rules(parsed, source_text)
        self.assertTrue(violations)
        for message in violations:
            for field_name in self.FORBIDDEN_FIELD_NAMES:
                self.assertNotIn(field_name, message)
            self.assertNotRegex(message, r"\[\d+\]")

    def test_所有业务规则消息都不透出字段名(self):
        checklist_no_evidence = _checklist()
        checklist_no_evidence["local_action"] = {
            "status": STATUS_RULED_OUT,
            "reason": "x",
            "counter_evidence": [],
        }
        self._assert_messages_clean({
            "undistinguishable_candidates": [],
            "hypothesis_checklist": checklist_no_evidence,
        })

        checklist_absence = _checklist()
        checklist_absence["local_action"] = {
            "status": STATUS_RULED_OUT,
            "reason": "x",
            "counter_evidence": [{
                "claim": "故障发生时没有对应的配置变更日志",
                "source": "x",
                "source_from": SOURCE_DEVICE,
                "contradiction": CONTRADICTION_ABSENCE,
                "time_relevance": TIME_FAULT_WINDOW,
            }],
        }
        self._assert_messages_clean({
            "undistinguishable_candidates": [],
            "hypothesis_checklist": checklist_absence,
        })

        checklist_snapshot = _checklist()
        checklist_snapshot["local_action"] = {
            "status": STATUS_RULED_OUT,
            "reason": "x",
            "counter_evidence": [{
                "claim": "接口当前 up",
                "source": "x",
                "source_from": SOURCE_DEVICE,
                "contradiction": CONTRADICTION_DIRECT,
                "time_relevance": TIME_CURRENT_SNAPSHOT,
            }],
        }
        self._assert_messages_clean({
            "undistinguishable_candidates": [],
            "hypothesis_checklist": checklist_snapshot,
        }, '"clock": "1789694011"\n01:13:31 -> 0\n01:36:31 -> 0')

        checklist_ambiguity = _checklist(
            local_action=STATUS_SUPPORTED,
            remote_or_upstream=STATUS_CANNOT_DETERMINE,
        )
        self._assert_messages_clean({
            "undistinguishable_candidates": [],
            "hypothesis_checklist": checklist_ambiguity,
        })

        base = {
            "undistinguishable_candidates": [],
            "hypothesis_checklist": _checklist(local_action=STATUS_SUPPORTED),
        }
        self._assert_messages_clean({
            **base,
            "alert_roles": [
                {"eventid": "E1", "role": ROLE_ROOT, "reason": "x", "caused_by_eventid": ""},
                {"eventid": "E2", "role": ROLE_CONSEQUENCE, "reason": "x", "caused_by_eventid": ""},
            ],
            "grouping": [{"events": ["E1", "E2"], "why_same": "x"}],
        })
        self._assert_messages_clean({
            **base,
            "alert_roles": [
                {"eventid": "E1", "role": ROLE_ROOT, "reason": "x", "caused_by_eventid": ""},
                {"eventid": "E2", "role": ROLE_CONSEQUENCE, "reason": "x", "caused_by_eventid": "E404"},
            ],
            "grouping": [{"events": ["E1", "E2"], "why_same": "x"}],
        })
        self._assert_messages_clean({
            **base,
            "alert_roles": [
                {"eventid": "E1", "role": ROLE_ROOT, "reason": "x", "caused_by_eventid": ""},
                {"eventid": "E2", "role": ROLE_CONSEQUENCE, "reason": "x", "caused_by_eventid": "E1"},
            ],
            "grouping": [{"events": ["E1"], "why_same": "x"}],
        })
        self._assert_messages_clean({
            **base,
            "alert_roles": [{"eventid": "E1", "role": ROLE_ROOT, "reason": "x", "caused_by_eventid": ""}],
            "related_alerts": [{"eventid": "E1", "host": "A1", "link": "x", "reason": "x"}],
        }, "E1")
        self._assert_messages_clean({
            **base,
            "alert_roles": [{"eventid": "E1", "role": ROLE_ROOT, "reason": "x", "caused_by_eventid": ""}],
            "related_alerts": [{"eventid": "E404", "host": "A1", "link": "x", "reason": "x"}],
        }, "E1")


if __name__ == "__main__":
    unittest.main()


class TestNegationMarkersOnlyCatchAbsence(unittest.TestCase):
    """否定词规则只拦「没找到证据」，不拦推理用词。维护者拍板。

 下面三句是 syslog 修通后模型第一次拿到铁证时写的，原词表每次都拦，
 但它们都是拿正面的直接证据排除方向，推理是对的。
    """

    TODAY = [
        "日志显示状态为 administratively down，排除了纯硬件物理失效导致的非管理性 down。",
        "接口状态为本端管理性关闭，对端无法触发此状态。",
        "接口是被管理性关闭的，而非因质量差自动保护性关闭（后者通常有不同日志或状态描述）。",
    ]

    def _violations(self, claim, contradiction=CONTRADICTION_DIRECT):
        checklist = _checklist()
        checklist["local_hardware_or_resource"] = {
            "status": STATUS_RULED_OUT,
            "reason": "x",
            "counter_evidence": [{
                "claim": claim,
                "source": "Interface Ethernet0/1, changed state to administratively down",
                "source_from": SOURCE_DEVICE,
                "contradiction": contradiction,
                "time_relevance": TIME_FAULT_WINDOW,
            }],
        }
        parsed = {"confidence": "high", "undistinguishable_candidates": [], "hypothesis_checklist": checklist}
        return check_business_rules(parsed)

    def test_拿正面证据排除方向的推理不再被拦(self):
        for claim in self.TODAY:
            self.assertEqual(self._violations(claim), [], claim)

    def test_没找到证据硬标direct照样拦(self):
        # **这是没选「标了直接反证就不查」的原因。** 反证类型是模型自己标的，可以撒谎。
        for claim in ("没有发现任何配置变更记录", "日志里未见相关告警", "找不到对端的链路异常记录"):
            self.assertTrue(self._violations(claim), claim)


class TestRelatedAlerts(unittest.TestCase):
    """A9：同一件事在拓扑相邻的另一台设备上报的告警，由模型点名，代码核它真查到过。"""

    _REL = {"eventid": "93557", "host": "A1", "link": "D1 Ethernet1/0 ↔ A1 Ethernet0/1", "reason": "同一条链路两端"}

    def _v(self, related, source="zbx_problems 返回 eventid 93557 A1-viosl2 Interface Gi0/1 down"):
        parsed = {
            "confidence": "high",
            "hypothesis_checklist": _checklist(),
            "undistinguishable_candidates": [],
            "alert_roles": [{"eventid": "93555", "role": ROLE_ROOT, "reason": "x", "caused_by_eventid": ""}],
            "related_alerts": related,
        }
        return check_business_rules(parsed, source)

    def test_必填(self):
        self.assertIn("related_alerts", ANALYSIS_JSON_SCHEMA["json_schema"]["schema"]["required"])

    def test_原文里查到过的通过(self):
        self.assertEqual(self._v([self._REL]), [])

    def test_原文里没有的算编造(self):
        self.assertTrue(self._v([{**self._REL, "eventid": "99999"}]))

    def test_本次候选不许放进关联(self):
        self.assertTrue(self._v([{**self._REL, "eventid": "93555"}], source="93555"))


class TestManagementPlaneCategory(unittest.TestCase):
    """#17：D1 管理面全断、邻居看它 OSPF 全程 FULL，五类里没有一个框能放这件事。

 模型只能在「对端/上游故障」和「本端硬件故障」之间打转，最后 low confidence。
 那不是模型笨，是分类缺了一类。
    """

    def test_六类里有管理通道这一类(self):
        self.assertIn("management_plane_or_reachability", HYPOTHESIS_CATEGORIES)
        self.assertIn("management_plane_or_reachability", ANALYSIS_JSON_SCHEMA["json_schema"]["schema"]
                      ["properties"]["hypothesis_checklist"]["required"])

    def test_这一类的说明要写清判据在邻居那里(self):
        self.assertIn("邻居", HYPOTHESIS_CATEGORIES["management_plane_or_reachability"])

    def test_跟采集器坏了那类分得开(self):
        mgmt = HYPOTHESIS_CATEGORIES["management_plane_or_reachability"]
        collect = HYPOTHESIS_CATEGORIES["monitoring_or_collection_artifact"]
        self.assertNotEqual(mgmt, collect)
        self.assertIn("不止这一台", collect)


class TestGroupingDescription(unittest.TestCase):
    """`grouping` 的描述必须明说「不相关的要拆开」。

 实测：原来的描述只强调「所有 eventid 必须被覆盖」「单告警也要填一组」，
 一个字没说不相关的该拆。拿两条真正无关的告警（D1 接口 down + Zabbix server 网卡降速）
 做真实模型调用，它把两条塞进同一组，却在 `why_same` 里写
 「视为两个独立的、信息缺失的事件记录，暂不合并」——**文字和结构对不上**。
 补上这两句之后同样的输入拆成了 2 组，而四条真正相关的 BGP 告警仍然合成 1 组。

 这条测试锁的是描述里的关键语义，不是措辞。改描述可以，但这几个意思不能丢。
    """

    def test_描述里必须说清不相关的要拆成多组(self):
        schema = analysis_json_schema(use_refs=False)
        grouping = schema["json_schema"]["schema"]["properties"]["grouping"]
        desc = grouping["description"]
        self.assertIn("一组 = 一次故障", desc)
        self.assertIn("互不相关", desc)
        self.assertIn("拆成多组", desc)
        # 保守方向：拿不准要拆，不要合
        self.assertIn("拆开", desc)

    def test_why_same_的描述要拦住在里面写暂不合并(self):
        schema = analysis_json_schema(use_refs=False)
        why = schema["json_schema"]["schema"]["properties"]["grouping"]["items"]["properties"]["why_same"]
        self.assertIn("不要放在同一组", why["description"])


class Test根因不许外泄字段名(unittest.TestCase):
    """维护者 「不要透出我们代码里设定的变量」。

 200 条历史记录里，28 条的 root_cause 直接写着 undistinguishable_candidates，
 23 条写着 remote_or_upstream——**是 schema 自己要求模型这么写的**。
 这句话会发到飞书给网工看，字段名对他毫无意义。
    """

    def _root_cause_desc(self) -> str:
        props = ANALYSIS_JSON_SCHEMA["json_schema"]["schema"]["properties"]
        return props["root_cause"]["description"]

    def test_不再要求模型写指向内部结构的话(self):
        """旧描述是**指示**模型写「见 undistinguishable_candidates」。
        现在这串字只能出现在禁令里，不能出现在示范句里。"""
        desc = self._root_cause_desc()
        self.assertNotIn("写成类似", desc)
        self.assertIn("不许写「见 undistinguishable_candidates」", desc)

    def test_明确禁止抄字段名并给了中文对照(self):
        desc = self._root_cause_desc()
        self.assertIn("不许", desc)
        self.assertIn("本端有人动过配置", desc)   # local_action 的中文说法
        self.assertIn("对端或上游", desc)         # remote_or_upstream


class Testheadline描述带实测反例(unittest.TestCase):
    """实测 28/28/38 字且都以设备名开头。
 原描述只说「不要带设备名」，压不住——补一个具体反例。
    """

    def test_描述里有反例和正例(self):
        desc = ANALYSIS_JSON_SCHEMA["json_schema"]["schema"]["properties"]["headline"]["description"]
        self.assertIn("不要以设备名开头", desc)
        self.assertIn("A1设备Ethernet0/1接口被人为执行shutdown", desc)  # 反例
        self.assertIn("人为 shutdown 导致链路中断", desc)                # 正例


class Test故障已结束却没有故障窗口证据(unittest.TestCase):
    """真实误判（V2-vios Gi0/3，OSPF + 3 条 BGP 同一时刻 down、0 秒自愈）
 暴露的缺口：`time_relevance` 原来只有反证那边有，证据链这边没这个字段，
 规则看不见「这条是现在才查到的」。

 **这条只抓最糟的那种**：整条证据链一条故障窗口的都没有，却照样下结论。
    """

    def _parsed(self, evidence):
        return {"root_cause": "x", "evidence": evidence,
                "hypothesis_checklist": {}, "undistinguishable_candidates": []}

    def _fired(self, evidence, source="告警已恢复"):
        return [v for v in check_business_rules(self._parsed(evidence), source)
                if "没有一条来自故障窗口" in v]

    def test_全是当前快照要报(self):
        ev = [{"claim": "接口现在 up", "source": "Gi0/3 is up", "source_from": "设备",
               "time_relevance": "当前快照"}]
        self.assertTrue(self._fired(ev))

    def test_有一条故障窗口就不报(self):
        ev = [{"claim": "接口现在 up", "source": "Gi0/3 is up", "source_from": "设备",
               "time_relevance": "当前快照"},
              {"claim": "故障时刻丢包", "source": "icmppingloss=100", "source_from": "监控",
               "time_relevance": "故障窗口"}]
        self.assertFalse(self._fired(ev))

    def test_故障还没结束就不报(self):
        """还在故障中的时候，当前快照就是故障时刻的状态，没毛病。

        `_fault_is_probably_ended()` **默认返回 True**（看不出来就当已结束，
        宁可多要一次故障窗口证据）。要判成"还在故障中"必须给它明确信号——
        `"r_eventid": "0"` 就是 Zabbix 对未恢复问题的真实返回。

        顺带：原文里带「恢复」二字一律算已结束，「仍未恢复」也会中招。
        那同样是有意的保守，不是 bug。
        """
        ev = [{"claim": "接口 down", "source": "Gi0/3 is down", "source_from": "设备",
               "time_relevance": "当前快照"}]
        self.assertFalse(self._fired(ev, source='{"eventid": "9", "r_eventid": "0"}'))

    def test_老记录没这个字段就不报(self):
        """之前的记录没有 time_relevance，不能拿新字段去卡旧数据。"""
        ev = [{"claim": "c", "source": "s", "source_from": "监控"}]
        self.assertFalse(self._fired(ev))


class Test同一条规则在多方向上犯只说一次(unittest.TestCase):
    """真实反例（一条真实告警）：六个方向全标已排除、一条反证都没给，
 卡片上刷出六行一模一样的话，只有方向名不同。维护者：「对不上的地方说的什么鬼」。

 违规是给值班的人看的提醒，不是清单。同一条规则在多个方向上犯，只说一次。
    """

    def _parsed(self, checklist):
        return {"root_cause": "接口被人为 shutdown", "hypothesis_checklist": checklist,
                "undistinguishable_candidates": [],
                "evidence": [{"claim": "c", "source": "s", "source_from": "设备",
                              "time_relevance": "故障窗口"}]}

    def test_六个方向全排除没反证只报一条(self):
        cl = {c: {"status": STATUS_RULED_OUT, "reason": "r", "counter_evidence": []}
              for c in HYPOTHESIS_CATEGORIES}
        v = check_business_rules(self._parsed(cl), "已恢复")
        self.assertEqual(len(v), 1, v)
        self.assertIn("全标成已排除", v[0])
        self.assertIn("排查过程是空的", v[0])

    def test_只有两个方向犯就点名那两个(self):
        cl = {c: {"status": STATUS_RULED_OUT, "reason": "r",
                  "counter_evidence": _VALID_COUNTER_EVIDENCE} for c in HYPOTHESIS_CATEGORIES}
        cl["local_action"] = {"status": STATUS_SUPPORTED, "reason": "r", "counter_evidence": []}
        for cat in ("remote_or_upstream", "link_or_path_quality"):
            cl[cat] = {"status": STATUS_RULED_OUT, "reason": "r", "counter_evidence": []}
        v = [x for x in check_business_rules(self._parsed(cl), "已恢复") if "没给出任何反证" in x]
        self.assertEqual(len(v), 1, v)
        self.assertIn("对端或上游", v[0])
        self.assertIn("链路本身或路径质量", v[0])

    def test_全排除时不再另报一条没有方向支持(self):
        """两条说的是同一件事，全排除那条更具体，让它盖住另一条。"""
        cl = {c: {"status": STATUS_RULED_OUT, "reason": "r", "counter_evidence": []}
              for c in HYPOTHESIS_CATEGORIES}
        v = check_business_rules(self._parsed(cl), "已恢复")
        self.assertFalse([x for x in v if "没有任何一条排查结论支持" in x], v)
