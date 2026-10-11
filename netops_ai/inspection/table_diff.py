"""通用「表对表」比较：同一条命令这次和上一次的输出各解析成行记录表（见 table_parse），按 key 字段对齐，
报告新增 / 消失 / 字段变化。路由表、ARP、MAC、邻居、接口……靠配置（key / compare 字段）区分，不写死任何一种命令。

规则字段：
    type: table_diff_vs_previous
    key: [字段...]            行的身份（必填）
    compare: [字段...]        比较哪些字段；省略 = 除 key 和「名字一看就每次都变」的字段（uptime / age…）以外的全部
    ignore_fields: [字段...]  明确不比的字段
    max_added / max_removed / max_changed   各自允许多少（默认 0）
    template: <TextFSM>       没有现成解析器时自带
    fallback: text            解析不了时退回逐行文本对比
纯函数；返回的结果字典和 checklist 里的明细对比规则同形（added / removed / *_sample / state / passed），报告层通用。
"""
from __future__ import annotations

from typing import Any, Callable

from netops_ai.inspection import table_parse as TP

DEFAULT_SAMPLE = 20
TYPE = "table_diff_vs_previous"


def _list(v: Any) -> list[str]:
    if v is None:
        return []
    return [v] if isinstance(v, str) else [str(x) for x in v]


def validate(where: str, e: dict[str, Any], severities: tuple[str, ...]) -> list[str]:
    out: list[str] = []
    for k in ("key", "compare", "ignore_fields"):
        v = e.get(k)
        if v is None and k != "key":
            continue
        good = isinstance(v, list) and all(isinstance(x, str) and x.strip() for x in v) and (k != "key" or bool(v))
        if not good:
            out.append(f"{where}.{k}: a non-empty list of field names" if k == "key" else f"{where}.{k}: a list of field names")
    for k in ("max_added", "max_removed", "max_changed", "sample"):
        if k in e and (not isinstance(e[k], int) or isinstance(e[k], bool) or e[k] < 0):
            out.append(f"{where}.{k}: an integer >= 0")
    if e.get("severity", "warning") not in severities:
        out.append(f"{where}.severity: one of {', '.join(severities)}")
    if e.get("template") is not None:
        out += [f"{where}.{p}" for p in TP.validate_template(e["template"])]
    if e.get("fallback") not in (None, "text"):
        out.append(f"{where}.fallback: only 'text' is supported")
    if e.get("platform") is not None and not isinstance(e["platform"], str):
        out.append(f"{where}.platform: a string")
    return out


def describe(e: dict[str, Any]) -> str:
    return (f"table by {'+'.join(_list(e.get('key')))} vs previous run (allowed +{e.get('max_added', 0)} / "
            f"-{e.get('max_removed', 0)} / ~{e.get('max_changed', 0)})")


def compare_fields(e: dict[str, Any], fields: list[str]) -> list[str]:
    key = set(_list(e.get("key")))
    if e.get("compare"):
        return [f for f in _list(e["compare"]) if f not in key]
    skip = key | set(_list(e.get("ignore_fields"))) | set(TP.volatile_fields(fields))
    return [f for f in fields if f not in skip]


def _group(rows: list[dict[str, str]], key: list[str], cmp_: list[str]) -> dict[tuple[str, ...], set[tuple[str, ...]]]:
    out: dict[tuple[str, ...], set[tuple[str, ...]]] = {}
    for r in rows:
        out.setdefault(tuple(r.get(k, "") for k in key), set()).add(tuple(r.get(c, "") for c in cmp_))
    return out


def _fmt_key(k: tuple[str, ...]) -> str:
    return " ".join(v for v in k if v) or "(empty key)"


def _fmt_vals(cmp_: list[str], vals: tuple[str, ...]) -> str:
    return ", ".join(f"{c}={v}" for c, v in zip(cmp_, vals) if v)


def _row_text(key: tuple[str, ...], cmp_: list[str], variants: set[tuple[str, ...]]) -> str:
    extra = [f"({_fmt_vals(cmp_, v)})" for v in sorted(variants) if _fmt_vals(cmp_, v)]
    return " ".join([_fmt_key(key), *extra])


def diff_tables(cur_rows: list[dict[str, str]], old_rows: list[dict[str, str]], key: list[str], cmp_: list[str]) -> dict[str, Any]:
    cur, old = _group(cur_rows, key, cmp_), _group(old_rows, key, cmp_)
    added = [_row_text(k, cmp_, cur[k]) for k in sorted(cur.keys() - old.keys())]
    removed = [_row_text(k, cmp_, old[k]) for k in sorted(old.keys() - cur.keys())]
    changed = []
    for k in sorted(cur.keys() & old.keys()):
        if cur[k] == old[k]:
            continue
        gone, new = sorted(old[k] - cur[k]), sorted(cur[k] - old[k])
        if len(gone) == 1 and len(new) == 1:  # 单值变化：逐字段说「从什么变成什么」
            parts = [f"{c}: {a or '-'} -> {b or '-'}" for c, a, b in zip(cmp_, gone[0], new[0]) if a != b]
            changed.append(f"{_fmt_key(k)}: " + "; ".join(parts))
        else:
            changed.append(f"{_fmt_key(k)}: " + "; ".join([f"-({_fmt_vals(cmp_, v)})" for v in gone]
                                                          + [f"+({_fmt_vals(cmp_, v)})" for v in new]))
    return {"added": added, "removed": removed, "changed": changed, "current": len(cur), "reference": len(old)}


def evaluate(e: dict[str, Any], output: str, truncated: bool, command: str, vendor: str,
             ref: tuple[str, str, str, bool] | None, ever_attempted: bool,
             text_fallback: Callable[[], dict[str, Any]] | None = None) -> dict[str, Any]:
    """一条表对表规则的结果。`ref` = 参考运行 (时间, run id, 输出, 是否截断)；None = 没有可比的上一次。
    `text_fallback()` 由调用方给：解析不了且规则允许时退回的逐行文本对比。`passed` 为 None = 这次不判。"""
    key = _list(e.get("key"))
    sample = int(e.get("sample", DEFAULT_SAMPLE))
    out: dict[str, Any] = {"type": e["type"], "severity": e.get("severity", "warning"), "value": describe(e), "current": None,
                           "reference": None, "reference_run_id": None, "reference_at": None, "reference_source": None,
                           "added": None, "removed": None, "changed": None, "added_sample": [], "removed_sample": [],
                           "changed_sample": [], "truncated": {"current": truncated, "reference": False}}

    def parsed(text: str) -> TP.ParseResult:
        return TP.parse(command, text, vendor=vendor, template=e.get("template"), platform=e.get("platform"))

    cur = parsed(output)
    problem = cur.error
    if not problem:
        missing = [k for k in key if k not in cur.fields]
        if missing:
            problem = f"key field(s) {missing} not in the parsed table; available fields: {', '.join(cur.fields)}"
        elif not cur.rows and output.strip():
            problem = "the output has content but no row could be parsed from it (the format differs from the parser)"
    if problem:
        if e.get("fallback") == "text" and text_fallback is not None:
            res = text_fallback()
            res["detail"] = f"table parser unavailable ({problem}); compared text lines instead: " + str(res.get("detail", ""))
            return res
        return {**out, "passed": False, "state": "error", "detail": f"cannot parse the output into a table: {problem}"}
    cmp_ = compare_fields(e, cur.fields)
    out.update(current=len(_group(cur.rows, key, cmp_)), parser=cur.source, fields=cur.fields, compare=cmp_)
    if ref is None:
        if ever_attempted:
            return {**out, "passed": None, "state": "no_baseline",
                    "detail": "earlier runs did not collect this output (unreachable, refused or error); recorded now, not judged"}
        return {**out, "passed": None, "state": "baseline",
                "detail": f"first run: {out['current']} rows recorded as the baseline, not judged"}
    at, run_id, ref_text, ref_trunc = ref
    out.update(reference_run_id=run_id, reference_at=at, reference_source="run",
               truncated={"current": truncated, "reference": ref_trunc})
    if truncated or ref_trunc:  # 表被截断：少的行不是真的消失，逐行判不可靠
        return {**out, "passed": None, "state": "truncated",
                "detail": "an output was truncated; a table comparison would be unreliable, not judged"}
    old = parsed(ref_text)
    if old.error or any(k not in old.fields for k in key):
        return {**out, "passed": None, "state": "no_baseline",
                "detail": f"the previous run's output could not be parsed with the same rule ({old.error or 'key field missing'}); recorded now"}
    d = diff_tables(cur.rows, old.rows, key, cmp_)
    out.update(reference=d["reference"], added=len(d["added"]), removed=len(d["removed"]), changed=len(d["changed"]),
               added_sample=d["added"][:sample], removed_sample=d["removed"][:sample], changed_sample=d["changed"][:sample])
    bad = []
    for name, sym in (("added", "+"), ("removed", "-"), ("changed", "~")):
        got, limit = len(d[name]), int(e.get(f"max_{name}", 0))
        if got > limit:
            bad.append(f"{sym}{got} > {limit}")
    detail = (f"+{len(d['added'])} / -{len(d['removed'])} / ~{len(d['changed'])} vs run {run_id}"
              + (f": over the limit ({', '.join(bad)})" if bad else ""))
    return {**out, "passed": not bad, "state": "fail" if bad else "pass", "detail": detail}
