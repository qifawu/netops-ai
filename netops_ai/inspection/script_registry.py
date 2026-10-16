"""判定脚本库：助手写的脚本不是写完就用，而是先进「待审」，人批准后才能被计划引用；每一版都留痕。

一个脚本 = 一个 id + 若干版本（v1、v2……）。每版一个 JSON 文件 `<库目录>/<id>/v<N>.json`，记录：
源码、sha256、生成它的需求原文（goal）、用的模型、自测结果摘要、状态、批准人 / 时间、批准时的 HMAC 签名。

状态流转：``pending`` →（批准）→ ``approved`` →（撤销）→ ``revoked``；``pending`` →（拒绝）→ ``rejected``。
- 只有 ``approved`` 且「源码哈希 + 签名」都对得上的版本能被取用（`get_approved`）；文件被改过就当没批准；
- 批准要求：静态检查通过、自测通过、源码哈希对得上；
- 改源码 = 新版本（回到 pending），旧的已批准版本不受影响，直到有人批准新版本并撤销旧版本。

纯文件操作，不 import 框架；库目录默认 `records/check-scripts/`，可用环境变量 `NETOPS_SCRIPT_LIB_DIR` 改。
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from netops_ai.inspection import check_script as CS

STATUSES = ("pending", "approved", "rejected", "revoked")
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")


class RegistryError(ValueError):
    pass


def script_key(plan_name: str, check_id: str) -> str:
    """计划里一条脚本检查在库里的 id：`<计划名>.<检查项 id>`（小写，非法字符变 -）。"""
    raw = re.sub(r"[^a-z0-9_.-]+", "-", f"{plan_name or 'draft'}.{check_id}".lower()).strip("-.")
    return raw[:64] or "script"


def lib_dir() -> Path:
    env = os.environ.get("NETOPS_SCRIPT_LIB_DIR")
    return Path(env) if env else Path(__file__).resolve().parents[2] / "records" / "check-scripts"


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _check_id(script_id: str) -> str:
    if not isinstance(script_id, str) or not _ID_RE.match(script_id):
        raise RegistryError("script id: lowercase letters, digits, '_', '.', '-' (max 64)")
    return script_id


def _path(script_id: str, version: int, root: Path | None = None) -> Path:
    return (root or lib_dir()) / _check_id(script_id) / f"v{int(version)}.json"


def _write(path: Path, rec: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def versions(script_id: str, root: Path | None = None) -> list[int]:
    d = (root or lib_dir()) / _check_id(script_id)
    return sorted(int(m.group(1)) for f in d.glob("v*.json") if (m := re.fullmatch(r"v(\d+)\.json", f.name))) if d.exists() else []


def load(script_id: str, version: int, root: Path | None = None) -> dict[str, Any] | None:
    try:
        return json.loads(_path(script_id, version, root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def submit(script_id: str, source: str, *, goal: str, model: str = "", self_test: dict[str, Any] | None = None,
           root: Path | None = None) -> dict[str, Any]:
    """助手写好一版：进待审。同样的源码已经有一版（任何状态）就不重复建，返回那一版。"""
    problems = CS.validate_source(source)
    if problems:
        raise RegistryError("; ".join(problems))
    digest = CS.sha256(source)
    existing = versions(script_id, root)
    for v in reversed(existing):
        rec = load(script_id, v, root)
        if rec and rec.get("sha256") == digest and rec.get("status") in ("pending", "approved"):
            return rec
    st = self_test or {}
    rec = {"script_id": script_id, "version": (existing[-1] + 1) if existing else 1, "status": "pending", "source": source,
           "sha256": digest, "goal": goal, "model": model, "created_at": _now(), "created_by": "assistant",
           "self_test": {"ok": bool(st.get("ok")), "plausible": bool(st.get("plausible")), "cases": len(st.get("cases") or []),
                         "problems": list(st.get("problems") or [])[:5]},
           "approved_by": "", "approved_at": "", "signature": "", "history": [{"at": _now(), "event": "submitted"}]}
    _write(_path(script_id, rec["version"], root), rec)
    return rec


def _transition(script_id: str, version: int, root: Path | None, allowed_from: tuple[str, ...], to: str, who: str,
                note: str = "") -> dict[str, Any]:
    rec = load(script_id, version, root)
    if rec is None:
        raise RegistryError(f"{script_id} v{version}: not found")
    if rec.get("status") not in allowed_from:
        raise RegistryError(f"{script_id} v{version}: cannot go from {rec.get('status')!r} to {to!r}")
    rec["status"] = to
    rec.setdefault("history", []).append({"at": _now(), "event": to, "by": who, **({"note": note} if note else {})})
    return rec


def approve(script_id: str, version: int, approver: str, *, root: Path | None = None) -> dict[str, Any]:
    """批准一版。必须：源码哈希对得上、静态检查通过、自测通过。批准时才签名。"""
    if not approver or not str(approver).strip():
        raise RegistryError("approver is required")
    rec = _transition(script_id, version, root, ("pending",), "approved", approver)
    if CS.sha256(rec["source"]) != rec["sha256"]:
        raise RegistryError("the stored source no longer matches its hash; refusing to approve")
    problems = CS.validate_source(rec["source"])
    if problems:
        raise RegistryError("; ".join(problems))
    if not rec.get("self_test", {}).get("ok"):
        raise RegistryError("the script did not pass its self-tests and cannot be approved")
    rec.update(approved_by=approver, approved_at=_now(), signature=CS.sign(rec["source"]))
    _write(_path(script_id, version, root), rec)
    return rec


def reject(script_id: str, version: int, who: str, note: str = "", *, root: Path | None = None) -> dict[str, Any]:
    rec = _transition(script_id, version, root, ("pending",), "rejected", who, note)
    _write(_path(script_id, version, root), rec)
    return rec


def revoke(script_id: str, version: int, who: str, note: str = "", *, root: Path | None = None) -> dict[str, Any]:
    rec = _transition(script_id, version, root, ("approved",), "revoked", who, note)
    rec["signature"] = ""
    _write(_path(script_id, version, root), rec)
    return rec


def usable(rec: dict[str, Any] | None) -> bool:
    """这一版现在能不能被计划取用：已批准、源码哈希没变、签名有效、静态检查仍然通过。"""
    return bool(rec and rec.get("status") == "approved" and CS.sha256(str(rec.get("source") or "")) == rec.get("sha256")
                and CS.verify(str(rec.get("source") or ""), rec.get("signature")) and not CS.validate_source(rec.get("source")))


def get_approved(script_id: str, version: int | None = None, *, root: Path | None = None) -> dict[str, Any] | None:
    """取一版能用的脚本（不指定版本 = 最新的可用版）。找不到 / 被改过 / 被撤销返回 None。"""
    for v in ([version] if version is not None else reversed(versions(script_id, root))):
        rec = load(script_id, v, root)
        if usable(rec):
            return rec
    return None


def list_scripts(root: Path | None = None) -> list[dict[str, Any]]:
    """库里每个脚本一行：最新版本、状态、可用版本。给审核 / 管理页用。"""
    base = root or lib_dir()
    out = []
    for d in sorted(p for p in base.glob("*") if p.is_dir()) if base.exists() else []:
        vs = versions(d.name, root)
        if not vs:
            continue
        last = load(d.name, vs[-1], root) or {}
        good = get_approved(d.name, root=root)
        out.append({"script_id": d.name, "latest_version": vs[-1], "latest_status": last.get("status"),
                    "usable_version": good["version"] if good else None, "goal": last.get("goal", ""),
                    "approved_by": (good or {}).get("approved_by", "")})
    return out
