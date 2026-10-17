"""外网链路翻滚（flap）检测 + 给运营商（ISP）的邮件草稿。

检测：读 Zabbix 里这条链路接口的 `Operational status`（ifOperStatus：1 = up，其它 = 非 up）历史，数「up → 非 up」的次数，
窗口内达到阈值就算翻滚。只读 Zabbix，不碰设备。

通知：**只生成草稿**（`outbox`，状态 pending），人看过、确认才会发给运营商——对外发信不可撤回，内容又是自动写的。
同一条链路在冷却期内（默认 6 小时）不重复出草稿，避免翻滚一晚上堆出几十封。

邮件正文只写事实（哪条电路、哪个接口、什么时间 up / down 了几次），不猜原因、不指责，请对方核查线路 / 对端设备。

链路清单：仓库根 `isp-links.yaml`（见 `examples/isp-links.yaml`）::

    - name: wan-primary          # 链路名（草稿去重用）
      host: R1                   # Zabbix 里的主机名
      interface: Gi0/1           # 接口名（和 Zabbix 监控项「Interface Gi0/1(): Operational status」一致）
      circuit_id: CN-BJ-000123   # 运营商的电路编号
      isp: Example Telecom
      email: [noc@isp.example]   # 收件人（发送时还要在 OUTBOX_ALLOWED_RECIPIENTS 白名单里）
      threshold: 3               # 窗口内 down 次数达到几次算翻滚（默认 3）
      window_minutes: 60         # 统计窗口（默认 60）
      cooldown_hours: 6          # 出过草稿后多久内不再出（默认 6）
      signature: "Network Operations, Example Corp"
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from netops_ai import outbox

UP = 1


def load_links(path: Path) -> list[dict[str, Any]]:
    import yaml

    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or []
    except OSError:
        return []
    if not isinstance(data, list):
        raise ValueError("isp-links.yaml must be a list of links")
    links = []
    for i, d in enumerate(data):
        if not isinstance(d, dict) or not all(d.get(k) for k in ("name", "host", "interface", "email")):
            raise ValueError(f"link #{i + 1}: name, host, interface and email are required")
        emails = [d["email"]] if isinstance(d["email"], str) else list(d["email"])
        links.append({"threshold": 3, "window_minutes": 60, "cooldown_hours": 6, "circuit_id": "", "isp": "", "signature": "", **d, "email": emails})
    return links


def _status_item(zbx: Any, host: str, iface: str) -> str | None:
    want = re.compile(rf"^Interface {re.escape(iface)}\(.*?\): Operational status$")
    for h in zbx.list_hosts():
        if (h.get("host") or h.get("name")) != host:
            continue
        for it in zbx.list_items(h["hostid"]):
            if want.match(str(it.get("name") or "")) and str(it.get("status", "0")) == "0":
                return it["itemid"]
    return None


def detect(zbx: Any, link: dict[str, Any], now: datetime | None = None) -> dict[str, Any]:
    """返回 {flaps, events, window, item_found, enough_data}。events = [(时间, "down"|"up")]，按时间升序。"""
    now = now or datetime.now(timezone.utc)
    start = now - timedelta(minutes=int(link["window_minutes"]))
    item = _status_item(zbx, link["host"], link["interface"])
    out: dict[str, Any] = {"flaps": 0, "events": [], "start": start, "end": now, "item_found": item is not None, "points": 0}
    if item is None:
        return out
    rows = zbx.get_history(item, history_type=3, time_from=int(start.timestamp()), time_till=int(now.timestamp()), limit=2000)
    pts = sorted((int(r["clock"]), int(float(r["value"]))) for r in rows)
    out["points"] = len(pts)
    prev = None
    for clock, val in pts:
        if prev is not None and prev == UP and val != UP:
            out["flaps"] += 1
            out["events"].append((datetime.fromtimestamp(clock, timezone.utc), "down"))
        elif prev is not None and prev != UP and val == UP:
            out["events"].append((datetime.fromtimestamp(clock, timezone.utc), "up"))
        prev = val
    return out


def compose(link: dict[str, Any], det: dict[str, Any]) -> tuple[str, str]:
    """邮件主题和正文（英文：对外沟通默认英文）。只陈述事实。"""
    w = int(link["window_minutes"])
    subject = f"[{link['circuit_id'] or link['name']}] Repeated link state changes on our side – {det['flaps']} down events in {w} min"
    lines = [f"Hello {link['isp'] or 'NOC'} team,", "",
             f"Our monitoring recorded repeated link state changes on circuit {link['circuit_id'] or '(circuit ID not set)'}"
             f" (our interface {link['interface']} on {link['host']}).", "",
             f"Window: {det['start']:%Y-%m-%d %H:%M} – {det['end']:%Y-%m-%d %H:%M} UTC ({w} minutes)",
             f"Down events: {det['flaps']}", "", "Timeline (UTC):"]
    lines += [f"  {t:%Y-%m-%d %H:%M:%S}  {state}" for t, state in det["events"][:40]]
    lines += ["", "Could you please check the circuit and your side of the link and let us know what you find? "
              "We can provide interface counters and logs from our side if useful.", "", "Thank you,", link["signature"] or "Network Operations"]
    return subject, "\n".join(lines)


def recent_draft(link: dict[str, Any], now: datetime, root: Path | None = None) -> bool:
    cutoff = now - timedelta(hours=int(link["cooldown_hours"]))
    for rec in outbox.list_drafts(root=root):
        if rec.get("kind") == "isp-flap" and (rec.get("meta") or {}).get("link") == link["name"] and rec.get("status") != "discarded":
            try:
                if datetime.fromisoformat(rec["created_at"]) >= cutoff:
                    return True
            except ValueError:
                continue
    return False


def check_links(zbx: Any, links: list[dict[str, Any]], *, now: datetime | None = None, root: Path | None = None) -> list[dict[str, Any]]:
    """逐条链路检测，翻滚就出草稿（冷却期内不重复）。返回每条链路的结果：{link, flaps, action, draft?}。"""
    now = now or datetime.now(timezone.utc)
    results = []
    for link in links:
        try:
            det = detect(zbx, link, now)
        except Exception as exc:  # noqa: BLE001 - 一条链路取数失败不影响其它
            results.append({"link": link["name"], "flaps": 0, "action": f"error: {type(exc).__name__}: {str(exc)[:120]}"})
            continue
        res: dict[str, Any] = {"link": link["name"], "flaps": det["flaps"]}
        if not det["item_found"]:
            res["action"] = "no status item found in Zabbix for this interface"
        elif det["flaps"] < int(link["threshold"]):
            res["action"] = "ok"
        elif recent_draft(link, now, root):
            res["action"] = "flapping; a draft already exists within the cooldown"
        else:
            subject, body = compose(link, det)
            draft = outbox.create(channel="email", kind="isp-flap", subject=subject, body=body, to=link["email"],
                                  meta={"link": link["name"], "flaps": det["flaps"], "circuit_id": link["circuit_id"]}, root=root)
            res.update(action="draft created", draft=draft["id"])
        results.append(res)
    return results


def main() -> None:
    """`python -m netops_ai.inspection.isp_flap [--links isp-links.yaml]`：检测并出草稿；用计划任务每几分钟跑一次。"""
    import argparse

    from netops_ai.llm.factory import env
    from netops_ai.zabbix.client import ZabbixClient

    ap = argparse.ArgumentParser(description="外网链路翻滚检测，生成给运营商的邮件草稿（不发送）")
    ap.add_argument("--links", default="isp-links.yaml")
    a = ap.parse_args()
    e = env()
    links = load_links(Path(a.links))
    with ZabbixClient(url=e.get("ZABBIX_URL", ""), user=e.get("ZABBIX_USER", ""), password=e.get("ZABBIX_PASSWORD", "")) as zbx:
        for r in check_links(zbx, links):
            print(r)


if __name__ == "__main__":
    main()
