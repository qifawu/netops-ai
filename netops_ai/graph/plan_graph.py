"""对话式制定 / 调整巡检计划的流程图（LangGraph `StateGraph`）。

每一轮对话是图上的一次执行；保存前的「三项确认」是同一张图里的人工确认环节（`interrupt`），
靠 checkpointer（默认 `MemorySaver`，thread_id = 对话会话 id）把图停在那里等用户：

    START → route_intent ─┬─(新建，还没说话)→ opening → END
                          ├─(调整，还没选计划)→ list_plans → END（列出已有计划让用户挑）
                          ├─(调整，选定了计划)→ load_plan → END（加载成草案，问要改什么）
                          └→ gather_context ─┬─(描述了想查什么却没给命令 / 说不记得命令)→ lookup_commands → propose
                                             └→ propose
    propose ─┬─(模型不可用)→ rules_fallback
             └→ validate ─┬─(通过)→ respond
                          ├─(不过且 repairs<2)→ repair → validate（修正环）
                          ├─(不过且修正用尽)→ respond_blocked → END（如实告知、不放行）
                          └─(修正时模型不可用：repair → rules_fallback)
    respond / rules_fallback ─┬─(可确认)→ confirm_plan
                              └→ END
    confirm_plan（interrupt：①命令 ②机器 ③周期 逐项确认）
             ├─(三项都确认)→ save ─┬→ END
             │                     └─(重名，改名后再确认)→ confirm_plan
             ├─(有一项没确认 / 要改)→ propose
             └─(用户直接继续聊)→ gather_context

节点只做编排，逻辑都在 `netops_ai/inspection/` 下的纯函数里（plan_chat / plans / command_lookup）。
模型、拓扑、文档库路径、checkpointer 都能注入：测试用 fake 模型和临时索引，不连网。
"""

from __future__ import annotations

import re
import uuid
from pathlib import Path
from typing import Any, Callable, Literal, TypedDict

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from netops_ai.inspection import command_lookup as CMD
from netops_ai.inspection import plan_chat as PC
from netops_ai.inspection import plans as P

MAX_REPAIRS = PC.MAX_FIX_ROUNDS
CONFIRM_ITEMS = ("commands", "devices", "schedule")
_EDIT_WORDS = re.compile(r"调整|修改|改一下|改改|编辑|暂停|\b(?:edit|adjust|modify|change|pause)\b", re.IGNORECASE)


class PlanState(TypedDict, total=False):
    # ---- 对外的状态
    messages: list[dict[str, str]]      # 用户 / 助手的对话（前端每轮带全量）
    draft: dict[str, Any] | None        # 当前草案（清单格式 + schedule + enabled）
    validation: dict[str, Any]          # {ok, problems, missing}
    repairs: int                        # 本轮已让模型修正了几次
    suggestions: list[dict[str, Any]]
    lang: str
    ready: bool
    # ---- 入口分流
    mode: str                           # new / edit
    plan: str                           # 调整时用户选的计划名（网页上点「调整」带进来）
    editing: str                        # 已加载、正在调整的计划名
    route: str
    plans: list[dict[str, Any]]         # list_plans 列出的已有计划
    # ---- 命令检索
    lookup: bool
    candidates: list[dict[str, Any]]
    docs_status: str
    lookup_note: str
    # ---- 本轮的中间量
    reply: str
    quick_replies: list[str]
    source: str                         # opening / plans / llm / rules
    llm_messages: list[dict[str, str]]
    model_out: dict[str, Any] | None
    candidate: dict[str, Any] | None
    problems: list[str]
    model_error: str
    removed: list[str]
    # ---- 确认 / 保存
    confirm_card: dict[str, Any] | None
    saved: dict[str, Any] | None
    save_error: dict[str, Any] | None


#: 每轮开始时要清掉的中间量（同一个 thread 上一轮留下的值不能串到这一轮）
_TURN_RESET: dict[str, Any] = {
    "repairs": 0, "suggestions": [], "ready": False, "reply": "", "quick_replies": [], "source": "", "llm_messages": [],
    "model_out": None, "candidate": None, "problems": [], "model_error": "", "removed": [], "saved": None, "save_error": None,
    "validation": {"ok": False, "problems": [], "missing": []}, "route": "", "plans": [], "lookup": False, "candidates": [],
    "docs_status": "", "lookup_note": "", "confirm_card": None,
}


def _last_user(messages: list[dict[str, str]]) -> str:
    return next((m["content"] for m in reversed(messages or []) if m.get("role") == "user"), "")


def build_graph(*, llm_factory: Callable[[], Any] | None = None,
                topology_loader: Callable[[], dict[str, Any]] | None = None,
                docs_db: Callable[[], str | Path | None] | None = None) -> StateGraph:
    """搭图（未编译）。`llm_factory()` 每轮现建一个客户端（.env 改了不用重启）；返回 None 等于模型不可用。
    `docs_db()` 给文档库索引路径（默认 `.env` 的 DOC_SEARCH_DB，再默认 records/docs_kb.db）。"""

    def _llm() -> Any:
        if llm_factory is not None:
            return llm_factory()
        from netops_ai.llm.client import LLMClient

        return LLMClient()

    def _topo() -> dict[str, Any]:
        if topology_loader is not None:
            return topology_loader()
        from netops_ai.topology import load_topology

        return load_topology()

    def _docs() -> str | Path | None:
        if docs_db is not None:
            return docs_db()
        from netops_ai.llm.factory import env

        return env().get("DOC_SEARCH_DB") or None

    # ------------------------------------------------------------------ 入口
    def route_intent(state: PlanState) -> dict:
        """分流：新建（从空草案开始）还是调整现有计划（先选计划、加载成草案）。"""
        msgs = PC._clean_messages(state.get("messages"))
        lang = "en" if state.get("lang") == "en" else "zh"
        mode = "edit" if state.get("mode") == "edit" else "new"
        editing = state.get("editing") or ""
        draft = state.get("draft") or {}
        text = _last_user(msgs)
        names = [p["name"] for p in P.list_plans()]
        wanted = state.get("plan") or ""
        if not wanted and text:
            wanted = next((n for n in sorted(names, key=len, reverse=True)
                           if re.search(rf"(?<![\w.-]){re.escape(n)}(?![\w.-])", text)), "")
        has_draft = bool(draft.get("devices") or draft.get("checks"))
        if mode == "edit" and not editing:
            route = "load_plan" if wanted in names else "list_plans"
        elif mode == "new" and not has_draft and wanted in names and _EDIT_WORDS.search(text or ""):
            route, mode = "load_plan", "edit"  # 新建面板里说「调整 core-health」：转到调整路径
        elif not PC.has_user_message(msgs):
            route = "opening"
        else:
            route = "gather_context"
        return {"messages": msgs, "lang": lang, "mode": mode, "plan": wanted if wanted in names else "", "route": route,
                "editing": editing}

    def opening(state: PlanState) -> dict:
        """新建、还没有用户消息：固定开场白（不调模型），带快捷回复。"""
        return _turn_view(PC.opening(_topo(), state.get("draft"), state["lang"]))

    def list_plans(state: PlanState) -> dict:
        """调整现有计划、还没选：列出已有计划（名字、设备、检查项、周期、启用、最近结果）让用户挑。"""
        briefs = [P.plan_brief(p) for p in P.list_plans()]
        text = _last_user(state.get("messages") or [])
        out = PC.list_plans_turn(briefs, state["lang"], not_found=text.strip()[:40] if text else "")
        return {**_turn_view(out), "plans": briefs}

    def load_plan(state: PlanState) -> dict:
        """把选中的计划加载成草案，说明它现在的样子，问要改什么。"""
        draft = P.load_as_draft(state["plan"])
        return {**_turn_view(PC.loaded_plan_turn(draft, _topo(), state["lang"])), "editing": draft["name"], "mode": "edit"}

    # ------------------------------------------------------------------ 提案 / 校验
    def gather_context(state: PlanState) -> dict:
        """准备这一轮的上下文；判断要不要先去文档库找命令。拓扑信息只有名字 / 角色 / 邻居数，无地址无凭据。"""
        return {"repairs": 0, "problems": [], "model_error": "", "lookup": CMD.wants_lookup(_last_user(state["messages"]))}

    def lookup_commands(state: PlanState) -> dict:
        """用户描述了想查什么却没给命令 / 说不记得命令：在文档库（命令目录 + docs_kb 索引）里找候选，逐条过白名单。"""
        r = CMD.lookup(_last_user(state["messages"]), db_path=_docs(), lang=state["lang"])
        return {"candidates": r["candidates"], "docs_status": r["docs_status"], "lookup_note": r["note"]}

    def propose(state: PlanState) -> dict:
        """调模型拿结构化输出：回复、完整草案、建议、是否可确认、是否暂停。"""
        convo = PC.build_messages(state["messages"], state.get("draft"), _topo(), state["lang"],
                                  editing=state.get("editing") or "", candidates=state.get("candidates") or [])
        try:
            out, convo = PC.call_model(_llm(), convo)
        except PC.ModelUnavailable as exc:
            return {"model_error": str(exc), "llm_messages": convo}
        return {"model_out": out, "llm_messages": convo}

    def validate(state: PlanState) -> dict:
        """模板展开、补地址，再过 `check_draft`（格式 + 只读白名单）。"""
        out = dict(state["model_out"])
        if state.get("editing"):
            out["plan_name"] = state["editing"]  # 调整现有计划：名字不变（存回同一个文件）
        cand, problems = PC.evaluate(out, state.get("draft"), _topo(), state["lang"], _last_user(state.get("messages") or []))
        return {"candidate": cand, "problems": problems}

    def repair(state: PlanState) -> dict:
        """带着问题原文让模型整份重给一次。"""
        convo = PC.repair_messages(state["llm_messages"], state["model_out"], state["problems"])
        try:
            out, convo = PC.call_model(_llm(), convo)
        except PC.ModelUnavailable as exc:
            return {"model_error": str(exc), "repairs": state.get("repairs", 0) + 1}
        return {"model_out": out, "llm_messages": convo, "repairs": state.get("repairs", 0) + 1}

    def respond(state: PlanState) -> dict:
        """校验通过：整理回复、合并建议（模型的 + 规则的）、算 ready。"""
        return _with_lookup(state, PC.finish_turn(state["model_out"], state["candidate"], [], state.get("repairs", 0), _topo(), state["lang"]))

    def respond_blocked(state: PlanState) -> dict:
        """修正用尽仍不过：问题原样告诉用户，坏检查项从草案拿掉，ready=False，不进确认环节。"""
        return _with_lookup(state, PC.finish_turn(state["model_out"], state["candidate"], state["problems"], state.get("repairs", 0),
                                                  _topo(), state["lang"]))

    def rules_fallback(state: PlanState) -> dict:
        """模型不可用：规则推荐（按角色挑模板 + 默认周期），明确说明是规则推荐；草案可以手工确认。"""
        base = state.get("draft")
        out = PC.rule_turn(state["messages"], base, _topo(), state["lang"], state.get("model_error") or "-")
        if state.get("editing") and base:
            out["draft"]["name"] = state["editing"]
        return _with_lookup(state, out)

    # ------------------------------------------------------------------ 确认 / 保存
    def confirm_plan(state: PlanState) -> Command[Literal["save", "propose", "gather_context"]]:
        """保存前的三项确认（人工中断）：①每台设备要执行的只读命令 ②实施机器 ③实施周期。
        恢复：`{action: "confirm", confirmed: {commands, devices, schedule}, draft}` 三项都 True → save；
        有一项不是 True → 回 propose 继续对话；`{action: "chat", messages, draft}` → 回到 gather_context。"""
        card = P.confirmation_card(state.get("draft") or {})
        decision = interrupt({"confirm_card": card, "items": list(CONFIRM_ITEMS)})
        decision = decision if isinstance(decision, dict) else {}
        draft = decision.get("draft") or state.get("draft")
        lang = decision.get("lang") or state.get("lang", "zh")
        if decision.get("action") == "confirm":
            confirmed = decision.get("confirmed") or {}
            pending = [k for k in CONFIRM_ITEMS if confirmed.get(k) is not True]
            if not pending:
                return Command(goto="save", update={"draft": draft, "save_error": None, "confirm_card": P.confirmation_card(draft)})
            note = _unconfirmed_message(pending, lang, decision.get("note") or "")
            msgs = [*(decision.get("messages") or state.get("messages") or []), {"role": "user", "content": note}]
            return Command(goto="propose", update={**_TURN_RESET, "messages": msgs, "draft": draft, "lang": lang})
        msgs = PC._clean_messages(decision.get("messages") or state.get("messages") or [])
        return Command(goto="gather_context", update={**_TURN_RESET, "messages": msgs, "draft": draft, "lang": lang})

    def save(state: PlanState) -> dict:
        """保存前再校验一次 → 写 `inspection-plans/<name>.yaml`（调整现有计划时覆盖原文件）→ 调度线程每分钟扫目录，算出下次运行。"""
        draft = state.get("draft") or {}
        editing = state.get("editing") or ""
        renamed = bool(editing) and draft.get("name") != editing
        try:
            if renamed and P.is_running(editing):
                raise P.PlanError(f"plan {editing!r} is running; rename it after the run finishes", status=409)
            plan = P.save_plan(draft, enabled=draft.get("enabled", True) is not False,
                               overwrite=bool(editing) and not renamed)
            if renamed:  # 调整时改了名：新文件已写好，旧文件摘掉（只删计划文件，历史记录留着）
                P.delete_plan(editing, with_history=False)
        except P.PlanError as exc:
            return {"save_error": {"message": str(exc), "problems": exc.problems, "status": exc.status}}
        return {"saved": P.plan_view(plan), "save_error": None, "editing": plan["name"]}

    # ------------------------------------------------------------------ 条件边
    def after_route(state: PlanState) -> str:
        return state["route"]

    def after_context(state: PlanState) -> str:
        return "lookup_commands" if state.get("lookup") else "propose"

    def after_propose(state: PlanState) -> str:
        return "rules_fallback" if state.get("model_error") else "validate"

    def after_validate(state: PlanState) -> str:
        if not state.get("problems"):
            return "respond"
        return "repair" if state.get("repairs", 0) < MAX_REPAIRS else "respond_blocked"

    def after_repair(state: PlanState) -> str:
        return "rules_fallback" if state.get("model_error") else "validate"

    def after_respond(state: PlanState) -> str:
        return "confirm_plan" if state.get("ready") and (state.get("validation") or {}).get("ok") else END

    def after_save(state: PlanState) -> str:
        err = state.get("save_error")
        return "confirm_plan" if err and err.get("status") == 409 else END  # 重名：改个名字可以再确认

    g = StateGraph(PlanState)
    for name, fn in (("route_intent", route_intent), ("opening", opening), ("list_plans", list_plans), ("load_plan", load_plan),
                     ("gather_context", gather_context), ("lookup_commands", lookup_commands), ("propose", propose),
                     ("validate", validate), ("repair", repair), ("respond", respond), ("respond_blocked", respond_blocked),
                     ("rules_fallback", rules_fallback), ("save", save)):
        g.add_node(name, fn)
    g.add_node("confirm_plan", confirm_plan, destinations=("save", "propose", "gather_context"))
    g.add_edge(START, "route_intent")
    g.add_conditional_edges("route_intent", after_route, ["opening", "list_plans", "load_plan", "gather_context"])
    for n in ("opening", "list_plans", "load_plan", "respond_blocked"):
        g.add_edge(n, END)
    g.add_conditional_edges("gather_context", after_context, ["lookup_commands", "propose"])
    g.add_edge("lookup_commands", "propose")
    g.add_conditional_edges("propose", after_propose, ["validate", "rules_fallback"])
    g.add_conditional_edges("validate", after_validate, ["respond", "repair", "respond_blocked"])
    g.add_conditional_edges("repair", after_repair, ["validate", "rules_fallback"])
    g.add_conditional_edges("respond", after_respond, ["confirm_plan", END])
    g.add_conditional_edges("rules_fallback", after_respond, ["confirm_plan", END])
    g.add_conditional_edges("save", after_save, ["confirm_plan", END])
    return g


def _unconfirmed_message(pending: list[str], lang: str, note: str) -> str:
    zh = lang != "en"
    label = {"commands": ("实施的命令", "the commands"), "devices": ("实施的机器", "the devices"), "schedule": ("实施周期", "the schedule")}
    items = ("、".join(label[k][0] for k in pending)) if zh else ", ".join(label[k][1] for k in pending)
    head = f"我还没确认：{items}，要改一下。" if zh else f"I have not confirmed {items} yet; I want to change it."
    return head + (f" {note}" if note else "")


def _turn_view(turn: dict[str, Any]) -> dict:
    keys = ("reply", "draft", "validation", "ready", "suggestions", "quick_replies", "source", "removed")
    out = {k: turn[k] for k in keys if k in turn}
    out["repairs"] = turn.get("fix_rounds", 0)
    return out


def _with_lookup(state: PlanState, turn: dict[str, Any]) -> dict:
    """回复里带上检索到的候选命令（给用户选，不自动进草案）。"""
    out = _turn_view(turn)
    if state.get("candidates") is not None:
        out["candidates"] = state.get("candidates") or []
    return out


class PlanConversation:
    """一个对话会话 = 图上的一个 thread。前端每轮带全量消息；图停在确认环节时，下一句话从中断处恢复。"""

    def __init__(self, *, llm_factory: Callable[[], Any] | None = None, topology_loader: Callable[[], dict[str, Any]] | None = None,
                 docs_db: Callable[[], str | Path | None] | None = None, checkpointer: Any = None) -> None:
        self.graph = build_graph(llm_factory=llm_factory, topology_loader=topology_loader, docs_db=docs_db).compile(
            checkpointer=checkpointer if checkpointer is not None else MemorySaver())

    def _cfg(self, session: str) -> dict:
        return {"configurable": {"thread_id": session}}

    def awaiting_confirm(self, session: str) -> bool:
        return bool(self.graph.get_state(self._cfg(session)).next)

    def chat(self, session: str, messages: list[dict[str, str]], draft: dict[str, Any] | None, lang: str = "zh", *,
             mode: str = "new", plan: str = "") -> dict:
        cfg = self._cfg(session)
        if self.awaiting_confirm(session):  # 停在确认环节：用户没确认而是继续聊 → 从中断处恢复
            self.graph.invoke(Command(resume={"action": "chat", "messages": messages, "draft": draft, "lang": lang}), cfg)
        else:
            self.graph.invoke({**_TURN_RESET, "messages": messages, "draft": draft, "lang": lang, "mode": mode, "plan": plan}, cfg)
        return self.view(session)

    def confirm(self, session: str, draft: dict[str, Any] | None = None, confirmed: dict[str, bool] | None = None,
                note: str = "", messages: list[dict[str, str]] | None = None) -> dict:
        """三项确认。三项都 True → 保存；否则回到对话。图不在确认环节（还没到可确认 / 会话已过期）就拒绝。"""
        if not self.awaiting_confirm(session):
            raise P.PlanError("nothing is waiting for confirmation in this conversation (send another message first)", status=409)
        self.graph.invoke(Command(resume={"action": "confirm", "draft": draft, "confirmed": confirmed or {}, "note": note,
                                          "messages": messages}), self._cfg(session))
        return self.view(session)

    def view(self, session: str) -> dict:
        snap = self.graph.get_state(self._cfg(session))
        v = snap.values
        card = None
        for task in snap.tasks or ():
            for it in getattr(task, "interrupts", ()) or ():
                if isinstance(it.value, dict) and it.value.get("confirm_card"):
                    card = it.value["confirm_card"]
        return {
            "session": session,
            "messages": v.get("messages") or [],
            "reply": v.get("reply", ""),
            "draft": v.get("draft"),
            "validation": v.get("validation") or {"ok": False, "problems": [], "missing": []},
            "ready": bool(v.get("ready")),
            "suggestions": v.get("suggestions") or [],
            "quick_replies": v.get("quick_replies") or [],
            "source": v.get("source", ""),
            "fix_rounds": v.get("repairs", 0),
            "removed": v.get("removed") or [],
            "mode": v.get("mode", "new"),
            "editing": v.get("editing", ""),
            "plans": v.get("plans") or [],
            "candidates": v.get("candidates") or [],
            "docs_status": v.get("docs_status", ""),
            "lookup_note": v.get("lookup_note", ""),
            "awaiting_confirm": bool(snap.next),
            "confirm_card": card,
            "saved": v.get("saved"),
            "save_error": v.get("save_error"),
        }


def new_session_id() -> str:
    return uuid.uuid4().hex


def mermaid() -> str:
    """流程图的 mermaid 源码（文档附录用）。"""
    return build_graph(llm_factory=lambda: None, topology_loader=dict, docs_db=lambda: None).compile(
        checkpointer=MemorySaver()).get_graph().draw_mermaid()
