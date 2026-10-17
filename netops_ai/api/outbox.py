"""对外发送草稿箱的接口：列出 / 确认发送 / 丢弃。实现在 `netops_ai.outbox`，这里只做参数和状态码。

确认发送是**唯一**会真正对外发信的入口；必须带确认人（`approver`）。看板默认没有登录，谁能打开页面谁就能确认——
对外发信的确认权限要靠登录 / 反向代理 / 网络隔离管住。
"""
from __future__ import annotations

import os
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from netops_ai import outbox as OB

router = APIRouter(prefix="/api/outbox", tags=["outbox"])


class Decision(BaseModel):
    approver: str = Field(default="", max_length=64)


def _env() -> dict[str, str]:
    from netops_ai.api.dashboard import _env as dash_env

    return dash_env()


def senders() -> dict[str, Any]:
    e = _env()
    out: dict[str, Any] = {"email": OB.email_sender(e)}
    if e.get("FEISHU_WEBHOOK_URL", "").startswith("http"):
        out["feishu"] = OB.feishu_sender(e["FEISHU_WEBHOOK_URL"])
    return out


def _who(request: Request, body: Decision) -> str:
    """确认人：开了登录就取登录用户名（不能冒名），没开登录取请求里的 approver。"""
    user = getattr(request.state, "user", None)
    return (user or {}).get("username") or body.approver


def _view(rec: dict[str, Any]) -> dict[str, Any]:
    return {**rec, "attachments": [os.path.basename(a) for a in rec.get("attachments") or []]}


@router.get("")
def list_drafts(status: str | None = None):
    if status is not None and status not in OB.STATUSES:
        return JSONResponse({"message": f"status must be one of {', '.join(OB.STATUSES)}"}, status_code=400)
    return {"drafts": [_view(r) for r in OB.list_drafts(status)]}


@router.get("/{draft_id}")
def get_draft(draft_id: str):
    rec = OB.get(draft_id)
    return _view(rec) if rec else JSONResponse({"message": "draft not found"}, status_code=404)


@router.post("/{draft_id}/approve")
def approve(draft_id: str, body: Decision, request: Request):
    try:
        rec = OB.approve_and_send(draft_id, _who(request, body), senders(), allowlist=_env().get("OUTBOX_ALLOWED_RECIPIENTS"))
    except OB.OutboxError as exc:
        return JSONResponse({"message": str(exc)}, status_code=400)
    return JSONResponse(_view(rec), status_code=200 if rec["status"] == "sent" else 502)


@router.post("/{draft_id}/discard")
def discard(draft_id: str, body: Decision, request: Request):
    try:
        return _view(OB.discard(draft_id, _who(request, body) or "dashboard"))
    except OB.OutboxError as exc:
        return JSONResponse({"message": str(exc)}, status_code=400)
