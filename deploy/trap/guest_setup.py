from __future__ import annotations

import argparse
import os
import shlex
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

GUEST_HOST = "192.0.2.128"
GUEST_USER = "root"
REMOTE_HANDLER = "/usr/local/bin/zabbix_trap_handler.py"
TRAP_LOG_DIR = "/var/log/snmptrap"
TRAP_LOG_FILE = f"{TRAP_LOG_DIR}/snmptrap.log"
ZABBIX_CONF = "/etc/zabbix/zabbix_server.conf"
ZABBIX_CONF_BACKUP = "/etc/zabbix/zabbix_server.conf.bak-trap"


class Runner(Protocol):
    def run(self, command: str) -> tuple[int, str, str]: ...

    def put_text(self, path: str, content: str, mode: int = 0o644) -> None: ...

    def close(self) -> None: ...


@dataclass
class StepResult:
    name: str
    status: str
    detail: str


class ParamikoRunner:
    def __init__(self, host: str, user: str, password: str) -> None:
        import paramiko

        self._ssh = paramiko.SSHClient()
        self._ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        self._ssh.connect(hostname=host, username=user, password=password, timeout=15)

    def run(self, command: str) -> tuple[int, str, str]:
        stdin, stdout, stderr = self._ssh.exec_command(command)
        del stdin
        return stdout.channel.recv_exit_status(), stdout.read().decode(errors="replace"), stderr.read().decode(
            errors="replace"
        )

    def put_text(self, path: str, content: str, mode: int = 0o644) -> None:
        sftp = self._ssh.open_sftp()
        tmp_path = f"{path}.tmp-trap"
        try:
            with sftp.file(tmp_path, "w") as file_obj:
                file_obj.write(content)
            sftp.chmod(tmp_path, mode)
            self.run(f"mv {shlex.quote(tmp_path)} {shlex.quote(path)}")
        finally:
            sftp.close()

    def close(self) -> None:
        self._ssh.close()


class DryRunRunner:
    def __init__(self) -> None:
        self.commands: list[str] = []
        self.files: list[str] = []

    def run(self, command: str) -> tuple[int, str, str]:
        self.commands.append(command)
        return 0, "", ""

    def put_text(self, path: str, content: str, mode: int = 0o644) -> None:
        del content
        self.files.append(f"write {path} mode={oct(mode)}")

    def close(self) -> None:
        pass


def shell_single_quote(value: str) -> str:
    return shlex.quote(value)


def snmptrapd_conf(community: str) -> str:
    return "\n".join(
        [
            f"authCommunity log,execute,net {community}",
            f"traphandle default {REMOTE_HANDLER}",
            "",
        ]
    )


def handler_source() -> str:
    return Path(__file__).with_name("zabbix_trap_handler.py").read_text(encoding="utf-8")


def status_from_rc(rc: int, ok_detail: str, fail_detail: str) -> StepResult:
    if rc == 0:
        return StepResult(ok_detail, "OK", "")
    return StepResult(ok_detail, "FAILED", fail_detail)


def ensure_packages(runner: Runner) -> StepResult:
    rc, _, err = runner.run("apt-cache policy snmptrapd snmp")
    if rc != 0:
        return StepResult("packages", "FAILED", f"apt-cache policy failed: {err.strip()}")
    rc, out, _ = runner.run("dpkg-query -W -f='${Status}\\n' snmptrapd snmp 2>/dev/null | grep -c 'install ok installed'")
    if rc == 0 and out.strip() == "2":
        return StepResult("packages", "SKIPPED", "snmptrapd and snmp already installed")
    rc, _, err = runner.run("DEBIAN_FRONTEND=noninteractive apt-get install -y snmptrapd snmp")
    return StepResult("packages", "OK" if rc == 0 else "FAILED", err.strip())


def ensure_snmptrapd_conf(runner: Runner, community: str) -> StepResult:
    expected = snmptrapd_conf(community)
    rc, out, _ = runner.run("test -f /etc/snmp/snmptrapd.conf && cat /etc/snmp/snmptrapd.conf")
    if rc == 0 and out == expected:
        return StepResult("snmptrapd.conf", "SKIPPED", "already matches")
    runner.put_text("/etc/snmp/snmptrapd.conf", expected, 0o644)
    return StepResult("snmptrapd.conf", "OK", "written")


def ensure_handler(runner: Runner) -> StepResult:
    runner.put_text(REMOTE_HANDLER, handler_source(), 0o755)
    rc, _, err = runner.run(f"chmod +x {REMOTE_HANDLER}")
    return StepResult("trap handler", "OK" if rc == 0 else "FAILED", err.strip())


def ensure_log_dir(runner: Runner) -> StepResult:
    rc, out, _ = runner.run("ps -eo user:32,comm | awk '$2==\"zabbix_server\" {print $1; exit}'")
    user = out.strip() or "zabbix"
    # snmptrapd 在 Ubuntu 上以 Debian-snmp 用户跑 traphandle，日志属主是 zabbix：
    # 让 Debian-snmp 进 zabbix 组，目录/文件对组可写（真机实测：只 chown 会 Permission denied）
    command = (
        f"mkdir -p {TRAP_LOG_DIR} && touch {TRAP_LOG_FILE} && chown -R {shell_single_quote(user)}:{shell_single_quote(user)} {TRAP_LOG_DIR} "
        f"&& chmod 775 {TRAP_LOG_DIR} && chmod 664 {TRAP_LOG_FILE} "
        f"&& (id Debian-snmp >/dev/null 2>&1 && usermod -aG {shell_single_quote(user)} Debian-snmp || true) "
        f"&& systemctl restart snmptrapd || true"
    )
    rc, _, err = runner.run(command)
    return StepResult("trap log dir", "OK" if rc == 0 else "FAILED", err.strip())


def ensure_zabbix_conf(runner: Runner) -> StepResult:
    check = (
        f"grep -Eq '^StartSNMPTrapper=1$' {ZABBIX_CONF} "
        f"&& grep -Eq '^SNMPTrapperFile={TRAP_LOG_FILE}$' {ZABBIX_CONF}"
    )
    rc, _, _ = runner.run(check)
    if rc == 0:
        return StepResult("zabbix_server.conf", "SKIPPED", "already configured")
    command = (
        f"test -f {ZABBIX_CONF_BACKUP} || cp {ZABBIX_CONF} {ZABBIX_CONF_BACKUP}; "
        f"grep -Eq '^#?\\s*StartSNMPTrapper=' {ZABBIX_CONF} "
        f"&& sed -i -E 's@^#?\\s*StartSNMPTrapper=.*@StartSNMPTrapper=1@' {ZABBIX_CONF} "
        f"|| printf '\\nStartSNMPTrapper=1\\n' >> {ZABBIX_CONF}; "
        f"grep -Eq '^#?\\s*SNMPTrapperFile=' {ZABBIX_CONF} "
        f"&& sed -i -E 's@^#?\\s*SNMPTrapperFile=.*@SNMPTrapperFile={TRAP_LOG_FILE}@' {ZABBIX_CONF} "
        f"|| printf '\\nSNMPTrapperFile={TRAP_LOG_FILE}\\n' >> {ZABBIX_CONF}"
    )
    rc, _, err = runner.run(command)
    return StepResult("zabbix_server.conf", "OK" if rc == 0 else "FAILED", err.strip())


def ensure_services(runner: Runner, yes_restart: bool) -> list[StepResult]:
    results: list[StepResult] = []
    rc, _, err = runner.run("systemctl enable --now snmptrapd")
    results.append(StepResult("snmptrapd service", "OK" if rc == 0 else "FAILED", err.strip()))
    if yes_restart:
        rc, _, err = runner.run("systemctl restart zabbix-server")
        results.append(StepResult("zabbix-server restart", "OK" if rc == 0 else "FAILED", err.strip()))
    else:
        results.append(StepResult("zabbix-server restart", "SKIPPED", "needs --yes-restart"))
    return results


def verify_services(runner: Runner) -> list[StepResult]:
    checks = [
        ("udp/162 listener", "ss -ulnp | grep :162 | grep snmptrapd"),
        ("snmptrapd active", "systemctl is-active snmptrapd"),
        ("zabbix-server active", "systemctl is-active zabbix-server"),
    ]
    results = []
    for name, command in checks:
        rc, out, err = runner.run(command)
        results.append(StepResult(name, "OK" if rc == 0 else "FAILED", (out or err).strip()))
    return results


def apply_plan(runner: Runner, community: str, yes_restart: bool) -> list[StepResult]:
    results = [
        ensure_packages(runner),
        ensure_snmptrapd_conf(runner, community),
        ensure_handler(runner),
        ensure_log_dir(runner),
        ensure_zabbix_conf(runner),
    ]
    results.extend(ensure_services(runner, yes_restart))
    results.extend(verify_services(runner))
    return results


def dry_run_results(community: str, yes_restart: bool) -> list[StepResult]:
    restart = "will restart zabbix-server" if yes_restart else "needs --yes-restart"
    return [
        StepResult("packages", "DRY-RUN", "apt-cache policy; install snmptrapd snmp if missing"),
        StepResult("snmptrapd.conf", "DRY-RUN", f"write authCommunity for community length {len(community)}"),
        StepResult("trap handler", "DRY-RUN", f"install {REMOTE_HANDLER}"),
        StepResult("trap log dir", "DRY-RUN", f"ensure {TRAP_LOG_DIR} writable by zabbix-server user"),
        StepResult("zabbix_server.conf", "DRY-RUN", f"backup to {ZABBIX_CONF_BACKUP}; enable SNMP trapper"),
        StepResult("services", "DRY-RUN", restart),
        StepResult("verify", "DRY-RUN", "check UDP/162 and service states"),
    ]


def print_results(results: list[StepResult]) -> None:
    print("step\tstatus\tdetail")
    for result in results:
        print(f"{result.name}\t{result.status}\t{result.detail}")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Install snmptrapd and connect it to zabbix-server on the EVE guest.")
    parser.add_argument("--host", default=GUEST_HOST)
    parser.add_argument("--user", default=GUEST_USER)
    parser.add_argument("--community", default=os.environ.get("SNMP_COMMUNITY", ""))
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--yes-restart", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    if not args.community:
        print("guest_setup: --community or SNMP_COMMUNITY is required", file=sys.stderr)
        return 2
    if not args.apply:
        print_results(dry_run_results(args.community, args.yes_restart))
        return 0
    password = os.environ.get("EVE_GUEST_PASSWORD")
    if not password:
        print("guest_setup: EVE_GUEST_PASSWORD is required for --apply", file=sys.stderr)
        return 2
    runner = ParamikoRunner(args.host, args.user, password)
    try:
        print_results(apply_plan(runner, args.community, args.yes_restart))
        return 0
    finally:
        runner.close()


if __name__ == "__main__":
    raise SystemExit(main())
