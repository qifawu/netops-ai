# Examples / 示例

`alerts/` holds six **synthetic** alert records (hand-written; no real network, host or address is involved). They have the same shape as the `records/alert-*.json` files the pipeline writes.

| File | Scenario | Outcome |
|---|---|---|
| `01-interface-admin-down.json` | Interface shut down by hand | High confidence, root cause named, every ruled-out hypothesis carries counter-evidence |
| `02-ospf-neighbor-timeout.json` | OSPF neighbor lost | Honest "can't tell": two candidates, what data would separate them, and the exact commands; the agent also tried `clear ip ospf process` and was refused |
| `03-bgp-neighbor-admin-shutdown.json` | BGP neighbor administratively shut down | High confidence; the agent's `clear ip bgp` attempt is refused by the whitelist |
| `04-device-reload.json` | Device restarted | High confidence: operator `reload`, not power loss or a crash |
| `05-high-cpu.json` | CPU above 90 % | Medium: traffic is being punted to the CPU, loop vs. noisy host cannot be told apart yet |
| `06-interface-errors.json` | CRC errors and late collisions | Medium: half-duplex 100 Mb/s, suspected duplex mismatch, peer port still to be checked |

```bash
python tools/demo_replay.py             # print every example and render its Feishu card
python tools/demo_replay.py --card      # plus the Feishu card JSON
python tools/seed_demo.py               # copy them into records/ so the dashboard shows them
```

`seed_demo.py` shifts the timestamps to the last few hours and never overwrites existing files.

`alerts/` 下是六条**合成**的告警记录（手写，不涉及任何真实网络、主机或地址），形状与流水线落盘的 `records/alert-*.json` 一致：01 接口被人为 shutdown（高置信）；02 OSPF 邻居丢失，老实说「判不出」并写明还差什么、用哪条命令能判；03 BGP 邻居被管理性关闭，agent 想 `clear ip bgp` 被白名单拒绝；04 设备被 reload 重启；05 CPU 高，流量被送到 CPU，环路还是单台主机分不出；06 接口 CRC 错误加 late collision，疑似双工不匹配，对端口待查。`python tools/demo_replay.py` 打印并渲染飞书卡片；`python tools/seed_demo.py` 把它们灌进 `records/`（时间戳平移到最近几小时，不覆盖已有文件），看板就有数据了。
