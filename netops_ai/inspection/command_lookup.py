"""用户不记得命令时帮他找：在「文档库」里检索候选只读命令，每条带出处和一句用途，并逐条过只读白名单。

两个来源：

- **内置命令目录** `playbooks/catalog/cisco_ios.yaml`：意图名 / 命令 / 用途说明做关键词匹配（中文描述先换成英文关键词）。
- **文档库索引** `netops_ai.docs_kb`（SQLite FTS5，`python tools/kb_ingest.py <目录>` 建，默认 `records/docs_kb.db`，
  `.env` 的 `DOC_SEARCH_DB` 可改）：检索命中的段落里抽出 `show ...` 命令。**模块不存在、索引不存在或为空时优雅降级**，
  只用内置目录，并如实说明「文档库里没有收录」。

检索结果只是命令和用途的参考，**不是设备证据**；被白名单拒绝的候选照样列出但标成不可用，用户选不了。
用户选定之后才并进草案（`plans.apply_patch` 的 `add_custom_checks`），这里不改草案。
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Any

from netops_ai.devices.whitelist import check as whitelist_check

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DOCS_DB = REPO_ROOT / "records" / "docs_kb.db"
CATALOG_SOURCE = "playbooks/catalog/cisco_ios.yaml"
MAX_CANDIDATES = 8

#: 「想查什么但没给命令 / 不记得命令」的说法。命中且消息里没有现成的 show 命令，就先检索再提案。
_ASK_RX = re.compile(
    r"不记得|忘了|想不起|不知道.{0,6}命令|什么命令|哪个命令|哪条命令|用啥命令|怎么查|怎么看|查一下|看一下|看看|有没有|是否|"
    r"don'?t remember|forgot|which command|what command|how (?:do|can) i (?:see|check)|check whether|is there a command",
    re.IGNORECASE,
)
_HAS_COMMAND_RX = re.compile(r"\bshow\s+[a-z]", re.IGNORECASE)

#: 中文描述 → 英文关键词（命令目录和文档都是英文）。长词在前。
_ZH_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("生成树", ("spanning-tree", "stp")), ("错误计数", ("error", "counters")), ("邻居", ("neighbor",)),
    ("接口", ("interface", "interfaces")), ("端口", ("interface", "port")), ("错误", ("error", "errors")),
    ("计数", ("counters",)), ("丢包", ("drops", "error")), ("路由", ("route", "routing", "protocols")),
    ("日志", ("logging", "log")), ("时钟", ("clock",)), ("时间", ("clock",)), ("重启", ("reload", "uptime")),
    ("版本", ("version",)), ("内存", ("memory",)), ("掉", ("down", "neighbor")), ("状态", ("state", "status")),
    ("配置变更", ("config",)), ("对端", ("neighbor", "cdp")), ("会话", ("session", "summary")),
)
_SUBJECTS = frozenset({"ospf", "bgp", "eigrp", "isis", "cdp", "lldp", "stp", "spanning-tree", "cpu", "memory", "logging",
                       "clock", "version", "hsrp", "vlan", "arp", "crc", "err-disabled", "uptime", "reload"})
_STOP = frozenset("the a an of to in on is are and or for with how what which do i see check whether show me my".split())


def wants_lookup(text: str) -> bool:
    """消息是不是在「描述想查什么、但没给命令」或「说不记得命令」。"""
    return bool(text) and bool(_ASK_RX.search(text)) and not _HAS_COMMAND_RX.search(text)


def keywords(text: str) -> list[str]:
    low = (text or "").lower()
    out: list[str] = []
    for zh, en in _ZH_KEYWORDS:
        if zh in low:
            out.extend((zh, *en))  # 中文词本身也留着：目录里的用途说明是中文写的
    out += [w for w in re.findall(r"[a-z][a-z0-9-]{1,}", low) if w not in _STOP]
    return list(dict.fromkeys(out))


def _verdict(command: str, vendor: str = "cisco") -> tuple[bool, str]:
    sample = command.replace("{interface}", "GigabitEthernet0/1").replace("{interface_short}", "Gi0/1")
    v = whitelist_check(vendor, sample)
    return bool(v.allowed), v.reason


def search_catalog(text: str, vendor: str = "cisco") -> list[dict[str, Any]]:
    """内置命令目录：关键词在 意图名 / 命令 / 用途 里出现的次数打分。"""
    try:
        from netops_ai.playbooks.catalog import load_catalog

        intents = load_catalog().get({"cisco": "cisco_ios"}.get(vendor, vendor), {})
    except Exception:  # noqa: BLE001 - 目录读不到就是没有候选
        return []
    words = keywords(text)
    # 点名了协议 / 对象（OSPF、BGP、CPU……）就必须命中它：「OSPF 邻居」不该把「BGP 邻居」也带出来
    subjects = [w for w in words if w in _SUBJECTS]
    scored = []
    for intent, d in intents.items():
        cmd = str(d.get("command") or "")
        if not cmd or "{alert_log_prefix}" in cmd:
            continue
        hay = f"{intent.replace('_', ' ')} {cmd} {d.get('note') or ''}".lower()
        if subjects and not any(s in hay for s in subjects):
            continue
        score = sum(1 for w in words if w in hay)
        if score:
            scored.append((score, intent, cmd, str(d.get("note") or "")))
    scored.sort(key=lambda x: -x[0])
    best = scored[0][0] if scored else 0
    out = []
    for score, intent, cmd, note in scored:
        if score < max(1, best - 1) or len(out) >= 4:
            break  # 只留跟最佳命中差不多的几条，弱相关的不凑数
        if any(c["command"] == cmd for c in out):
            continue  # 目录里有两个意图共用一条命令
        out.append({"command": cmd, "purpose": note, "source": f"{CATALOG_SOURCE} › {intent}", "origin": "catalog",
                    "needs_interface": "{interface}" in cmd, "score": score})
    return out


_DOC_CMD_RX = re.compile(r"(?<![\w-])(show[ 	]+[a-z][a-z0-9 _./|:-]{1,90})", re.IGNORECASE)


_PROSE = frozenset("that the a an you how some whether if which this these those what when where it its is are only no up "
                   "us me them there here".split())
_PLACEHOLDER_RX = re.compile(r"^(?:[a-z]+-)?(?:id|name|number|address|value|type|slot|port)$|^<.*>$")


def _clean_doc_command(raw: str) -> str:
    cmd = re.split(r"\s{2,}|[,;。，；)）\]]|\s+(?:to|for|and|on|before|after|when|while|if|in|shows?|displays?|lists?)\s", raw.strip(), maxsplit=1)[0]
    return re.sub(r"[\s.:|-]+$", "", cmd).strip()


def docs_status(db_path: str | Path | None) -> str:
    """文档库状态：ok / empty（索引在但没内容）/ missing（模块或索引不存在）。"""
    try:
        import netops_ai.docs_kb.search  # noqa: F401
    except ImportError:
        return "missing"
    path = Path(db_path or DEFAULT_DOCS_DB)
    if not path.exists():
        return "missing"
    try:
        conn = sqlite3.connect(path)
        try:
            n = conn.execute("SELECT count(*) FROM docs_fts").fetchone()[0]
        finally:
            conn.close()
    except sqlite3.Error:
        return "empty"
    return "ok" if n else "empty"


def search_docs(text: str, db_path: str | Path | None, vendor: str = "cisco", limit: int = 8) -> list[dict[str, Any]]:
    """文档库：FTS 检索命中段落，抽出 `show ...` 命令。模块 / 索引不在就返回空。"""
    try:
        from netops_ai.docs_kb.search import search
    except ImportError:
        return []
    path = Path(db_path or DEFAULT_DOCS_DB)
    query = " ".join(keywords(text)) or text
    try:
        rows = search(path, query, vendor=vendor, limit=limit)
    except Exception:  # noqa: BLE001 - 文档库坏了不该让对话失败
        return []
    out: list[dict[str, Any]] = []
    for row in rows:
        snippet = str(row.get("snippet") or "")
        heading = str(row.get("heading_path") or row.get("title") or "")
        # 先按句子切开：命令里可以有「.」（flash:x.txt），但一条命令不会跨句；不切的话第一条会把后一句整个吞掉
        for sentence in re.split(r"(?<=[.!?。！？])\s+|\n+", snippet):
            for m in _DOC_CMD_RX.finditer(sentence):
                cmd = _clean_doc_command(m.group(1))
                toks = cmd.lower().split()
                if (len(toks) < 2 or toks[1] in _PROSE or any(_PLACEHOLDER_RX.match(t) for t in toks)
                        or any(c["command"].lower() == cmd.lower() for c in out)):
                    continue  # 正文里的「show that ...」是句子不是命令；带 vlan-id 这类占位词的照抄了也跑不了
                out.append({"command": cmd, "purpose": (heading or sentence)[:120], "source": f"{row.get('source', '')} › {heading}",
                            "origin": "docs", "needs_interface": False, "score": 0})
    return out


def lookup(text: str, *, db_path: str | Path | None = None, vendor: str = "cisco", lang: str = "zh") -> dict[str, Any]:
    """检索候选命令。返回 {query, candidates[{command, purpose, source, origin, allowed, reason, needs_interface}], docs_status, note}。"""
    zh = lang != "en"
    status = docs_status(db_path)
    docs = search_docs(text, db_path, vendor) if status == "ok" else []
    cands = docs + [c for c in search_catalog(text, vendor) if all(c["command"].lower() != d["command"].lower() for d in docs)]
    out = []
    for c in cands[:MAX_CANDIDATES]:
        ok, reason = _verdict(c["command"], vendor)
        c = {k: v for k, v in c.items() if k != "score"}
        c["allowed"] = ok
        c["reason"] = "" if ok else (f"不可用，只读白名单不允许（{reason}）" if zh else f"Not usable: refused by the read-only whitelist ({reason})")
        out.append(c)
    if status != "ok":
        note = ("文档库里没有收录（索引不存在或为空），以下来自内置命令目录。" if zh
                else "The documentation library has no index (missing or empty); the candidates below come from the built-in command catalog.")
    elif not docs:
        note = "文档库里没有检索到相关命令，以下来自内置命令目录。" if zh else "No matching command in the documentation library; the candidates below come from the built-in catalog."
    else:
        note = "以下候选来自文档库和内置命令目录，只作命令 / 用途参考，不是设备证据。" if zh else "Candidates from the documentation library and the built-in catalog — a reference for commands, not device evidence."
    if not out:
        note += ("没有找到合适的候选命令，可以换个说法描述想查的内容。" if zh else " No candidate found; try describing what you want to see differently.")
    return {"query": text, "candidates": out, "docs_status": status, "note": note}
