"""给管理层的总结：把流量报告的**事实清单**交给模型写成几句话的摘要，连 PDF 一起生成一份「待确认」的草稿（见 `outbox`）。

只做草稿，**不发送**：人在草稿箱里看过摘要和 PDF、点确认才会发出去（邮件走白名单收件人，飞书走 webhook）。

摘要的写法被严格限定：只能依据传入的事实清单，不能编数字、不能推断原因（原因需要排查，不是流量报告能给的）；
模型不可用时报告照出、只是没有摘要段，草稿正文退回事实清单本身。
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from netops_ai import outbox
from netops_ai.inspection import traffic_report as TR

SYSTEM = """你为网络运维负责人写给管理层的流量摘要。只依据下面给出的「事实清单」写，规则：
- 3 到 5 句话，面向非技术读者，先说整体结论（平稳 / 有需要关注的地方），再点出最值得注意的 1~2 件事。
- 只能用清单里出现的数字和接口名；不要编造数字，不要推断原因，也不要给操作建议——流量报告只能说明「发生了什么」。
- 清单里没有高利用率接口、也没有明显环比变化时，如实说「整体平稳」。
- 用简体中文，不用 markdown，不用项目符号。"""


def make_narrator(llm: Any) -> Callable[[str], str]:
    """`narrate(事实清单) -> 摘要文字`。`llm` 要有 `complete(messages, ...)`，返回对象带 `content`。"""
    def narrate(facts: str) -> str:
        resp = llm.complete([{"role": "system", "content": SYSTEM}, {"role": "user", "content": facts}],
                            temperature=0.2, max_completion_tokens=600)
        return str(getattr(resp, "content", "") or "").strip()
    return narrate


def create_draft(kind: str, zbx: Any, out_dir: Path, *, llm: Any = None, channel: str = "email", to: list[str] | None = None,
                 hosts: list[str] | None = None, now: datetime | None = None, created_by: str = "system",
                 root: Path | None = None) -> dict[str, Any]:
    """生成 PDF + 摘要，写一份待确认的草稿并返回（含 `draft` 和 `report`）。不发送。"""
    res = TR.generate(kind, zbx, out_dir, now=now, hosts=hosts, narrate=make_narrator(llm) if llm is not None else None)
    title = f"链路流量{TR.KIND_ZH[kind]}（{res['start']:%Y-%m-%d}）"
    body = (res["narrative"] or res["facts"]) + "\n\n" + "完整数据见附件 PDF。" + ("" if res["narrative"] else "（本次未生成 AI 摘要，以上为统计事实。）")
    draft = outbox.create(channel=channel, kind=f"traffic-{kind}", subject=title, body=body, to=to or [],
                          attachments=[str(res["path"])], created_by=created_by,
                          meta={"period": [res["start"].isoformat(), res["end"].isoformat()], "ai_summary": bool(res["narrative"])}, root=root)
    return {"draft": draft, "report": res}
