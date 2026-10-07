"""巡检的处置建议，巡检侧唯一一次模型调用：检测器只说「有异常」，值班的人要的是哪几条今晚要管。

跟告警研判一个做法：一次调用不给工具、strict schema、引文逐字回指 findings，并把拓扑一起喂进去。
"""

from __future__ import annotations

import json
from pathlib import Path

from netops_ai.analysis.verify import summarize, verify_evidence
from netops_ai.llm.client import LLMClient
from netops_ai.llm.factory import env

REPO_ROOT = Path(__file__).resolve().parents[2]
ADVICE_PATH = REPO_ROOT / "records" / "inspection-advice.json"

#: 喂多少条。**不是省钱，是省注意力**：56 条原文里 49 条同质，
#: 全灌进去模型会把篇幅摊平，真正要紧的那一条反而被淹掉。
MAX_FINDINGS = 40


def compact_status(status: dict | None) -> str:
    """状态巡检里 bad/warn 的检查，压成可逐字引用的文本；ok/skip 不进（没事的不占注意力）。

    `item_key` 一栏用检查名（interfaces/ospf/bgp/errors/reachable），host 用设备名，跟趋势发现同一套回指。
    """
    rows = []
    for dev in (status or {}).get("devices") or []:
        for c in dev.get("checks") or []:
            if c.get("status") not in ("bad", "warn"):
                continue
            evidence = " / ".join(c.get("evidence") or [])
            rows.append(
                f"[S{len(rows)}] {dev.get('name', '')} · 状态检查（{c.get('check', '')}） | {c['status']} | {c.get('summary', '')}"
                + (f" | 设备输出：{evidence}" if evidence else "")
            )
    return "\n".join(rows)


def compact(payload: dict) -> str:
    """把 findings 压成可逐字引用的文本。直接用检测器写好的 `reason`，数字不重排。"""
    lines = [
        f"扫了 {payload.get('scanned_hosts', 0)} 台主机、{payload.get('scanned_items', 0)} 个数值监控项，"
        f"其中 {payload.get('items_with_data', 0)} 个有数据。",
        "",
    ]
    for i, row in enumerate((payload.get("findings") or [])[:MAX_FINDINGS]):
        f = row.get("finding") or {}
        lines.append(
            f"[{i}] {row.get('host_name', '')} · {row.get('item_name', '')}（{row.get('item_key', '')}）"
            f" | {f.get('kind', '')}"
            + (f" / {f.get('health_impact')}" if f.get("health_impact") else "")
            + f" | {f.get('reason', '')}"
        )
    return "\n".join(lines)


def topology_hint() -> str:
    """拓扑摘要。取不到就返回空串——**巡检不能因为 NetBox 挂了就不出报告**。"""
    try:
        from netops_ai import netbox_cli
    except ImportError:  # 开源版没有台账与影响面摘要
        return ""
    try:
        data = netbox_cli.cmd_topology(object())
    except Exception as exc:  # noqa: BLE001 - 拓扑是加分项，不是前置条件
        return f"（拓扑取不到：{type(exc).__name__}: {exc}）"
    rows = "\n".join(
        f"- {d['name']}（{d['role_cn']}）上联 {d['uplinks'] or '无'}" for d in data.get("devices") or []
    )
    single = "、".join(data.get("single_homed") or []) or "无"
    return f"{rows}\n只有一条上联（那条断了整台失联）：{single}"


ADVICE_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "inspection_advice",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "summary": {
                    "type": "string",
                    "description": "一句话总体判断。要到可处置粒度：今晚要不要管、管哪台。不要复述发现条数。",
                },
                "needs_attention": {
                    "type": "array",
                    "description": "真的要人看的。**宁可少，不要凑数**——把 40 条都搬过来等于没筛。",
                    "items": {
                        "type": "object",
                        "properties": {
                            "host": {"type": "string"},
                            "item_key": {"type": "string"},
                            "severity": {"type": "string", "enum": ["high", "medium"]},
                            "why": {"type": "string", "description": "为什么这条要紧，说清楚影响面。"},
                            "suggestion": {
                                "type": "string",
                                "description": "具体动作：去哪台、查什么、看什么。**只读动作**，不要建议改配置。",
                            },
                            "evidence": {
                                "type": "string",
                                "description": "从巡检原文里**逐字抄**一句支撑它的话，不许改写、不许自己算数字。",
                            },
                        },
                        "required": ["host", "item_key", "severity", "why", "suggestion", "evidence"],
                        "additionalProperties": False,
                    },
                },
                "ignorable": {
                    "type": "array",
                    "description": "明确可以不管的，**必须说清为什么**。说不清就放 cannot_tell，不要放这里。",
                    "items": {
                        "type": "object",
                        "properties": {
                            "host": {"type": "string"},
                            "item_key": {"type": "string"},
                            "why": {"type": "string"},
                        },
                        "required": ["host", "item_key", "why"],
                        "additionalProperties": False,
                    },
                },
                "cannot_tell": {
                    "type": "array",
                    "description": "判不出的。**这一栏空着才是可疑的**，49 条同质发现不可能条条都判得出。",
                    "items": {
                        "type": "object",
                        "properties": {
                            "host": {"type": "string"},
                            "item_key": {"type": "string"},
                            "what_data_would_help": {"type": "string"},
                        },
                        "required": ["host", "item_key", "what_data_would_help"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["summary", "needs_attention", "ignorable", "cannot_tell"],
            "additionalProperties": False,
        },
    },
}

PROMPT = """你在看一份只读网络巡检的结果。这些项**都还没触发告警**，巡检找的是
「还没越线但值得看」的趋势，所以不要把它们当成故障。

你的任务是把它们分成三堆：今晚要管的、可以不管的、判不出的。

规矩：
- `evidence` 必须从下面的巡检原文里**逐字抄**，一个字都不许改，数字不许自己重算。
  抄不出原话的条目就不要放进 needs_attention。
- 建议只能是**只读动作**（去哪台、查什么、看哪个时间段），不许建议改配置。
- 单上联的设备同样一条抖动比双上联的严重，判严重程度时用下面的拓扑。
- **宁可 needs_attention 少几条，不要凑数。** cannot_tell 空着反而可疑。

## 拓扑

{topology}

## 巡检原文

{findings}

## 状态巡检（登设备只读检查，规则判定；只列 bad/warn）

这部分是**当前就不对**的状态，不是趋势：bad 通常意味着已经有东西坏了，优先级高于趋势。
没有列出的设备/检查就是正常。`host` 填设备名，`item_key` 填括号里的检查名。

{status}
"""


def advise(payload: dict, *, topology: str | None = None, status: dict | None = None) -> dict:
    """出一份带处置建议的巡检报告。**引文核不过会被标出来，不静默通过。**

    `status` 是最近一份状态巡检快照；传了就把 bad/warn 的检查一起喂进去，引文也能回指到设备输出。
    """
    status_text = compact_status(status)
    findings_text = compact(payload)
    prompt = PROMPT.format(
        topology=topology if topology is not None else topology_hint(),
        findings=findings_text,
        status=status_text or "（没有状态巡检结果，或全部正常）",
    )
    cfg = env()
    # **超时要比告警侧长。** 告警侧一次只看一条告警，这里是几十条发现一次过，
    # 输入 6k 上下、还要它分三堆，实测默认 60 秒不够（qwen3.8-flash 带 reasoning）。
    client = LLMClient(
        base_url=cfg.get("LLM_BASE_URL", ""), api_key=cfg.get("LLM_API_KEY", ""),
        model=cfg.get("LLM_MODEL", ""), timeout=300.0,
    )
    response = client.complete(
        [
            {"role": "system", "content": "你是网络巡检助手，只输出符合 schema 的 JSON。"},
            {"role": "user", "content": prompt},
        ],
        response_format=ADVICE_SCHEMA,
        max_completion_tokens=8000,
        temperature=0.2,
    )
    parsed = response.parsed or {}
    # 证据池就是巡检原文。`source_from` 用 zabbix：这些数就是从 Zabbix 历史里算出来的，
    # 不是给它硬塞一个池子名。
    verdicts = verify_evidence(
        [{"claim": a.get("why", ""), "source": a.get("evidence", ""), "source_from": "zabbix"}
         for a in parsed.get("needs_attention") or []],
        findings_text + "\n" + status_text,
        None,
    )
    for item, v in zip(parsed.get("needs_attention") or [], verdicts):
        item["evidence_verified"] = v.verified
        if not v.verified:
            item["evidence_problem"] = v.reason
    parsed["verification"] = summarize(verdicts)
    parsed["findings_fed"] = min(len(payload.get("findings") or []), MAX_FINDINGS)
    parsed["findings_total"] = len(payload.get("findings") or [])
    parsed["status_fed"] = status_text.count("\n") + 1 if status_text else 0
    return parsed


def save(advice: dict) -> Path:
    ADVICE_PATH.parent.mkdir(exist_ok=True)
    ADVICE_PATH.write_text(json.dumps(advice, ensure_ascii=False, indent=2), encoding="utf-8")
    return ADVICE_PATH


def load() -> dict | None:
    if not ADVICE_PATH.exists():
        return None
    return json.loads(ADVICE_PATH.read_text(encoding="utf-8"))
