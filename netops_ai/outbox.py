"""对外发送的「草稿箱」：所有要发给外部（运营商、管理层）的邮件 / 飞书消息，先生成草稿，人确认后才真发。

为什么：对外发信不可撤回，内容又是系统自动写的。所以**系统自己永远不直接发**——只写草稿（`pending`），
由人看过、点确认（`approve_and_send`）才发；发完留痕（谁确认的、什么时候、结果）。

另有两道硬闸门：
- 收件人白名单：邮件只能发给 `OUTBOX_ALLOWED_RECIPIENTS`（逗号分隔，可写完整地址或 `@域名`）里的地址，不在名单里的草稿能存、不能发；
- 没配 SMTP / webhook 就报「没配置」，不会假装发成功。

存放：`<records>/outbox/<id>.json`，目录可用 `NETOPS_OUTBOX_DIR` 改。附件只存路径，发送时才读文件。
纯标准库；发送通道（邮件 / 飞书）可注入，方便测试。
"""
from __future__ import annotations

import json
import mimetypes
import os
import smtplib
import ssl
import uuid
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Callable

STATUSES = ("pending", "sent", "discarded", "failed")
CHANNELS = ("email", "feishu")


class OutboxError(ValueError):
    pass


def outbox_dir() -> Path:
    env = os.environ.get("NETOPS_OUTBOX_DIR")
    return Path(env) if env else Path(__file__).resolve().parents[1] / "records" / "outbox"


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _path(draft_id: str, root: Path | None = None) -> Path:
    if not draft_id.isalnum():
        raise OutboxError("bad draft id")
    return (root or outbox_dir()) / f"{draft_id}.json"


def _write(rec: dict[str, Any], root: Path | None = None) -> None:
    path = _path(rec["id"], root)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def create(*, channel: str, kind: str, subject: str, body: str, to: list[str] | None = None, attachments: list[str] | None = None,
           created_by: str = "system", meta: dict[str, Any] | None = None, root: Path | None = None) -> dict[str, Any]:
    """写一份草稿，状态 pending，**不发送**。"""
    if channel not in CHANNELS:
        raise OutboxError(f"channel must be one of {CHANNELS}")
    if channel == "email" and not to:
        raise OutboxError("an email draft needs at least one recipient")
    rec = {"id": uuid.uuid4().hex[:12], "created_at": _now(), "created_by": created_by, "channel": channel, "kind": kind,
           "to": list(to or []), "subject": subject[:200], "body": body, "attachments": [str(a) for a in attachments or []],
           "status": "pending", "approved_by": "", "sent_at": "", "error": "", "meta": meta or {}}
    _write(rec, root)
    return rec


def get(draft_id: str, root: Path | None = None) -> dict[str, Any] | None:
    try:
        return json.loads(_path(draft_id, root).read_text(encoding="utf-8"))
    except (OSError, ValueError, OutboxError):
        return None


def list_drafts(status: str | None = None, root: Path | None = None) -> list[dict[str, Any]]:
    base = root or outbox_dir()
    rows = []
    for f in sorted(base.glob("*.json")) if base.exists() else []:
        try:
            rec = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if status is None or rec.get("status") == status:
            rows.append(rec)
    return sorted(rows, key=lambda r: r.get("created_at", ""), reverse=True)


def discard(draft_id: str, who: str, *, root: Path | None = None) -> dict[str, Any]:
    rec = get(draft_id, root)
    if rec is None:
        raise OutboxError("draft not found")
    if rec["status"] != "pending":
        raise OutboxError(f"only a pending draft can be discarded (this one is {rec['status']})")
    rec.update(status="discarded", approved_by=who)
    _write(rec, root)
    return rec


def allowed(addr: str, allowlist: str | None = None) -> bool:
    """收件人是否在白名单：`allowlist` 默认取环境变量 `OUTBOX_ALLOWED_RECIPIENTS`；空白名单 = 一个都不放行。"""
    raw = allowlist if allowlist is not None else os.environ.get("OUTBOX_ALLOWED_RECIPIENTS", "")
    items = [x.strip().lower() for x in raw.split(",") if x.strip()]
    a = addr.strip().lower()
    return any(a == it or (it.startswith("@") and a.endswith(it)) for it in items)


# ------------------------------------------------------------------ 发送通道

def email_sender(env: dict[str, str] | None = None) -> Callable[[dict[str, Any]], None]:
    """SMTP 发信：`SMTP_HOST / SMTP_PORT / SMTP_USER / SMTP_PASSWORD / SMTP_FROM / SMTP_SECURITY(ssl|starttls|none)`。
    没配 SMTP_HOST 或 SMTP_FROM 直接报错，不静默。"""
    cfg = env if env is not None else dict(os.environ)

    def send(rec: dict[str, Any]) -> None:
        host, sender = cfg.get("SMTP_HOST", ""), cfg.get("SMTP_FROM", "")
        if not host or not sender:
            raise OutboxError("SMTP is not configured (SMTP_HOST / SMTP_FROM)")
        msg = EmailMessage()
        msg["From"], msg["To"], msg["Subject"] = sender, ", ".join(rec["to"]), rec["subject"]
        msg.set_content(rec["body"])
        for a in rec.get("attachments") or []:
            p = Path(a)
            ctype = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
            maintype, _, subtype = ctype.partition("/")
            msg.add_attachment(p.read_bytes(), maintype=maintype, subtype=subtype, filename=p.name)
        security = (cfg.get("SMTP_SECURITY") or "starttls").lower()
        port = int(cfg.get("SMTP_PORT") or (465 if security == "ssl" else 587 if security == "starttls" else 25))
        ctx = ssl.create_default_context()
        with (smtplib.SMTP_SSL(host, port, timeout=20, context=ctx) if security == "ssl" else smtplib.SMTP(host, port, timeout=20)) as s:
            if security == "starttls":
                s.starttls(context=ctx)
            if cfg.get("SMTP_USER"):
                s.login(cfg["SMTP_USER"], cfg.get("SMTP_PASSWORD", ""))
            s.send_message(msg)

    return send


def feishu_sender(webhook_url: str) -> Callable[[dict[str, Any]], None]:
    """飞书群机器人：发一张文本卡片（飞书 webhook 不带附件，附件在草稿里只作为「报告已生成」的提示）。"""
    def send(rec: dict[str, Any]) -> None:
        from netops_ai.feishu.sender import send_card

        card = {"header": {"title": {"tag": "plain_text", "content": rec["subject"][:100]}},
                "elements": [{"tag": "div", "text": {"tag": "lark_md", "content": rec["body"][:3000]}}]}
        ok, err = send_card(webhook_url, card)
        if not ok:
            raise OutboxError(f"feishu send failed: {err}")
    return send


def approve_and_send(draft_id: str, who: str, senders: dict[str, Callable[[dict[str, Any]], None]], *, allowlist: str | None = None,
                     root: Path | None = None) -> dict[str, Any]:
    """人确认后才真发。检查：草稿还是 pending、确认人不为空、邮件收件人全在白名单。发送失败状态记 failed 并带原因，可再试。"""
    if not who or not who.strip():
        raise OutboxError("approver is required")
    rec = get(draft_id, root)
    if rec is None:
        raise OutboxError("draft not found")
    if rec["status"] not in ("pending", "failed"):
        raise OutboxError(f"this draft is {rec['status']}; nothing to send")
    if rec["channel"] == "email":
        blocked = [a for a in rec["to"] if not allowed(a, allowlist)]
        if blocked:
            raise OutboxError(f"recipient(s) not on the allowlist (OUTBOX_ALLOWED_RECIPIENTS): {', '.join(blocked)}")
    send = senders.get(rec["channel"])
    if send is None:
        raise OutboxError(f"no sender configured for channel {rec['channel']!r}")
    rec["approved_by"] = who
    try:
        send(rec)
    except Exception as exc:  # noqa: BLE001 - 失败要留痕、可重试，不吞
        rec.update(status="failed", error=f"{type(exc).__name__}: {str(exc)[:200]}")
        _write(rec, root)
        return rec
    rec.update(status="sent", sent_at=_now(), error="")
    _write(rec, root)
    return rec
