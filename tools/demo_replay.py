#!/usr/bin/env python
"""离线回放：拿落盘的告警记录，把「模型说完之后」那一半流水线原样重跑一遍——**不连 Zabbix、不连设备、不调模型**。

    python tools/demo_replay.py                      # 回放 examples/alerts/ 里三条合成样例
    python tools/demo_replay.py path/to/alert.json   # 回放你自己的记录（netops-ai 落盘的 alert-*.json 同一形状）
    python tools/demo_replay.py --card               # 同时把飞书卡片 JSON 打出来

每条记录依次做三件事，用的都是线上同一份代码：

1. **证据逐字核对**（`netops_ai.analysis.verify`）：模型声称的每条证据，必须能在当时取到的 Zabbix / 设备原文里找到；
   找不到的标 `fabricated`（编造）。这是「可信的 AI 诊断」的核心——结论可以是模型写的，支撑它的那句原话不许是。
2. **业务规则校验**（`netops_ai.analysis.schema.check_business_rules`）：结构化输出管不到的跨字段矛盾，
   比如置信度写 high、清单里却还有没排除的方向。
3. **飞书卡片渲染**（`netops_ai.feishu.card.build_card`）：核对不过的证据和规则违反会出现在卡片上。

样例是**手写的合成数据**（不是真实告警），第三条故意带一条编造的证据，用来演示校验会抓出它。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from netops_ai.analysis.schema import check_business_rules  # noqa: E402
from netops_ai.analysis.verify import summarize, verify_evidence  # noqa: E402
from netops_ai.feishu.card import build_card  # noqa: E402

# 私有仓库里示例放在 open/examples/alerts/（导出时 overlay 到根）；公开版的示例就在 examples/alerts/
DEFAULT_DIR = next((p for p in (REPO_ROOT / "open" / "examples" / "alerts", REPO_ROOT / "examples" / "alerts") if p.is_dir()),
                   REPO_ROOT / "examples" / "alerts")
GRADE_MARK = {"verbatim": "✓ 逐字", "cross_source": "✓ 跨来源", "reformatted": "~ 重排版", "fabricated": "✗ 编造"}


def replay(record: dict) -> dict:
    """回放一条记录，返回结果（不打印）。纯函数，测试直接调。"""
    analysis = record.get("analysis_parsed") or {}
    zabbix_text = record.get("zabbix_context_text") or ""
    device_text = record.get("device_context_text") or ""
    verdicts = verify_evidence(analysis.get("evidence") or [], zabbix_text, device_text or None)
    violations = check_business_rules(analysis, f"{zabbix_text}\n\n{device_text}")
    payload = record.get("webhook_payload") or {}
    eventid = str(record.get("eventid") or payload.get("eventid") or "")
    card = build_card({
        "eventids": [eventid],
        "analysis": analysis,
        "alert_names": {eventid: payload.get("name", "")},
        "host": payload.get("host", ""),
        "violations": violations,
        "evidence_verification": {
            "summary": summarize(verdicts),
            "details": [
                {"index": v.index, "verified": v.verified, "grade": v.grade, "source_from": v.source_from, "reason": v.reason}
                for v in verdicts
            ],
        },
    })
    return {"eventid": eventid, "verdicts": verdicts, "violations": violations, "card": card, "analysis": analysis, "payload": payload}


def _print(result: dict, *, show_card: bool) -> None:
    payload, analysis = result["payload"], result["analysis"]
    print(f"\n=== 告警 {result['eventid']} · {payload.get('host', '')} · {payload.get('name', '')}")
    print(f"根因：{analysis.get('root_cause', '（无）')}")
    print(f"置信度：{analysis.get('confidence', '?')}")
    print("证据核对：")
    for v in result["verdicts"]:
        print(f"  [{GRADE_MARK.get(v.grade, v.grade)}] ({v.source_from}) {v.claim}")
        if not v.verified:
            print(f"      ↳ 原文里找不到：{v.source[:80]}")
    if result["violations"]:
        print("业务规则违反：")
        for item in result["violations"]:
            print(f"  - {item}")
    else:
        print("业务规则：无违反")
    header = (result["card"].get("header") or {})
    title = (header.get("title") or {}).get("content", "")
    print(f"飞书卡片：标题「{title}」，配色 {header.get('template', '')}")
    if show_card:
        print(json.dumps(result["card"], ensure_ascii=False, indent=2))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("records", nargs="*", help="告警记录 json；不给就回放 examples/alerts/ 下全部")
    ap.add_argument("--card", action="store_true", help="同时打印飞书卡片 JSON")
    args = ap.parse_args(argv)
    paths = [Path(p) for p in args.records] or sorted(DEFAULT_DIR.glob("*.json"))
    if not paths:
        print(f"没有可回放的记录（默认目录 {DEFAULT_DIR} 为空）")
        return 2
    total = fabricated = violated = 0
    for path in paths:
        result = replay(json.loads(path.read_text(encoding="utf-8")))
        _print(result, show_card=args.card)
        total += len(result["verdicts"])
        fabricated += sum(1 for v in result["verdicts"] if v.grade == "fabricated")
        violated += 1 if result["violations"] else 0
    print(f"\n汇总：{len(paths)} 条记录，{total} 条证据，其中 {fabricated} 条被判编造；{violated} 条记录有业务规则违反。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
