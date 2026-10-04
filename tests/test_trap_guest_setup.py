from __future__ import annotations

from deploy.trap.guest_setup import apply_plan, dry_run_results, snmptrapd_conf


class FakeRunner:
    def __init__(self, replies: dict[str, tuple[int, str, str]] | None = None) -> None:
        self.replies = replies or {}
        self.commands: list[str] = []
        self.files: dict[str, str] = {}

    def run(self, command: str) -> tuple[int, str, str]:
        self.commands.append(command)
        return self.replies.get(command, (0, "", ""))

    def put_text(self, path: str, content: str, mode: int = 0o644) -> None:
        self.files[path] = content
        self.commands.append(f"PUT {path} {oct(mode)}")

    def close(self) -> None:
        pass


def ready_runner() -> FakeRunner:
    return FakeRunner(
        {
            "dpkg-query -W -f='${Status}\\n' snmptrapd snmp 2>/dev/null | grep -c 'install ok installed'": (
                0,
                "2\n",
                "",
            ),
            "ps -eo user:32,comm | awk '$2==\"zabbix_server\" {print $1; exit}'": (0, "zabbix\n", ""),
        }
    )


def test_snmptrapd_conf_uses_authcommunity_without_disableauthorization() -> None:
    conf = snmptrapd_conf("<SNMP_COMMUNITY>")
    assert "authCommunity log,execute,net <SNMP_COMMUNITY>" in conf
    assert "disableAuthorization" not in conf
    assert "traphandle default /usr/local/bin/zabbix_trap_handler.py" in conf


def test_dry_run_does_not_need_runner_or_password() -> None:
    results = dry_run_results("<SNMP_COMMUNITY>", yes_restart=False)
    assert all(result.status == "DRY-RUN" for result in results)
    assert any(result.detail == "needs --yes-restart" for result in results)


def test_apply_plan_records_expected_commands_and_skips_restart_without_flag() -> None:
    runner = ready_runner()
    results = apply_plan(runner, "<SNMP_COMMUNITY>", yes_restart=False)
    assert "/etc/snmp/snmptrapd.conf" in runner.files
    assert "/usr/local/bin/zabbix_trap_handler.py" in runner.files
    assert any("apt-cache policy snmptrapd snmp" == command for command in runner.commands)
    assert not any(command == "systemctl restart zabbix-server" for command in runner.commands)
    assert any(result.name == "zabbix-server restart" and result.status == "SKIPPED" for result in results)


def test_apply_plan_restarts_zabbix_only_when_confirmed() -> None:
    runner = FakeRunner()
    apply_plan(runner, "<SNMP_COMMUNITY>", yes_restart=True)
    assert "systemctl restart zabbix-server" in runner.commands


def test_zabbix_conf_step_backs_up_before_editing() -> None:
    runner = ready_runner()
    orig_run = runner.run

    def run(command: str) -> tuple[int, str, str]:
        rc, out, err = orig_run(command)
        # 「已配置好」的检查（grep -Eq ... && grep -Eq ...）要返回未配置，才会走到备份+修改那一步
        if command.startswith("grep -Eq") and "StartSNMPTrapper=1" in command:
            return 1, "", ""
        return rc, out, err

    runner.run = run  # type: ignore[method-assign]
    apply_plan(runner, "<SNMP_COMMUNITY>", yes_restart=False)
    zabbix_commands = [cmd for cmd in runner.commands if "zabbix_server.conf.bak-trap" in cmd]
    assert zabbix_commands
    assert "StartSNMPTrapper=1" in zabbix_commands[0]
    assert "SNMPTrapperFile=/var/log/snmptrap/snmptrap.log" in zabbix_commands[0]
