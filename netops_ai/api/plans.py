"""巡检计划接口：对话制定、草案合并、保存、列表、启停 / 改周期、立即运行、运行历史、趋势分析。

计划本身存仓库根 `inspection-plans/<name>.yaml`，跟 `tools/checklist_run.py` 读写的是同一份。
对话流程是一张 LangGraph 图（`netops_ai/graph/plan_graph.py`），「确认并启用」是图里的人工确认中断：
`/chat` 每轮跑一次图，图走到确认环节就停住；`/confirm` 从中断处恢复 → 保存。会话 id = 图的 thread_id。
其它逻辑在 `netops_ai/inspection/plans.py` 和 `plan_chat.py`，这里只做参数和状态码。错误一律 4xx + message，不 500。
"""

from __future__ import annotations

import threading
from typing import Any, Literal

from fastapi import APIRouter
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field

from netops_ai.inspection import plans as P

router = APIRouter(prefix="/api/inspection/plans", tags=["inspection-plans"])


class ChatMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(max_length=4000)


class PlanChatRequest(BaseModel):
    session: str = Field(default="", max_length=64, pattern=r"^[A-Za-z0-9_-]*$")
    messages: list[ChatMessage] = Field(default_factory=list, max_length=60)
    draft: dict[str, Any] | None = None
    lang: Literal["zh", "en"] = "zh"
    #: new = 新建计划；edit = 调整现有计划（`plan` 是网页上点「调整」的那个计划名，不给就先列出已有计划让用户挑）
    mode: Literal["new", "edit"] = "new"
    plan: str = Field(default="", max_length=64)
    #: 用户在选择气泡里提交的选择：{widget_id, type: device_picker|schedule_picker|check_picker, value}。走确定性路径，不调模型
    selection: dict[str, Any] | None = None


class PlanWidgetRequest(BaseModel):
    session: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    type: Literal["device_picker", "schedule_picker", "check_picker"]
    draft: dict[str, Any] | None = None
    lang: Literal["zh", "en"] = "zh"


class PlanConfirmRequest(BaseModel):
    session: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    draft: dict[str, Any] | None = None
    #: 三项确认：commands（每台设备要执行的命令）/ devices（实施机器）/ schedule（实施周期），三项都 true 才保存
    confirmed: dict[str, bool] = Field(default_factory=dict)
    note: str = Field(default="", max_length=1000)
    messages: list[ChatMessage] = Field(default_factory=list, max_length=60)


class PlanDraftRequest(BaseModel):
    draft: dict[str, Any] | None = None
    patch: dict[str, Any] | None = None
    #: 页面上还挂着的建议（带 patch），返回其中哪些仍然有用
    suggestions: list[dict[str, Any]] = Field(default_factory=list, max_length=20)
    lang: Literal["zh", "en"] = "zh"


class PlanSaveRequest(BaseModel):
    draft: dict[str, Any]
    enabled: bool = True
    overwrite: bool = False


class PlanPatchRequest(BaseModel):
    enabled: bool | None = None
    schedule: dict[str, Any] | None = None
    #: 改名（新名字同样只允许 [A-Za-z0-9_.-]，重名 409，正在运行 409）
    name: str | None = Field(default=None, max_length=64)


class PlanTrendRequest(BaseModel):
    last: int = Field(default=12, ge=2, le=50)
    lang: Literal["zh", "en"] = "zh"


def _err(exc: P.PlanError) -> JSONResponse:
    return JSONResponse({"message": str(exc), "problems": exc.problems}, status_code=exc.status)


def _topology() -> dict:
    from netops_ai.topology import load_topology

    return load_topology()


_CONVERSATION = None
_CONV_LOCK = threading.Lock()


def _conversation():
    """进程内一个图实例（checkpointer 是内存的：重启后进行中的会话要再说一句才能确认）。拓扑通过 `_topology` 取，测试可替换。"""
    global _CONVERSATION
    with _CONV_LOCK:
        if _CONVERSATION is None:
            from netops_ai.graph.plan_graph import PlanConversation

            _CONVERSATION = PlanConversation(topology_loader=lambda: _topology())
        return _CONVERSATION


@router.post("/chat")
def api_plan_chat(body: PlanChatRequest) -> JSONResponse:
    """一轮对话（跑一次图）。没有用户消息时返回开场白（不调模型）；模型不可用自动降级成规则推荐。
    草案可确认时图停在确认环节，返回 `awaiting_confirm: true`。"""
    from netops_ai.graph.plan_graph import new_session_id

    try:
        _topology()
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"message": f"拓扑读不到：{type(exc).__name__}: {exc}"}, status_code=503)
    session = body.session or new_session_id()
    out = _conversation().chat(session, [m.model_dump() for m in body.messages], body.draft, body.lang, mode=body.mode, plan=body.plan,
                               selection=body.selection)
    return JSONResponse(out)


@router.post("/widget")
def api_plan_widget(body: PlanWidgetRequest) -> JSONResponse:
    """用户主动重新打开一个选择气泡（草案预览里「选择设备」、已提交气泡上的「修改」）。新气泡成为唯一能提交的那个。"""
    try:
        return JSONResponse(_conversation().open_widget(body.session, body.type, body.draft, body.lang, topology=_topology()))
    except P.PlanError as exc:
        return _err(exc)


@router.post("/confirm")
def api_plan_confirm(body: PlanConfirmRequest) -> JSONResponse:
    """确认卡：从图的确认中断处恢复。三项（命令 / 机器 / 周期）都确认 → 保存（再校验一次）→ 写 YAML → 调度生效；
    有一项没确认 → 回到对话（助手问要怎么改）。`draft` 是页面上当前的草案（可能在确认卡里改过），保存时照样整份校验。"""
    try:
        out = _conversation().confirm(body.session, body.draft, body.confirmed, body.note, [m.model_dump() for m in body.messages])
    except P.PlanError as exc:
        return _err(exc)
    if out.get("save_error"):
        err = out["save_error"]
        return JSONResponse({**out, "message": err["message"], "problems": err.get("problems") or []}, status_code=err.get("status", 400))
    return JSONResponse(out)


@router.post("/draft")
def api_plan_draft(body: PlanDraftRequest) -> JSONResponse:
    """把一条建议的 patch 并进草案（「采纳」按钮），或只是重新校验（改了周期 / 名字）。确定性的，不调模型。"""
    from netops_ai.inspection.plan_chat import live_suggestions, merge_suggestions

    try:
        topo = _topology()
        draft = P.apply_patch(body.draft, body.patch, topo, body.lang)
    except P.PlanError as exc:
        return _err(exc)
    clean, removed = P.sanitize(draft)
    v = P.check_draft(clean)
    if removed:
        v = {**v, "ok": False, "problems": removed + v["problems"]}
    return JSONResponse({"draft": clean, "validation": v, "ready": v["ok"],
                         "suggestions": merge_suggestions([], clean, topo, body.lang),
                         "live_suggestions": live_suggestions(body.suggestions, clean, topo, body.lang)})


@router.get("")
def api_list_plans() -> list[dict]:
    return [P.plan_view(p) for p in P.list_plans()]


@router.post("")
def api_save_plan(body: PlanSaveRequest) -> JSONResponse:
    """用户点了「确认并启用」。保存前再校验一次（含只读白名单），不过就 400，不写文件。"""
    try:
        plan = P.save_plan(body.draft, enabled=body.enabled, overwrite=body.overwrite)
    except P.PlanError as exc:
        return _err(exc)
    return JSONResponse(P.plan_view(plan))


@router.patch("/{name}")
def api_patch_plan(name: str, body: PlanPatchRequest) -> JSONResponse:
    """启用 / 暂停、改周期、改名。改周期从现在起算下一次；正在进行的那次运行不受影响（它开跑时已经读好了计划）。"""
    from . import schedule

    try:
        if body.name is not None and body.name != name:
            P.rename_plan(name, body.name)
            schedule.forget_plan(name)
            name = body.name
        plan = P.update_plan(name, enabled=body.enabled, schedule=body.schedule) if (body.enabled is not None or body.schedule is not None) else P.load_plan(name)
    except P.PlanError as exc:
        return _err(exc)
    return JSONResponse(P.plan_view(plan))


@router.get("/{name}/delete-preview")
def api_plan_delete_preview(name: str) -> JSONResponse:
    """删除确认框要列的东西：计划文件路径、这个计划的历史运行文件有几个、是否正在运行。"""
    try:
        return JSONResponse(P.delete_preview(name))
    except P.PlanError as exc:
        return _err(exc)


@router.delete("/{name}")
def api_delete_plan(name: str, with_history: bool = False) -> JSONResponse:
    """删除计划（网页上先弹确认框）。只删 `inspection-plans/<name>.yaml`；`with_history=true` 时再删这个计划自己的
    历史运行文件（名字精确匹配）和趋势结论。**正在运行就 409**，等这次跑完再删；不存在 404。没有任何路径参数。"""
    from . import schedule

    try:
        out = P.delete_plan(name, with_history=with_history)
    except P.PlanError as exc:
        return _err(exc)
    schedule.forget_plan(name)
    return JSONResponse(out)


@router.get("/{name}/report")
def api_plan_report(name: str, format: str = "md", runs: int = 10) -> Response:
    """导出巡检报告（md / 自包含 html）：计划定义、运行汇总、逐设备逐检查结果和设备输出原文、指标趋势、失败项、趋势分析结论。
    跟内置巡检导出一样导出真实内容（演示模式的遮罩只在页面上）。报告里没有凭据。"""
    from netops_ai.inspection import plan_report

    if format not in ("md", "html"):
        return JSONResponse({"message": "format 只支持 md / html"}, status_code=400)
    try:
        plan = P.load_plan(name)
        hist = P.history(name, max(1, min(int(runs), 100)))
    except P.PlanError as exc:
        return _err(exc)
    trend = P.load_trend(name)
    if format == "html":
        return Response(plan_report.render_html(plan, hist, trend), media_type="text/html; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="plan-{name}-report.html"'})
    return Response(plan_report.render_markdown(plan, hist, trend), media_type="text/markdown; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="plan-{name}-report.md"'})


@router.post("/{name}/run")
def api_run_plan(name: str) -> JSONResponse:
    """立即运行一次（后台线程）。同一个计划正在跑就 409。"""
    from . import schedule

    try:
        P.load_plan(name)
    except P.PlanError as exc:
        return _err(exc)
    if P.is_running(name):
        return JSONResponse({"message": f"计划 {name} 正在运行，等这一次跑完"}, status_code=409)

    def work() -> None:
        try:
            schedule.run_plan_and_record(name)
        except P.PlanBusy:
            pass

    threading.Thread(target=work, name=f"netops-plan-run-{name}", daemon=True).start()
    return JSONResponse({"status": "started"})


def _slim(run: dict, with_output: bool) -> dict:
    """运行记录给页面看的形状：设备原始回显只在需要时带（未通过 / 出错的检查项），而且截短。"""
    devices = []
    for d in run.get("devices") or []:
        checks = []
        for c in d.get("checks") or []:
            row = {k: c.get(k) for k in ("id", "command", "status", "severity", "detail", "metrics", "evaluations")}
            if with_output and c.get("status") in ("fail", "error") and c.get("output"):
                row["output"] = c["output"][:1500]
            checks.append(row)
        devices.append({"name": d.get("name"), "error": d.get("error", ""), "checks": checks})
    return {**P.run_summary(run), "devices": devices}


@router.get("/{name}/runs")
def api_plan_runs(name: str, last: int = 20) -> JSONResponse:
    """运行历史（新的在前）。最新一次带未通过检查项的回显原文，其它只带状态和数字。"""
    try:
        P.load_plan(name)
        hist = P.history(name, max(1, min(last, 100)))
    except P.PlanError as exc:
        return _err(exc)
    runs = [_slim(r, i == len(hist) - 1) for i, r in enumerate(hist)]
    return JSONResponse({"name": name, "running": P.is_running(name), "runs": list(reversed(runs))})


@router.post("/{name}/trend")
def api_plan_trend(name: str, body: PlanTrendRequest) -> JSONResponse:
    """趋势分析：把最近几次运行排成表交给模型。没配模型 / 调用失败时只返回提示词和趋势表，不报 5xx。"""
    from netops_ai.inspection.checklist import build_trend_messages, trend_table

    try:
        P.load_plan(name)
        hist = P.history(name, body.last)
    except P.PlanError as exc:
        return _err(exc)
    if not hist:
        return JSONResponse({"message": "这个计划还没有运行记录"}, status_code=404)
    messages = build_trend_messages(hist)
    lang_line = ("\nAnswer in Simplified Chinese." if body.lang == "zh" else "\nAnswer in English.")
    messages[0] = {**messages[0], "content": messages[0]["content"] + lang_line}
    out: dict[str, Any] = {"name": name, "runs": len(hist), "table": trend_table(hist), "enough_runs": len(hist) >= P.TREND_MIN_RUNS}
    try:
        from netops_ai.llm.client import LLMClient

        resp = LLMClient().complete(messages, temperature=0.2, max_completion_tokens=2000)
        out.update(llm=True, analysis=resp.content)
        from datetime import datetime, timezone

        P.save_trend(name, {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "runs": len(hist),
                            "analysis": resp.content})  # 导出报告要带上最近一次的结论
    except Exception as exc:  # noqa: BLE001 - 没配模型也要能看趋势表
        out.update(llm=False, error=(str(exc).splitlines()[0] if str(exc) else type(exc).__name__)[:200], messages=messages)
    return JSONResponse(out)
