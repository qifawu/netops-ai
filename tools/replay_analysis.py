"""拿一条已落盘的告警记录当输入，把研判那一步重放 N 次，数假设清单的标注分布。

用来量「schema / 提示改了之后模型标注变没变」：不造新故障、不登设备，只花研判那一次 LLM 调用。
 python tools/replay_analysis.py 一条真实告警记录 -n 6
输出：每次的 6 个方向状态、confidence、business_rule_violations 条数，最后给汇总。

**起这个工具只能重放 09-24 之前的老记录。** 那天把独立的研判调用砍了
（结论改成在取证轨迹末尾出），`analyze` 这条路上线上已经不走了。
重放新记录要连工具回显一起回放整条循环，那是另一个工具，还没写。
"""
from __future__ import annotations

import argparse
import collections
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from netops_ai.analysis.analyzer import analyze  # noqa: E402
from netops_ai.analysis.schema import check_business_rules  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("record")
    ap.add_argument("-n", type=int, default=5)
    ap.add_argument("--variant", default="", help="临时改 status 描述/枚举顺序的实验变体：A / B / AB（只在本进程内生效）")
    a = ap.parse_args()
    if a.variant:
        from netops_ai.analysis import schema as S
        st = S._HYPOTHESIS_ITEM_SCHEMA["properties"]["status"]
        if "A" in a.variant:
            st["description"] = (
                "先回答：这个方向是不是本次告警的原因？"
                "是（有直接证据）→ 有证据支持——**根因所在的那个方向必须填这个，不能填已排除**；"
                "不是，并且有一条数据正面否定它 → 已排除（必须填 counter_evidence）；"
                "既没有支持也没有正面否定 → 暂时无法判断。「没看到证据」不算反证。"
            )
        if "C" in a.variant:
            st["enum"] = [S.STATUS_SUPPORTED, "被反证排除", S.STATUS_CANNOT_DETERMINE]
            st["description"] = st["description"].replace("已排除", "被反证排除")
        if "R" in a.variant:  # 强制用「内联」schema（不走 $ref）
            from netops_ai.llm.client import LLMClient
            LLMClient.analysis_schema_use_refs = lambda self: False
        if "D" in a.variant:  # status 用英文枚举（模型端），出来后靠 normalize_enum 转中文
            st["enum"] = ["supported", "ruled_out", "cannot_determine"]
            st["description"] = ("supported=现有证据支持这个方向；ruled_out=现有证据里有正面反证足以排除这个方向（必须填 counter_evidence）；"
                                 "cannot_determine=没有支持也没有排除的证据——「没看到证据」不是反证，拿不出正面反证就填 cannot_determine")
        if "H" in a.variant:  # headline 加 maxLength
            S.analysis_json_schema  # noqa
            import copy
            orig = S.analysis_json_schema
            def patched(**kw):
                sch = orig(**kw)
                def walk(o):
                    if isinstance(o, dict):
                        if o.get("type") == "object" and "headline" in (o.get("properties") or {}):
                            o["properties"]["headline"]["maxLength"] = 30
                            o["properties"]["headline"]["description"] += " **写成一个完整的短句，不带括号、不带英文注释、不带设备名；写不下就少写细节，不要写半句。**正例：「人为 shutdown 导致接口中断」「对端重启导致邻居断开」。"
                        for v in o.values(): walk(v)
                    elif isinstance(o, list):
                        for v in o: walk(v)
                walk(sch); return sch
            S.analysis_json_schema = patched
            import netops_ai.analysis.analyzer as AN
            AN.analysis_json_schema = patched
        if "B" in a.variant:
            st["enum"] = [S.STATUS_SUPPORTED, S.STATUS_CANNOT_DETERMINE, S.STATUS_RULED_OUT]
    rec = json.loads(Path(a.record).read_text(encoding="utf-8"))
    zt, dt = rec.get("zabbix_context_text") or "", rec.get("device_context_text")
    fault = float(rec.get("alert_clock") or 0) or None
    dist = collections.Counter()
    supported_ok = 0
    viol_total = 0
    for i in range(a.n):
        run = analyze(zt, dt, fault_time_epoch=fault)
        p = run.parsed or {}
        chk = p.get("hypothesis_checklist") or {}
        st = {k: (v or {}).get("status") for k, v in chk.items()}
        try:
            viol = check_business_rules(p)
        except TypeError:
            viol = []
        viol_total += len(viol)
        la = st.get("local_action")
        supported_ok += la in ("有证据支持", "supported")
        dist.update(f"{k}={v}" for k, v in st.items())
        hl = p.get("headline", "")
        print(f"#{i + 1} 标题{len(hl)}字「{hl}」 local_action={la} conf={p.get('confidence')} 违规={len(viol)} 状态={list(st.values())}", flush=True)
    print(f"\nlocal_action 标成「有证据支持」：{supported_ok}/{a.n}；平均违规 {viol_total / a.n:.1f} 条")


if __name__ == "__main__":
    main()
