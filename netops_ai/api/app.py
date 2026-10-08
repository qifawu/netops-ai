"""后端入口：Zabbix webhook、前端要的查询接口、对话，外加托管前端产物。

webhook 只收事件、立刻返回（Zabbix webhook 60s 硬超时），分析丢给 `BackgroundTasks`。
"""

from __future__ import annotations

from pathlib import Path

import json

from fastapi import BackgroundTasks, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles

from . import dashboard
from .pipeline import _env, process_alert
from .schemas import (
    ChatRequest,
    InspectionConfigRequest,
    InspectionIgnoreRequest,
    PlaybookApproveRequest,
    PlaybookDraftRequest,
    PlaybookFileRequest,
    PlaybookFormToYamlRequest,
    PlaybookGraphRequest,
    PlaybookLintRequest,
    PlaybookProposeRequest,
    PlaybookValidateRequest,
    PlaybookRejectRequest,
    PlaybookReviewRequest,
    PlaybookRollbackRequest,
    TranslateRequest,
    ZabbixWebhookPayload,
)

STATIC_DIR = Path(__file__).resolve().parent / "static"

app = FastAPI(title="netops-ai webhook")


@app.on_event("startup")
def _start_schedule() -> None:
    from . import schedule

    schedule.start()


@app.on_event("startup")
def _warm_up() -> None:
    """后台预热：第一次打开「命令审计」要现 import 一整套 langchain（3 秒以上），「设备与拓扑」要现拉 NetBox。
    启动时在后台线程里先做掉，不拖慢启动，用户第一次点开页面就是热的。任何一步失败都不影响服务。"""
    import threading

    def work() -> None:
        for step in (
            lambda: __import__("netops_ai.graph.agent_loop"),
            lambda: __import__("netops_ai.graph.chat_agent"),
            lambda: dashboard.load_alerts(),
            lambda: __import__("netops_ai.topology", fromlist=["load_topology"]).load_topology(),
        ):
            try:
                step()
            except Exception:  # noqa: BLE001
                pass

    threading.Thread(target=work, name="warm-up", daemon=True).start()



# 对话里出的图存在这儿。**图必须回到对话里**——用 Grafana 接等于再多
# 一个服务、一个端口、一套登录，而要求是在一个系统里处理这些事。
from netops_ai.api.charts import CHART_DIR, render_chart_png  # noqa: E402

CHART_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/charts", StaticFiles(directory=str(CHART_DIR)), name="charts")


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}


@app.post("/webhooks/zabbix")
def zabbix_webhook(payload: ZabbixWebhookPayload, background_tasks: BackgroundTasks) -> dict:
    background_tasks.add_task(process_alert, payload.model_dump())
    return {"status": "accepted", "eventid": payload.eventid}


from netops_ai.api.kb import router as _kb_router  # noqa: E402

app.include_router(_kb_router)  # 知识库页面：只读概览 + 检索试验台

from netops_ai.api.settings import router as _settings_router  # noqa: E402

app.include_router(_settings_router)  # 系统设置页：连接配置 + 运行参数，读写 .env

from netops_ai.api.plans import router as _plans_router  # noqa: E402

app.include_router(_plans_router)  # 巡检计划：对话制定 / 保存 / 启停 / 立即运行 / 历史 / 趋势


@app.get("/api")
def api_index() -> dict:
    """导航。`/` 给了前端页面。"""
    return {"name": "netops-ai", "docs": "/docs", "health": "/healthz"}


@app.get("/api/records")
def api_list_records() -> list[dict]:
    return dashboard.list_records()


@app.get("/api/records/{eventid}")
def api_get_record(eventid: str) -> JSONResponse:
    record = dashboard.load_record(eventid)
    if record is None:
        return JSONResponse({"error": f"找不到 eventid={eventid} 的记录"}, status_code=404)
    return JSONResponse(record)


@app.get("/api/records/{eventid}/report.md")
def api_export_report(eventid: str) -> PlainTextResponse:
    record = dashboard.load_record(eventid)
    if record is None:
        return PlainTextResponse(f"找不到 eventid={eventid} 的记录", status_code=404)
    return PlainTextResponse(
        dashboard.render_markdown_report(record),
        media_type="text/markdown",
        # 页面上点「导出报告」是要一个文件，不是在浏览器里打开一页纯文本
        headers={"Content-Disposition": f'attachment; filename="alert-{eventid}.md"'},
    )


@app.post("/api/translate")
def api_translate(body: TranslateRequest) -> JSONResponse:
    """界面切到英文时，AI 生成的自由文本（结论/理由/回答）按需翻译，磁盘缓存复用。
    只应传模型自己写的解读性文本，绝不能传设备/日志的逐字证据——见 translate.py 顶部说明。"""
    from .translate import TranslateError, translate_text

    try:
        return JSONResponse({"translated": translate_text(body.text, body.lang)})
    except TranslateError as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)


@app.get("/api/audit")
def api_audit() -> dict:
    return dashboard.build_audit_view(dashboard.load_alerts())


@app.get("/api/overview")
def api_overview() -> dict:
    """前端总览页。卡片和最近故障都来自 `records/` 里的真实记录。"""
    alerts = dashboard.load_alerts()
    return dashboard.build_overview(alerts, dashboard.build_incidents(alerts))


@app.get("/api/incidents")
def api_incidents() -> list:
    """一次故障一行，不是一条告警一行。含角色、证据、工具轨迹。"""
    return dashboard.build_incidents(dashboard.load_alerts())


@app.get("/api/status")
def api_status() -> JSONResponse:
    return JSONResponse(dashboard.build_status())


@app.get("/api/topology")
def api_topology(refresh: int = 0) -> JSONResponse:
    """`refresh=1`：作废缓存、现去 NetBox 取一次（页面上的「重新读取」按钮）。平时读缓存。"""
    if refresh:
        from netops_ai import topology as _topology

        _topology.invalidate_cache()
    alerts = dashboard.load_alerts()
    return JSONResponse(dashboard.build_topology_view(dashboard.build_incidents(alerts)))


def netbox_signature_ok(secret: str, body: bytes, header: str) -> bool:
    """NetBox webhook 的签名：`X-Hub-Signature: sha512=<hex(HMAC-SHA512(secret, body))>`。没配 secret 就不校验（实验环境）。"""
    import hmac

    if not secret:
        return True
    digest = hmac.new(secret.encode("utf-8"), body, "sha512").hexdigest()
    got = (header or "").split("=", 1)[-1].strip()
    return hmac.compare_digest(digest, got)


@app.post("/webhooks/netbox")
async def netbox_webhook(request: Request) -> JSONResponse:
    """NetBox 里设备/接口/线缆有增删改时（Event Rule → Webhook）通知我们：作废拓扑缓存，下次读取重新去 NetBox 取。"""
    from netops_ai import topology as _topology

    body = await request.body()
    if not netbox_signature_ok(_env().get("NETBOX_WEBHOOK_SECRET", ""), body, request.headers.get("X-Hub-Signature", "")):
        return JSONResponse({"status": "bad_signature"}, status_code=403)
    _topology.invalidate_cache()
    model = ""
    try:
        model = str(json.loads(body or b"{}").get("model") or "")
    except ValueError:
        pass
    return JSONResponse({"status": "cache_invalidated", "model": model})


@app.get("/api/inspection")
def api_get_inspection() -> JSONResponse:
    latest = dashboard.load_latest_inspection()
    if latest is None:
        return JSONResponse({"error": "还没跑过巡检，先 POST /api/inspection/run"}, status_code=404)
    # **返回前端要的形状，不是原始 payload。** 原来直接端 `latest` 出去，
    # 里面没有 `groups`，巡检页拿到就白屏——细节见 `build_inspection_view` 的注释。
    return JSONResponse(dashboard.build_inspection_view(latest))


# ---- 巡检配置（inspection.yaml）：阈值 / 范围 / 忽略清单 ----
# 只读写仓库根那一个 inspection.yaml，请求里没有任何路径参数。改完用 POST /api/inspection/run 重跑。

from netops_ai.inspection import config as _insp_config  # noqa: E402


@app.exception_handler(_insp_config.ConfigError)
async def _inspection_config_error_handler(request: Request, exc: _insp_config.ConfigError) -> JSONResponse:
    return JSONResponse({"message": str(exc)}, status_code=400)


def _inspection_config_view() -> dict:
    try:
        return {**_insp_config.describe(_insp_config.load_config()), "ignore_list": _insp_config.load_config().ignore}
    except _insp_config.ConfigError as exc:
        return {**_insp_config.describe(error=str(exc)), "ignore_list": []}


@app.get("/api/inspection/config")
def api_get_inspection_config() -> dict:
    return _inspection_config_view()


@app.put("/api/inspection/config")
def api_put_inspection_config(body: InspectionConfigRequest) -> dict:
    """整体替换 detectors 和 scope（没写的参数回到默认值），忽略清单不动。非法值 400 + 人话说明。"""
    _insp_config.save_config(body.detectors, body.scope)
    return _inspection_config_view()


@app.post("/api/inspection/ignore")
def api_add_inspection_ignore(body: InspectionIgnoreRequest) -> dict:
    _insp_config.add_ignore(body.host, body.item_key, body.reason)
    return _inspection_config_view()


@app.delete("/api/inspection/ignore")
def api_delete_inspection_ignore(host: str, item_key: str) -> dict:
    _cfg, removed = _insp_config.remove_ignore(host, item_key)
    if not removed:
        return JSONResponse({"message": "忽略清单里没有这一条"}, status_code=404)
    return _inspection_config_view()


@app.post("/api/inspection/run")
def api_run_inspection(background_tasks: BackgroundTasks) -> dict:
    # 扫 2000+ 监控项要跑几秒到几十秒，丢后台，不卡这次 HTTP 请求。趋势（读 Zabbix）和状态（登设备只读）一起跑。
    background_tasks.add_task(dashboard.run_full_inspection)
    return {"status": "started"}


@app.get("/api/inspection/export")
def api_export_inspection(format: str = "md") -> Response:
    """导出当前巡检报告：`md` 或自包含 `html`。内容就是页面那份数据，不另算。"""
    from netops_ai.inspection import export as _export

    latest = dashboard.load_latest_inspection()
    if latest is None:
        return JSONResponse({"error": "还没跑过巡检，先 POST /api/inspection/run"}, status_code=404)
    view = dashboard.build_inspection_view(latest)
    if format == "html":
        return Response(_export.render_html(view), media_type="text/html; charset=utf-8",
                        headers={"Content-Disposition": 'attachment; filename="inspection-report.html"'})
    if format == "md":
        return Response(_export.render_markdown(view), media_type="text/markdown; charset=utf-8",
                        headers={"Content-Disposition": 'attachment; filename="inspection-report.md"'})
    return JSONResponse({"error": "format 只支持 md / html"}, status_code=400)


@app.post("/api/schedule/run")
def api_schedule_run(background_tasks: BackgroundTasks) -> dict:
    """手动触发一轮定时任务，不等下一个周期。"""
    from . import schedule

    background_tasks.add_task(schedule.run_once)
    return {"status": "started"}


@app.post("/api/inspection/advise")
def api_advise_inspection(background_tasks: BackgroundTasks) -> JSONResponse:
    """给最近一次巡检出处置建议。**单独一个入口，不跟着页面加载跑**——
    这是巡检侧唯一一次模型调用，要花钱花时间（实测 59 秒），
    挂在 `GET /api/inspection` 上会变成"每刷一次页面烧一次钱"。
    """
    latest = dashboard.load_latest_inspection()
    if latest is None:
        return JSONResponse({"error": "还没跑过巡检，先 POST /api/inspection/run"}, status_code=404)
    def _run() -> None:
        from netops_ai.inspection import advise as _advise

        try:
            kept, _ignored = dashboard.split_ignored(latest, _insp_config.load_config())
        except _insp_config.ConfigError:
            kept = latest.get("findings") or []  # 配置文件坏了就别因此不出建议，按全部发现来
        _advise.save(_advise.advise({**latest, "findings": kept}, status=dashboard.load_latest_status()))

    background_tasks.add_task(_run)
    return JSONResponse({"status": "started"})


# 前端产物由这个进程托管。**必须挂在文件最后**：`StaticFiles` 挂在 `/` 会吃掉后面所有路径。
# 没 build 就不挂，接口照常能用。

_WEB_DIST = Path(__file__).resolve().parents[2] / "web" / "dist"
if _WEB_DIST.is_dir():
    app.mount("/", StaticFiles(directory=str(_WEB_DIST), html=True), name="web")
