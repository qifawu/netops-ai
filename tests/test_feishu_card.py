"""飞书卡片渲染（M3 加的）：纯函数，重点测两件事——置信度决定卡片形态、
措辞红线（不许出现"系统将执行"这类话）。"""

from __future__ import annotations

import json
import unittest

from netops_ai.feishu.card import build_card, build_inspection_card, build_receipt_card
from netops_ai.feishu.card import AlertGroup
from netops_ai.inspection.scan import ItemFinding, ScanReport
from netops_ai.inspection.detectors import SelfHealingFlapFinding


def _analysis(confidence="high", root_cause="x", evidence=None, candidates=None) -> dict:
    return {
        "root_cause": root_cause,
        "confidence": confidence,
        "evidence": evidence or [],
        "undistinguishable_candidates": candidates or [],
    }


class TestBuildCard(unittest.TestCase):
    def test_high置信度用绿色模板(self):
        card = build_card({"eventids": ["1"], "analysis": _analysis(confidence="high")})
        self.assertEqual(card["header"]["template"], "green")

    def test_low置信度用橙色模板(self):
        """置信度只管配色和那行说明，**不决定标题说不说「没判出根因」**。

 改的：原来这条还断言标题里有「没判出根因」，那是 bug
 的来源——置信度低不等于判不出来。见 `Test判没判出来不看置信度`。
        """
        card = build_card({"eventids": ["1"], "analysis": _analysis(confidence="low")})
        self.assertEqual(card["header"]["template"], "orange")

    def test_high置信度标题是根因本身(self):
        card = build_card({"eventids": ["1"], "analysis": _analysis(confidence="high", root_cause="Gi0/1 被人为 shutdown")})
        self.assertIn("Gi0/1 被人为 shutdown", card["header"]["title"]["content"])

    def test_接受AlertGroup对象不只是dict(self):
        group = AlertGroup(eventids=["1", "2"], analysis=_analysis(confidence="high"))
        card = build_card(group)
        self.assertIn("合并 2 条", card["header"]["title"]["content"])

    def test_合并数量体现在标题里(self):
        card = build_card({"eventids": ["1", "2", "3"], "analysis": _analysis(confidence="high")})
        self.assertIn("合并 3 条", card["header"]["title"]["content"])

    def test_单条不显示合并数量(self):
        card = build_card({"eventids": ["1"], "analysis": _analysis(confidence="high")})
        self.assertNotIn("合并", card["header"]["title"]["content"])

    def test_undistinguishable_candidates非空时出现在卡片里(self):
        candidates = [{"candidates": ["a", "b"], "why_indistinguishable": "w", "what_data_would_help": "d"}]
        card = build_card({"eventids": ["1"], "analysis": _analysis(confidence="low", candidates=candidates)})
        card_text = json.dumps(card, ensure_ascii=False)
        # 改版：小标题从「判不出的候选方向（AI 承认没法区分）」
        # 改成一句人话，说的是同一件事
        self.assertIn("这几个方向现有数据分不出来", card_text)
        self.assertIn("w", card_text)

    def test_建议动作不许出现系统将执行类措辞(self):
        # 红线：这个项目根本不做配置下发，卡片上只能是"建议人做什么"
        forbidden = ["系统将执行", "已自动处理", "自动下发", "系统已处理", "自动执行"]
        for confidence in ("high", "medium", "low"):
            card = build_card({"eventids": ["1"], "analysis": _analysis(confidence=confidence)})
            card_text = json.dumps(card, ensure_ascii=False)
            for word in forbidden:
                self.assertNotIn(word, card_text)

    def test_建议动作出现建议二字(self):
        card = build_card({"eventids": ["1"], "analysis": _analysis(confidence="high")})
        card_text = json.dumps(card, ensure_ascii=False)
        self.assertIn("建议", card_text)

    def test_eventid写进备注(self):
        card = build_card({"eventids": ["1001", "1002"], "analysis": _analysis()})
        card_text = json.dumps(card, ensure_ascii=False)
        self.assertIn("1001", card_text)
        self.assertIn("1002", card_text)

    def test_合并卡片列出每条eventid和告警名(self):
        group = AlertGroup(
            eventids=["1001", "1002"],
            alert_names={"1001": "Gi0/1 Link down", "1002": "Unavailable by ICMP ping"},
            analysis=_analysis(),
            summary="这段时间还有 1 条相关告警，已合并到首报事件链。",
        )
        card = build_card(group)
        card_text = json.dumps(card, ensure_ascii=False)
        # 改版后这块挪到卡片最前面当前提，措辞也换了：先说清这次是哪几条告警，
        # 再讲查到了什么、所以是什么
        self.assertIn("这次合并了 2 条告警", card_text)
        self.assertIn("Gi0/1 Link down", card_text)
        self.assertIn("Unavailable by ICMP ping", card_text)
        self.assertIn("这段时间还有 1 条相关告警", card_text)

    def test_只有fabricated证据进入站不住(self):
        group = AlertGroup(
            eventids=["1"],
            analysis=_analysis(evidence=[{"claim": "a", "source": "b", "source_from": "zabbix"}]),
            evidence_verification={
                "summary": {
                    "total": 2,
                    "verified": 1,
                    "unverified": 1,
                    "reformatted": 1,
                    "fabricated": 1,
                },
                "details": [
                    {"index": 0, "verified": True, "grade": "reformatted", "reason": "格式能对上"},
                    {"index": 1, "verified": False, "grade": "fabricated", "reason": "这段引文在原文里找不到"},
                ],
            },
        )
        card_text = json.dumps(build_card(group), ensure_ascii=False)
        self.assertIn("需要核实", card_text)
        self.assertIn("第 2 条证据", card_text)
        self.assertIn("引文和日志原文对不上", card_text)
        self.assertIn("1 条引文格式跟原文有出入，内容能对上", card_text)
        self.assertNotIn("格式能对上", card_text)


class TestBuildReceiptCard(unittest.TestCase):
    def test_回执卡只包含收到和稍后结论(self):
        card = build_receipt_card(eventid="1001", name="Gi0/1 Link down")
        card_text = json.dumps(card, ensure_ascii=False)
        self.assertIn("收到告警", card_text)
        self.assertIn("正在分析", card_text)
        self.assertIn("可能还有相关告警", card_text)
        self.assertIn("结论稍后", card_text)
        self.assertIn("1001", card_text)
        self.assertIn("Gi0/1 Link down", card_text)

    def test_回执卡不含分析结论字段和措辞红线(self):
        card = build_receipt_card(eventid="1001", name="Gi0/1 Link down")
        card_text = json.dumps(card, ensure_ascii=False)
        forbidden = [
            "root_cause",
            "confidence",
            "证据链",
            "根因",
            "置信度",
            "疑似",
            "系统将执行",
            "已自动处理",
            "自动下发",
            "系统已处理",
            "自动执行",
        ]
        for word in forbidden:
            self.assertNotIn(word, card_text)


class TestBuildInspectionCard(unittest.TestCase):
    def test_没有发现时是绿色且说一切正常(self):
        report = ScanReport(scanned_hosts=1, scanned_items=5, items_with_data=5, findings=[])
        card = build_inspection_card(report)
        self.assertEqual(card["header"]["template"], "green")
        self.assertIn("一切正常", card["header"]["title"]["content"])

    def test_有发现时是橙色且列出每一条(self):
        finding = SelfHealingFlapFinding(
            kind="self_healing_flap", flap_count=3, avg_recovery_seconds=300, max_recovery_seconds=600,
            reason="3 次自愈",
        )
        report = ScanReport(
            scanned_hosts=1, scanned_items=1, items_with_data=1,
            findings=[ItemFinding(host_name="V1", item_name="Gi0/1", item_key="net.if.status[x]", finding=finding)],
        )
        card = build_inspection_card(report)
        self.assertEqual(card["header"]["template"], "orange")
        card_text = json.dumps(card, ensure_ascii=False)
        self.assertIn("反复抖动又自愈", card_text)
        self.assertIn("V1", card_text)
        self.assertIn("3 次自愈", card_text)


class TestHeadlineDoesNotCutWordsInHalf(unittest.TestCase):
    """真实发出去的卡片标题长这样（截图为证）：

 本端设备接口 GigabitEthernet0/1 被人为执行了 shutdow（合并 5 条同类告警）

 正文里是完整的 `shutdown`，标题少了个 n——以前是 `root_cause[:40]`，
 硬截 40 个字符，从单词中间切开，连省略号都没有。
    """

    def test_the_exact_string_that_produced_shutdow(self):
        from netops_ai.feishu.card import _headline

        root_cause = "本端设备接口 GigabitEthernet0/1 被人为执行了 shutdown 操作（管理状态变更为关闭）"
        headline = _headline(root_cause)
        self.assertNotIn("shutdow（", headline)
        self.assertFalse(headline.rstrip("…").endswith("shutdow"), f"又切了半个词：{headline}")
        self.assertTrue(headline.endswith("…"), "截断了就要有省略号")

    def test_english_words_are_never_split(self):
        from netops_ai.feishu.card import _headline

        for word in ("shutdown", "established", "GigabitEthernet0/1", "administratively"):
            root_cause = "接口状态异常，原因是本端被设置为 " + word + " 并且持续了很长时间需要人工核实"
            headline = _headline(root_cause).rstrip("…")
            with self.subTest(word=word):
                tail = headline.rsplit(" ", 1)[-1]
                self.assertTrue(
                    tail == "" or word.startswith(tail) is False or tail == word,
                    f"{word} 被切成了 {tail!r}",
                )

    def test_short_root_cause_is_untouched(self):
        from netops_ai.feishu.card import _headline

        self.assertEqual(_headline("接口没有被 shutdown，根因在对端设备"), "接口没有被 shutdown，根因在对端设备")

    def test_whitespace_is_normalised_so_the_header_stays_one_line(self):
        from netops_ai.feishu.card import _headline

        self.assertEqual(_headline("根因\n在\t对端"), "根因 在 对端")

    def test_headline_never_exceeds_the_card_header_width(self):
        from netops_ai.feishu.card import _HEADLINE_LIMIT, _headline

        long_text = "本端设备接口 GigabitEthernet0/1 被人为执行了 shutdown 操作导致级联告警" * 3
        self.assertLessEqual(len(_headline(long_text)), _HEADLINE_LIMIT + 1)

    def test_empty_root_cause_does_not_crash(self):
        from netops_ai.feishu.card import _headline

        self.assertEqual(_headline(""), "")


if __name__ == "__main__":
    unittest.main()


class TestRelatedAlertsOnCard(unittest.TestCase):
    def test_关联告警紧跟根因(self):
        a = _analysis(confidence="high")
        a["related_alerts"] = [{"eventid": "93557", "host": "A1", "link": "D1 Ethernet1/0 ↔ A1 Ethernet0/1", "reason": "同一条链路两端"}]
        card = build_card({"eventids": ["93555"], "analysis": a})
        texts = [e["text"]["content"] for e in card["elements"] if e.get("tag") == "div"]
        # **改版后关联告警是前提，不是结论的附注。** 卡片按「这次是哪几条告警 →
        # 查到了什么 → 所以是什么」排，所以它排在最前面，结论在它后面。
        self.assertIn("#93557", texts[0])
        self.assertIn("D1 Ethernet1/0 ↔ A1 Ethernet0/1", texts[0])
        self.assertIn("结论", texts[2])


class Test卡片规范化输出(unittest.TestCase):
    """维护者 「飞书的告警也很乱，你不能规范下 parsed output 吗」。
 下面四条锁住那次改动，别再退回去。
    """

    def _group(self, **kw):
        base = dict(
            eventids=["101411"],
            host="V1-vios",
            analysis={
                "headline": "人为 shutdown 导致链路中断",
                "root_cause": "本端管理员在控制台执行了配置变更，将接口 Ethernet0/1 的管理状态从 up 改为 down，导致链路协议断开。",
                "confidence": "high",
                "evidence": [{"claim": "Syslog 记录 administratively down", "source": "s", "source_from": "zabbix"}],
            },
        )
        base.update(kw)
        return AlertGroup(**base)

    def _divs(self, card):
        return "\n".join(e["text"]["content"] for e in card["elements"] if e["tag"] == "div")

    def test_标题用headline短定性而不是根因整句(self):
        title = build_card(self._group())["header"]["title"]["content"]
        self.assertIn("人为 shutdown 导致链路中断", title)
        self.assertIn("V1-vios", title)
        # 根因整句在正文里，不该被硬截进标题
        self.assertNotIn("ifAdminStatus", title)
        self.assertNotIn("本端管理员在控制台", title)

    def test_建议不复读根因(self):
        card = build_card(self._group())
        advice = [e["text"]["content"] for e in card["elements"]
                  if e["tag"] == "div" and e["text"]["content"].startswith("建议")]
        self.assertTrue(advice)
        self.assertNotIn("本端管理员在控制台", advice[0])

    def test_证据链带编号和中文来源(self):
        body = self._divs(build_card(self._group()))
        self.assertIn("1. **[监控]** Syslog 记录 administratively down", body)

    def test_校验层查出的矛盾单独成块(self):
        card = self._group(violations=["证据与 root_cause 的持续 down 状态不一致"])
        body = self._divs(build_card(card))
        # 措辞从「自相矛盾」改成「对不上的地方」：前者像在说模型坏了，
        # 后者说的是这条结论内部哪里接不上，值班的人更看得懂
        self.assertIn("需要核实", body)
        self.assertIn("持续 down 状态不一致", body)
        # 没有矛盾的时候不该凭空冒出这一块
        self.assertNotIn("需要核实", self._divs(build_card(self._group())))

    def test_标题洗掉markdown标记(self):
        """卡片头是 plain_text，模型夹的 ** 和反引号会字面显示出来。"""
        g = self._group()
        g.analysis["headline"] = "**人为 shutdown** 导致 `Gi0/1` 中断"
        title = build_card(g)["header"]["title"]["content"]
        self.assertNotIn("**", title)
        self.assertNotIn("`", title)
        self.assertIn("人为 shutdown 导致 Gi0/1 中断", title)


class Test判不出来时给可动手的下一步(unittest.TestCase):
    """维护者 「AI 查不出的时候最好让 AI 给建议：需要什么额外信息、
 该怎么能继续推进。而不是就查不出就撂挑子了」。
    """

    def _low(self, **cand):
        base = {"candidates": ["本端有人动过配置", "对端或上游的问题"],
                "why_indistinguishable": "拿不到本端内核日志"}
        base.update(cand)
        return AlertGroup(eventids=["1"], host="A1", analysis={
            "headline": "判不出根因，需人工介入", "root_cause": "现有数据无法区分",
            "confidence": "low", "evidence": [], "undistinguishable_candidates": [base]})

    def _advice(self, g):
        return next(e["text"]["content"] for e in build_card(g)["elements"]
                    if e["tag"] == "div" and e["text"]["content"].startswith("建议"))

    def test_它自己能查就说自己能查(self):
        a = self._advice(self._low(
            how_to_get_it="登 A1 跑 show logging | begin Sep 23 12:08", who="agent_can_retry"))
        self.assertIn("我自己能查", a)
        self.assertIn("show logging | begin", a)

    def test_要人上手就说要人上手(self):
        a = self._advice(self._low(
            how_to_get_it="查 09-23 12:00 前后的变更单，确认有没有人动过 A1", who="needs_human"))
        self.assertIn("要人上手", a)
        self.assertIn("变更单", a)

    def test_旧记录没有新字段时回落(self):
        a = self._advice(self._low(what_data_would_help="需要 eth0 的内核日志"))
        self.assertIn("eth0 的内核日志", a)

    def test_连缺什么都没说时不假装有建议(self):
        """它说分不出，却没说清还缺哪条数据。

 **候选必须非空**——候选空就是"它判出来了"，改过。
        """
        g = AlertGroup(eventids=["1"], host="A1", analysis={
            "headline": "判不出", "root_cause": "x", "confidence": "low",
            "evidence": [], "undistinguishable_candidates": [{"candidates": ["remote_or_upstream"]}]})
        self.assertIn("去看一眼完整证据链", self._advice(g))

    def test_标题不跟建议打架(self):
        """它自己能再查一轮时，标题不许写「需要人工介入」——会把人白叫起来。"""
        retry = build_card(self._low(how_to_get_it="登 A1 跑 show logging", who="agent_can_retry"))
        self.assertNotIn("需要人工介入", retry["header"]["title"]["content"])
        self.assertIn("它能自己补", retry["header"]["title"]["content"])
        human = build_card(self._low(how_to_get_it="查变更单", who="needs_human"))
        self.assertIn("需要人工介入", human["header"]["title"]["content"])


class Test标题去掉重复的设备名(unittest.TestCase):
    """Win 实测：`headline` 三次真跑分别 28、28、38 字，
 **而且都以设备名开头**，而标题里 `where` 已经有 `A1 · ` 了。

 **没有做硬截断到 20 字。** 截到 20 会把结论本身切掉
 （「A1设备Ethernet0…」比超长更糟）——真正的毛病是冗余不是长度。
    """

    def _title(self, headline, host="A1"):
        g = AlertGroup(eventids=["1"], host=host, analysis={
            "headline": headline, "root_cause": "x", "confidence": "high", "evidence": []})
        return build_card(g)["header"]["title"]["content"]

    def test_开头的设备名去掉但结论留着(self):
        t = self._title("A1设备Ethernet0/1接口被人为执行shutdown操作导致链路中断")
        self.assertTrue(t.startswith("A1 · Ethernet0/1"))
        self.assertIn("被人为执行shutdown", t)   # 结论不许被截掉

    def test_各种连接词都认(self):
        for h in ("A1上的OSPF邻居断开", "A1的OSPF邻居断开", "A1 OSPF邻居断开"):
            self.assertNotIn("A1 · A1", self._title(h))

    def test_去完只剩零头就不去(self):
        # 宁可冗余，也别把话砍没
        self.assertEqual(self._title("A1掉了"), "A1 · A1掉了")

    def test_句子中间的设备名不动(self):
        """中间提到的可能是对端，去掉会改变意思。"""
        self.assertIn("对端 A1", self._title("链路断了，对端 A1 也 down", host="A1"))


class Test卡片按因果顺序排(unittest.TestCase):
    """维护者 看了一张真卡：「老实说 可读性很差」「人的逻辑是 因为 A B C 所以 D」。

 改版前是先抛结论再往下找补，而结论那段自己又把依据罗列一遍，跟证据链重复。
 结论没被埋起来——**标题里就是结论**，所以正文可以老实走因果。
    """

    def _card(self, **analysis_kw):
        a = {
            "headline": "Gi0/3 邻居瞬断",
            "confidence": "medium",
            "root_cause": "无法判定具体物理根因，证据指向监控采集层面的抖动或误报。",
            "evidence": [{"source_from": "zabbix", "claim": "告警持续 0 秒", "source": "已自动恢复"}],
        }
        a.update(analysis_kw)
        return build_card({"eventids": ["102310"], "host": "V2-vios", "analysis": a})

    def _divs(self, card):
        return [e["text"]["content"] for e in card["elements"] if e.get("tag") == "div"]

    def test_先查到了什么再给结论(self):
        divs = self._divs(self._card())
        self.assertTrue(divs[0].startswith("**查到了什么**"), divs[0][:40])
        # 小标题 从「所以」改成「结论」——维护者：「这句话看的我很迷茫」
        self.assertTrue(divs[1].startswith("**结论**"), divs[1][:40])

    def test_结论不重复罗列证据链已经讲过的依据(self):
        """那张真卡的根因段 300 多字，里面 1/2/3/4 四条依据跟证据链讲的是同一件事。"""
        divs = self._divs(self._card(
            root_cause="无法判定具体物理根因，证据指向监控采集层面的抖动。关键依据：1. 所有告警持续0秒；2. SSH登录成功；3. 接口当前 up。"
        ))
        conclusion = next(d for d in divs if d.startswith("**结论**"))
        self.assertIn("证据指向监控采集层面的抖动", conclusion)
        self.assertNotIn("关键依据", conclusion)
        self.assertNotIn("SSH登录成功", conclusion)

    def test_证据链为空时不许切根因(self):
        """证据链空着的时候，根因里那几条依据就是仅有的依据，切掉等于把话砍没。"""
        divs = self._divs(self._card(
            evidence=[],
            root_cause="判不出。关键依据：1. 所有告警持续0秒；2. SSH登录成功。",
        ))
        conclusion = next(d for d in divs if d.startswith("**结论**"))
        self.assertIn("SSH登录成功", conclusion)


class Test引文只截不改写(unittest.TestCase):
    """那张卡里一条 ICMP 证据的引文是六个
 `1790207973=0.000618333333333334` 连排、占了五行，一个字读不出来。
    """

    def _quote_line(self, source):
        card = build_card({"eventids": ["1"], "analysis": {
            "confidence": "high", "root_cause": "x",
            "evidence": [{"source_from": "zabbix", "claim": "c", "source": source}],
        }})
        body = "\n".join(e["text"]["content"] for e in card["elements"] if e.get("tag") == "div")
        return next(l for l in body.split("\n") if l.strip().startswith(">"))

    def test_超长引文截断并说明截掉多少(self):
        line = self._quote_line("ICMP response time: " + "1790207973=0.000618333333333334, " * 20)
        self.assertIn("引文还有", line)
        self.assertIn("字", line)
        self.assertLess(len(line), 260)

    def test_短引文一个字不动(self):
        raw = "*Sep 24 00:03:52.832: %SSH-5-SSH2_USERAUTH: Succeeded"
        self.assertIn(raw, self._quote_line(raw))

    def test_不把时间戳改写成时分秒(self):
        """引文要拿去跟原文逐字核对，改写一次核对就失去意义。"""
        line = self._quote_line("clock=1790208003 故障持续约 0 秒")
        self.assertIn("1790208003", line)


class Test判没判出来不看置信度(unittest.TestCase):
    """维护者发来一张真卡截图：「这一长段 又说分析不出来
 运维人员看了半天不得气炸」。

 那张卡（一条真实告警）根因写得明明白白——历史旧名 A1-iol 的 Ethernet0/1 在故障窗口内
 02:53:43 和 02:58:39 两次被 console 执行 shutdown，五条证据逐条带回显——
 标题和建议却都在说「没判出根因」。

 根子是把两件事混成了一件：**判没判出来，和有多确定，是两回事。**
 置信度当天刚改成从 hypothesis_checklist 推导，模型把根因写死了却没在清单里把
 对应方向标成「有证据支持」，推导给了 low，标题和建议就都翻了。

 它自己承认判不出的信号是 `undistinguishable_candidates` 非空。
    """

    def _card(self, *, confidence, candidates):
        return build_card({
            "eventids": ["102462"], "host": "A1-viosl2",
            "analysis": {
                "headline": "Ethernet0/1 被 console 执行 shutdown",
                "root_cause": "A1-viosl2 的 GigabitEthernet0/1 在故障窗口内两次被通过 console 执行 shutdown",
                "confidence": confidence,
                "evidence": [{"source_from": "设备", "claim": "日志明确记录",
                              "source": "%LINK-5-CHANGED: Interface GigabitEthernet0/1, changed state to administratively down",
                              "time_relevance": "故障窗口"}],
                "undistinguishable_candidates": candidates,
            },
        })

    def _text(self, card):
        return "\n".join(e["text"]["content"] for e in card["elements"] if e.get("tag") == "div")

    def test_置信度低但判出来了_标题不许说没判出(self):
        card = self._card(confidence="low", candidates=[])
        title = card["header"]["title"]["content"]
        self.assertNotIn("没判出根因", title)
        self.assertIn("shutdown", title)

    def test_置信度低但判出来了_建议不许说分不出(self):
        body = self._text(self._card(confidence="low", candidates=[]))
        self.assertNotIn("分不出根因", body)
        self.assertIn("按上面的根因", body)

    def test_它自己说分不出时_标题和建议才转口径(self):
        card = self._card(confidence="low", candidates=[
            {"candidates": ["remote_or_upstream"], "why_indistinguishable": "缺对端日志",
             "how_to_get_it": "在 D2 上跑 show logging | begin", "who": "需要人工取证"}])
        self.assertIn("没判出根因", card["header"]["title"]["content"])
        self.assertIn("分不出根因", self._text(card))

    def test_置信度仍然管配色(self):
        """砍掉标题那层联动，不等于置信度没用了——它还管配色和那行说明。"""
        self.assertEqual(self._card(confidence="low", candidates=[])["header"]["template"], "orange")
        self.assertEqual(self._card(confidence="high", candidates=[])["header"]["template"], "green")
