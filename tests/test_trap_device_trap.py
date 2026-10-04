from __future__ import annotations

from deploy.trap.device_trap import (
    build_apply_commands,
    build_trap_commands,
    classify_command_output,
    identify_trap_source,
    mask_sensitive_command,
    parse_creds,
    present_commands,
    run_apply,
    run_check,
)


class FakeConsole:
    def __init__(self, replies: dict[str, str]) -> None:
        self.replies = replies
        self.sent: list[str] = []

    def send(self, line: str, wait: float = 0.8) -> str:
        self.sent.append(line)
        return self.replies.get(line, "")

    def close(self) -> None:
        pass


def test_parse_creds_from_ini_text_without_real_file() -> None:
    creds = parse_creds(
        """
[SNMP]
SNMP_COMMUNITY = <SNMP_COMMUNITY>
DEVICE_ENABLE_SECRET = <ENABLE_SECRET>
V2_ENABLE_SECRET = <V2_ENABLE_SECRET>
"""
    )
    assert creds["SNMP_COMMUNITY"] == "<SNMP_COMMUNITY>"
    assert creds["DEVICE_ENABLE_SECRET"] == "<ENABLE_SECRET>"
    assert creds["V2_ENABLE_SECRET"] == "<V2_ENABLE_SECRET>"


def test_identify_trap_source_iol_ethernet() -> None:
    output = """
Interface              IP-Address      OK? Method Status                Protocol
Ethernet0/0            192.0.2.54       YES manual up                    up
Ethernet0/1            unassigned      YES unset  administratively down down
"""
    assert identify_trap_source(output, "192.0.2.54") == "Ethernet0/0"


def test_identify_trap_source_vios_gigabit() -> None:
    output = """
Interface                  IP-Address      OK? Method Status                Protocol
GigabitEthernet0/0         192.0.2.50       YES NVRAM  up                    up
GigabitEthernet0/1         unassigned      YES NVRAM  administratively down down
"""
    assert identify_trap_source(output, "192.0.2.50") == "GigabitEthernet0/0"


def test_rejected_command_is_reported_without_stopping_later_commands() -> None:
    session = FakeConsole(
        {
            "show ip interface brief": "Ethernet0/0 192.0.2.54 YES manual up up",
            "no snmp-server enable traps config": "% Invalid input detected at '^' marker.",
            "no snmp-server enable traps syslog": "",
            "snmp-server enable traps snmp linkdown linkup coldstart warmstart": "",
            "snmp-server enable traps bgp": "% Invalid input detected at '^' marker.",
            "snmp-server enable traps ospf state-change": "",
            "snmp-server host 192.0.2.128 version 2c <SNMP_COMMUNITY> snmp bgp ospf": "",
            "snmp-server trap-source Ethernet0/0": "",
            "snmp-server ifindex persist": "",
            "interface Ethernet0/0": "",
            "no snmp trap link-status": "",
            "exit": "",
        }
    )
    results = run_apply("A1", session, "<SNMP_COMMUNITY>", "192.0.2.54")
    assert [r.command for r in results][-1] == "exit"
    assert [r.command for r in results[:2]] == [
        "no snmp-server enable traps config",
        "no snmp-server enable traps syslog",
    ]
    assert any(r.command == "no snmp-server enable traps config" and r.status == "OK" for r in results)
    assert any(r.command == "snmp-server enable traps bgp" and r.status == "REJECTED" for r in results)
    assert "end" in session.sent


def test_check_reports_present_and_missing_for_idempotency_visibility() -> None:
    commands = build_trap_commands("<SNMP_COMMUNITY>", "Ethernet0/0")
    session = FakeConsole(
        {
            "show ip interface brief": "Ethernet0/0 192.0.2.54 YES manual up up",
            "show running-config | include snmp-server": "\n".join(
                [*commands[:2], "snmp-server enable traps config"]
            ),
            "show running-config interface Ethernet0/0": "interface Ethernet0/0\n no snmp trap link-status\n",
            "show snmp": "SNMP agent enabled",
        }
    )
    results = run_check("A1", session, "<SNMP_COMMUNITY>", "192.0.2.54")
    assert [r.status for r in results[:2]] == ["PRESENT", "PRESENT"]
    assert any(r.status == "MISSING" for r in results)
    assert any(r.command == "snmp-server enable traps config" and r.status == "UNWANTED" for r in results)
    assert any(r.command == "snmp-server enable traps syslog" and r.status == "ABSENT" for r in results)
    assert any(r.command == "no snmp trap link-status" and r.status == "PRESENT" for r in results)


def test_apply_command_sequence_is_narrowed_and_scoped_to_mgmt_interface() -> None:
    commands = build_apply_commands("<SNMP_COMMUNITY>", "GigabitEthernet0/0")
    assert "snmp-server enable traps config" not in commands
    assert "snmp-server enable traps syslog" not in commands
    assert commands[:2] == ["no snmp-server enable traps config", "no snmp-server enable traps syslog"]
    assert "snmp-server host 192.0.2.128 version 2c <SNMP_COMMUNITY> snmp bgp ospf" in commands
    assert commands[-3:] == ["interface GigabitEthernet0/0", "no snmp trap link-status", "exit"]


def test_present_commands_exact_line_match() -> None:
    commands = ["snmp-server host 192.0.2.128 version 2c <SNMP_COMMUNITY> snmp bgp ospf"]
    assert present_commands(" snmp-server host 192.0.2.128 version 2c <SNMP_COMMUNITY> snmp bgp ospf\n", commands)[commands[0]]


def test_classify_invalid_incomplete_unknown() -> None:
    assert classify_command_output("% Incomplete command.")[0] == "REJECTED"
    assert classify_command_output("% Unknown command")[0] == "REJECTED"
    assert classify_command_output("ok")[0] == "OK"


def test_print_layer_masks_runtime_community_value() -> None:
    assert (
        mask_sensitive_command("snmp-server host 192.0.2.128 version 2c <runtime> snmp bgp ospf")
        == "snmp-server host 192.0.2.128 version 2c <SNMP_COMMUNITY> snmp bgp ospf"
    )


def test_parse_creds_strips_inline_comment_and_keeps_percent() -> None:
    creds = parse_creds("[SNMP]\nSNMP_COMMUNITY=abc%def  # 注释别当成 community\nDEVICE_ENABLE_SECRET=x%y\n")
    assert creds["SNMP_COMMUNITY"] == "abc%def"
    assert creds["DEVICE_ENABLE_SECRET"] == "x%y"
