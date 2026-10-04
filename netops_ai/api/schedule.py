"""定时任务：隔 `SCHEDULE_INTERVAL_MINUTES` 分钟跑一轮巡检，并给每台设备存一份配置（A13 的「故障前」）。

一个守护线程，不引调度库。处置建议要调模型花钱，不在这里跑。
每轮结果写 `records/schedule-state.json`，头区状态点读它。
"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timezone

from . import dashboard

STATE_PATH = dashboard.RECORDS_DIR / "schedule-state.json"
_started = False


def _pull_all_configs(env: dict) -> tuple[bool, str]:
    try:
        from netops_ai import configs
    except ImportError:  # 开源版没有配置备份
        return True, "未启用（本版本不含配置备份）"
    from netops_ai import topology as topo

    from .pipeline import _device_adapter_from_env

    if not env.get("DEVICE_USERNAME"):
        return False, "没配 DEVICE_USERNAME，跳过配置快照"
    vendor = env.get("DEVICE_VENDOR", "cisco")
    done, failed = [], []
    for dev in topo.load_topology().values():
        if not dev.host:
            continue
        try:
            with _device_adapter_from_env({**env, "DEVICE_HOST": dev.host}, vendor) as adapter:
                text, err = configs.pull(adapter, vendor)
            if text:
                configs.save(dev.host, text, source="定时")
                done.append(dev.name)
            else:
                failed.append(f"{dev.name}：{err}")
        except Exception as exc:  # noqa: BLE001 - 一台失败不影响其它台
            failed.append(f"{dev.name}：{type(exc).__name__}: {exc}")
    note = f"存了 {len(done)} 台" + (f"；失败 {len(failed)} 台：" + "；".join(failed) if failed else "")
    return not failed and bool(done), note


def run_once() -> dict:
    """跑一轮，返回并落盘这一轮的状态。每一项失败都只记下来，不抛。"""
    env = dashboard._env()
    state = {"at": datetime.now(timezone.utc).isoformat(), "jobs": {}}
    try:
        errors = dashboard.run_full_inspection()
        state["jobs"]["inspection"] = (
            {"ok": False, "note": "；".join(f"{k}: {v}" for k, v in errors.items())} if errors else {"ok": True, "note": "巡检完成"}
        )
    except Exception as exc:  # noqa: BLE001
        state["jobs"]["inspection"] = {"ok": False, "note": f"{type(exc).__name__}: {exc}"}
    ok, note = _pull_all_configs(env)
    state["jobs"]["configs"] = {"ok": ok, "note": note}
    try:
        STATE_PATH.parent.mkdir(exist_ok=True)
        STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError:
        pass
    return state


def load_state() -> dict | None:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def interval_minutes(env: dict) -> int:
    try:
        return max(0, int(env.get("SCHEDULE_INTERVAL_MINUTES") or 0))
    except ValueError:
        return 0


def start() -> None:
    """FastAPI 启动时调一次。没配间隔就什么都不做。"""
    global _started
    minutes = interval_minutes(dashboard._env())
    if _started or not minutes:
        return
    _started = True

    def loop() -> None:
        while True:
            time.sleep(minutes * 60)
            run_once()

    threading.Thread(target=loop, name="netops-schedule", daemon=True).start()
