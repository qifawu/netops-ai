"""② 研判：把 Zabbix 文本和取证结果拼成时间线，一次 strict schema 调用出结论。不取数、不给工具。"""
from __future__ import annotations

import os

from dataclasses import dataclass
from collections.abc import Sequence

from netops_ai.knowledge import NETWORK_DOMAIN_KNOWLEDGE
from netops_ai.llm.client import LLMClient, LLMResponse

from .schema import HYPOTHESIS_CATEGORIES, analysis_json_schema, derive_confidence, normalize_statuses
from .timeline import build_timeline_text

_CHECKLIST_LINES = "\n".join(f"   - {k}：{v}" for k, v in HYPOTHESIS_CATEGORIES.items())

SYSTEM_PROMPT = f"""你是一名网络运维故障分析助手。你会拿到一次真实故障的一条或多条候选 Zabbix 告警数据，
可能还有登设备抓到的 show/display 命令原文。你的任务是判断这次故障的根因。

严格规则：
1. 只能使用下面提供的数据分析，不能用你自己的通用知识编造设备上没有出现过的命令、
   指标或日志内容——这是最重要的一条红线，出现一次幻觉，其它做得再好也不算数。
2. 输入里可能只有一条候选告警，也可能有多条候选告警。你必须先判断候选告警之间
   的因果关系和分组，再判断根因：把每条候选写进 `alert_roles`，把 incident
   分组写进 `grouping`。单条候选也要填这两个字段：`alert_roles` 一项，
   `grouping` 一组且 `events` 只有这个 eventid。
3. `alert_roles.role` 只能是 `root`、`consequence`、`independent` 三个值：
   - `root`：这条告警最接近本次 incident 的触发原因；
   - `consequence`：这条告警是别的候选告警造成的连带结果，必须填
     `caused_by_eventid`，且只能指向本次候选里的 eventid；
   - `independent`：现有证据不足以说明它被其它候选导致，或它属于另一件独立故障。
   指不出 caused_by_eventid 时，不允许硬标 consequence，只能标 independent。
4. `grouping` 必须覆盖本次输入的全部候选 eventid，不能漏掉，也不能加入输入里没有的
   eventid。多个告警只有在时间、对象、协议依赖或同窗证据支持同一次故障时才归为一组。
5. 根因必须到"可处置粒度"：看完你的结论，一个网工应该知道接下来该找谁、查什么、
   动什么。仅仅把告警名字换一种说法（比如"接口 X 状态异常"）不算判出根因。
6. 每条证据（evidence）的 `source` 字段必须是从输入原文**逐字复制**的一段连续
   文本——一个字都不能改，不能补全省略的内容，不能把不相邻的两段拼在一起，
   **更不能把 Zabbix 侧和设备侧的内容拼进同一条 source**。复制不出来就别用
   这条证据。每条证据还要标 `source_from`（这段引用到底来自 zabbix 还是 device），
   两个来源不能混在一条里。
7. 如果数据不足以判断到可处置粒度，诚实地说判不出具体根因，只给出数据支持的
   范围，不要为了显得自信而过度归因（比如把"同时发生"说成"因为A所以B"）。
8. **`hypothesis_checklist` 这 6 类标准根因方向，每一类都要认真评估、明确
   表态，不许因为"一眼看上去不像"就跳过或者随便填 ruled_out**：
{_CHECKLIST_LINES}
   哪怕你直觉上觉得答案很明显是某一类，也要把其它 {len(HYPOTHESIS_CATEGORIES) - 1} 类过一遍、写清楚为什么
   排除——**上一轮最大的教训就是"自由联想候选"会系统性漏掉某个方向（尤其是
   "本端主动变更"这类不显眼但很常见的真实原因），固定清单不许你跳过任何一类**。
9. **最容易犯的错误不是判错，是数据不够时悄悄选一个看起来合理的具体方向**。
   `hypothesis_checklist` 里如果有两个及以上类别是 `supported` 或
   `cannot_determine`（都没被排除），**必须**用 `undistinguishable_candidates`
   字段把它们**全部**列出来、说明为什么现在区分不了、需要什么数据才能区分——
   不许只挑其中几个说、更不许在 `root_cause` 里自己偷偷选一个当结论。
10. **不用自己判断把握有多大**，那是按你填的 `hypothesis_checklist`
   算出来的：排干净几个方向，把握就到哪一档。你只要把每个方向的状态和
   反证填实，别为了显得确定去硬标「已排除」。
11. `ruled_out` 字段用来记比 `hypothesis_checklist` 更具体的排除项（比如某个
   具体命令结果、某个特定型号问题），`hypothesis_checklist` 是 5 大类的强制
   表态，两者不是一回事，都要填。
12. **`hypothesis_checklist` 里标 `ruled_out` 必须同时填 `counter_evidence`**：
   要有一条从输入原文逐字引用的**正面反证**，直接否定这个方向——不是
   "我没看到支持这个方向的记录"，是"有一条数据明确说明不是这个方向"。
   **「没看到证据」和「有反证」是两件完全不同的事**：前者只能让你标
   `cannot_determine`，绝不能标 `ruled_out`。拿不出逐字反证的方向，
   一律 `cannot_determine`，`counter_evidence` 留空数组。每条
   `counter_evidence` 还必须标 `time_relevance`：`fault_window` 表示证据
   来自故障发生时刻附近（日志、历史监控值）；`current_snapshot` 表示这是
   现在查出来的状态（show 命令实时回显）；`time_invariant` 表示跟时间无关
   的事实（配置、硬件型号、MIB 定义）。
13. **`counter_evidence` 每条都要标 `contradiction`：`direct` 还是
    `absence`**。`direct` 是这条引用的内容本身跟这个方向直接冲突
    （比如假设"对端关的"，引文却是本端 `administratively down`）；
    `absence` 是这条引用只是说明"某类记录没有出现"（比如"没有配置变更
    日志"）。**`absence` 从来不是反证，哪怕引用的原文逐字为真也不算**——
    一句否定式的结论（"这不是人为的"）永远不能靠引用一条肯定句的原文来
    证明，那条原文只能证明"某件事发生过"，证明不了"另一件事没发生过"。
    **常见的自我检查方法**：如果你写的 `claim` 里出现"没有/未/无/找不到/
    未发现/不存在"这类词，几乎一定是 `absence`，这时候老实把 `status`
    改回 `cannot_determine`，不要标 `ruled_out`。
14. **判断一次已经结束或时间线不清楚的故障时，不能拿现在查到的状态去证明
    故障发生时不是某个方向**。例如设备当前 `show interfaces` 显示接口 up，
    只能说明"现在是 up"，不能反推"告警发生时没有被人为 shutdown"。这种
    当前快照要标 `time_relevance=current_snapshot`，它不能作为 `ruled_out`
    的反证；要排除一个方向，必须优先使用故障窗口附近的日志、历史监控值
    （`fault_window`），或真正不随时间变化的事实（`time_invariant`）。
    机械判定：如果某个方向的唯一反证是 `current_snapshot`，这个方向的
    `status` 必须是 `cannot_determine`，`counter_evidence` 必须是 `[]`。
    具体例子：`show interfaces` 现在显示 `GigabitEthernet0/0 is up`，不能
    排除告警发生时有人做过 shutdown/no shutdown 或其它主动恢复动作。
{NETWORK_DOMAIN_KNOWLEDGE}
下面这些是通用的网络协议/运维领域知识，帮你理解输入数据里的技术含义，
**不是针对这次故障的提示，也不能替代第 1 条红线**——用它们来正确解读
已经给你的真实数据，不能反过来编造数据里没有出现过的内容。
"""


@dataclass
class AnalysisRun:
    response: LLMResponse
    parsed: dict | None


def _with_derived_confidence(parsed: dict | None) -> dict | None:
    """把代码推出来的置信度写回结果里。

 **模型不再自己填 `confidence`**——业界说自报置信度是最弱的信号，
 问模型确不确定，答案是由产生原始结论的同一个过程产生的。但落盘记录、飞书卡、前端都还读这个键，
 所以在唯一出口这里把推导值补上，下游一行不用改。
    """
    if not isinstance(parsed, dict):
        return parsed
    normalize_statuses(parsed)
    parsed["confidence"] = derive_confidence(parsed)
    return parsed


ZabbixCandidate = dict[str, str]


def _format_zabbix_candidates(
    zabbix_text: str | Sequence[ZabbixCandidate],
    device_text: str | None,
    *,
    fault_time_epoch: float | None,
    device_collected_epoch: float | None,
) -> str:
    """多候选告警：每个候选各自一条时间线。设备回显在每个候选里都放一份，判谁因谁果常要参照同一份现场。"""
    if isinstance(zabbix_text, str):
        candidates: list[ZabbixCandidate] = [{"eventid": "unknown", "text": zabbix_text}]
    else:
        candidates = [
            {
                "eventid": str(candidate.get("eventid") or f"candidate-{i}"),
                "text": str(candidate.get("text") or ""),
            }
            for i, candidate in enumerate(zabbix_text, start=1)
        ]

    parts = ["## Zabbix 侧候选告警（统一时间线，每条候选各自一份）\n\n"]
    for i, candidate in enumerate(candidates, start=1):
        timeline = build_timeline_text(
            candidate["text"],
            device_text,
            fault_time_epoch=fault_time_epoch,
            device_collected_epoch=device_collected_epoch,
        )
        parts += [f"### 候选告警 {i}（eventid: {candidate['eventid']}）\n\n", timeline, "\n\n"]
    return "".join(parts).rstrip() + "\n"


#: 研判 prompt 的硬上限（字符）。**最后一道保险，不是省钱手段。**
#: 52 条候选 × 21.6 万字符的上下文，模型直接以「输入超长」400 掉，
#: 整个 incident 一个结论都没有。截断之后至少还能拿着前面的证据出结论。
#: `ANALYSIS_PROMPT_MAX_CHARS` 可覆盖。
PROMPT_MAX_CHARS = 120_000


def _cap(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    head = int(limit * 0.7)
    return (
        text[:head]
        + f"\n\n……（中间省略 {len(text) - limit} 字符，上下文超过 {limit} 字的硬上限。"
        "省略的部分不能引用，需要的话用工具自己查）……\n\n"
        + text[-(limit - head):]
    )


def build_user_prompt(
    zabbix_text: str | Sequence[ZabbixCandidate],
    device_text: str | None,
    *,
    fault_time_epoch: float | None = None,
    device_collected_epoch: float | None = None,
) -> str:
    try:
        limit = int(os.environ.get("ANALYSIS_PROMPT_MAX_CHARS") or PROMPT_MAX_CHARS)
    except ValueError:
        limit = PROMPT_MAX_CHARS
    return _cap(
        _format_zabbix_candidates(
            zabbix_text,
            device_text,
            fault_time_epoch=fault_time_epoch,
            device_collected_epoch=device_collected_epoch,
        ),
        limit,
    )


def analyze(
    zabbix_text: str | Sequence[ZabbixCandidate],
    device_text: str | None = None,
    *,
    client: LLMClient | None = None,
    fault_time_epoch: float | None = None,
    device_collected_epoch: float | None = None,
    max_completion_tokens: int = 8000,
    temperature: float = 0.3,
) -> AnalysisRun:
    client = client or LLMClient()
    use_refs_func = getattr(client, "analysis_schema_use_refs", None)
    use_refs = bool(use_refs_func()) if callable(use_refs_func) else True
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": build_user_prompt(
                zabbix_text,
                device_text,
                fault_time_epoch=fault_time_epoch,
                device_collected_epoch=device_collected_epoch,
            ),
        },
    ]
    response = client.complete(
        messages,
        response_format=analysis_json_schema(use_refs=use_refs),
        max_completion_tokens=max_completion_tokens,
        temperature=temperature,
    )
    return AnalysisRun(response=response, parsed=_with_derived_confidence(response.parsed))


_DEGRADED_SHAPE_HINT = """
严格按下面这个 JSON 形状输出，只输出 JSON，不要任何 JSON 之外的文字：

{
  "root_cause": "一句话根因",
  "evidence": [{"claim": "...", "source": "逐字引用", "source_from": "监控 | 设备"}],
  "ruled_out": [{"possibility": "...", "reason": "..."}],
  "alert_roles": [
    {"eventid": "...", "role": "根因 | 连带 | 无关", "reason": "...", "caused_by_eventid": "role=连带 时填本次候选 eventid，否则填空字符串"}
  ],
  "grouping": [
    {"events": ["..."], "why_same": "..."}
  ],
  "hypothesis_checklist": {
    "local_action": {"status": "supported | ruled_out | cannot_determine", "reason": "...", "counter_evidence": [{"claim": "...", "source": "逐字引用", "source_from": "监控 | 设备", "contradiction": "direct | absence（absence 不是反证，不能支撑 ruled_out）", "time_relevance": "fault_window | current_snapshot | time_invariant（已结束故障不能用 current_snapshot 支撑 ruled_out）"}]},
    "local_hardware_or_resource": {"status": "...", "reason": "...", "counter_evidence": []},
    "remote_or_upstream": {"status": "...", "reason": "...", "counter_evidence": []},
    "link_or_path_quality": {"status": "...", "reason": "...", "counter_evidence": []},
    "monitoring_or_collection_artifact": {"status": "...", "reason": "...", "counter_evidence": []}
  },
  "undistinguishable_candidates": [
    {"candidates": ["...", "..."], "why_indistinguishable": "...", "what_data_would_help": "..."}
  ]
}
"""


def analyze_degraded(
    zabbix_text: str | Sequence[ZabbixCandidate],
    device_text: str | None = None,
    *,
    client: LLMClient | None = None,
    fault_time_epoch: float | None = None,
    device_collected_epoch: float | None = None,
    max_completion_tokens: int = 8000,
    temperature: float = 0.3,
) -> AnalysisRun:
    """降级路径：strict json_schema 反复被拒之后退到这条路——只用宽松的
    `json_object` 模式（还是强制 JSON，但不做 strict 结构校验），靠 prompt 里
    手写的形状说明撑着。**这是降级，不是正常路径**，调用方要在存档里标出来，
    不能当成跟 `analyze()` 一样可靠。
    """
    client = client or LLMClient()
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT + _DEGRADED_SHAPE_HINT},
        {
            "role": "user",
            "content": build_user_prompt(
                zabbix_text,
                device_text,
                fault_time_epoch=fault_time_epoch,
                device_collected_epoch=device_collected_epoch,
            ),
        },
    ]
    response = client.complete(
        messages,
        response_format={"type": "json_object"},
        max_completion_tokens=max_completion_tokens,
        temperature=temperature,
    )
    return AnalysisRun(response=response, parsed=_with_derived_confidence(response.parsed))
