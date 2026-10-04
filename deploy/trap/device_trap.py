from __future__ import annotations

import argparse
import configparser
import re
import socket
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

EVE_HOST = "192.0.2.128"
ZABBIX_IP = "192.0.2.128"
CRED_PATH = Path.home() / ".netops-ai-credentials.txt"

# V1/V2 console ports are not verified; override them at runtime with --port.
DEFAULT_PORTS = {
    "V1": 32768,
    "V2": 32769,
    "D1": 32770,
    "D2": 32773,
    "A1": 32774,
    "A2": 32775,
    "A3": 32776,
}

MGMT_IPS = {
    "V1": "192.0.2.50",
    "V2": "192.0.2.51",
    "D1": "192.0.2.52",
    "D2": "192.0.2.53",
    "A1": "192.0.2.54",
    "A2": "192.0.2.55",
    "A3": "192.0.2.56",
}

REMOVAL_COMMANDS = [
    "no snmp-server enable traps config",
    "no snmp-server enable traps syslog",
]

TRAP_COMMAND_TEMPLATES = [
    "snmp-server enable traps snmp linkdown linkup coldstart warmstart",
    "snmp-server enable traps bgp",
    "snmp-server enable traps ospf state-change",
    "snmp-server host {zabbix_ip} version 2c {community} snmp bgp ospf",
    "snmp-server trap-source {trap_source_if}",
    "snmp-server ifindex persist",
]

INTERFACE_COMMANDS = ["interface {trap_source_if}", "no snmp trap link-status", "exit"]
UNWANTED_TRAP_COMMANDS = [
    "snmp-server enable traps config",
    "snmp-server enable traps syslog",
]

REJECT_PATTERNS = ("% Invalid input", "% Incomplete", "% Unknown")


class ConsoleSession(Protocol):
    def send(self, line: str, wait: float = 0.8) -> str: ...

    def close(self) -> None: ...


@dataclass
class CommandResult:
    device: str
    command: str
    status: str
    reason: str = ""


class SocketConsole:
    def __init__(self, host: str, port: int, timeout: int = 15) -> None:
        self._socket = socket.create_connection((host, port), timeout=timeout)
        self._socket.settimeout(2)

    def read(self, wait: float = 1.0, maxbytes: int = 65536) -> str:
        time.sleep(wait)
        data = b""
        try:
            while True:
                chunk = self._socket.recv(4096)
                if not chunk:
                    break
                data += chunk
                if len(data) >= maxbytes:
                    break
        except socket.timeout:
            pass
        return data.decode(errors="replace")

    def send(self, line: str, wait: float = 0.8) -> str:
        self._socket.send((line + "\r").encode())
        return self.read(wait)

    def close(self) -> None:
        self._socket.close()


def read_creds(path: str | Path) -> dict[str, str]:
    text = Path(path).read_text(encoding="utf-8")
    return parse_creds(text)


def _strip_inline_comment(value: str) -> str:
    """凭据文件里有 `SNMP_COMMUNITY=xxx # 说明` 这种行（真机实测，不剥会把注释当 community 下发）。"""
    return re.split(r"\s+#", value.strip(), maxsplit=1)[0].strip()


def parse_creds(text: str) -> dict[str, str]:
    creds: dict[str, str] = {}
    parser = configparser.ConfigParser(interpolation=None)  # 口令里可能有 %，别做插值（真机实测）
    parser.read_string(text if re.search(r"^\s*\[", text, re.M) else "[DEFAULT]\n" + text)
    for section in parser.sections():
        for key, value in parser.items(section):
            creds[key.upper()] = _strip_inline_comment(value)
    for key, value in parser.defaults().items():
        creds[key.upper()] = _strip_inline_comment(value)
    return creds


def enable_secret_for(device: str, creds: dict[str, str]) -> str:
    if device.upper() == "V2" and creds.get("V2_ENABLE_SECRET"):
        return creds["V2_ENABLE_SECRET"]
    return creds.get("DEVICE_ENABLE_SECRET", "")


def identify_trap_source(show_ip_interface_brief: str, mgmt_ip: str) -> str:
    for raw in show_ip_interface_brief.splitlines():
        line = raw.strip()
        if not line or line.lower().startswith("interface "):
            continue
        parts = line.split()
        if len(parts) >= 2 and parts[1] == mgmt_ip:
            return parts[0]
    raise ValueError(f"management IP {mgmt_ip} was not found in show ip interface brief")


def build_trap_commands(community: str, trap_source_if: str, zabbix_ip: str = ZABBIX_IP) -> list[str]:
    return [
        template.format(community=community, trap_source_if=trap_source_if, zabbix_ip=zabbix_ip)
        for template in TRAP_COMMAND_TEMPLATES
    ]


def build_apply_commands(community: str, trap_source_if: str, zabbix_ip: str = ZABBIX_IP) -> list[str]:
    formatted_interface = [command.format(trap_source_if=trap_source_if) for command in INTERFACE_COMMANDS]
    return [*REMOVAL_COMMANDS, *build_trap_commands(community, trap_source_if, zabbix_ip), *formatted_interface]


def classify_command_output(output: str) -> tuple[str, str]:
    for marker in REJECT_PATTERNS:
        if marker in output:
            reason = next((line.strip() for line in output.splitlines() if marker in line), marker)
            return "REJECTED", reason
    return "OK", ""


def present_commands(running_config_snmp: str, commands: list[str]) -> dict[str, bool]:
    lines = {line.strip() for line in running_config_snmp.splitlines() if line.strip()}
    return {command: command in lines for command in commands}


def unwanted_commands(running_config_snmp: str) -> dict[str, bool]:
    lines = {line.strip() for line in running_config_snmp.splitlines() if line.strip()}
    return {command: command in lines for command in UNWANTED_TRAP_COMMANDS}


def enter_enable(session: ConsoleSession, secret: str) -> None:
    session.send("", 0.5)
    output = session.send("enable", 0.8)
    if "Password" in output:
        session.send(secret, 0.8)
    session.send("terminal length 0", 0.5)


def detect_trap_source(session: ConsoleSession, mgmt_ip: str, override: str = "") -> str:
    """管理口自动识别；IOL console 回显慢，第一次读不全（真机实测）就加长等待再读一次。"""
    if override:
        return override
    last: Exception | None = None
    for wait in (2.5, 5.0):
        try:
            return identify_trap_source(session.send("show ip interface brief", wait), mgmt_ip)
        except ValueError as exc:
            last = exc
    assert last is not None
    raise last


def run_check(
    device: str, session: ConsoleSession, community: str, mgmt_ip: str, trap_source_if: str = ""
) -> list[CommandResult]:
    trap_source = detect_trap_source(session, mgmt_ip, trap_source_if)
    commands = build_trap_commands(community, trap_source)
    running = session.send("show running-config | include snmp-server", 1.0)
    interface_running = session.send(f"show running-config interface {trap_source}", 1.0)
    session.send("show snmp", 1.0)
    present = present_commands(running, commands)
    results = [
        CommandResult(device, command, "PRESENT" if is_present else "MISSING", "")
        for command, is_present in present.items()
    ]
    results.append(
        CommandResult(
            device,
            "no snmp trap link-status",
            "PRESENT" if "no snmp trap link-status" in interface_running else "MISSING",
            f"interface {trap_source}",
        )
    )
    results.extend(
        CommandResult(device, command, "UNWANTED" if is_present else "ABSENT", "")
        for command, is_present in unwanted_commands(running).items()
    )
    return results


def run_apply(
    device: str,
    session: ConsoleSession,
    community: str,
    mgmt_ip: str,
    save: bool = False,
    trap_source_if: str = "",
) -> list[CommandResult]:
    trap_source = detect_trap_source(session, mgmt_ip, trap_source_if)
    commands = build_apply_commands(community, trap_source)
    results: list[CommandResult] = []
    session.send("configure terminal", 0.7)
    for command in commands:
        output = session.send(command, 0.8)
        status, reason = classify_command_output(output)
        if command in REMOVAL_COMMANDS and status == "REJECTED":
            status = "OK"
            reason = f"ignored removal rejection: {reason}"
        results.append(CommandResult(device, command, status, reason))
    session.send("end", 0.6)
    if save:
        output = session.send("write memory", 3.0)
        status, reason = classify_command_output(output)
        results.append(CommandResult(device, "write memory", status, reason))
    return results


def dry_run(device: str, community: str, trap_source_if: str) -> list[CommandResult]:
    return [CommandResult(device, command, "DRY-RUN") for command in build_apply_commands(community, trap_source_if)]


def print_results(results: list[CommandResult]) -> None:
    print("device\tstatus\tcommand\treason")
    for result in results:
        print(f"{result.device}\t{result.status}\t{mask_sensitive_command(result.command)}\t{result.reason}")


def mask_sensitive_command(command: str) -> str:
    return re.sub(r"(snmp-server host \S+ version 2c )\S+(\s+.*)?$", r"\1<SNMP_COMMUNITY>\2", command)


def make_session(host: str, port: int) -> ConsoleSession:
    return SocketConsole(host, port)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Configure SNMP trap settings on one lab IOS device via EVE console.")
    parser.add_argument("device", choices=sorted(DEFAULT_PORTS))
    parser.add_argument("--host", default=EVE_HOST)
    parser.add_argument("--port", type=int)
    parser.add_argument("--creds", default=str(CRED_PATH))
    parser.add_argument("--community")
    parser.add_argument("--trap-source-if", help="Override automatic management interface detection.")
    parser.add_argument("--check", action="store_true", help="Read-only check; connects to the device.")
    parser.add_argument("--apply", action="store_true", help="Apply configuration to the device.")
    parser.add_argument("--save", action="store_true", help="Run write memory after --apply.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    port = args.port or DEFAULT_PORTS[args.device]
    creds = read_creds(args.creds) if args.apply or args.check else {}
    community = args.community or creds.get("SNMP_COMMUNITY", "")
    if args.apply and not community:
        print("device_trap: SNMP community is required for --apply", file=sys.stderr)
        return 2
    if args.apply and not enable_secret_for(args.device, creds):
        print("device_trap: enable secret is required for --apply", file=sys.stderr)
        return 2

    if not args.apply and not args.check:
        trap_source = args.trap_source_if or "<auto-from-show-ip-interface-brief>"
        print_results(dry_run(args.device, community or "<SNMP_COMMUNITY>", trap_source))
        return 0

    session = make_session(args.host, port)
    try:
        enter_enable(session, enable_secret_for(args.device, creds))
        if args.check:
            results = run_check(
                args.device, session, community or "<SNMP_COMMUNITY>", MGMT_IPS[args.device], args.trap_source_if or ""
            )
        else:
            results = run_apply(
                args.device, session, community, MGMT_IPS[args.device], save=args.save, trap_source_if=args.trap_source_if or ""
            )
        print_results(results)
        return 0
    finally:
        session.close()


if __name__ == "__main__":
    raise SystemExit(main())
