"""链路流量日报 / 周报 / 月报 PDF 的接口：现生成现下载。实现在 `netops_ai.inspection.traffic_report`，这里只做参数和状态码。"""
from __future__ import annotations

import os
from pathlib import Path

from fastapi import APIRouter, Query
from fastapi.responses import FileResponse, JSONResponse

from netops_ai.inspection import traffic_report as TR

router = APIRouter(prefix="/api/reports/traffic", tags=["reports"])


def report_dir() -> Path:
    return Path(os.environ.get("NETOPS_REPORT_DIR") or Path(__file__).resolve().parents[2] / "records" / "reports")


def _zabbix():
    from netops_ai.api.dashboard import _env
    from netops_ai.zabbix.client import ZabbixClient

    e = _env()
    return ZabbixClient(url=e.get("ZABBIX_URL", ""), user=e.get("ZABBIX_USER", ""), password=e.get("ZABBIX_PASSWORD", ""))


@router.get("/{kind}")
def traffic_report(kind: str, host: list[str] = Query(default=[])):
    """`kind` = daily / weekly / monthly；`host` 可重复，只统计这些 Zabbix 主机（不给 = 全部）。返回 PDF。"""
    if kind not in TR.KINDS:
        return JSONResponse({"message": f"kind must be one of {', '.join(TR.KINDS)}"}, status_code=400)
    try:
        with _zabbix() as zbx:
            res = TR.generate(kind, zbx, report_dir(), hosts=host or None)
    except Exception as exc:  # noqa: BLE001 - 取不到 Zabbix 数据：4xx/5xx + 人话，不抛堆栈
        return JSONResponse({"message": f"could not build the report: {type(exc).__name__}: {str(exc)[:200]}"}, status_code=502)
    return FileResponse(res["path"], media_type="application/pdf", filename=Path(res["path"]).name)
