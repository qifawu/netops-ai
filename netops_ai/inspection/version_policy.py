"""软件版本合规提醒：你维护一份「目标版本清单」，系统拿设备实际版本对照，落后的提醒（可附上该版本命中的 CVE 数）。

**为什么不自动找最新版本、更不自动升级**：厂商官网的最新版本多数要登录和授权才能看 / 下载，没有公开接口；升级是改设备、
不可逆的高风险动作，和本项目「只读」的定位相反。所以这里只做提醒：目标版本由人定（参考厂商公告、CVE 报告、内部测试结论），
系统负责持续核对「谁落后了」。

清单（仓库根 `version-policy.yaml`，示例见 `examples/version-policy.yaml`）::

    - devices: ["*"]            # 设备名列表，"*" = 其余所有设备（先匹配到的条目生效）
      product: ios              # ios / ios_xe，和 `cve_report.parse_version` 的 product 一致
      target: "15.9(3)M9"       # 目标版本：低于它就算落后
      note: "2026-09 内部测试通过"

版本比较只在同一产品线内做（按数字和字母段逐段比较，如 15.9(3)M9 < 15.9(3)M10）；认不出的版本串标「无法比较」，不猜。
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

STATUSES = ("ok", "behind", "ahead", "unknown", "no_policy")


def version_key(v: str) -> tuple[tuple[int, int | str], ...]:
    """`15.9(3)M9` → ((0,15),(0,9),(0,3),(1,'m'),(0,9))：数字段比大小，字母段按字母序；数字段排在字母段前。"""
    return tuple((0, int(t)) if t.isdigit() else (1, t) for t in re.findall(r"\d+|[a-z]+", v.lower()))


def compare(actual: str, target: str) -> int | None:
    """-1 落后 / 0 一致 / 1 更新；任一方没有数字段（认不出）返回 None。"""
    a, t = version_key(actual), version_key(target)
    if not any(k == 0 for k, _ in a) or not any(k == 0 for k, _ in t):
        return None
    # 段类型对不上（一边是数字一边是字母）时按「类型 + 值」比，保证可排序
    return (a > t) - (a < t)


def load_policy(path: Path) -> list[dict[str, Any]]:
    import yaml

    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or []
    except OSError:
        return []
    if not isinstance(data, list):
        raise ValueError("version-policy.yaml must be a list")
    out = []
    for i, d in enumerate(data):
        if not isinstance(d, dict) or not d.get("target") or not d.get("product"):
            raise ValueError(f"policy entry #{i + 1}: product and target are required")
        devs = [d["devices"]] if isinstance(d.get("devices"), str) else list(d.get("devices") or ["*"])
        out.append({"note": "", **d, "devices": devs, "target": str(d["target"])})
    return out


def evaluate(versions: dict[str, dict[str, Any]], policy: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """对每台设备给出 {device, product, version, target, status, note}。`versions` 是 `cve_report.collect_versions` 的结果。"""
    rows = []
    for name, v in sorted(versions.items()):
        row: dict[str, Any] = {"device": name, "product": v.get("product", ""), "version": v.get("raw", ""), "target": "", "note": ""}
        if "error" in v:
            rows.append({**row, "status": "unknown", "note": v["error"]})
            continue
        rule = next((p for p in policy if p["product"] == v["product"] and ("*" in p["devices"] or name in p["devices"])), None)
        if rule is None:
            rows.append({**row, "status": "no_policy"})
            continue
        c = compare(v["version"], rule["target"].lower())
        status = "unknown" if c is None else "ok" if c == 0 else "behind" if c < 0 else "ahead"
        rows.append({**row, "target": rule["target"], "note": rule["note"], "status": status})
    return rows


STATUS_CN = {"ok": "达标", "behind": "落后", "ahead": "高于目标", "unknown": "无法比较", "no_policy": "未设目标"}


def to_blocks(rows: list[dict[str, Any]], cve_counts: dict[str, int] | None = None) -> list[tuple]:
    behind = [r for r in rows if r["status"] == "behind"]
    blocks: list[tuple] = [("h1", "软件版本合规"),
                           ("p", f"共 {len(rows)} 台设备；落后于目标版本 {len(behind)} 台。目标版本由人维护，系统只核对、不自动升级。")]
    blocks.append(("table", ["设备", "当前版本", "目标版本", "状态"] + (["命中 CVE"] if cve_counts is not None else []) + ["备注"],
                   [[r["device"], r["version"] or "-", r["target"] or "-", STATUS_CN[r["status"]]]
                    + ([cve_counts.get(r["device"], "-")] if cve_counts is not None else []) + [r["note"]] for r in rows]))
    return blocks


def reminder_text(rows: list[dict[str, Any]], cve_counts: dict[str, int] | None = None) -> str:
    behind = [r for r in rows if r["status"] == "behind"]
    if not behind:
        return "所有设备的软件版本都达到目标版本。"
    lines = [f"有 {len(behind)} 台设备的软件版本落后于目标版本："]
    for r in behind:
        extra = f"，该版本命中 {cve_counts[r['device']]} 条 CVE" if cve_counts and r["device"] in cve_counts else ""
        lines.append(f"- {r['device']}：当前 {r['version']}，目标 {r['target']}{extra}")
    lines.append("升级需要人评估后执行，系统不会自动升级。")
    return "\n".join(lines)


def create_reminder_draft(rows: list[dict[str, Any]], *, channel: str = "feishu", to: list[str] | None = None,
                          cve_counts: dict[str, int] | None = None, root: Path | None = None) -> dict[str, Any] | None:
    """有落后设备才出一份提醒草稿（进草稿箱，人确认才发）；都达标返回 None。"""
    from netops_ai import outbox

    if not any(r["status"] == "behind" for r in rows):
        return None
    return outbox.create(channel=channel, kind="version-reminder", subject="软件版本落后提醒", body=reminder_text(rows, cve_counts),
                         to=to or [], root=root)
