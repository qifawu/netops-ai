#!/usr/bin/env python3
"""数一数 `check_business_rules` 的每条规则在真实记录上触发过几次。

**为什么要这个**：规则只增不减，加的时候都觉得有理，但有没有真拦住过东西，
凭印象说不清。没触发过的那几条，要么是想出来的，要么是采样没覆盖到——
这两件事得分开，而分开的唯一办法是看数。

**用法**：

 .venv/bin/python tools/rule_hit_report.py # 数 records/
 .venv/bin/python tools/rule_hit_report.py --dir other/ # 数别的目录
 .venv/bin/python tools/rule_hit_report.py --since

**读数的时候必须先看两条警告**，脚本自己会打：

1. **schema 演进会造假阳。** 拿今天的规则去卡昨天的记录，老记录会因为缺少
 当时还不存在的字段被判违规。实测：`how_to_get_it` 那条规则在
 198 条老记录上"触发" 40 次，而这 198 条**一条都没有这个字段**，全是假阳。
 脚本会按天分桶，并统计每个字段的出现情况，让这种假阳自己露出来。
2. **0 次不等于「从来不触发」，只等于「这批没有」。** 那天，
 `标连带却指不出被谁引起` 在 Mac 的 198 条记录里是 0 次，而同一天
 Windows 上一条真实告警恰好就触发了它——pipeline 跑在 Windows，
 Mac 这边根本没有当天的记录。**删规则前必须两台的记录都数过。**
"""

from __future__ import annotations

import argparse
import collections
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from netops_ai.analysis.schema import check_business_rules  # noqa: E402

#: 规则的识别指纹：每条消息里最有辨识度的一段。
#: **加了新规则就往这里补一行**，否则它会掉进"没归类上"。
RULE_FINGERPRINTS = [
    ("已排除但没给反证", "却没给出任何反证"),
    ("拿「没发现记录」当反证", "只是说没发现相关记录"),
    ("拿当前快照反推故障时刻", "是当前才查到的状态"),
    ("反证里写了否定词", "这类说法是在说没找到证据"),
    ("故障已结束却无故障窗口证据", "没有一条来自故障窗口"),
    ("多方向没排除却没写候选", "既然这些方向还分不出来"),
    ("写了候选但方向已排干净", "候选根因里写了分不清的方向"),
    ("候选没写怎么拿/谁去拿", "没有写清后续证据该怎么拿"),
    ("标连带却指不出被谁引起", "却没指出是被哪一条引起的"),
    ("说被某条引起但那条不在本次", "不在本次候选告警里"),
    ("分组跟本次告警对不上", "故障分组必须刚好覆盖"),
    ("关联告警放了本次的", "本来就是这次要分析的"),
    ("关联告警在原文里找不到", "在取证拿到的原文里找不到"),
]

#: 判断"这条记录写的时候 schema 有没有这个字段"用的探针。
#: 一个字段在某一天的记录里**一次都没出现**，那天针对它的规则就全是假阳。
FIELD_PROBES = {
    "evidence[].time_relevance": lambda p: any(
        e.get("time_relevance") for e in (p.get("evidence") or [])),
    "候选[].how_to_get_it": lambda p: any(
        c.get("how_to_get_it") for c in (p.get("undistinguishable_candidates") or [])),
    "候选[].who": lambda p: any(
        c.get("who") for c in (p.get("undistinguishable_candidates") or [])),
}


def classify(message: str) -> str:
    for name, fingerprint in RULE_FINGERPRINTS:
        if fingerprint in message:
            return name
    return ""


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="业务校验规则命中统计")
    ap.add_argument("--dir", default="records", help="记录目录，默认 records/")
    ap.add_argument("--since", default="", help="只数这天起的记录，如 2026-09-24")
    args = ap.parse_args(argv)

    files = sorted(pathlib.Path(args.dir).glob("alert-*.json"))
    if not files:
        print(f"{args.dir} 下没有 alert-*.json")
        return 1

    by_day: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    day_total: collections.Counter = collections.Counter()
    field_days: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    unmatched: collections.Counter = collections.Counter()

    for path in files:
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError) as exc:
            print(f"读不了 {path.name}：{exc}")
            continue
        parsed = record.get("analysis_parsed")
        if not isinstance(parsed, dict) or not parsed:
            continue
        day = str(record.get("started_at") or "?")[:10]
        if args.since and day < args.since:
            continue
        day_total[day] += 1
        for field, probe in FIELD_PROBES.items():
            if probe(parsed):
                field_days[field][day] += 1
        for message in check_business_rules(parsed, record.get("zabbix_context_text") or None):
            name = classify(message)
            if name:
                by_day[day][name] += 1
            else:
                unmatched[message[:60]] += 1

    days = sorted(day_total)
    if not days:
        print("没有带结论的记录")
        return 1

    print(f"记录目录 {args.dir}：{len(files)} 个文件，其中有结论的 {sum(day_total.values())} 条\n")
    width = max(len(name) for name, _ in RULE_FINGERPRINTS) + 2
    print(" " * width + "".join(f"{d[5:]:>8s}" for d in days) + "     合计")
    print("-" * (width + 8 * len(days) + 9))
    for name, _ in RULE_FINGERPRINTS:
        row = [by_day[d].get(name, 0) for d in days]
        total = sum(row)
        mark = "   ← 这批一次没触发" if total == 0 else ""
        print(f"{name:{width}s}" + "".join(f"{n:8d}" for n in row) + f"{total:9d}{mark}")

    print("\n字段出现情况（某天是 0 就说明那天针对这个字段的规则全是假阳）：")
    for field in FIELD_PROBES:
        row = "  ".join(f"{d[5:]}={field_days[field].get(d, 0)}/{day_total[d]}" for d in days)
        print(f"  {field:28s} {row}")

    if unmatched:
        print("\n没归类上的消息（规则加了但 RULE_FINGERPRINTS 没补）：")
        for message, count in unmatched.most_common(10):
            print(f"  {count:4d}  {message}")

    print("\n读数之前先看这两条：")
    print("  1. 某条规则在某天触发很多，先去上面的字段表确认那天有没有这个字段——没有就是假阳。")
    print("  2. 合计 0 次不等于「从来不触发」，只等于「这批记录没有」。"
          "pipeline 跑在 Windows，Mac 这边的记录不全，删规则前两台都要数。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
