"""试运行（dry-run）：保存前，对草案里选定的设备真的执行一遍草案里的只读命令，看命令在设备上能不能跑通。

跟正式运行走**同一条路**：同一个设备适配器（命令先过只读白名单，被拒的不发出）、同样的指标提取和规则判定
（含有状态的指标规则——对照的是这个计划已存下的运行记录，草案还没保存过就是「首次 / 无基线」）。
但**什么都不写**：不存运行记录、不写 schedule-state、不碰到点判断用的时间，也不会触发任何告警。

设备之间并发（有上限），整体有超时，可以中途取消；每做完一项报一次进度。
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timezone
from typing import Any, Callable

from netops_ai.inspection import checklist as CL

MAX_WORKERS = 4
TOTAL_TIMEOUT_SECONDS = 120
OUTPUT_HEAD_LINES = 20


def draft_key(draft: dict[str, Any]) -> str:
    """草案「会影响试运行结果」那部分的指纹：设备、检查项（命令 / 规则 / 提取）。草案改了指纹就变 → 页面上把旧结果标成已过期。"""
    import hashlib
    import json

    body = {"devices": sorted((d.get("name"), d.get("host")) for d in draft.get("devices") or []),
            "checks": [{k: c.get(k) for k in ("id", "command", "devices", "expect", "extract")} for c in draft.get("checks") or []]}
    return hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")).hexdigest()[:16]


def _status(res: Any) -> tuple[str, str]:
    if not res.allowed:
        return "denied", res.denial_reason or ""
    if res.error:
        err = str(res.error)
        return ("timeout" if "timed out" in err.lower() or "timeout" in err.lower() else "error"), err
    return "ok", ""


def _run_one(adapter: Any, check: dict[str, Any], dev: str, hist: CL.MetricHistory, head_lines: int) -> dict[str, Any]:
    t0 = time.monotonic()
    try:
        res = adapter.run(check["command"])
    except Exception as exc:  # noqa: BLE001 - 一项出错不影响其它项
        return _empty(check, "error", f"{type(exc).__name__}: {exc}", int((time.monotonic() - t0) * 1000))
    elapsed = int((time.monotonic() - t0) * 1000)
    status, detail = _status(res)
    out = (res.output or "") if status == "ok" else ""
    body = out.strip("\r\n").splitlines()
    item: dict[str, Any] = {**_empty(check, status, detail, elapsed), "lines": len(body), "bytes": len(out.encode("utf-8")),
                            "output_head": "\n".join(body[:head_lines]), "truncated_lines": max(0, len(body) - head_lines)}
    if status != "ok":
        return item
    for x in check.get("extract") or []:
        item["metrics"][x["name"]] = CL._extract(x, out)
    item["evaluations"] = CL.evaluate_expectations(check, out, item["metrics"], dev, hist)
    item["would_pass"] = not any(r.get("passed") is False for r in item["evaluations"])
    return item


def _empty(check: dict[str, Any], status: str, detail: str, elapsed_ms: int = 0) -> dict[str, Any]:
    return {"id": check["id"], "command": check["command"], "status": status, "detail": detail, "elapsed_ms": elapsed_ms,
            "lines": 0, "bytes": 0, "metrics": {}, "evaluations": [], "would_pass": None, "output_head": "", "truncated_lines": 0}


def dry_run(draft: dict[str, Any], adapter_factory: Callable[[Any], Any], *, devices: list[str] | None = None,
            history: list[dict[str, Any]] | None = None, cancel: threading.Event | None = None,
            progress: Callable[[int, int], None] | None = None, max_workers: int = MAX_WORKERS,
            total_timeout: float = TOTAL_TIMEOUT_SECONDS, head_lines: int = OUTPUT_HEAD_LINES) -> dict[str, Any]:
    """试运行一次，返回逐设备逐检查项的结果。不写任何东西。"""
    # 白名单拒绝不算草案错误：那几条照样列出来，状态是「被拒」，由适配器挡住不发出（跟正式运行一样）
    problems = [p for p in CL.validate_checklist(draft) if "refused by the read-only whitelist" not in p]
    if problems:
        raise ValueError("invalid draft: " + "; ".join(problems))
    cancel = cancel or threading.Event()
    wanted = set(devices) if devices else None
    devs = [d for d in draft["devices"] if wanted is None or d["name"] in wanted]
    plan_checks = {d["name"]: [c for c in draft["checks"] if c.get("devices") is None or d["name"] in c["devices"]] for d in devs}
    total = sum(len(v) for v in plan_checks.values())
    done = [0]
    lock = threading.Lock()
    hist = CL.MetricHistory(history)
    results: dict[str, dict[str, Any]] = {d["name"]: {"name": d["name"], "host": d["host"], "error": "", "checks": []} for d in devs}

    def tick(n: int = 1) -> None:
        with lock:
            done[0] += n
            if progress:
                progress(done[0], total)

    def work(d: dict[str, Any]) -> None:
        out = results[d["name"]]
        checks = plan_checks[d["name"]]
        if cancel.is_set():
            return
        try:
            adapter = adapter_factory(type("Dev", (), {"name": d["name"], "host": d["host"]})())
        except Exception as exc:  # noqa: BLE001
            msg = f"{type(exc).__name__}: {exc}"
            out["error"] = msg
            status = "timeout" if "timed out" in msg.lower() else "error"
            out["checks"] = [_empty(c, status, msg) for c in checks]
            tick(len(checks))
            return
        try:
            for c in checks:
                if cancel.is_set():
                    break
                out["checks"].append(_run_one(adapter, c, d["name"], hist, head_lines))
                tick()
        finally:
            close = getattr(adapter, "close", None)
            if callable(close):
                close()

    started = datetime.now(timezone.utc)
    deadline = time.monotonic() + total_timeout
    pool = ThreadPoolExecutor(max_workers=max(1, min(max_workers, len(devs) or 1)), thread_name_prefix="dry-run")
    futures = {pool.submit(work, d): d["name"] for d in devs}
    pending = set(futures)
    outcome = "done"
    while pending:
        finished, pending = wait(pending, timeout=0.2, return_when=FIRST_COMPLETED)
        if cancel.is_set():
            outcome = "cancelled"
            break
        if time.monotonic() > deadline:
            outcome = "timeout"
            cancel.set()  # 让还在跑的设备别再发下一条命令
            break
    pool.shutdown(wait=False, cancel_futures=True)
    # 没跑到的项也列出来，写明原因（取消 / 整体超时）
    for d in devs:
        out = results[d["name"]]
        ran = {c["id"] for c in out["checks"]}
        for c in plan_checks[d["name"]]:
            if c["id"] not in ran:
                out["checks"].append(_empty(c, "cancelled" if outcome == "cancelled" else "timeout",
                                            "cancelled" if outcome == "cancelled" else f"overall time limit {total_timeout:g}s reached"))
    flat = [c for d in results.values() for c in d["checks"]]
    summary = {s: sum(1 for c in flat if c["status"] == s) for s in ("ok", "denied", "timeout", "error", "cancelled")}
    summary["would_fail"] = sum(1 for c in flat if c.get("would_pass") is False)
    return {"state": outcome, "cancelled": outcome == "cancelled", "timed_out": outcome == "timeout", "draft_key": draft_key(draft),
            "started_at": started.isoformat(timespec="seconds"),
            "finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "devices": [results[d["name"]] for d in devs], "summary": summary, "total": total}
