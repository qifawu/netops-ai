# Examples

**English** | [简体中文](README.zh-CN.md)

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
