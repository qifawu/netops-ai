"""把分析结果画成飞书交互式卡片（v1 结构）。纯函数，不碰网络。

**红线**：不做配置下发，卡片只写「建议人去做什么」，不许出现「系统将执行 / 已自动处理」。
`_suggested_action()` 是唯一生成这段文字的地方。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class AlertGroup:
    """一组被合并的告警。`alerts` 保留每条原始告警的 eventid/name，
    `analysis` 是为卡片展示合成后的分析视图。
    """

    eventids: list[str] = field(default_factory=list)
    analysis: dict = field(default_factory=dict)
    alert_names: dict[str, str] = field(default_factory=dict)
    alerts: list[dict] = field(default_factory=list)
    summary: str = ""
    host: str = ""
    #: 校验层（`check_business_rules`）算出来的自相矛盾，重试到上限仍然违反的那些。
    #: **原样带上卡片**：这是结论最可能出问题的地方，埋在后台记录里没人会去翻。
    violations: list[str] = field(default_factory=list)
    #: 证据逐字核对结果。只有 fabricated 会进入「站不住的地方」。
    evidence_verification: dict | None = None

    @property
    def count(self) -> int:
        return len(self.eventids)



#: confidence -> 卡片颜色模板。low 用醒目的橙色（不是红色——红色在飞书里
#: 通常留给"故障/严重告警"，这里是"AI 判不出，需要人看"，性质不一样，
#: 用橙色区分开，避免跟真正的严重告警撞色造成误判优先级）
_TEMPLATE_BY_CONFIDENCE = {
    "high": "green",
    "medium": "blue",
    "low": "orange",
}

_CONFIDENCE_LABEL = {"high": "高", "medium": "中", "low": "低"}



#: 卡片标题的长度上限。飞书卡片头就这么宽，再长会被客户端自己截，
#: 那时候连省略号都没有。
_HEADLINE_LIMIT = 40

#: 截断时允许回退多远去找一个干净的断点。超过这个距离就宁可硬截——
#: 回退太多会把标题砍得只剩半句，比一个半拉单词更糟。
_HEADLINE_BACKOFF = 12

#: 能当断点的字符：中英文标点和空格。
_HEADLINE_BREAKS = set(" \t，。；：、！？（）,.;:!?()[]{}<>「」『』—-/\\")


import re

from netops_ai.analysis.schema import STATUS_SUPPORTED, WHO_AGENT_CAN_RETRY, normalize_enum
from netops_ai.labels import humanize_text, label_for

#: 卡片头要洗掉的行内 Markdown 标记。
_MD_MARK = re.compile(r"\*\*([^*]+)\*\*|`([^`]+)`")


def _strip_host_prefix(headline: str, host: str) -> str:
    """去掉 headline 开头重复的设备名。

 schema 里写了「不要带设备名」，**实测三次真跑分别是
 28、28、38 字，而且都以设备名开头**（如
 「A1设备Ethernet0/1接口被人为执行shutdown操作导致链路中断」）。
 标题里 `where` 已经有 `A1 · ` 了，说两遍纯属浪费标题栏宽度。

 **只去开头这一处，且必须紧跟设备名。** 句子中间提到的设备名
 可能是对端，去掉会改变意思。
    """
    if not host or not headline.startswith(host):
        return headline
    rest = headline[len(host):].lstrip()
    for word in ("设备", "上", "的"):
        if rest.startswith(word):
            rest = rest[len(word):].lstrip()
    # 去完只剩个零头就别去了，宁可冗余也别把话砍没
    return rest if len(rest) >= 6 else headline


def _headline(root_cause: str, limit: int = _HEADLINE_LIMIT) -> str:
    """把根因压成一行标题，**不许从单词中间切**（`shutdown` 被切成 `shutdow` 过，截断还可能翻转语义）。
    优先在标点和空格处断，断不到才硬截，都加省略号。
    """
    # **卡片头是 plain_text，不渲染 Markdown。** 模型在 headline/root_cause 里
    # 夹一个 `**` 或反引号，标题上就是字面的星号。div 里走 lark_md 没这问题，
    # 只有这里要洗。
    text = " ".join(str(root_cause or "").split())
    text = _MD_MARK.sub(r"\1\2", text)
    if len(text) <= limit:
        return text

    cut = limit
    # 只有当下一个字符会把一个词劈开时才需要回退。中文逐字成词，
    # 回退是多余的，还会白白砍掉几个字。
    if text[cut - 1] not in _HEADLINE_BREAKS and text[cut] not in _HEADLINE_BREAKS:
        for back in range(1, _HEADLINE_BACKOFF + 1):
            if text[cut - back] in _HEADLINE_BREAKS:
                cut -= back
                break

    return text[:cut].rstrip(" \t，。；：、/-") + "…"

_HEADLINE_OK = 24  # schema 要求 ≤20，留一点余量


def _title_text(analysis: dict, host: str, headline: str, root_cause: str) -> str:
    """标题文字。模型给的 headline 够短就用；**超长（真跑 40~47 字，还带括号里的英文注释，
 连 maxLength 都压不住——被强截成「…(Admin」）就不用它**，改用清单里「有证据支持」那个方向的
 中文名（如「本端有人动过配置」），稳定、干净、不会半句；一个都没有再退回 root_cause 的断句截断。
    """
    if headline:
        cand = _strip_host_prefix(humanize_text(headline), host)
        if len(cand) <= _HEADLINE_OK:
            return cand
    supported = [
        cat for cat, item in (analysis.get("hypothesis_checklist") or {}).items()
        if isinstance(item, dict) and normalize_enum(item.get("status", "")) == STATUS_SUPPORTED
    ]
    if supported:
        return "、".join(label_for(c) for c in supported)
    return _strip_host_prefix(humanize_text(headline), host) if headline else humanize_text(root_cause)


def _suggested_action(analysis: dict) -> str:
    """只写"建议人去做什么"，不许出现"系统将/已执行"这类措辞。

 **按「它自己承认判不出没有」分支，不按置信度。**

 出过一张真卡：根因写得明明白白（历史旧名 A1-iol 的 Ethernet0/1 被 console
 执行了 shutdown，两次的时间点都点出来了），下面一行却是「现有证据还分不出根因」。
 值班的人读到这儿只会骂人。

 根子是把两件事混成了一件——**判没判出来，和有多确定，是两回事**。
 置信度那天刚改成从 `hypothesis_checklist` 推导，模型把根因写死了却没在清单里
 把对应方向标成「有证据支持」，推导判了 low，建议就跟着走了「分不出」那条分支。

 它自己承认判不出的信号是 `undistinguishable_candidates` 非空，不是置信度低。
 置信度只管「有多确定」，现在只用来选配色和那行说明。
    """
    candidates = analysis.get("undistinguishable_candidates") or []

    if not candidates:
        # **不要把 root_cause 再抄一遍。** 它就在同一张卡上面三行的位置，
        # 复读一遍只是把卡撑长，读的人还得比对两段是不是同一句话。
        return "建议：按上面的根因联系对应责任人核实并处理。"

    # **判不出来不等于撂挑子。** 说清「怎么拿到能定性的数据」和「谁去拿」，
    # 人看完要能立刻动手。`how_to_get_it` 是下结论那次一起写的，
    # 不再多花一次模型调用问一遍。旧记录没这字段，回落到 what_data_would_help。
    how = "；".join(
        humanize_text(c.get("how_to_get_it") or c.get("what_data_would_help") or "").strip()
        for c in candidates
        if (c.get("how_to_get_it") or c.get("what_data_would_help") or "").strip()
    )
    retry = any(normalize_enum(c.get("who")) == WHO_AGENT_CAN_RETRY for c in candidates)
    head = ("建议：现有证据还分不出根因，**但下面这步我自己能查**，"
            "补齐之后可以再判一轮。" if retry
            else "建议：现有证据还分不出根因，下面这步要人上手。")
    if not how:
        # 它说分不出，又没说清缺什么。这种情况人得去看原始证据。
        return head + "\n它没说清还差哪条数据，去看一眼完整证据链。"
    return f"{head}\n{how}"


_SOURCE_LABEL = {code: label_for(code) for code in ("zabbix", "device", "sop")}


#: 单条引文在卡片上最多多长。
#:
#: **不截的后果是真见过的**：那张卡里一条 ICMP 证据的引文是
#: 六个 `1790207973=0.000618333333333334` 连排，占了五行，一个字都读不出来；
#: 另一条 SSH 日志把 `chacha20-poly1305@openssh.com` 和 hmac 套件整串带上，占三行。
#: 160 字够放完一条 syslog 或一条 show 命令回显的要害。
_QUOTE_LIMIT = 160


def _format_quote(text: str) -> str:
    """引文压成一行并限长。**只截不改写**——截掉多少要说出来。

    故意不做「把 epoch 转成时分秒」「浮点数保留三位」这类美化：
    引文是拿去跟原文逐字核对的东西，一旦改写，核对就失去意义。
    """
    one_line = " ".join(str(text or "").split())
    if len(one_line) <= _QUOTE_LIMIT:
        return one_line
    return f"{one_line[:_QUOTE_LIMIT]}…（引文还有 {len(one_line) - _QUOTE_LIMIT} 字，完整原文见后台记录）"


def _format_evidence(evidence: list[dict]) -> str:
    """证据链编号列出，来源用中文标签。

真正的"证据跟结论对不上"走 `violations`（校验层算出来的），单独一块，不混在这里。
    """
    if not evidence:
        return "（无）"
    lines = []
    for i, e in enumerate(evidence[:5], 1):
        src = _SOURCE_LABEL.get(e.get("source_from", ""), label_for(e.get("source_from", "")))
        lines.append(f"{i}. **[{src}]** {humanize_text(e.get('claim', ''))}")
        if e.get("source"):
            lines.append(f"   > {_format_quote(humanize_text(e.get('source', '')))}")
    if len(evidence) > 5:
        lines.append(f"（还有 {len(evidence) - 5} 条，完整证据链见后台记录）")
    return "\n".join(lines)


def _is_degraded(analysis: dict) -> bool:
    return bool(analysis.get("degraded"))


def _confidence_note(analysis: dict, count: int) -> str:
    if _is_degraded(analysis):
        text = "置信度 未评估"
    else:
        confidence = analysis.get("confidence", "low")
        text = f"置信度 {_CONFIDENCE_LABEL.get(confidence, confidence)}"
    return text + (f" · 合并 {count} 条" if count > 1 else "")


#: 根因里常见的「下面开始罗列依据」的起头。**证据链就在同一张卡的上面**，
#: 同一批依据说两遍是 那张卡难读的直接原因：
#: 根因段落 300 多字，里面 1/2/3/4 四条依据，跟下面证据链讲的是同一件事，
#: 两份还不完全一致，读的人得逐条比对。
_EVIDENCE_LEAD_IN = ("关键依据", "主要依据", "依据如下", "证据如下", "判断依据")


def _conclusion_text(root_cause: str, has_evidence: bool) -> str:
    """根因只留因果那一句，罗列依据的部分交给证据链。

    **只在证据链真的非空时才切**——证据链空着的时候，根因里那几条依据
    就是仅有的依据，切掉等于把话砍没。切完剩不到 20 字也不切，
    那说明这句话的主干本来就在后半截。
    """
    text = str(root_cause or "")
    if not has_evidence:
        return text
    for mark in _EVIDENCE_LEAD_IN:
        head, sep, _ = text.partition(mark)
        if sep and len(head.strip(" \n，。；：、")) >= 20:
            return head.rstrip(" \n，。；：、")
    return text


def _format_undistinguishable(candidates: list[dict]) -> str:
    if not candidates:
        return ""
    lines = []
    for c in candidates:
        names = "、".join(humanize_text(name) for name in (c.get("candidates") or []))
        lines.append(f"- **{names}**：{humanize_text(c.get('why_indistinguishable', ''))}")
    return "\n".join(lines)


#: 卡片最多列几条「站不住的地方」。再多就是墙，没人会读完。
MAX_LISTED_VIOLATIONS = 3

#: 卡片最多列几条已合并告警。线卡一挂就是 40 多条，全列出来卡片自己成了噪音；其余的在记录里。
MAX_LISTED_ALERTS = 8


def _format_alerts(eventids: list[str], alert_names: dict[str, str]) -> str:
    if not eventids:
        return "（未知）"
    lines = []
    for eventid in eventids[:MAX_LISTED_ALERTS]:
        name = alert_names.get(str(eventid), "")
        lines.append(f"- `{eventid}`" + (f"：{name}" if name else ""))
    rest = len(eventids) - MAX_LISTED_ALERTS
    if rest > 0:
        lines.append(f"- **…另外 {rest} 条同类告警**（完整列表见本条最下方的 eventid）")
    return "\n".join(lines)


def _fabricated_evidence_violations(evidence_verification: dict | None) -> list[str]:
    details = (evidence_verification or {}).get("details") or []
    out: list[str] = []
    for d in details:
        if d.get("grade") != "fabricated":
            continue
        index = d.get("index")
        where = f"第 {int(index) + 1} 条证据" if isinstance(index, int) else "有一条证据"
        if "source 是空的" in str(d.get("reason") or ""):
            # 新诊断图的证据是「引用」（哪一步 + 关键词），没取到原文行不是模型改了字，别说成「引文对不上」
            reason = humanize_text("没能在对应步骤的原文里找到这条证据引用的那一行，请以后台记录里的原文为准")
        else:
            reason = humanize_text("引文和日志原文对不上（多半是把不相邻的两行拼在了一起或改了字），请以后台记录里的原文为准")
        out.append(f"{where}：{reason}")
    return out


def _reformatted_evidence_note(evidence_verification: dict | None) -> str:
    summary = (evidence_verification or {}).get("summary") or {}
    count = summary.get("reformatted")
    if count is None:
        count = (summary.get("grades") or {}).get("reformatted", 0)
    try:
        count = int(count)
    except (TypeError, ValueError):
        count = 0
    return f"{count} 条引文格式跟原文有出入，内容能对上。" if count > 0 else ""


def build_receipt_card(*, eventid: str, name: str = "") -> dict:
    """收到告警时立刻发的轻量回执卡。

    这张卡只说明"已经收到、正在分析、结论稍后"，故意不放根因、置信度、
    证据链或候选方向，避免在攒批窗口关闭前给出 premature conclusion。
    """
    title = f"收到告警 {eventid}" if eventid else "收到告警"
    alert_line = f"`{eventid}`" + (f"：{name}" if name else "")
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": title},
            "template": "blue",
        },
        "elements": [
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": f"**告警**：{alert_line}",
                },
            },
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": "正在分析，可能还有相关告警，结论稍后。",
                },
            },
            {
                "tag": "note",
                "elements": [
                    {"tag": "plain_text", "content": f"eventid: {eventid if eventid else '(未知)'}"}
                ],
            },
        ],
    }


def build_card(group: AlertGroup | dict) -> dict:
    """`group` 可以是 `AlertGroup`，也可以是
    单条 `{"eventids": [...], "analysis": {...}}`（不经过收敛、只有一条
    告警时的场景），两者字段名一致，不用调用方先转类型。
    """
    if isinstance(group, AlertGroup):
        eventids, analysis, alert_names, summary = group.eventids, group.analysis, group.alert_names, group.summary
        host, violations = group.host, group.violations
        evidence_verification = group.evidence_verification
    else:
        eventids, analysis = group.get("eventids", []), group.get("analysis") or {}
        alert_names = group.get("alert_names") or {}
        summary = group.get("summary", "")
        host, violations = group.get("host", ""), group.get("violations") or []
        evidence_verification = group.get("evidence_verification")

    degraded = _is_degraded(analysis)
    confidence = analysis.get("confidence", "low")
    root_cause = humanize_text(analysis.get("root_cause", "（无结论）"))
    candidates = analysis.get("undistinguishable_candidates") or []
    template = "orange" if degraded else _TEMPLATE_BY_CONFIDENCE.get(confidence, "grey")
    count = len(eventids)
    count_suffix = f"（合并 {count} 条同类告警）" if count > 1 else ""

    # **标题 = 设备 + 一句话定性。** 以前是把 root_cause 整句硬截到 40 字，
    # 截出来的是「本端管理员在控制台执行了配置变更，将接口 Ethernet0/1 的管理状态…」——
    # 占满标题栏却没说清是什么事，而且下面「根因」那行又把同一句完整说了一遍。
    # `headline` 是 schema 里新加的必填字段，模型直接给短定性。
    where = f"{host} · " if host else ""
    if degraded:
        title = f"⚠️ LLM 分析不可用"
        degraded_headline = str(analysis.get("headline") or "").strip()
        if degraded_headline:
            title += f"：{_headline(humanize_text(degraded_headline), 28)}"
        title += count_suffix
    elif candidates:
        # **「判没判出来」看它自己承认没有，不看置信度。**
        #
        # 出过一张真卡：根因写得明明白白（历史旧名 A1-iol 的 Ethernet0/1 被
        # console 执行了 shutdown），标题和建议却都在说「没判出根因」。
        # 原因是这里原来写的是 `confidence == "low"`，而置信度那天刚改成从
        # `hypothesis_checklist` 推导——模型把根因写死了却没在清单里把对应方向
        # 标成「有证据支持」，推导给了 low，标题就翻了。
        #
        # 判没判出来，和有多确定，是两回事。置信度现在只管配色和那行说明。
        #
        # **「没判出来」和「要人上手」也是两件事**——它自己还能再查一轮的时候，
        # 标题写「需要人工介入」会把人白叫起来，跟下面那条建议也直接打架。
        retry = any(normalize_enum(c.get("who")) == WHO_AGENT_CAN_RETRY for c in candidates)
        tail = "还差一步证据，它能自己补" if retry else "需要人工介入"
        title = f"⚠️ {where}没判出根因，{tail}{count_suffix}"
    else:
        headline = str(analysis.get("headline") or "").strip()
        # headline 用更紧的上限：它本来就该是一句话定性，
        # root_cause 是完整因果，两者宽度不该一样。
        # **不做硬截断到 20 字。** schema 要求 ≤20，但实测模型给 28~38 字，
        # 截到 20 会把结论本身切掉（「A1设备Ethernet0…」比超长更糟）。
        # 真正的毛病是冗余不是长度：去掉开头重复的设备名，
        # 再按卡片头实际宽度（40）留一道兜底。
        text = _title_text(analysis, host, headline, root_cause)
        title = f"{where}{_headline(text)}{count_suffix}"

    # **卡片按人讲故障的顺序排：这次是哪几条告警 → 查到了什么 → 所以是什么 → 下一步。**
    #
    # 维护者看完一张真卡说「老实说 可读性很差」，又说「人的逻辑是
    # 因为 A B C 所以 D」。正文小标题后来按维护者的话从「所以」改成「结论」。原来的排法是先抛结论再往下找补，而结论那段自己
    # 又把依据罗列了一遍，跟下面的证据链重复。
    #
    # 结论没有因此被埋起来——**标题里就是结论**（headline 或"没判出根因"），
    # 所以正文可以老老实实走因果。
    def div(content: str) -> dict:
        return {"tag": "div", "text": {"tag": "lark_md", "content": content}}

    elements: list[dict] = []

    if count > 1:
        elements.append(div(f"**这次合并了 {count} 条告警**\n{_format_alerts(eventids, alert_names)}"))

    related = analysis.get("related_alerts") or []
    if related:
        lines = [
            f"- #{r.get('eventid', '')} {r.get('host', '')}：{humanize_text(r.get('link', ''))}"
            f"（{humanize_text(r.get('reason', ''))}）"
            for r in related
        ]
        elements.append(div("**同一件事的其它告警**\n" + "\n".join(lines)))

    evidence = analysis.get("evidence") or []
    if elements:
        elements.append({"tag": "hr"})
    if degraded:
        elements.append(div("**⚠️ LLM 分析不可用**\n以下不是根因判断，只是自动取得的证据。"))
    elements.append(div(f"**查到了什么**\n{_format_evidence(evidence)}"))

    elements.append({"tag": "hr"})
    elements.append(div(f"**结论**：{_conclusion_text(root_cause, bool(evidence))}"))
    elements.append({
        "tag": "note",
        "elements": [{"tag": "plain_text",
                      "content": _confidence_note(analysis, count)}],
    })

    all_violations = [*violations, *_fabricated_evidence_violations(evidence_verification)]
    if all_violations:
        # 校验层算出来的自相矛盾，**重试到上限仍然违反**。原样带上卡片：
        # 这是结论最可能出问题的地方，埋在后台记录里没人会去翻。
        # **封顶。** 违规是给人看的提醒，不是清单。刷满一屏就没人看了——
        # 真实反例：六个方向各报一条一模一样的话，卡片上一堵墙。
        # 源头已经按规则聚合过，这里再留一道保险。
        shown = [humanize_text(v) for v in all_violations[:MAX_LISTED_VIOLATIONS]]
        rest = len(all_violations) - len(shown)
        # 注意别叫 title：下面卡片头用的就是 title，这里覆盖了会把「根因结论」标题换成这句警告（飞书截图）
        warn_title = f"⚠️ 这条结论有 {len(all_violations)} 处需要核实"
        body = "\n".join(f"- {v}" for v in shown)
        if rest > 0:
            body += f"\n- **…另外 {rest} 处**（完整清单在后台记录里）"
        elements.append(div(f"**{warn_title}**\n以下是系统自动校验没通过的地方（不是设备又出了新故障），结论请先当作参考：\n{body}"))

    reformatted_note = _reformatted_evidence_note(evidence_verification)
    if reformatted_note:
        elements.append(div(reformatted_note))

    undistinguishable_text = _format_undistinguishable(candidates)
    if undistinguishable_text:
        elements.append(div(f"**这几个方向现有数据分不出来**\n{undistinguishable_text}"))

    if summary:
        elements.append(div(humanize_text(summary)))

    elements.append({"tag": "hr"})
    elements.append(div(humanize_text(analysis.get("suggested_action", "")) if degraded else _suggested_action(analysis)))
    elements.append({
        "tag": "note",
        "elements": [
            {"tag": "plain_text", "content": f"eventid: {', '.join(eventids) if eventids else '(未知)'}"}
        ],
    })

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": title},
            "template": template,
        },
        "elements": elements,
    }


_INSPECTION_KIND_LABEL = {code: label_for(code) for code in ("trend", "periodic_spike", "self_healing_flap")}


def build_inspection_card(report) -> dict:
    """巡检报告卡片。只读 `ScanReport` 的字段、不 import 它，免得 feishu 反向依赖 inspection。"""
    findings = report.findings
    title = f"🔍 定时巡检：{len(findings)} 个发现" if findings else "🔍 定时巡检：一切正常"
    template = "orange" if findings else "green"

    elements: list[dict] = [
        {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": (
                    f"扫了 {report.scanned_hosts} 台主机、{report.scanned_items} 个监控项，"
                    f"{report.items_with_data} 个有历史数据"
                ),
            },
        },
        {"tag": "hr"},
    ]

    if not findings:
        elements.append(
            {"tag": "div", "text": {"tag": "lark_md", "content": "没有发现趋势/周期性尖峰/反复自愈这三类异常。"}}
        )
    for f in findings:
        kind_label = _INSPECTION_KIND_LABEL.get(f.kind, label_for(f.kind))
        elements.append(
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": (
                        f"**[{kind_label}] {f.host_name} / {f.item_name}**"
                        f"（`{f.item_key}`）\n{humanize_text(f.finding.reason)}"
                    ),
                },
            }
        )

    if report.errors:
        elements.append({"tag": "hr"})
        elements.append(
            {
                "tag": "div",
                "text": {"tag": "lark_md", "content": "**巡检过程中的错误**\n" + "\n".join(f"- {humanize_text(e)}" for e in report.errors)},
            }
        )

    return {
        "config": {"wide_screen_mode": True},
        "header": {"title": {"tag": "plain_text", "content": title}, "template": template},
        "elements": elements,
    }
