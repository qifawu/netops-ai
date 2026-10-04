"""Shared LangChain tool-calling loop for chat and alert investigation."""

from __future__ import annotations

import json
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence

from langchain.agents import create_agent
from langchain.agents.middleware import wrap_model_call
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import BaseTool

from netops_ai.llm.client import LLMClient
from netops_ai.llm.factory import build_chat_model, detect_llm_route, env
from netops_ai.llm.retry import LLMTransportRetriesExhausted, call_with_llm_retry, retry_error_text
from netops_ai.labels import label_for
from netops_ai.playbooks.engine import (
    AI_GOTO,
    END_GOTO,
    AGENT_FILLED_PLACEHOLDERS,
    branch_error,
    branch_value,
    next_step_id,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
TRACE_DIR = REPO_ROOT / "records" / "chat-traces"
LLM_RETRY_SLEEP = time.sleep

# **加了新工具，这段里描述工具全集的话要跟着改。** 它一直写着「只有 Zabbix 和设备 show」，
# 模型就在那几个工具里打转、把预算打满；去掉这段它 3/3 次一上来就选对。不实的「全集」比少给工具更有害。
#: 系统提示只放**跨工具的规矩**。「什么时候用哪个工具」一律写在那个工具自己的
#: 描述里（`zabbix/cli.py` 的 `CommandSpec.description`、`netbox_cli.py` 同名字段、
#: `graph/chat_agent.py` 里几个手写的 `StructuredTool`）。
#:
#: **从 14 条砍到 5 条。** 维护者问「前置后置需要这么多离谱的控制吗，
#: 别我们出的教程误人子弟了」，又说「假如我是接入 cc 或者 codex 到我的网络环境，
#: 它会怎么查怎么测」。照那把尺子量下来，原来 14 条里有 9 条是「用哪个工具」，
#: 而且其中 4 条工具描述里本来就写着——同一句话两个地方各写一遍，改一处另一处就成了反例。
#:
#: 要说清楚的是：**这不省 token**。工具描述跟系统提示一样每次请求全都要发。
#: 省的是「两处打架」和「读的人不知道该信哪个」。
DEFAULT_SYSTEM_PROMPT = """你是网络运维只读查询助手。你可以自由选择工具。工具全集都是只读的，没有任何写入口：Zabbix 查询、设备 show 命令、团队 SOP、NetBox 台账与拓扑、只读巡检、本系统自己产出的历史结论。

回答要求：
- 先用工具取证，再回答，不要编造监控项、itemid 或设备输出。
- 回答“我查过什么”只能依据本会话里真实出现过的工具调用；看不到记录就说看不到，不许说没调过。
- 用户提到的地址/设备在台账和监控里都查不到时，只能说“没有任何证据表明它存在过”，请用户确认地址；不许把“路由表里没有”推成“漏配”，更不许给出改配置命令；“用户说以前是好的”不算证据。
- **工具返回里如果有 `markdown` 字段，原样贴出来，不要自己重排。**
  那是已经排好的表格（告警列表、历史结论），自己重排既不稳定又费 token，
  而且 severity 数字和 Unix 时间戳你翻一次错一次。
- 跟网络运维无关的问题，直接说这里只管网络运维，不要调用任何工具。
- 最终用中文回答，清楚写出证据和不确定性。
"""


def _default_token_budget() -> int:
    """`ANALYSIS_TOKEN_BUDGET`，0 或没配 = 不限。"""
    raw = str(env().get("ANALYSIS_TOKEN_BUDGET", "")).strip()
    if not raw:
        return 0
    try:
        value = int(float(raw))
    except ValueError:
        return 0
    return value if value > 0 else 0


def _env_switch(cfg: dict[str, str], name: str, default: bool) -> bool:
    raw = str(cfg.get(name, "")).strip().lower()
    if not raw:
        return default
    return raw not in {"off", "0", "false", "no"}


def _env_number(cfg: dict[str, str], name: str, default: float) -> float:
    try:
        value = float(str(cfg.get(name, "")).strip())
    except ValueError:
        return default
    return value if value >= 0 else default


@dataclass(frozen=True)
class LoopHygiene:
    """三件「中性的循环卫生」。**只陈述事实、不劝阻**，跟闸门（`AgentLoopBudget`）是两回事。

 撤闸实验里的确定性浪费：同一「工具+参数」反复执行（`show interfaces Gi0/4` 4 次）、
 被拒命令反复重试、每次模型调用重发完整历史（累计 prompt 41.6 万 token）、
 模型看不到自己用了多少预算。
    """

    #: 每次工具返回末尾追加一行 `[进度] …`。`AGENT_LOOP_PROGRESS=off` 关。
    progress: bool = True
    #: 同一次分析里「工具名 + 规范化参数」完全相同的调用直接返回上次结果。`AGENT_LOOP_CACHE=off` 关。
    cache: bool = True
    #: 反映当前状态的工具（设备命令、当前告警……）缓存多久。`AGENT_LOOP_CACHE_TTL`，秒。
    cache_ttl: float = 60.0
    #: 送给模型前把较早的工具结果换成占位。**默认关**，`AGENT_LOOP_COMPACT=on` 才开。
    compact: bool = False
    #: 压缩时保留最近几个工具结果原文。`AGENT_LOOP_COMPACT_KEEP`。
    compact_keep: int = 3

    @classmethod
    def from_env(cls) -> "LoopHygiene":
        cfg = env()
        return cls(
            progress=_env_switch(cfg, "AGENT_LOOP_PROGRESS", True),
            cache=_env_switch(cfg, "AGENT_LOOP_CACHE", True),
            cache_ttl=_env_number(cfg, "AGENT_LOOP_CACHE_TTL", 60.0),
            compact=_env_switch(cfg, "AGENT_LOOP_COMPACT", False),
            compact_keep=int(_env_number(cfg, "AGENT_LOOP_COMPACT_KEEP", 3)),
        )


@dataclass(frozen=True)
class AgentLoopBudget:
    #: 0 = 不限，直接喂给 LangGraph 的 `recursion_limit`（不传该键，退回它自己的默认值）。
    max_iterations: int = 18
    #: 0 = 不限。
    max_tool_calls: int = 12
    #: 连续几跳没带来新信息就停。**这一条替掉了原来的三个计数器**
    #: （同一工具连续失败 2 次 / 同一「工具+参数」第 3 次 / 同一工具连调 6 次）。
    #:
    #: 三个计数器防的是同一件事：这一跳没有让我们比上一跳知道得更多。
    #: 分成三条的代价是每条都要单独定阈值，而且**互相还留着缝**——
    #: Win 那次 `zbx_top_talkers` 返回全零榜单、模型挨个 `zbx_items`
    #: 翻了 12 步，每次 itemid 不同（哈希不重复）、每次都成功（不是失败）、
    #: 工具也在换，三道闸一道都没拦住。
    #:
    #: 换成按「有没有新信息」算之后，那 12 步从第 3 步就停了。
    #: 阈值能从 6 收到 3，是因为它不再误伤合法动作：同一轮里
    #: `topology_neighbors` 连查 A1~A4 四台，每台结果都不同，**一次都不计数**。
    #:
    #: 业界叫 action hash window（见 docs/DECISIONS.md）。0 = 不限。
    no_progress_threshold: int = 3
    #: 撞预算时，补一次**不给工具**的收尾调用，让它用已查到的东西作答。
    #: 关掉就退回「只回一句没查完」。**单测一律关掉**——它会发真请求。
    wrap_up_on_abort: bool = True
    #: 整轮循环的 token 上限（prompt + completion 累计）。0 = 不限。
    #:
    #: **为什么轮数上限不够用**：多跳循环每跳都把全量对话历史重发一遍。
    #: 4 跳看着不多，但如果每跳上下文 20k，总量是 80k 而不是 20k——
    #: `max_iterations` 数的是次数，不是体积，拦不住这种增长。
    max_tokens: int = field(default_factory=_default_token_budget)

    @classmethod
    def unbounded(cls, *, max_tokens: int | None = None) -> "AgentLoopBudget":
        """只留 LangChain/LangGraph 自己的默认上限，关掉我们额外加的三道闸。

 内部文档 A16（负责人拍板）：告警这条线先暂时不设
 `max_tool_calls`/`no_progress_threshold`，`max_iterations=0` 时
 `run_agent_loop` 不再传 `recursion_limit`，退回 LangGraph 自己的默认值。
 **这是临时状态，不是把安全闸删掉重写**——`no_progress_threshold` 当初是
 看到过真实的原地打转案例才加的（见 `_no_progress_reason` 的注释），
 取消之后必须在下一轮真机验证里盯会不会打转、token/耗时涨多少。
 token 预算默认不动（继续吃 `ANALYSIS_TOKEN_BUDGET`），因为这次要取消的
 是「轮数限制」，不是 token 限制；真要也不限就显式传 `max_tokens=0`。
        """
        return cls(
            max_iterations=0,
            max_tool_calls=0,
            no_progress_threshold=0,
            max_tokens=_default_token_budget() if max_tokens is None else max_tokens,
        )


@dataclass
class ToolCallRecord:
    tool: str
    args: dict[str, Any]
    ok: bool
    result: Any = None
    error: str = ""
    elapsed_ms: int = 0
    deduped: bool = False
    #: 缓存返回（`LoopHygiene.cache`）。同时 `deduped=True`：不消耗 max_tool_calls、计入无进展。
    cached: bool = False


def _as_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, default=str)


def _compact_sop_hint(text: str, *, limit: int = 240) -> str:  # 从 150 放宽：下一步的工具参数要摆全
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _step_action_command(step: dict[str, Any]) -> str:
    """这一步靠哪个参数跟实际调用对上。加了 `key`：原来 zbx_items 步骤没有可比的参数，
 任何一次 zbx_items 调用（查 icmppingloss、查 snmp 可用性……）都会被认成这一步。
    """
    action = step.get("action") or {}
    return str(
        action.get("command") or action.get("key_contains") or action.get("key")
        or action.get("interface") or action.get("host") or ""
    ).strip()


def _command_matches(expected: str, got: str, *, loose: bool = False) -> bool:
    """SOP 里的命令跟实际调用的命令比。空白折叠、不分大小写；`<接口>`（告警里没有接口时的占位）
    和 `<对端地址>`（agent 从前一步输出里取）各匹配任意一个不含空白的词。`loose=True` 只比 `|` 之前的部分。"""

    def norm(text: str) -> str:
        text = " ".join(str(text).split())
        return text.split("|", 1)[0].strip() if loose else text

    pattern = re.escape(norm(expected))
    for placeholder in AGENT_FILLED_PLACEHOLDERS:
        pattern = pattern.replace(re.escape(placeholder), r"\S+")
    return re.fullmatch(pattern, norm(got), flags=re.IGNORECASE) is not None


def sop_action_text(step: dict[str, Any]) -> str:
    """把 SOP 步骤的 action 原样摆出来：`tool=zbx_syslog; since=...; until=...`。

 原来只摆 `command`（没有就退到 `key_contains`），`topology_neighbors` 的 `interface`
 会被丢掉，`zbx_history` 的 `key_contains: syslog` 又被显示成 `command=syslog`——
 一个工具没有的参数（外部 agent 反馈第 1 条）。现在参数名就是工具的真实参数名。
    """
    action = step.get("action") or {}
    parts = [f"tool={action.get('tool', '')}"]
    parts.extend(f"{key}={value}" for key, value in action.items() if key not in {"tool", "render_warning"})
    return "; ".join(parts)


def sop_branches_text(step: dict[str, Any]) -> str:
    """`条件→去向, …`，SOP 计划和 [SOP 下一步] 共用。"""
    return ", ".join(
        f"{b.get('when', '')}→{b.get('goto', '')}" for b in (step.get("branches") or []) if isinstance(b, dict)
    )


#: 计划外步骤（辅助步骤、超出 max_main_steps 的主步骤）在 [SOP 下一步] 里的 why/expect 封顶。
#: action 和 branches 不封顶：的教训是模型看不到命令和走完后的去向。
SOP_HINT_WHY_LIMIT = 320
SOP_HINT_EXPECT_LIMIT = 160


def _sop_step_detail(step: dict[str, Any]) -> str:
    """计划里没列出的步骤，走到时给全：why/expect（封顶）+ action 原样 + branches 原样。"""
    parts = [
        f"why={_compact_sop_hint(str(step.get('why') or ''), limit=SOP_HINT_WHY_LIMIT)}",
        f"expect={_compact_sop_hint(str(step.get('expect') or ''), limit=SOP_HINT_EXPECT_LIMIT)}",
        sop_action_text(step),
    ]
    branches = sop_branches_text(step)
    if branches:
        parts.append(f"branches: {branches}")
    return "; ".join(parts)


def _record_command(record: ToolCallRecord) -> str:
    if record.tool == "device_show":
        return str(record.args.get("command") or "").strip()
    if record.tool == "device_show_many":
        commands = record.args.get("commands") or []
        return "\n".join(str(c).strip() for c in commands)
    for key in ("command", "key_contains", "key", "interface", "item_id", "host"):
        if record.args.get(key):
            return str(record.args.get(key)).strip()
    return ""


def _result_output(record: ToolCallRecord) -> str:
    result = record.result
    if isinstance(result, dict):
        if "output" in result:
            return str(result.get("output") or "")
        if "results" in result:
            return "\n".join(str(item.get("output") or item.get("error") or "") for item in result.get("results") or [])
    return _as_text(result)


def _result_is_empty(record: ToolCallRecord) -> bool:
    result = record.result
    if isinstance(result, dict):
        if result.get("empty"):
            return True
        if "output" in result:
            return not str(result.get("output") or "").strip()
    return result in (None, "", [], {})


def _result_is_denied(record: ToolCallRecord) -> bool:
    result = record.result
    if isinstance(result, dict):
        if result.get("allowed") is False:
            return True
        if result.get("denial_reason"):
            return True
    text = f"{record.error} {_as_text(result)}".lower()
    return "whitelist" in text or "denied" in text or "not allowed" in text


@dataclass
class SopRuntimeState:
    data: dict[str, Any]
    statuses: dict[str, str] = field(init=False)
    call_indexes: dict[str, int] = field(default_factory=dict)
    invalid_steps: list[str] = field(default_factory=list)
    deviations: list[str] = field(default_factory=list)
    first_evidence_call_index: int | None = None
    #: 上一句 [SOP 下一步] 指向、还没走的那一步
    pending: str | None = None

    def __post_init__(self) -> None:
        self.statuses = {str(s.get("id") or ""): "not_reached" for s in self.steps if s.get("id")}

    @property
    def steps(self) -> list[dict[str, Any]]:
        return list(self.data.get("steps") or [])

    @property
    def matched(self) -> bool:
        return bool(self.data.get("matched"))

    @property
    def main_ids(self) -> list[str]:
        return [str(s.get("id")) for s in self.steps if s.get("id") and s.get("main", True)]

    @property
    def plan_ids(self) -> set[str]:
        """初始 SOP 计划里列出的步骤：前 `limits.max_main_steps` 个主步骤（同 `pipeline._render_sop_plan`）。"""
        try:
            max_main_steps = int((self.data.get("limits") or {}).get("max_main_steps", 5))
        except (TypeError, ValueError):
            max_main_steps = 5
        return set(self.main_ids[:max_main_steps])

    def _step_by_id(self, step_id: str) -> dict[str, Any] | None:
        return next((s for s in self.steps if str(s.get("id") or "") == step_id), None)

    def _matches(self, record: ToolCallRecord, step: dict[str, Any], *, loose: bool = False) -> bool:
        """这次调用是不是在走这一步。`loose` 只给「上一句 [SOP 下一步] 指向的那一步」用：
        命令 `|` 之前的部分对上就算（`show logging | include …` 的过滤条件各人写法不同）。
        """
        action = step.get("action") or {}
        if str(action.get("tool") or "") != record.tool:
            return False
        command = _step_action_command(step)
        if not command:
            return True
        record_command = _record_command(record)
        candidates = (
            [line.strip() for line in record_command.splitlines()]
            if record.tool == "device_show_many"
            else [record_command]
        )
        return any(_command_matches(command, got, loose=loose) for got in candidates)

    def _matching_step(self, record: ToolCallRecord) -> dict[str, Any] | None:
        """先认 [SOP 下一步] 刚指向的那一步，再认没走过的，最后才是走过的。

 验证：Trap 告警名不带接口，triage_interface 的命令被整条拿掉，于是**任何** device_show
 都「匹配」到它（排在第一）；local_log_buffer 的 `show logging` 输出里有 administratively down，
 提示又指回 local_shutdown_evidence——一条刚查过、结果为空的 zbx_syslog（外部 agent 反馈第 1 条）。
        """
        if self.pending:
            pending = self._step_by_id(self.pending)
            if pending is not None and self._matches(record, pending, loose=True):
                return pending
        matching = [step for step in self.steps if self._matches(record, step)]
        fresh = [s for s in matching if self.statuses.get(str(s.get("id") or "")) == "not_reached"]
        return (fresh or matching or [None])[0]

    def hint_for(self, record: ToolCallRecord, *, tool_call_index: int) -> str:
        if not self.matched:
            return ""
        if record.deduped:
            return ""
        if self.first_evidence_call_index is None and record.tool != "sop_lookup":
            self.first_evidence_call_index = tool_call_index
        step = self._matching_step(record)
        if step is None:
            return ""
        step_id = str(step.get("id") or "")
        if not step_id:
            return ""
        denied = _result_is_denied(record)
        failed = bool(record.error) or (not record.ok and not denied)
        if denied:
            status = "denied"
        elif failed:
            status = "failed"
        elif _result_is_empty(record):
            status = "empty"
        else:
            status = "done"
        self.statuses[step_id] = status
        self.call_indexes[step_id] = tool_call_index
        self.pending = None
        # empty 跟剧本执行器一样记进 invalid_steps（这一步没取到证据），但分支按 default 走，见 engine.branch_error
        if status in {"denied", "failed", "empty"} and step_id not in self.invalid_steps:
            self.invalid_steps.append(step_id)
        output = _result_output(record)
        # `when: value == …` 按工具的真实返回判断（原来一直传 None，device-unreachable 的分支从没触发过）。
        # 只有真正取到结果（done）才有 value；报错/被拒/空结果都是 None，照旧走 error 或 default。
        value = branch_value(record.tool, step.get("action") or {}, record.result) if status == "done" else None
        goto = next_step_id(step, output=output, error=branch_error(status), value=value)
        if branch_error(status):
            prefix = "这一步报错或被拒绝，按 error 分支；"
        elif status == "empty":
            prefix = "这一步返回为空，按 default 分支；"
        else:
            prefix = ""
        if goto == END_GOTO:
            return f"\n[{prefix}SOP 已走完，可以收尾]"
        if goto == AI_GOTO:
            return f"\n[{prefix}SOP 到此结束，剩下的自己判断]"
        next_step = self._step_by_id(goto)
        if next_step is None:
            return f"\n[{prefix}SOP 到此结束，剩下的自己判断]"
        if self.statuses.get(goto, "not_reached") != "not_reached":
            # 走过的步骤不再指回：再走一遍结果不会变，只会原地打转（验证 外部 agent 反馈第 1 条）
            return (
                f"\n[{prefix}分支指向的 {goto} 已在第 {self.call_indexes.get(goto)} 次工具调用走过"
                f"（{self.statuses[goto]}），SOP 到此结束，剩下的自己判断]"
            )
        self.pending = goto
        if goto not in self.plan_ids:
            # 计划里不再列辅助步骤，这里是模型第一次看到它，命令和去向都要给全。
            return f"\n[{prefix}SOP 下一步：{goto}（计划外步骤）——{_sop_step_detail(next_step)}]"
        why = str(next_step.get("why") or "").split("。", 1)[0]
        return "\n[" + _compact_sop_hint(f"{prefix}SOP 下一步：{goto}——{why}；{sop_action_text(next_step)}") + "]"

    def usage(self) -> dict[str, Any]:
        main_ids = self.main_ids
        done_main = [sid for sid in main_ids if self.statuses.get(sid) == "done"]
        coverage = round(len(done_main) / len(main_ids), 3) if main_ids else 0.0
        first_main_index = min((self.call_indexes[sid] for sid in done_main if sid in self.call_indexes), default=None)
        if not self.matched:
            adherence = "not_matched"
        elif not done_main:
            adherence = "matched_not_used"
        elif coverage >= 0.6 and (self.first_evidence_call_index is None or self.first_evidence_call_index == first_main_index):
            adherence = "used"
        else:
            adherence = "used_with_deviation"
        return {
            "playbook": self.data.get("playbook") if self.matched else None,
            "matched": self.matched,
            "matched_because": list(self.data.get("matched_because") or []),
            "steps": [
                {
                    "id": sid,
                    "status": self.statuses.get(sid, "not_reached"),
                    "tool_call_index": self.call_indexes.get(sid),
                }
                for sid in [str(s.get("id")) for s in self.steps if s.get("id")]
            ],
            "coverage": coverage,
            "adherence": adherence,
            "deviations": list(self.deviations),
            "invalid_steps": list(self.invalid_steps),
        }


@dataclass(frozen=True)
class _HistoryQueryRecord:
    index: int
    tool: str
    key: tuple[str, str]
    window: tuple[int, int]
    window_label: str
    queried_at: int
    end_is_now: bool


@dataclass(frozen=True)
class _CachedCall:
    #: 第几次工具调用（1 起，跟 `trace.tool_calls` 的下标 +1 对应）
    index: int
    at: float
    #: 那次返回给模型的原文，去掉了 SOP 提示和进度行
    reply: Any


def _result_is_whitelist_denial(record: ToolCallRecord) -> bool:
    result = record.result
    return isinstance(result, dict) and (result.get("allowed") is False or bool(result.get("denial_reason")))


#: 允许把 result 一起推给前端的工具。加之前先想清楚这个工具的返回有多大。
_RESULT_IN_EVENT = frozenset({"zbx_chart"})


class AgentLoopAbort(RuntimeError):
    """Raised inside a tool call when the shared loop must stop honestly."""


@dataclass
class ChatRunTrace:
    trace_id: str
    question: str
    session_id: str
    started_at: float = field(default_factory=time.time)
    tool_calls: list[ToolCallRecord] = field(default_factory=list)
    final_answer: str = ""
    final_structured: dict[str, Any] | None = None
    #: 整条 tool-calling 轨迹的文本。**收尾那次结构化调用看的就是它**，
    #: 逐字引文核对也拿它当底本——核对器看到的必须跟模型看到的一模一样。
    transcript: str = ""
    error: str = ""
    termination_reason: str = ""
    final_schema_from: str = ""
    final_schema_abort_reason: str = ""
    model_call_count: int = 0
    elapsed_ms: int = 0
    incomplete: bool = False
    #: 模型最近一次说的话。预算中途把循环掐断时，`agent.invoke()` 抛出去、
    #: 消息拿不到了，靠它留住已经写好的探路小结（的 5 条 qwen 记录都是
    #: 「结论已经完整、下一次工具调用才被打断」）。
    last_model_text: str = ""
    budget: dict[str, Any] = field(default_factory=dict)
    plan: str = ""
    sop: SopRuntimeState | None = None
    sop_usage: dict[str, Any] = field(default_factory=dict)

    #: 每次工具调用完回调一次，给前端推进度。挂在 `record_tool_call()` 上，因为那是所有工具的必经之路。
    on_event: Any = None

    def emit(self, kind: str, **payload: Any) -> None:
        """推一个事件。**绝不抛异常**——前端断线、队列满、回调写错，都不该
        让一次真实分析跟着崩。和记账、飞书上报一个待遇。
        """
        if self.on_event is None:
            return
        try:
            self.on_event({"kind": kind, "trace_id": self.trace_id, **payload})
        except Exception:  # noqa: BLE001 - 推进度永远不许影响主链路
            pass

    #: 本轮累计消耗的 token（prompt + completion），由挂在模型上的回调累加。
    tokens_spent: int = 0

    #: 连续多少跳没带来新信息。见 `_no_progress_reason()`。
    _no_progress_streak: int = 0
    #: 问过的「工具+参数」。判「这个问过了」用，不做别的。
    _asked: set = field(default_factory=set)
    #: 历史类查询窗口。专门拦同 itemid/同主机的重叠时间窗，避免换个 since 重查一遍。
    _history_queries: list[_HistoryQueryRecord] = field(default_factory=list)
    #: 完全相同调用的缓存：(工具名, 规范化参数 JSON) → `_CachedCall`。见 `apply_loop_hygiene()`。
    _call_cache: dict = field(default_factory=dict)
    #: 缓存新鲜度用的时钟（秒）。单测注入假时钟，不真 sleep。
    clock: Any = None
    #: 最近一次追加到工具返回末尾的 SOP 提示。缓存存的是去掉它之后的返回。
    _last_sop_hint: str = ""

    def charge_tokens(self, prompt_tokens: int, completion_tokens: int) -> None:
        """记一次模型调用的消耗，只累加不中断。中断在 `record_tool_call()` 里做：
        回调线程里抛的异常会被 LangChain 吞掉。
        """
        self.tokens_spent += max(0, int(prompt_tokens)) + max(0, int(completion_tokens))

    def record_tool_call(self, record: ToolCallRecord, budget: AgentLoopBudget) -> None:
        self.tool_calls.append(record)
        if self.sop is not None:
            self.sop_usage = self.sop.usage()
        self._remember_history_query(record)
        self.emit(
            "tool_call",
            tool=record.tool,
            tool_label=label_for(record.tool),
            args=record.args,
            ok=record.ok,
            error=record.error,
            elapsed_ms=record.elapsed_ms,
            index=len(self.tool_calls),
            budget_max=budget.max_tool_calls,
            deduped=record.deduped,
            # 只有画图工具把结果带出去，给上层渲染成 PNG。
            # **别对所有工具这么干**：`device_show` 的回显能有几千行，
            # 塞进 SSE 是自找的。
            result=record.result if record.tool in _RESULT_IN_EVENT and record.ok else None,
        )
        # token 预算先于轮数预算检查：**贵的是体积，不是次数**。
        # 超了就带着已经查到的证据出一个残缺结论（`AgentLoopAbort` 会被
        # 接住并走这条路），不是继续烧——已经花掉的 token 不能白花。
        if budget.max_tokens and self.tokens_spent >= budget.max_tokens:
            self.incomplete = True
            self.termination_reason = (
                f"token 预算已用完：{self.tokens_spent}/{budget.max_tokens}"
                f"（ANALYSIS_TOKEN_BUDGET）。已查了 {len(self.tool_calls)} 步，"
                "带着现有证据收尾。要查完就提高预算或收窄问题。"
            )
            raise AgentLoopAbort(self.termination_reason)

        charged_tool_calls = sum(1 for call in self.tool_calls if not call.deduped)
        if budget.max_tool_calls and not record.deduped and charged_tool_calls >= budget.max_tool_calls:
            self.incomplete = True
            self.termination_reason = (
                f"工具调用预算已用完：max_tool_calls={budget.max_tool_calls}。"
                "本轮没有查完，需要收窄问题或提高预算后继续。"
            )
            raise AgentLoopAbort(self.termination_reason)

        # ---- 无进展检测（业界的第四道闸）----
        # **放在成败判断之前**：打转的调用往往每次都"成功"，只是没带来新信息。
        why = self._no_progress_reason(record)
        if not why:
            self._no_progress_streak = 0
            return
        self._no_progress_streak += 1
        if budget.no_progress_threshold and self._no_progress_streak >= budget.no_progress_threshold:
            self.incomplete = True
            self.termination_reason = (
                f"连续 {self._no_progress_streak} 跳没带来新信息（最后一跳：{record.tool}，{why}）。"
                "再查下去拿不到新东西，这条路停了——带着已经查到的证据出结论，"
                "或者把问题收窄再问。"
            )
            raise AgentLoopAbort(self.termination_reason)

    def sop_hint_for_tool_result(self, record: ToolCallRecord) -> str:
        if self.sop is None:
            return ""
        hint = self.sop.hint_for(record, tool_call_index=len(self.tool_calls) + 1)
        self.sop_usage = self.sop.usage()
        self._last_sop_hint = hint
        return hint

    def now(self) -> float:
        return float((self.clock or time.monotonic)())

    def progress_line(self, budget: AgentLoopBudget) -> str:
        """`[进度] …`：只报数字，不说该怎么办。数字跟闸门判断用的是同一份计数。"""

        def limit(value: int) -> str:
            return f"上限 {value}" if value else "无上限"

        charged = sum(1 for call in self.tool_calls if not call.deduped)
        free = len(self.tool_calls) - charged
        parts = [
            f"本次分析已调用工具 {charged} 次（{limit(budget.max_tool_calls)}）",
            f"连续无新信息 {self._no_progress_streak} 次（{limit(budget.no_progress_threshold)}）",
        ]
        if free:
            parts.append(f"另有 {free} 次缓存或去重返回未计入调用次数")
        if budget.max_tokens:
            parts.append(f"token 已用 {self.tokens_spent} / 上限 {budget.max_tokens}")
        return "\n[进度] " + "；".join(parts)

    def cached_call(self, key: tuple[str, str], ttl: float | None) -> "_CachedCall | None":
        """`ttl=None` 表示本次分析内一直有效；过期返回 None（调用方重新执行并覆盖这一条）。"""
        entry = self._call_cache.get(key)
        if entry is None:
            return None
        if ttl is not None and self.now() - entry.at > ttl:
            return None
        return entry

    def remember_call(self, key: tuple[str, str], record: ToolCallRecord, reply: Any) -> None:
        """只缓存成功的和被白名单拒绝的。报错（连不上、超时）不缓存：那是瞬时状态，缓存了就成了粘住的失败。"""
        if record.deduped or not (record.ok or _result_is_whitelist_denial(record)):
            return
        base = reply
        hint = self._last_sop_hint
        if isinstance(reply, str) and hint and reply.endswith(hint):
            base = reply[: -len(hint)]
        self._call_cache[key] = _CachedCall(index=len(self.tool_calls), at=self.now(), reply=base)

    def _no_progress_reason(self, record: ToolCallRecord) -> str:
        """这一跳有没有让我们比上一跳知道得更多。返回空串表示有，否则是原因。

 三种算没有：调用失败、工具自己说返回是空的、这个「工具+参数」问过了。

 **一开始写的是「返回跟之前某次一模一样」，被测试当场打回**：
 连扫 A1~A4 四台接入是合法动作，四台要是都干净，返回就是四份一样的空——
 按返回比会在第三台停手。换成按「问过没问过」比就没这问题：
 换了参数就是在问新东西，哪怕答案一样也是新知识。

 「工具自报 empty」那条要靠工具配合，见 `chat_agent._tool_reply`
 给空结果打的标记，和 `zabbix/cli.py` 里全零榜单自己承认是空的那段。
 那次 `zbx_top_talkers` 全零之后挨个翻 `zbx_items` 12 步，
 每次参数都不同、每次都"成功"，就是靠这条才拦得住。
        """
        if not record.ok:
            return "调用失败"
        if record.cached:
            return "与之前某次调用的工具和参数完全相同，返回的是缓存"
        if record.deduped:
            return "重复历史查询已拦截"
        if isinstance(record.result, dict) and record.result.get("empty"):
            return "工具说这次返回是空的"
        key = (record.tool, json.dumps(record.args, ensure_ascii=False, sort_keys=True, default=str))
        if key in self._asked:
            return "这个工具用这组参数已经问过了"
        self._asked.add(key)
        return ""

    def find_history_duplicate(
        self, tool: str, args: dict[str, Any], *, now: int | None = None
    ) -> _HistoryQueryRecord | None:
        """跟之前哪次历史查询的窗口重叠；None 表示放行。

        解析不了时间窗时放行。`until` 为空表示查到现在；如果再次查"到现在"且距上次
        已超过 60 秒，就把它当新窗口，允许用户显式要"最新"。
        """
        query = _normalize_history_query(tool, args, now=now)
        if query is None:
            return None
        q_key, q_window, q_label, q_now, q_end_is_now = query
        for old in self._history_queries:
            if old.key != q_key:
                continue
            if q_end_is_now and old.end_is_now and q_now - old.queried_at > 60:
                continue
            if _windows_overlap(q_window, old.window):
                return old
        return None

    @staticmethod
    def dedup_note(old: _HistoryQueryRecord) -> str:
        """只陈述发生了什么。**上次的结果由调用方原样附上**（`chat_agent._dedup_reply`）：
 外部 agent 反馈，只说「结果已在上下文里」时外部 agent 看不到那次结果，没法引用也没法判断。
        """
        return (
            f"{_dedup_subject(old.key)} 与 #{old.index} 号调用的查询窗口重叠"
            f"（窗口 {old.window_label}），这次没有重新查询；previous_result 是 #{old.index} 号调用的返回。"
            "与已查窗口不重叠的区间会正常查询。"
            "这次不消耗 max_tool_calls，计入一次无进展。"
        )

    def dedupe_history_query(self, tool: str, args: dict[str, Any], *, now: int | None = None) -> str:
        """返回重复历史查询的说明；空串表示放行。"""
        old = self.find_history_duplicate(tool, args, now=now)
        return "" if old is None else self.dedup_note(old)

    def _remember_history_query(self, record: ToolCallRecord) -> None:
        if not record.ok or record.deduped:
            return
        query = _normalize_history_query(record.tool, record.args)
        if query is None:
            return
        q_key, q_window, q_label, q_now, q_end_is_now = query
        self._history_queries.append(
            _HistoryQueryRecord(
                index=len(self.tool_calls),
                tool=record.tool,
                key=q_key,
                window=q_window,
                window_label=q_label,
                queried_at=q_now,
                end_is_now=q_end_is_now,
            )
        )


_RELATIVE_TIME_RE = re.compile(r"^-(\d+)([mhdw])$")
_RELATIVE_SECONDS = {"m": 60, "h": 3600, "d": 86400, "w": 604800}
_HISTORY_TOOLS = {"zbx_history", "zbx_trends"}


def _parse_query_time(value: Any, *, now: int) -> int | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.isdigit():
        return int(text)
    match = _RELATIVE_TIME_RE.match(text)
    if match:
        amount, unit = match.groups()
        return now - int(amount) * _RELATIVE_SECONDS[unit]
    return None


def _normalize_history_window(args: dict[str, Any], *, now: int) -> tuple[tuple[int, int], str, bool] | None:
    start = _parse_query_time(args.get("since", "-1h"), now=now)
    if start is None:
        return None
    raw_until = str(args.get("until", "") or "").strip()
    end_is_now = not raw_until
    end = now if end_is_now else _parse_query_time(raw_until, now=now)
    if end is None or start >= end:
        return None
    end_label = "now" if end_is_now else str(raw_until)
    return (start, end), f"{args.get('since', '-1h')}..{end_label} [{start},{end}]", end_is_now


def _normalize_history_query(
    tool: str, args: dict[str, Any], *, now: int | None = None
) -> tuple[tuple[str, str], tuple[int, int], str, int, bool] | None:
    now = int(time.time()) if now is None else now
    if tool in _HISTORY_TOOLS:
        item_id = str(args.get("item_id", "")).strip()
        if not item_id:
            return None
        window = _normalize_history_window(args, now=now)
        if window is None:
            return None
        q_window, q_label, end_is_now = window
        return ("item_id", item_id), q_window, q_label, now, end_is_now
    if tool == "zbx_syslog":
        host = str(args.get("host", "")).strip().lower()
        if not host:
            return None
        include = str(args.get("include", "") or "")
        window = _normalize_history_window(args, now=now)
        if window is None:
            return None
        q_window, q_label, end_is_now = window
        return ("syslog", f"{host}\0{include}"), q_window, q_label, now, end_is_now
    return None


def _windows_overlap(left: tuple[int, int], right: tuple[int, int]) -> bool:
    return left[0] < right[1] and right[0] < left[1]


def _dedup_subject(key: tuple[str, str]) -> str:
    kind, value = key
    if kind == "item_id":
        return f"itemid {value}"
    host = value.split("\0", 1)[0]
    return f"主机 {host} 的 syslog"


# ---- 循环卫生：完全相同调用的缓存 + 逐轮进度 ----

#: 本次分析内一直有效：台账/拓扑/SOP/文档/本系统历史结论，几分钟内不会变。
_CACHE_PERMANENT_TOOLS = frozenset(
    {"topology_neighbors", "sop_lookup", "doc_search", "list_analyses", "get_analysis"}
)
#: 不缓存。`zbx_chart` 的 result 要推给前端画图、对话收尾也按 `c.tool == chart` 从 trace 里捞图，
#: 缓存返回的记录里没有图数据，会让第二张图静默消失。
_CACHE_NEVER_TOOLS = frozenset({"zbx_chart"})
#: 带时间窗的历史类：窗口两端都是绝对时间才算「固定窗口」，否则窗口随 now 移动。
_CACHE_WINDOW_TOOLS = frozenset({"zbx_syslog", "zbx_history", "zbx_trends"})


def _is_absolute_time(value: Any) -> bool:
    return str(value if value is not None else "").strip().isdigit()


def cache_ttl(tool: str, args: dict[str, Any], default_ttl: float) -> float | None:
    """这次调用的结果缓存多久：`None` = 本次分析内一直有效，`0` = 不缓存，正数 = 秒。"""
    if tool in _CACHE_NEVER_TOOLS:
        return 0
    if tool in _CACHE_PERMANENT_TOOLS or tool.startswith("nb_"):
        return None
    if tool in _CACHE_WINDOW_TOOLS:
        # zbx_history/zbx_trends 目前没有 until 参数（结束时间固定是现在），所以总是落到 TTL。
        if _is_absolute_time(args.get("since")) and _is_absolute_time(args.get("until")):
            return None
        return default_ttl
    # 其余都当「反映当前状态」：device_show(_many)、zbx_problems/items/hosts/top_talkers、run_inspection、没登记的新工具。
    return default_ttl


def _strip_strings(value: Any) -> Any:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        return [_strip_strings(v) for v in value]
    if isinstance(value, dict):
        return {k: _strip_strings(v) for k, v in value.items()}
    return value


def normalized_tool_args(tool: BaseTool, kwargs: dict[str, Any]) -> dict[str, Any]:
    """过一遍工具自己的 args_schema：补上默认值（`limit` 不填和填 50 算同一个）、
    `item_id` 的整数/字符串统一；再去掉字符串首尾空白。参数顺序由 `sort_keys` 处理。"""
    data: dict[str, Any] = dict(kwargs)
    schema = getattr(tool, "args_schema", None)
    if isinstance(schema, type) and hasattr(schema, "model_validate"):
        try:
            data = schema.model_validate(kwargs).model_dump()
        except Exception:  # noqa: BLE001 - 校验不过就按原样比，交给工具自己报错
            data = dict(kwargs)
    return _strip_strings(data)


def _cache_note(index: int) -> str:
    return f"与第 {index} 次调用的工具和参数相同，未重新执行；result 是那次的返回。"


def _cached_reply(tool: BaseTool, args: dict[str, Any], entry: _CachedCall, trace: ChatRunTrace, budget: AgentLoopBudget) -> str:
    age = max(0, int(round(trace.now() - entry.at)))
    meta = {"cached": True, "same_as_call": entry.index, "age_seconds": age, "note": _cache_note(entry.index)}
    try:
        previous: Any = json.loads(entry.reply) if isinstance(entry.reply, str) else entry.reply
    except ValueError:
        previous = entry.reply
    reply = json.dumps({**meta, "result": previous}, ensure_ascii=False, default=str)
    record = ToolCallRecord(tool=tool.name, args=args, ok=True, result=meta, deduped=True, cached=True)
    trace.record_tool_call(record, budget)
    return reply


def _hygienic_func(func: Callable[..., Any], tool: BaseTool, trace: ChatRunTrace, budget: AgentLoopBudget, hygiene: LoopHygiene):
    def run(*args: Any, **kwargs: Any) -> Any:
        key = None
        if hygiene.cache and not args:
            norm = normalized_tool_args(tool, kwargs)
            ttl = cache_ttl(tool.name, norm, hygiene.cache_ttl)
            if ttl != 0:
                key = (tool.name, json.dumps(norm, ensure_ascii=False, sort_keys=True, default=str))
                entry = trace.cached_call(key, ttl)
                if entry is not None:
                    reply = _cached_reply(tool, norm, entry, trace, budget)
                    return reply + trace.progress_line(budget) if hygiene.progress else reply
        before = len(trace.tool_calls)
        trace._last_sop_hint = ""
        reply = func(*args, **kwargs)
        # 工具没往 trace 里记（测试里的替身工具）就不缓存：对不上是第几次调用。
        if key is not None and len(trace.tool_calls) == before + 1:
            trace.remember_call(key, trace.tool_calls[-1], reply)
        if hygiene.progress and isinstance(reply, str):
            reply += trace.progress_line(budget)
        return reply

    return run


def apply_loop_hygiene(
    tools: Sequence[BaseTool], trace: ChatRunTrace, budget: AgentLoopBudget, hygiene: LoopHygiene
) -> list[BaseTool]:
    """给每个工具套一层：先查缓存，再执行，最后在返回末尾追加进度行。

    套在工具外面而不是写进每个工具：`sop_lookup`/`run_inspection`/历史/文档这几个工具不走
    `_append_sop_hint`，写在里面会漏；而且工具自己在 finally 里 `record_tool_call()`，
    外层拿到返回时计数已经更新，进度行的数字就是闸门此刻用的那份。
    缓存在 `find_history_duplicate`（重叠窗口拦截，在工具里面）之前。
    """
    if not (hygiene.progress or hygiene.cache):
        return list(tools)
    wrapped: list[BaseTool] = []
    for tool in tools:
        func = getattr(tool, "func", None)
        if func is None:
            wrapped.append(tool)
            continue
        wrapped.append(tool.model_copy(update={"func": _hygienic_func(func, tool, trace, budget, hygiene)}))
    return wrapped


# ---- 循环卫生：上下文压缩（默认关）----

COMPACT_HEAD_CHARS = 400


def _args_summary(args: Any, *, limit: int = 120) -> str:
    text = json.dumps(args, ensure_ascii=False, sort_keys=True, default=str)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def compaction_placeholder(index: int, tool: str, args: Any, text: str, *, head_chars: int = COMPACT_HEAD_CHARS) -> str:
    head = text[:head_chars]
    return (
        f"[第 {index} 次工具调用（{tool}，{_args_summary(args)}）的结果，共 {len(text)} 字符，"
        f"已省略 {len(text) - len(head)} 字符；开头 {len(head)} 字符：{head}]"
    )


def compact_tool_messages(messages: Sequence[Any], *, keep: int = 3, head_chars: int = COMPACT_HEAD_CHARS) -> list[Any]:
    """返回一份**新列表**：最近 `keep` 个工具结果保留原文，更早的换成占位。原消息对象一个都不改。

    只用在「每次模型调用前」（`_compaction_middleware`）。state 里的消息、`trace.tool_calls[*].result`、
    `trace.transcript` 都是全量原文——收尾结构化调用和逐字引文核对看的是那些。
    """
    tool_positions = [i for i, m in enumerate(messages) if isinstance(m, ToolMessage)]
    older = tool_positions[: max(0, len(tool_positions) - max(0, keep))]
    if not older:
        return list(messages)
    call_args: dict[str, Any] = {}
    for message in messages:
        for call in getattr(message, "tool_calls", None) or []:
            if call.get("id"):
                call_args[call["id"]] = call.get("args")
    ordinal = {pos: n for n, pos in enumerate(tool_positions, start=1)}
    out = list(messages)
    for pos in older:
        message = messages[pos]
        text = message_text(message) if isinstance(message.content, (str, list)) else str(message.content)
        if len(text) <= head_chars:
            continue
        placeholder = compaction_placeholder(
            ordinal[pos], message.name or "", call_args.get(message.tool_call_id, {}), text, head_chars=head_chars
        )
        out[pos] = message.model_copy(update={"content": placeholder})
    return out


def _compaction_middleware(keep: int):
    @wrap_model_call
    def compact_history(request, handler):  # noqa: ANN001 - LangChain middleware 签名
        return handler(request.override(messages=compact_tool_messages(request.messages, keep=keep)))

    return compact_history


@dataclass
class AgentLoopResult:
    answer: str
    messages: list[Any]
    trace: ChatRunTrace
    trace_path: Path
    structured: dict[str, Any] | None = None
    incomplete: bool = False
    termination_reason: str = ""


ToolFactory = Callable[[ChatRunTrace, AgentLoopBudget], Sequence[BaseTool]]


def _json_safe(value: Any, *, max_text: int = 6000) -> Any:
    if isinstance(value, str):
        return value if len(value) <= max_text else value[:max_text] + "...[truncated]"
    if isinstance(value, list):
        return [_json_safe(v, max_text=max_text) for v in value[:200]]
    if isinstance(value, dict):
        return {str(k): _json_safe(v, max_text=max_text) for k, v in value.items()}
    return value


def _redact_env_values(value: Any) -> Any:
    cfg = env()
    secrets = sorted(
        {
            v
            for k, v in cfg.items()
            if v
            and len(v) >= 4
            and any(marker in k.upper() for marker in ("PASSWORD", "TOKEN", "KEY", "COMMUNITY", "HOST", "URL", "USER"))
        },
        key=len,
        reverse=True,
    )
    if isinstance(value, str):
        text = value
        for secret in secrets:
            text = text.replace(secret, "<redacted>")
        return text
    if isinstance(value, list):
        return [_redact_env_values(v) for v in value]
    if isinstance(value, dict):
        return {k: _redact_env_values(v) for k, v in value.items()}
    return value


def save_trace(trace: ChatRunTrace) -> Path:
    TRACE_DIR.mkdir(parents=True, exist_ok=True)
    path = TRACE_DIR / f"{trace.trace_id}.json"
    payload = {
        "trace_id": trace.trace_id,
        "question": trace.question,
        "session_id": trace.session_id,
        "started_at": trace.started_at,
        "finished_at": time.time(),
        "elapsed_ms": trace.elapsed_ms,
        "model_call_count": trace.model_call_count,
        "tool_call_count": len(trace.tool_calls),
        "tokens_spent": trace.tokens_spent,
        "budget": trace.budget,
        "plan": trace.plan,
        "sop_usage": trace.sop_usage,
        "incomplete": trace.incomplete,
        "termination_reason": trace.termination_reason,
        "tool_calls": [
            {
                "tool": c.tool,
                "tool_label": label_for(c.tool),
                "args": _json_safe(c.args),
                "ok": c.ok,
                "result": _json_safe(c.result),
                "error": c.error,
                "elapsed_ms": c.elapsed_ms,
                "deduped": c.deduped,
                "cached": c.cached,
            }
            for c in trace.tool_calls
        ],
        "final_answer": trace.final_answer,
        "final_structured": _json_safe(trace.final_structured),
        "final_schema_from": trace.final_schema_from,
        "final_schema_abort_reason": trace.final_schema_abort_reason,
        "error": trace.error,
    }
    payload = _redact_env_values(payload)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def message_text(message: Any) -> str:
    """取消息里的文本。列表型 content 只拼文本 part，没有就是空串——
    以前退到 `json.dumps([])`，把字符串 "[]" 当成回答返回给了用户。
    """
    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        text_parts = [part.get("text", "") for part in content if isinstance(part, dict) and part.get("type") == "text"]
        return "\n".join(text_parts)
    return json.dumps(content, ensure_ascii=False)


def _system_prompt(plan: str | None, base: str | None = None) -> str:
    base = base or DEFAULT_SYSTEM_PROMPT
    if not plan:
        return base
    return base + "\n\n本次排查建议顺序：\n" + plan.strip()


def _count_model_messages(messages: list[Any]) -> int:
    return sum(1 for message in messages if isinstance(message, AIMessage))


def _emit_llm_retry(trace: ChatRunTrace, attempt: int, exc: BaseException) -> None:
    trace.emit("llm_retry", attempt=attempt, error=f"{type(exc).__name__}: {exc}")


def _wrap_up_call(question: str, trace: ChatRunTrace, reason: str) -> str:
    """预算用完时，**不给工具**再问一次，让它用已经查到的东西作答。

 为什么需要这一步：`last_model_text` 只在模型输出正文时才有内容，
 而工具调用那一轮模型往往**只输出 tool_calls、一个字正文都没有**。
 Win 实测，同一个问题连跑 4 次有 2 次撞预算，
 页面上只有一句「没查完」——**查到的东西全浪费了**。

 代价是多一次模型调用。**有界**：只调一次、不给工具、失败就退回原行为。
 比起把已经花掉的几万 token 全扔掉，这一次是划算的。
    """
    lines = []
    for c in trace.tool_calls:
        if not c.ok:
            continue
        lines.append(f"- {c.tool} {json.dumps(_json_safe(c.args), ensure_ascii=False)}\n"
                     f"  → {json.dumps(_json_safe(c.result, max_text=1500), ensure_ascii=False)}")
    if not lines:
        return "这一轮没查到任何东西。"
    cfg = env()
    # **没配模型就别发请求。** 不加这道，单测会去连真 endpoint——
    # 加这个功能时实测把整套测试从 2.6 秒拖到 31 秒，而且离线就红。
    if not cfg.get("LLM_BASE_URL") or not cfg.get("LLM_API_KEY"):
        return "已取得的证据摘要：\n" + "\n".join(lines)
    client = LLMClient(
        base_url=cfg.get("LLM_BASE_URL", ""),
        api_key=cfg.get("LLM_API_KEY", ""),
        model=cfg.get("LLM_MODEL", ""),
        sleep=LLM_RETRY_SLEEP,
    )
    response = client.complete(
        [
            {"role": "system", "content":
             # **顺序写死：先证据、再结论、最后才是缺口。** 实测，
             # 原来那句「能答多少答多少」被它读成了「说说下一步打算」——
             # 5 次收尾里 3 次只写了一两句要干什么，一条已查到的证据都没列。
             "你是网络运维助手。这一轮的查询预算已经用完，**不要再要求调用任何工具**，"
             "就用下面已经查到的东西回答。按这个顺序，三段都不许省：\n"
             "1. **已经查到了什么**：把关键的那几行原文摘出来，标明是哪个工具查的。\n"
             "2. **目前最可能的结论**：基于上面这些证据能说到哪一步就说到哪一步；"
             "**如果证据不足以指向任何结论，就直接说「现有证据还不足以判断」**，"
             "这是一个合格的回答，不要为了给个交代硬凑一个。\n"
             "3. **还差什么**：哪些没查、接着查的话下一步查什么。\n\n"
             # **这条是 R0b 第 2 轮换来的**：它在收尾里编了「BVI11 admin down
             # 导致 10.1.2.0/24 不发布」还附了修复建议，被追问才撤回。
             # 「查不到的对象不许编根因」那条规则在最终结论层管用，在这里不管用。
             "**硬规矩：没有直接证据的因果关系一个字都不许写。** "
             "上面「已经查到的」里没有出现过的设备、接口、配置、日志，不要提；"
             "看到某个接口是 down 的，不等于它就是这次故障的原因——"
             "要说因果，必须能指出是哪一条查询结果支持这个因果。"
             "**更不要给出任何修改配置的建议**，这套工具全是只读的。"},
            {"role": "user", "content":
             f"问题：{question}\n\n停下来的原因：{reason}\n\n已经查到的：\n" + "\n".join(lines)},
        ],
        max_completion_tokens=1500, temperature=0.2,
        on_retry=lambda attempt, exc: _emit_llm_retry(trace, attempt, exc),
    )
    return (response.content or "").strip()


def _abort_answer(question: str, trace: ChatRunTrace, reason: str, budget: AgentLoopBudget) -> str:
    partial = trace.last_model_text
    if not partial:
        try:
            partial = _wrap_up_call(question, trace, reason) if budget.wrap_up_on_abort else ""
            if partial and partial != "这一轮没查到任何东西。":
                trace.model_call_count += 1
        except Exception:  # noqa: BLE001 - 收尾失败也要给用户一个诚实答案
            partial = ""
    if not partial:
        partial = "这一轮没查到任何东西。"
    return f"（没查完：{reason}）\n\n{partial}"


def build_transcript(messages: list[Any]) -> str:
    """把整条 tool-calling 轨迹拼成文本。

    **这份文本就是「原文」**：收尾那次结构化调用看的是它，逐字引文核对也拿它当底本。
    以前核对用的是管道另外拼的一份（每个工具截到 3000 字符），**模型看得到的东西
    比核对器多**，引文对不上不是模型编的，是核对器少了一截。
    """
    return "\n\n".join(f"{type(m).__name__}: {message_text(m)}" for m in messages)


def build_transcript_from_trace(trace: ChatRunTrace) -> str:
    """从已经落在 trace 里的工具调用拼收尾 transcript。

    这条路径专门给 `agent.invoke()` 抛出后的场景用：LangChain 没有正常返回
    messages，但 trace 里已经有完整工具结果。这里不截断工具结果，避免结构化
    收尾看到的证据比取证循环少。
    """
    parts = []
    if trace.last_model_text:
        parts.append(f"AIMessage: {trace.last_model_text}")
    for index, call in enumerate(trace.tool_calls, start=1):
        result_text = call.result if isinstance(call.result, str) else json.dumps(call.result, ensure_ascii=False, default=str)
        parts.append(
            "\n".join(
                [
                    f"ToolCall {index}: {call.tool}",
                    f"Args: {json.dumps(call.args, ensure_ascii=False, default=str)}",
                    f"OK: {call.ok}",
                    f"Deduped: {call.deduped}",
                    f"Result: {result_text}",
                    f"Error: {call.error}",
                ]
            )
        )
    return "\n\n".join(parts)


def _final_schema_call(
    question: str,
    messages: list[Any],
    final_schema: dict,
    *,
    plan: str | None,
    system_prompt: str | None = None,
    context_prefix: str | None = None,
    transcript: str | None = None,
    trace: ChatRunTrace | None = None,
) -> dict[str, Any] | None:
    """循环跑完之后，**同一条轨迹**再走一次带 schema 的调用。

    **schema 只出现在这一次，不出现在工具调用的那几轮**——这是有依据的：
    strict schema 会压制工具调用（*Constraint Tax in Open-Weight LLMs*，
    强制 JSON 在 GSM8K 上掉 27.3 个百分点），推荐的缓解办法就是
    "separates tool execution from schema-constrained response generation"。
    """
    transcript = transcript if transcript is not None else build_transcript(messages)
    prompt = (
        f"{context_prefix}\n\n" if context_prefix else ""
    ) + (
        "下面是一次网络故障排查的完整 tool-calling 轨迹。请只基于这些取数结果下结论，"
        "按 strict json_schema 返回结构化分析；如果证据不足，必须在结果里如实说明还差什么。\n\n"
        f"原始问题：{question}\n\n"
        f"计划提示：{plan or '无'}\n\n"
        f"轨迹：\n{transcript}"
    )
    cfg = env()
    client = LLMClient(
        base_url=cfg.get("LLM_BASE_URL", ""),
        api_key=cfg.get("LLM_API_KEY", ""),
        model=cfg.get("LLM_MODEL", ""),
        sleep=LLM_RETRY_SLEEP,
    )
    response = client.complete(
        [
            {"role": "system", "content": system_prompt or "你是网络故障分析助手，只输出符合 schema 的 JSON。"},
            {"role": "user", "content": prompt},
        ],
        response_format=final_schema,
        max_completion_tokens=8000,
        temperature=0.2,
        on_retry=(lambda attempt, exc: _emit_llm_retry(trace, attempt, exc)) if trace is not None else None,
    )
    return response.parsed


def _abort_final_schema_context(reason: str) -> str:
    return (
        "注意：这次取证被预算/闸门/递归上限打断，没有完整跑完。"
        f"打断原因：{reason}\n"
        "你只能基于下面已有证据下结论；证据不足的部分必须如实写进结论，"
        "不许编造未出现过的设备、接口、日志、配置或因果关系。"
        "如果结论依赖的关键证据不足，置信度不得高于 medium。"
    )


def _run_final_schema_after_abort(
    *,
    question: str,
    messages: list[Any],
    trace: ChatRunTrace,
    final_schema: dict,
    plan: str | None,
    final_system_prompt: str | None,
    final_context: str | None,
    reason: str,
) -> dict[str, Any] | None:
    trace.transcript = build_transcript_from_trace(trace)
    trace.final_schema_from = "agent_loop_final_schema_after_abort"
    trace.final_schema_abort_reason = reason
    context = _abort_final_schema_context(reason)
    if final_context:
        context = f"{final_context}\n\n{context}"
    structured = _final_schema_call(
        question,
        messages,
        final_schema,
        plan=plan,
        system_prompt=final_system_prompt,
        context_prefix=context,
        transcript=trace.transcript,
        trace=trace,
    )
    trace.model_call_count += 1
    trace.final_structured = structured
    return structured


def _build_loop_model() -> Any:
    """模型只能从 `build_chat_model()` 造（那里挂了 token 记账）。这里以前手写过一份，账本一直漏记主路径。"""
    route = detect_llm_route(env())
    model = build_chat_model(force_gemini_native=route.provider == "gemini")
    # 实测：qwen3.8-flash 默认开着 thinking，取证循环每一步都在烧 reasoning token
    # （回一个字也有 17~44 个；一句简单问题输出 144~305 token，关掉后 ~51 token、耗时 3~5.7s→1.4s），
    # 而分析耗时 85%+ 是模型输出时间。`AGENT_LOOP_THINKING=off` 只关取证循环这一路；
    # 收尾结构化结论走另一个客户端，不受影响。默认不设 = 行为不变，方便 A/B。
    if str(env().get("AGENT_LOOP_THINKING", "")).strip().lower() in {"off", "0", "false", "no"}:
        current = getattr(model, "extra_body", None)
        if hasattr(model, "extra_body"):
            model.extra_body = {**(current or {}), "enable_thinking": False}
    return model


def _attach_token_counter(model: Any, trace: ChatRunTrace) -> Any:
    """给模型挂一个把 token 累加进 `trace` 的回调，预算靠它当场刹车（账本那个回调是事后算钱）。

    挂不上就原样返回：预算是安全网，不该成为模型能不能用的前提。
    """
    try:
        from langchain_core.callbacks import BaseCallbackHandler
    except ImportError:
        return model

    from netops_ai.llm.meter import _usage_from_llm_result

    class _TokenCounter(BaseCallbackHandler):
        def on_llm_end(self, response, **kwargs) -> None:  # noqa: ANN001
            try:
                usage = _usage_from_llm_result(response)
                trace.charge_tokens(usage.prompt_tokens, usage.completion_tokens)
            except Exception:  # noqa: BLE001 - 计数失败绝不许炸穿真实分析
                pass
            try:
                text = "".join(
                    getattr(g, "text", "") or "" for gen in response.generations for g in gen
                ).strip()
                if text:
                    trace.last_model_text = text
            except Exception:  # noqa: BLE001
                pass

    try:
        existing = list(getattr(model, "callbacks", None) or [])
        model.callbacks = [*existing, _TokenCounter()]
    except Exception:  # noqa: BLE001
        pass
    return model


def run_agent_loop(
    tools: Sequence[BaseTool] | ToolFactory,
    *,
    question: str,
    history: Sequence[Any] | None = None,
    session_id: str = "default",
    host_filter: str = "",
    plan: str | None = None,
    final_schema: dict | None = None,
    budget: AgentLoopBudget | None = None,
    trace_id: str | None = None,
    on_event: Any = None,
    system_prompt: str | None = None,
    final_system_prompt: str | None = None,
    final_context: str | None = None,
    sop_data: dict[str, Any] | None = None,
    hygiene: LoopHygiene | None = None,
) -> AgentLoopResult:
    """唯一的工具循环，可选地最后用 strict schema 收敛。`on_event` 给了就在关键节点回调，推进度用。

    `hygiene` 不给就读环境变量（`LoopHygiene.from_env`）：进度=开、缓存=开、压缩=关。
    """

    budget = budget or AgentLoopBudget()
    hygiene = hygiene or LoopHygiene.from_env()
    trace = ChatRunTrace(
        # 毫秒时间戳 + 4 位随机后缀：两个循环同一毫秒启动时，trace 文件不会互相覆盖、token 账本也不会记到同一个 id 下。
        trace_id=trace_id or f"{int(time.time() * 1000)}-{uuid.uuid4().hex[:4]}",
        question=question,
        session_id=session_id,
        budget=budget.__dict__.copy(),
        plan=plan or "",
        sop=SopRuntimeState(sop_data) if sop_data is not None else None,
        on_event=on_event,
    )
    if trace.sop is not None:
        trace.sop_usage = trace.sop.usage()
    trace.emit("start", question=question, budget=trace.budget)
    started = time.perf_counter()
    from netops_ai.llm.meter import CURRENT_TRACE_ID

    trace_token = CURRENT_TRACE_ID.set(trace.trace_id)
    messages: list[Any] = []
    answer = ""
    structured: dict[str, Any] | None = None
    try:
        model = _attach_token_counter(_build_loop_model(), trace)
        registered_tools = list(tools(trace, budget) if callable(tools) else tools)
        trace.emit("tools_ready", tools=[t.name for t in registered_tools])
        registered_tools = apply_loop_hygiene(registered_tools, trace, budget, hygiene)
        # 压缩只改每次模型调用前的 messages 副本；关着时不传 middleware，调用形状跟以前一模一样。
        extra = {"middleware": [_compaction_middleware(hygiene.compact_keep)]} if hygiene.compact else {}
        agent = create_agent(
            model=model, tools=registered_tools, system_prompt=_system_prompt(plan, base=system_prompt), **extra
        )
        hint = f"\n\n默认关注主机过滤词：{host_filter}" if host_filter else ""
        # max_iterations=0：不传 recursion_limit，退回 LangGraph 自己的默认上限（见 AgentLoopBudget.unbounded）。
        invoke_config = {"recursion_limit": budget.max_iterations} if budget.max_iterations else {}
        result = call_with_llm_retry(
            lambda: agent.invoke(
                {"messages": [*(history or []), HumanMessage(content=question + hint)]},
                config=invoke_config,
            ),
            on_retry=lambda attempt, exc: _emit_llm_retry(trace, attempt, exc),
            sleep=LLM_RETRY_SLEEP,
        )
        messages = list(result.get("messages", []))
        trace.model_call_count = _count_model_messages(messages)
        final_msg = next((m for m in reversed(messages) if isinstance(m, AIMessage) and message_text(m)), None)
        answer = message_text(final_msg) if final_msg is not None else ""
        if not answer and trace.tool_calls:
            # **查了一堆却一个字没答，也要走收尾。**
            # 12 轮里有 2 轮是这样——没撞任何闸门，
            # 模型最后一轮只发了 tool_calls、正文为空，我们就把空串当答案放出去了。
            # 两次都出现在用户质疑之后那一轮，用户看到的是「它不理我了」。
            answer = _wrap_up_call(question, trace, "模型最后没有给出文字回答")
        if final_schema is not None:
            trace.transcript = build_transcript(messages)
            structured = _final_schema_call(
                question, messages, final_schema, plan=plan,
                system_prompt=final_system_prompt, context_prefix=final_context,
                trace=trace,
            )
            trace.model_call_count += 1
            trace.final_structured = structured
            trace.final_schema_from = "agent_loop_final_schema"
            answer = json.dumps(structured, ensure_ascii=False) if structured is not None else ""
    except AgentLoopAbort as exc:
        # **预算用完不等于什么都没查到。** 以前这里把模型已经写出来的探路小结
        # 整段丢掉、只留一句"没查完"，而 的 5 条 qwen 记录都是"结论已经完整、
        # 第 7 次调用才被打断"。现在小结留着，后面再补一句为什么停。
        abort_reason = str(exc)
        trace.incomplete = True
        if not trace.termination_reason:
            trace.termination_reason = abort_reason
        trace.error = abort_reason
        if final_schema is not None:
            try:
                structured = _run_final_schema_after_abort(
                    question=question,
                    messages=messages,
                    trace=trace,
                    final_schema=final_schema,
                    plan=plan,
                    final_system_prompt=final_system_prompt,
                    final_context=final_context,
                    reason=abort_reason,
                )
                answer = json.dumps(structured, ensure_ascii=False) if structured is not None else ""
            except Exception as final_exc:  # noqa: BLE001 - 要同时保留闸门原因和收尾失败原因
                trace.error = (
                    f"{abort_reason}\n"
                    f"final_schema_after_abort failed: {type(final_exc).__name__}: {final_exc}"
                )
                answer = _abort_answer(question, trace, abort_reason, budget)
        else:
            answer = _abort_answer(question, trace, abort_reason, budget)
    except LLMTransportRetriesExhausted as exc:
        abort_reason = f"LLM 连接失败（重试 {exc.retry_count} 次）"
        investigation_error = f"取证阶段: {retry_error_text(exc)}"
        trace.incomplete = True
        trace.termination_reason = abort_reason
        trace.error = investigation_error
        if trace.tool_calls and final_schema is not None:
            try:
                structured = _run_final_schema_after_abort(
                    question=question,
                    messages=messages,
                    trace=trace,
                    final_schema=final_schema,
                    plan=plan,
                    final_system_prompt=final_system_prompt,
                    final_context=final_context,
                    reason=abort_reason,
                )
                answer = json.dumps(structured, ensure_ascii=False) if structured is not None else ""
            except Exception as final_exc:  # noqa: BLE001 - 取证和收尾两段原因都要留
                trace.error = (
                    f"{investigation_error}\n"
                    f"收尾阶段: {type(final_exc).__name__}: {final_exc}"
                )
                answer = _abort_answer(question, trace, abort_reason, budget)
        else:
            answer = _abort_answer(question, trace, abort_reason, budget)
    except Exception as exc:  # noqa: BLE001 - API callers need a structured error and a saved trace
        msg = f"{type(exc).__name__}: {exc}"
        if "recursion" in msg.lower() or "limit" in msg.lower():
            trace.incomplete = True
            limit_desc = f"max_iterations={budget.max_iterations}" if budget.max_iterations else "LangGraph 默认 recursion_limit"
            trace.termination_reason = (
                f"达到循环轮数上限（{limit_desc}）。"
                "本轮没有查完，需要收窄问题或提高预算后继续。"
            )
            if final_schema is not None:
                try:
                    structured = _run_final_schema_after_abort(
                        question=question,
                        messages=messages,
                        trace=trace,
                        final_schema=final_schema,
                        plan=plan,
                        final_system_prompt=final_system_prompt,
                        final_context=final_context,
                        reason=trace.termination_reason,
                    )
                    answer = json.dumps(structured, ensure_ascii=False) if structured is not None else ""
                except Exception as final_exc:  # noqa: BLE001
                    trace.error = (
                        f"{msg}\n"
                        f"{trace.termination_reason}\n"
                        f"final_schema_after_abort failed: {type(final_exc).__name__}: {final_exc}"
                    )
                    answer = _abort_answer(question, trace, trace.termination_reason, budget)
            else:
                answer = _abort_answer(question, trace, trace.termination_reason, budget)
        else:
            answer = ""
        if not trace.error:
            trace.error = msg
    finally:
        trace.elapsed_ms = int((time.perf_counter() - started) * 1000)
        trace.final_answer = answer
        if trace.sop is not None:
            trace.sop_usage = trace.sop.usage()
        trace_path = save_trace(trace)
        CURRENT_TRACE_ID.reset(trace_token)
        # 放 finally 里：**出错、超预算、正常收束，前端都要收到收尾事件**，
        # 否则流式那头会一直挂着等，用户看到的是「卡住了」而不是「失败了」。
        trace.emit(
            "done",
            answer=answer,
            incomplete=trace.incomplete,
            termination_reason=trace.termination_reason,
            error=trace.error,
            elapsed_ms=trace.elapsed_ms,
            tool_call_count=len(trace.tool_calls),
            model_call_count=trace.model_call_count,
            tokens_spent=trace.tokens_spent,
        )

    return AgentLoopResult(
        answer=answer,
        messages=messages,
        trace=trace,
        trace_path=trace_path,
        structured=structured,
        incomplete=trace.incomplete,
        termination_reason=trace.termination_reason,
    )
