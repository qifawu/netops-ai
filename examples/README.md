# Examples / 示例

`alerts/` holds three **synthetic** alert records (hand-written; no real network, host or address is involved). They have the same shape as the `records/alert-*.json` files the pipeline writes, so `tools/demo_replay.py` exercises the real verification, business-rule and card code on them.

| File | Shows |
|---|---|
| `01-interface-admin-down.json` | A clean case: every piece of evidence is quoted verbatim from the raw data, every ruled-out hypothesis carries counter-evidence, no rule is violated |
| `02-ospf-neighbor-timeout.json` | An honest "can't tell": two hypotheses cannot be separated from one device's data, so the record names them, says what data would help and the exact commands to get it, and the card is orange instead of green |
| `03-fabricated-evidence.json` | A deliberately bad record: one evidence quote does not exist in the device output (graded `fabricated`), and confidence is `high` while hypotheses remain unresolved (business-rule violation) |

```bash
python tools/demo_replay.py             # all three
python tools/demo_replay.py --card      # plus the Feishu card JSON
```

`alerts/` 下是三条**合成**的告警记录（手写，不涉及任何真实网络、主机或地址），形状与流水线落盘的 `records/alert-*.json` 一致，所以 `tools/demo_replay.py` 跑的是真实的证据核对、业务规则和卡片渲染代码：01 是干净样例；02 是老实承认「判不出」（卡片是橙色）；03 是故意写坏的——一条编造的证据 + 置信度 high 却还有没排除的方向。
