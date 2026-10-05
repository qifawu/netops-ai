# Examples / 示例

`alerts/` holds three **synthetic** alert records (hand-written; no real network, host or address is involved). They have the same shape as the `records/alert-*.json` files the pipeline writes, so `tools/demo_replay.py` can render the real Feishu card from them, and you can copy them into `records/` to see them on the dashboard.

| File | Shows |
|---|---|
| `01-interface-admin-down.json` | A clean case: a root cause with quoted evidence, every ruled-out hypothesis carries counter-evidence |
| `02-ospf-neighbor-timeout.json` | An honest "can't tell": two hypotheses cannot be separated from one device's data, so the record names them, says what data would help and the exact commands to get it, and the card is orange instead of green |
| `03-fabricated-evidence.json` | A deliberately flawed record, used by the tests as a negative case |

```bash
python tools/demo_replay.py             # all three
python tools/demo_replay.py --card      # plus the Feishu card JSON
```

`alerts/` 下是三条**合成**的告警记录（手写，不涉及任何真实网络、主机或地址），形状与流水线落盘的 `records/alert-*.json` 一致，所以 `tools/demo_replay.py` 能用它们渲染真实的飞书卡片，也可以复制进 `records/` 在看板里看：01 是干净样例；02 是老实承认「判不出」（卡片是橙色）；03 是故意写坏的，测试里当反例用。
