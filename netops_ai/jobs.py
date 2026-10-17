"""定时任务入口：把报表、检测、提醒这几件事做成一条命令一件事，交给系统的计划任务（Windows 任务计划程序 / cron）定时跑。

    python -m netops_ai.jobs <任务> [--out 目录]

| 任务 | 做什么 | 结果 |
|---|---|---|
| `isp-flap` | 检测 `isp-links.yaml` 里的外网链路是否翻滚 | 翻滚就出给运营商的邮件草稿 |
| `traffic-daily` / `traffic-weekly` / `traffic-monthly` | 生成流量报表 PDF + 给管理层的总结 | 草稿箱里一份待确认的草稿 |
| `version-reminder` | 设备版本对照 `version-policy.yaml` | 有落后的设备就出一份提醒草稿 |
| `cve` | 设备版本 × CVE 报告 | `<目录>/cve-<日期>.html` |
| `topology` | CDP / LLDP 拓扑自动发现 | `<目录>/topology-discovery.*` |

**所有对外发送都只写草稿**（草稿箱，人确认才发）；任务本身不发信。收件人 / 通道取环境变量：
`REPORT_CHANNEL`（email / feishu，默认 feishu）、`REPORT_TO`（邮件收件人，逗号分隔）。
退出码：0 = 跑完（含「没有需要处理的」）；1 = 任务出错（原因打印到标准错误），计划任务可据此告警。
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

JOBS = ("isp-flap", "traffic-daily", "traffic-weekly", "traffic-monthly", "version-reminder", "cve", "topology")


def _zabbix() -> Any:
    from netops_ai.llm.factory import env
    from netops_ai.zabbix.client import ZabbixClient

    e = env()
    return ZabbixClient(url=e.get("ZABBIX_URL", ""), user=e.get("ZABBIX_USER", ""), password=e.get("ZABBIX_PASSWORD", ""))


def _llm() -> Any:
    try:
        from netops_ai.llm.client import LLMClient

        c = LLMClient()
        return c if (c.base_url and c.api_key and c.model) else None
    except Exception:  # noqa: BLE001
        return None


def _recipients() -> tuple[str, list[str]]:
    channel = os.environ.get("REPORT_CHANNEL", "feishu").strip().lower()
    return channel, [x.strip() for x in os.environ.get("REPORT_TO", "").split(",") if x.strip()]


def run_isp_flap(zbx: Any, links_path: Path, *, now: datetime | None = None, root: Path | None = None) -> list[str]:
    from netops_ai.inspection import isp_flap as ISP

    links = ISP.load_links(links_path)
    if not links:
        return [f"no links configured ({links_path})"]
    return [f"{r['link']}: {r['action']} (down events: {r['flaps']})" + (f" draft={r['draft']}" if r.get("draft") else "")
            for r in ISP.check_links(zbx, links, now=now, root=root)]


def run_traffic(kind: str, zbx: Any, out_dir: Path, *, llm: Any = None, channel: str = "feishu", to: list[str] | None = None,
                now: datetime | None = None, root: Path | None = None) -> list[str]:
    from netops_ai.inspection import management_summary as MS

    if channel == "email" and not to:
        raise ValueError("REPORT_TO is required for the email channel")
    res = MS.create_draft(kind, zbx, out_dir, llm=llm, channel=channel, to=to or [], now=now, root=root)
    return [f"report: {res['report']['path']}", f"draft {res['draft']['id']} ({res['draft']['status']}): {res['draft']['subject']}"]


def run_version_reminder(known: dict[str, Any], adapter_factory: Callable[[Any], Any], policy_path: Path, *, channel: str = "feishu",
                         to: list[str] | None = None, root: Path | None = None) -> list[str]:
    from netops_ai.inspection import cve_report as CR
    from netops_ai.inspection import version_policy as VP

    policy = VP.load_policy(policy_path)
    if not policy:
        return [f"no version policy ({policy_path})"]
    rows = VP.evaluate(CR.collect_versions(known, adapter_factory), policy)
    behind = [r["device"] for r in rows if r["status"] == "behind"]
    draft = VP.create_reminder_draft(rows, channel=channel, to=to, root=root)
    return [f"{len(rows)} devices checked, behind target: {', '.join(behind) or 'none'}"] + ([f"draft {draft['id']} ({draft['status']})"] if draft else [])


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="定时任务入口（只写草稿 / 报告，不对外发送）")
    ap.add_argument("job", choices=JOBS)
    ap.add_argument("--out", default="reports", help="报告输出目录（默认 ./reports）")
    ap.add_argument("--links", default="isp-links.yaml")
    ap.add_argument("--policy", default="version-policy.yaml")
    a = ap.parse_args(argv)
    out = Path(a.out)
    try:
        if a.job == "isp-flap":
            with _zabbix() as zbx:
                lines = run_isp_flap(zbx, Path(a.links))
        elif a.job.startswith("traffic-"):
            channel, to = _recipients()
            with _zabbix() as zbx:
                lines = run_traffic(a.job.split("-", 1)[1], zbx, out, llm=_llm(), channel=channel, to=to)
        elif a.job == "version-reminder":
            from netops_ai import topology as topo

            channel, to = _recipients()
            lines = run_version_reminder(topo.load_topology(), topo._default_adapter_factory, Path(a.policy), channel=channel, to=to)
        elif a.job == "cve":
            from netops_ai import topology as topo
            from netops_ai.inspection import cve_report as CR
            from netops_ai.inspection.export import blocks_to_html

            report = CR.build_report(CR.collect_versions(topo.load_topology(), topo._default_adapter_factory), CR.Sources())
            out.mkdir(parents=True, exist_ok=True)
            path = out / f"cve-{datetime.now():%Y%m%d}.html"
            path.write_text(blocks_to_html(CR.to_blocks(report), "设备软件版本漏洞报告", classify=None), encoding="utf-8")
            lines = [str(path)]
        else:  # topology
            from netops_ai.inspection import topology_discovery as TD

            sys.argv = ["topology_discovery", "--out", str(out)]
            TD.main()
            lines = [f"written to {out}"]
    except Exception as exc:  # noqa: BLE001 - 计划任务靠退出码发现失败
        print(f"{a.job} failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
