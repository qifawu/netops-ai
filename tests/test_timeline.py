from __future__ import annotations

from datetime import datetime, timezone

from netops_ai.analysis.timeline import build_timeline_text, infer_fault_epoch


def _epoch(value: str) -> float:
    return datetime.fromisoformat(value).replace(tzinfo=timezone.utc).timestamp()


def test_infer_fault_epoch_from_zabbix_clock():
    text = '{"clock": "1789694011", "name": "Cisco IOS: Unavailable by ICMP ping"}'
    assert infer_fault_epoch(text) == 1789694011.0


def test_device_snapshot_carries_collection_time_and_delta():
    fault = _epoch("2026-09-18 01:13:31")
    collected = _epoch("2026-09-18 03:15:42")
    text = build_timeline_text(
        '01:11:31 -> 0\n{"clock": "1789694011"}',
        "show ip interface brief\nGi0/0 administratively down",
        fault_time_epoch=fault,
        device_collected_epoch=collected,
    )
    assert "故障时刻：2026-09-18 01:13:31" in text
    assert "【取数时刻 03:15:42 -- 距故障 2 小时 2 分】" in text
    assert "这是取数时刻的设备快照" in text


def test_device_show_logging_lines_enter_fault_window():
    fault = _epoch("2026-09-18 01:13:31")
    device = """### `show logging`

```
*Sep 18 01:11:03.807: %LINK-5-CHANGED: Interface GigabitEthernet0/0, changed state to administratively down
```
"""
    text = build_timeline_text("", device, fault_time_epoch=fault, device_collected_epoch=fault + 3600)
    assert "01:11:03  [device]" in text
    assert "administratively down" in text
