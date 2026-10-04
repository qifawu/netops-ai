#!/usr/bin/env python3
from __future__ import annotations

import os
import re
import sys
import traceback
from datetime import datetime
from pathlib import Path

TRAP_LOG = Path("/var/log/snmptrap/snmptrap.log")
ERROR_LOG = Path("/var/log/snmptrap/handler-errors.log")

ISO_PREFIX_RE = re.compile(r"(^|[\s=:])iso\.")
SOURCE_RE = re.compile(r"\[([0-9A-Fa-f:.]+)\]")


def parse_source_ip(source_line: str) -> str:
    match = SOURCE_RE.search(source_line)
    if match:
        return match.group(1)
    return source_line.strip()


def parse_trap_stdin(text: str) -> tuple[str, str, list[str]]:
    lines = text.splitlines()
    hostname = lines[0].strip() if lines else ""
    source_line = lines[1].strip() if len(lines) > 1 else ""
    return hostname, parse_source_ip(source_line), lines[2:]


def format_zabbix_trap(text: str, now: datetime | None = None) -> str:
    hostname, source_ip, varbinds = parse_trap_stdin(text)
    stamp = (now or datetime.now()).strftime("%Y%m%d.%H%M%S")
    body = [f"{stamp} ZBXTRAP {source_ip}", f"PDU INFO: hostname={hostname} source={source_ip}"]
    # snmptrapd 无 MIB 时把根 OID 写成 `iso.3.6...`；统一成 `1.3.6...`，Zabbix 监控项的数字 OID 正则才匹配得上（真机实测）
    body.extend(ISO_PREFIX_RE.sub(r"\g<1>1.", line) for line in varbinds)
    return "\n".join(body) + "\n"


def append_atomic(path: Path, data: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = data.encode("utf-8", errors="replace")
    fd = os.open(path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o644)
    try:
        os.write(fd, encoded)
    finally:
        os.close(fd)


def log_error(exc: BaseException, error_log: Path = ERROR_LOG) -> None:
    try:
        append_atomic(error_log, "".join(traceback.format_exception(exc)))
    except Exception:
        pass


def handle(text: str, trap_log: Path = TRAP_LOG, error_log: Path = ERROR_LOG) -> int:
    try:
        append_atomic(trap_log, format_zabbix_trap(text))
    except Exception as exc:
        log_error(exc, error_log)
    return 0


def main() -> int:
    return handle(sys.stdin.read())


if __name__ == "__main__":
    raise SystemExit(main())
