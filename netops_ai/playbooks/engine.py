"""YAML SOP 剧本的加载和匹配。不执行剧本：SOP 是 agent 手上的只读工具 `sop_lookup`。

原来的执行器 删了，要看：`git show 2fc22c8:netops_ai/playbooks/engine.py`。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

AI_GOTO = "__ai__"
END_GOTO = "__end__"
DEFAULT_MAX_MAIN_STEPS = 5
DEFAULT_MAX_TOKENS = 1500
ALLOWED_PROVENANCE_SOURCES = frozenset({"human", "ai_proposed_human_approved"})
#: 告警名里没有接口时，SOP 命令里的接口位置用它占着，命令本身照样摆出来（验证 外部 agent 反馈第 4 条：
#: Trap 告警名 `Trap: linkDown received on A1` 不带接口，原来整条 command 被拿掉，只剩 `tool=device_show`）。
INTERFACE_PLACEHOLDER = "<接口>"
#: 告警上下文里没有、要 agent 从前一步输出里取的对端地址（bgp-session 原来写死了 `192.0.2.51`，
#: 那还是 V2 的管理口，不是 BGP 邻居地址）。SOP 里原样写这个占位符，why 里说明从哪一步取。
PEER_ADDRESS_PLACEHOLDER = "<对端地址>"
#: 运行时认步骤时，这些占位符各匹配任意一个不含空白的词；lint 查白名单时换成代表值。
AGENT_FILLED_PLACEHOLDERS = (INTERFACE_PLACEHOLDER, PEER_ADDRESS_PLACEHOLDER)

#: SOP 步骤里能写的工具名。审批 AI 提议的步骤时拿它校验（`approve.py`）。
#: `ospf-adjacency` 的 `check_peer_side` 就是这么漏的：O3 把
#: `topology_neighbors` 只注册给了对话 agent，剧本引擎这边没接，
#: 那一步每次都返回 `unknown SOP action tool` 然后走默认分支——
#: 不报错、不留痕，看上去一直在跑。跟"同一个工具两个名字"是同一类病。
SUPPORTED_ACTION_TOOLS = (
    "device",
    "zabbix_history",
    "zabbix_reachability",
    "topology_neighbors",
    # SOP 可以直接写 agent 的真实工具名。参数名必须是那个工具的真实参数名，
    # `playbooks/lint.py` 的 `bad-action-param` 会逐个核对。
    "zbx_syslog",
    "zbx_items",
)


def load_playbooks(playbook_dir: Path) -> list[dict]:
    if not playbook_dir.exists():
        return []
    out = []
    for path in sorted(playbook_dir.glob("*.yaml")):
        if path.name.startswith("_"):
            continue
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        data["_path"] = str(path)
        data.setdefault("name", path.stem)
        data.setdefault("limits", {})
        data["limits"].setdefault("max_main_steps", DEFAULT_MAX_MAIN_STEPS)
        data["limits"].setdefault("max_tokens", DEFAULT_MAX_TOKENS)
        for step in data.get("steps") or []:
            step.setdefault("main", True)
        out.append(data)
    return out


@dataclass
class PlaybookMatch:
    """一条命中的 SOP，带命中理由和特异性分数。同时命中多条时靠它排序，而不是按文件名。"""

    playbook: dict
    score: int
    reasons: list[str]

    @property
    def name(self) -> str:
        return str(self.playbook.get("name", ""))


def match_playbooks(playbooks: list[dict], alert: dict) -> list[PlaybookMatch]:
    """返回全部命中的 SOP，按特异性从高到低（触发器关键词越长越具体，tag 记固定分）。
    同分的原样全返回，交给 agent 带着上下文自己挑。
    """
    scored: list[PlaybookMatch] = []
    for pb in playbooks:
        ok, score, reasons = _match_with_score(pb.get("match") or {}, alert)
        if ok:
            scored.append(PlaybookMatch(playbook=pb, score=score, reasons=reasons))
    # 同分保持加载顺序（文件名序），让结果可复现
    scored.sort(key=lambda m: -m.score)
    return scored


def find_matching_playbook(playbooks: list[dict], alert: dict) -> dict | None:
    """最贴切的那一条，没有就 None。会吞掉「同时命中好几条」，要看歧义用 `match_playbooks()`。"""
    matches = match_playbooks(playbooks, alert)
    return matches[0].playbook if matches else None


def evaluate_branch_condition(condition: Any, *, output: str = "", error: bool = False, value: Any = None) -> bool:
    """Evaluate the small SOP branch expression language.

    This intentionally supports only the expressions already used by the YAML:
    ``default``, ``error``, ``output_contains('text')`` and simple ``value ==`` / ``value !=``
    (value comes from :func:`branch_value`).
    Unknown expressions are false, so a later default branch can still carry on.
    """
    text = str(condition or "").strip()
    if text == "default":
        return True
    if text == "error":
        return bool(error)
    if text.startswith("output_contains(") and text.endswith(")"):
        needle = text[len("output_contains("):-1].strip()
        if (needle.startswith("'") and needle.endswith("'")) or (
            needle.startswith('"') and needle.endswith('"')
        ):
            needle = needle[1:-1]
        return needle in output
    if text.startswith("value ==") or text.startswith("value !="):
        op = "!=" if text.startswith("value !=") else "=="
        expected = text.split(op, 1)[1].strip()
        if (expected.startswith("'") and expected.endswith("'")) or (
            expected.startswith('"') and expected.endswith('"')
        ):
            expected = expected[1:-1]
        if value is None:
            return False  # 这一步没取到 value（报错、空结果、工具不产出 value），两种比较都不成立，落到 default
        equal = _same_value(value, expected)
        return equal if op == "==" else not equal
    return False


def _same_value(value: Any, expected: str) -> bool:
    """Zabbix 的 lastvalue 是字符串，`"1"`、`"1.0"`、`1` 都该等于 `'1'`。"""
    if str(value).strip() == expected:
        return True
    try:
        return float(str(value).strip()) == float(expected)
    except ValueError:
        return False


#: `when: "value == '…'"` 里的 value 从哪个工具的哪个字段取。**不在这张表里的工具没有 value**，
#: 写了 value 分支永远不会触发——`playbooks/lint.py` 的 `value-branch-unsupported` 会报。
#: 前 device-unreachable 写的是 `value == 'unreachable'`，运行时一直传 `value=None`，分支从没走过。
BRANCH_VALUE_FIELDS = {
    # zbx_items 按 key 子串过滤，`key=icmpping` 会同时列出 icmpping / icmppingloss / icmppingsec，
    # 所以取 key_ **完全等于** SOP 里 key 的那一条；SOP 没写 key 时只认唯一的一条。
    "zbx_items": "lastvalue",
}


def branch_value(tool: str, action: dict[str, Any], result: Any) -> Any:
    """从工具的真实返回里取出分支条件 `value` 的值；取不到就是 None（按 default 走）。"""
    field_name = BRANCH_VALUE_FIELDS.get(tool)
    if field_name is None or not isinstance(result, dict):
        return None
    rows = [row for row in (result.get("items") or []) if isinstance(row, dict)]
    wanted = str(action.get("key") or "").strip()
    if wanted:
        rows = [row for row in rows if str(row.get("key_") or "") == wanted]
    if len(rows) != 1:
        return None
    return rows[0].get(field_name)


#: 一步的执行结果里，哪几种算 `when: error`。**只有工具报错和白名单拒绝。**
#: 返回为空（`empty=true` 或输出空白）是查询成功、结果为空，按 `default` 走。
#: 前运行时把「空」也当 error，而 local_shutdown_evidence 的 error/default 恰好都指
#: local_log_buffer，规则里的重叠一直没暴露（验证 外部 agent 反馈第 3 条）。
#: 剧本执行器（`diag/executor.py`）和对话 agent 的 `SopRuntimeState` 都走这一处。
BRANCH_ERROR_STATUSES = frozenset({"failed", "denied"})


def branch_error(status: str) -> bool:
    return status in BRANCH_ERROR_STATUSES


def next_step_id(step: dict[str, Any], *, output: str = "", error: bool = False, value: Any = None) -> str:
    """Return the next SOP step id selected by this step's branches."""
    default_goto = AI_GOTO
    for branch in step.get("branches") or []:
        when = branch.get("when")
        if str(when).strip() == "default":
            default_goto = str(branch.get("goto") or AI_GOTO)
            continue
        if evaluate_branch_condition(when, output=output, error=error, value=value):
            return str(branch.get("goto") or AI_GOTO)
    return default_goto


#: tag 命中记多少分。取 12 是因为它该比一个短词（`"down"`，4 分）重，
#: 又不该压过一个具体的长词（`"OSPF neighbor"`，13 分）——tag 说明的是
#: "这类告警"，而具体的触发器名说明的是"这个告警"。
_TAG_MATCH_SCORE = 12

#: 厂商命中记多少分。给得高，是因为**一条厂商专用的 SOP 一定比通用的贴切**
#: （它的命令能真的跑）。厂商不匹配不是扣分是直接出局，见 `_match_with_score`。
_VENDOR_MATCH_SCORE = 30


def _match_with_score(match: dict, alert: dict) -> tuple[bool, int, list[str]]:
    """判断是否命中、算特异性、记理由。理由原样进 `sop_lookup` 的返回，人和 agent 都看得到为什么是这条。"""
    trigger = str(alert.get("trigger_name") or alert.get("name") or "")
    reasons: list[str] = []
    score = 0

    # 厂商对不上直接出局，不是扣分：SOP 里是厂商特定的字面命令，对不上每一步都会被白名单拒。
    # 不写 vendor 的 SOP 视为厂商无关。
    want_vendor = str(match.get("vendor") or "").strip().lower()
    if want_vendor:
        alert_vendor = str(alert.get("vendor") or "").strip().lower()
        if alert_vendor and alert_vendor != want_vendor:
            return (False, 0, [])
        if alert_vendor:
            score += _VENDOR_MATCH_SCORE
            reasons.append(f"厂商 {want_vendor}")

    needles = match.get("trigger_name_contains") or []
    if needles:
        hit = [n for n in needles if n in trigger]
        if not hit:
            return (False, 0, [])
        # 命中多个写法时按最长的那个记分：最长 = 最具体
        longest = max(hit, key=len)
        score += len(longest)
        reasons.append(f"告警名含 {longest!r}")

    tags = {str(t.get("tag")): str(t.get("value")) for t in alert.get("tags") or []}
    for item in match.get("tags") or []:
        # Historical records from before K2 did not persist Zabbix tags. Live
        # alerts still enforce tag matches; tagless replay falls back to the
        # trigger-name half of the match so old real captures remain usable for
        # apples-to-apples experiments.
        if not tags:
            continue
        if tags.get(str(item.get("tag"))) != str(item.get("value")):
            return (False, 0, [])
        score += _TAG_MATCH_SCORE
        reasons.append(f"tag {item.get('tag')}={item.get('value')}")

    if not needles and not (match.get("tags") or []):
        # 空 match 等于"匹配一切"，这几乎总是配置写错了。放行但记 0 分，
        # 保证任何有实际条件的 SOP 都排在它前面。
        reasons.append("这条 SOP 没写任何匹配条件（匹配一切）")

    return (True, score, reasons)
