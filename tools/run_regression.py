"""拿已有的 spike 样本当回归集跑模型分析，不造新故障。

用法：

 .venv/bin/python -m tools.run_regression spike-1 spike-2

对每个样本跑两组（A：只给 Zabbix；B：Zabbix + 设备），每组 3 次，
输出存进 内部文档。
`--prefix` 控制文件名前缀（默认 `v2-`），**不覆盖上一轮没有前缀的输出**——
回归的意义就在于能前后对比。

**只读 `input-zabbix.md`/`input-device.md`（以及可选的额外 Zabbix 变体文件），
绝不改它们**——那是回归集，改了就没法跟下一次结果比较了。

M2.1 加的：
- 三种失败分开处理，不再混在一起重试：**限流（429）**等它说的时间重试；
 **schema 校验失败**（模型吐的 JSON 不满足 strict schema）单独重试几次，
 重试完还不行就走**降级路径**（`analyze_degraded`，宽松 json_object 模式），
 降级也失败才算终态失败——全程不静默吞掉，每一步都记进输出文件。
- 拿到结果之后立刻跑两道机械校验：`schema.check_business_rules`（confidence
 跟 undistinguishable_candidates 矛不矛盾）、`verify.verify_evidence`
 （每条证据是不是从标注的来源逐字复制的），结果都存进输出 JSON，
 不用等我事后另外写脚本核对。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from netops_ai.analysis.analyzer import analyze, analyze_degraded  # noqa: E402
from netops_ai.analysis.schema import check_business_rules  # noqa: E402
from netops_ai.analysis.verify import summarize, verify_evidence  # noqa: E402
from netops_ai.llm.client import LLMClient, LLMError  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
SPIKE_DIR = REPO_ROOT / "experiments" / "spike"
RUNS_PER_GROUP = 3
MAX_RATE_LIMIT_RETRIES = 6
MAX_SCHEMA_RETRIES = 3
MAX_BUSINESS_RULE_RETRIES = 2

_RETRY_AFTER_RE = re.compile(r"try again in ([\d.]+)s")


def _load_dotenv(path: Path) -> dict:
    env = {}
    if not path.exists():
        return env
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip()
    return env


def _make_client() -> LLMClient:
    env = {**_load_dotenv(REPO_ROOT / ".env"), **os.environ}
    return LLMClient(
        base_url=env.get("LLM_BASE_URL", ""),
        api_key=env.get("LLM_API_KEY", ""),
        model=env.get("LLM_MODEL", ""),
    )


def _is_rate_limit(exc: LLMError) -> bool:
    return exc.status_code == 429 or "rate_limit" in str(exc)


def _is_schema_validation_failure(exc: LLMError) -> bool:
    msg = str(exc)
    return exc.status_code == 400 and (
        "does not match the expected schema" in msg or "json_validate_failed" in msg
    )


def _call_with_retries(zabbix_text: str, dev_text, client: LLMClient, label: str, log: list[str]):
    """三层重试：限流 -> 等它说的时间；schema 校验失败 -> 短退避重试；
    两者都用完 -> 降级到宽松 json_object 模式。全程把每一步动作记进 `log`，
    最终存进输出文件，不是只留在终端。
    """
    schema_attempts = 0
    while True:
        try:
            return analyze(zabbix_text, dev_text, client=client), False  # (result, degraded)
        except LLMError as exc:
            if _is_rate_limit(exc):
                m = _RETRY_AFTER_RE.search(str(exc))
                wait = float(m.group(1)) + 2 if m else 20.0
                msg = f"撞限流，等 {wait:.1f}s 后重试"
                print(f"[{label}] {msg}", flush=True)
                log.append(msg)
                time.sleep(wait)
                continue
            if _is_schema_validation_failure(exc) and schema_attempts < MAX_SCHEMA_RETRIES:
                schema_attempts += 1
                msg = f"strict schema 校验失败，第 {schema_attempts}/{MAX_SCHEMA_RETRIES} 次重试：{exc}"
                print(f"[{label}] {msg}", flush=True)
                log.append(msg)
                continue
            if _is_schema_validation_failure(exc):
                msg = f"strict schema 重试 {MAX_SCHEMA_RETRIES} 次仍失败，降级到宽松 json_object 模式：{exc}"
                print(f"[{label}] {msg}", flush=True)
                log.append(msg)
                result = analyze_degraded(zabbix_text, dev_text, client=client)
                return result, True
            # 其它类型的错误（配置错误、网络错误等）不重试，原样抛出去给上层记成终态失败
            raise


def _check_and_maybe_retry_business_rules(
    zabbix_text: str, dev_text, client: LLMClient, label: str, log: list[str], result, degraded: bool
):
    """confidence 跟 undistinguishable_candidates 矛盾属于"格式对但逻辑错"，
    strict schema 管不到，这里单独重试几次；重试完还矛盾就如实记下来，
    不静默改模型的输出去凑规则。
    """
    if result.parsed is None:
        return result, degraded, []

    source_text = f"{zabbix_text}\n\n{dev_text or ''}"
    violations = check_business_rules(result.parsed, source_text)
    attempts = 0
    while violations and attempts < MAX_BUSINESS_RULE_RETRIES:
        attempts += 1
        msg = f"业务规则违反（第 {attempts}/{MAX_BUSINESS_RULE_RETRIES} 次重试）：{violations}"
        print(f"[{label}] {msg}", flush=True)
        log.append(msg)
        result, degraded = _call_with_retries(zabbix_text, dev_text, client, label, log)
        violations = check_business_rules(result.parsed, source_text) if result.parsed is not None else []

    if violations:
        msg = f"重试 {MAX_BUSINESS_RULE_RETRIES} 次后仍然违反业务规则，原样记录：{violations}"
        print(f"[{label}] {msg}", flush=True)
        log.append(msg)
    return result, degraded, violations


def run_case(case: str, client: LLMClient, *, prefix: str, groups: dict[str, tuple]) -> None:
    """`groups`：{组名: (zabbix_text, device_text)}，调用方自己拼好每组喂什么。"""
    case_dir = SPIKE_DIR / case
    out_dir = case_dir / "model-output"
    out_dir.mkdir(exist_ok=True)

    for group, (zabbix_text, device_text) in groups.items():
        for run_idx in range(1, RUNS_PER_GROUP + 1):
            label = f"{prefix}{case}-{group}-run{run_idx}"
            print(f"[{label}] 调用中...", flush=True)
            t0 = time.time()
            log: list[str] = []
            try:
                result, degraded = _call_with_retries(zabbix_text, device_text, client, label, log)
                result, degraded, business_violations = _check_and_maybe_retry_business_rules(
                    zabbix_text, device_text, client, label, log, result, degraded
                )
            except LLMError as exc:
                print(f"[{label}] 终态失败：{exc}", flush=True)
                record = {
                    "case": case,
                    "group": group,
                    "run": run_idx,
                    "error": str(exc),
                    "retry_log": log,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
                (out_dir / f"{label}.json").write_text(
                    json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                continue

            elapsed = time.time() - t0
            evidence_verification = None
            if result.parsed is not None and isinstance(result.parsed.get("evidence"), list):
                v = verify_evidence(result.parsed["evidence"], zabbix_text, device_text)
                evidence_verification = {
                    "summary": summarize(v),
                    "details": [
                        {
                            "index": r.index,
                            "verified": r.verified,
                            "grade": r.grade,
                            "source_from": r.source_from,
                            "reason": r.reason,
                        }
                        for r in v
                    ],
                }

            counter_evidence_verification = None
            if result.parsed is not None and isinstance(result.parsed.get("hypothesis_checklist"), dict):
                per_category = {}
                for cat, item in result.parsed["hypothesis_checklist"].items():
                    ce = (item or {}).get("counter_evidence") or []
                    if not ce:
                        continue
                    v = verify_evidence(ce, zabbix_text, device_text)
                    per_category[cat] = {
                        "summary": summarize(v),
                        "details": [
                            {
                                "index": r.index,
                                "verified": r.verified,
                                "grade": r.grade,
                                "source_from": r.source_from,
                                "reason": r.reason,
                            }
                            for r in v
                        ],
                    }
                if per_category:
                    counter_evidence_verification = per_category

            record = {
                "case": case,
                "group": group,
                "run": run_idx,
                "model": client.model,
                "degraded": degraded,
                "business_rule_violations": business_violations,
                "retry_log": log,
                "elapsed_seconds": round(elapsed, 2),
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "parsed": result.parsed,
                "content": result.response.content,
                "reasoning": result.response.reasoning,
                "finish_reason": result.response.finish_reason,
                "usage": result.response.usage,
                "evidence_verification": evidence_verification,
                "counter_evidence_verification": counter_evidence_verification,
            }
            (out_dir / f"{label}.json").write_text(
                json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            root_cause = (result.parsed or {}).get("root_cause", "(解析失败，见 content 原文)")
            flags = []
            if degraded:
                flags.append("DEGRADED")
            if business_violations:
                flags.append("BUSINESS_RULE_VIOLATION")
            if evidence_verification and evidence_verification["summary"]["unverified"]:
                flags.append(f"UNVERIFIED_EVIDENCE×{evidence_verification['summary']['unverified']}")
            if counter_evidence_verification:
                unverified_ce = sum(
                    v["summary"]["unverified"] for v in counter_evidence_verification.values()
                )
                if unverified_ce:
                    flags.append(f"UNVERIFIED_COUNTER_EVIDENCE×{unverified_ce}")
            flag_str = f" [{','.join(flags)}]" if flags else ""
            print(f"[{label}] 完成 {elapsed:.1f}s{flag_str}，root_cause: {root_cause[:80]}", flush=True)


def default_groups(case: str) -> dict[str, tuple]:
    case_dir = SPIKE_DIR / case
    zabbix_text = (case_dir / "input-zabbix.md").read_text(encoding="utf-8")
    device_text = (case_dir / "input-device.md").read_text(encoding="utf-8")
    groups = {
        "A": (zabbix_text, None),
        "B": (zabbix_text, device_text),
    }
    # v2：轮询时间对齐版（M2.2 任务 b），优先用这份；v1 那份采集时两个监控项
    # 轮询时间没对齐，已知有缺陷，不再拿来跑回归，只留作历史对照
    adminstatus_v2_path = case_dir / "input-zabbix-with-adminstatus-v2.md"
    adminstatus_v1_path = case_dir / "input-zabbix-with-adminstatus.md"
    if adminstatus_v2_path.exists():
        groups["A-adminstatus"] = (adminstatus_v2_path.read_text(encoding="utf-8"), None)
    elif adminstatus_v1_path.exists():
        groups["A-adminstatus"] = (adminstatus_v1_path.read_text(encoding="utf-8"), None)
    return groups


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("cases", nargs="*", default=["spike-1", "spike-2"])
    parser.add_argument("--prefix", default="v2-", help="输出文件名前缀，避免覆盖上一轮")
    args = parser.parse_args()

    client = _make_client()
    for case in args.cases:
        run_case(case, client, prefix=args.prefix, groups=default_groups(case))


if __name__ == "__main__":
    main()
