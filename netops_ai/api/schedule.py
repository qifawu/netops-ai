"""定时任务：隔 `SCHEDULE_INTERVAL_MINUTES` 分钟跑一轮巡检，并给每台设备存一份配置（A13 的「故障前」）。

一个守护线程，不引调度库。处置建议要调模型花钱，不在这里跑。
每轮结果写 `records/schedule-state.json`，头区状态点读它。

**巡检计划**（`inspection-plans/*.yaml`，网页上对话制定的）走旁边另一个守护线程：每分钟看一眼哪些已启用的计划到点了，
到点的各起一个线程去跑（同一个计划不重入），结果存 `records/checklist-runs/`，状态写进同一个 schedule-state 的
`plans` 键（原有的 `at` / `jobs` 格式不动）。这个线程跟 `SCHEDULE_INTERVAL_MINUTES` 无关，没有启用的计划就什么都不做。
"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timezone
from typing import Callable

from . import dashboard

STATE_PATH = dashboard.RECORDS_DIR / "schedule-state.json"
_started = False
_plans_started = False
#: 内置巡检和计划线程都会写 schedule-state，读改写要串行
_STATE_LOCK = threading.Lock()
PLAN_TICK_SECONDS = 60


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
    with _STATE_LOCK:
        old = load_state() or {}
        if old.get("plans"):
            state["plans"] = old["plans"]  # 计划的状态是另一个线程写的，这里整份重写时要带上
        _write_state(state)
    return state


def _write_state(state: dict) -> None:
    try:
        STATE_PATH.parent.mkdir(exist_ok=True)
        tmp = STATE_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(STATE_PATH)
    except OSError:
        pass


def load_state() -> dict | None:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


# --------------------------------------------------------------------------- 巡检计划


def record_plan_state(name: str, entry: dict) -> None:
    """把一个计划这次运行的结果写进 schedule-state 的 `plans` 键，别的键原样保留。"""
    with _STATE_LOCK:
        state = load_state() or {}
        state.setdefault("plans", {})[name] = entry
        _write_state(state)


def forget_plan(name: str) -> None:
    """计划删除 / 改名后，从 schedule-state 的 `plans` 里摘掉它（状态点不再报它）。"""
    with _STATE_LOCK:
        state = load_state() or {}
        if name in (state.get("plans") or {}):
            state["plans"].pop(name)
            _write_state(state)


def run_plan_and_record(name: str, adapter_factory=None) -> dict:
    """跑一次计划，存结果，状态写进 schedule-state。重入时抛 `PlanBusy`（调用方决定怎么回）；其它失败只记下来不抛。"""
    from netops_ai.inspection import plans

    at = datetime.now(timezone.utc).isoformat()
    try:
        result, _path = plans.run_plan(name, adapter_factory)
    except plans.PlanBusy:
        raise
    except Exception as exc:  # noqa: BLE001 - 定时线程里一个计划失败不能影响别的
        entry = {"at": at, "ok": False, "note": f"{type(exc).__name__}: {exc}"}
        record_plan_state(name, entry)
        return entry
    s = result["summary"]
    ok = not (s["error"] or s["unreachable_devices"] or s["denied"])
    note = (f"通过 {s['pass']}，未通过 {s['fail']}，出错 {s['error']}，拒绝 {s['denied']}，不可达 {s['unreachable_devices']} 台")
    entry = {"at": at, "ok": ok, "note": note, "run_id": result["run_id"]}
    record_plan_state(name, entry)
    return entry


def plan_tick(now: datetime | None = None, spawn: Callable[[Callable[[], object]], object] | None = None,
              runner: Callable[[str], object] | None = None) -> list[str]:
    """看一眼哪些计划到点了，到点的交给 `spawn` 去跑。时钟、起线程的方式、执行函数都能注入，测试不用真等。"""
    from netops_ai.inspection import plans

    now = now or datetime.now(timezone.utc)
    runner = runner or run_plan_and_record
    spawn = spawn or (lambda fn: threading.Thread(target=fn, name="netops-plan-run", daemon=True).start())
    started = []
    for name in plans.due_plans(now):
        def job(name: str = name) -> None:
            try:
                runner(name)
            except plans.PlanBusy:
                pass  # 上一次还没跑完，这次跳过
        spawn(job)
        started.append(name)
    return started


def plans_status() -> tuple[int, bool, str]:
    """（启用的计划数, 上一次是否都正常, 说明）给头区状态点用。"""
    from netops_ai.inspection import plans

    enabled = [p["name"] for p in plans.list_plans() if p.get("enabled")]
    if not enabled:
        return 0, True, ""
    st = (load_state() or {}).get("plans") or {}
    ran = {n: st[n] for n in enabled if n in st}
    ok = all(e.get("ok") for e in ran.values())
    note = f"巡检计划 {len(enabled)} 个启用" + ("：" + "；".join(f"{n} {e.get('note', '')}" for n, e in ran.items()) if ran else "，还没跑过")
    return len(enabled), ok, note


def start_plans() -> None:
    global _plans_started
    if _plans_started:
        return
    _plans_started = True

    def loop() -> None:
        while True:
            time.sleep(PLAN_TICK_SECONDS)
            try:
                plan_tick()
            except Exception:  # noqa: BLE001 - 线程不能因为一次读文件出错就死掉
                pass

    threading.Thread(target=loop, name="netops-plan-schedule", daemon=True).start()


def interval_minutes(env: dict) -> int:
    try:
        return max(0, int(env.get("SCHEDULE_INTERVAL_MINUTES") or 0))
    except ValueError:
        return 0


def start() -> None:
    """FastAPI 启动时调一次。没配间隔就什么都不做。"""
    global _started
    start_plans()
    minutes = interval_minutes(dashboard._env())
    if _started or not minutes:
        return
    _started = True

    def loop() -> None:
        while True:
            time.sleep(minutes * 60)
            run_once()

    threading.Thread(target=loop, name="netops-schedule", daemon=True).start()
