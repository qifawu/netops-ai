from __future__ import annotations

from datetime import datetime

from deploy.trap.zabbix_trap_handler import format_zabbix_trap, handle, parse_source_ip


def test_parse_source_ip_from_udp_line_not_hostname() -> None:
    assert parse_source_ip("UDP: [192.0.2.54]:52034->[192.0.2.128]:162") == "192.0.2.54"


def test_format_link_down_trap_for_zabbix_trapper() -> None:
    text = """A1-viosl2
UDP: [192.0.2.54]:52034->[192.0.2.128]:162
DISMAN-EVENT-MIB::sysUpTimeInstance 123
SNMPv2-MIB::snmpTrapOID.0 IF-MIB::linkDown
IF-MIB::ifIndex.2 2
IF-MIB::ifDescr.2 Ethernet0/1
IF-MIB::ifAdminStatus.2 up
IF-MIB::ifOperStatus.2 down
"""
    out = format_zabbix_trap(text, datetime(2026, 9, 24, 10, 11, 12))
    assert out.startswith("20260924.101112 ZBXTRAP 192.0.2.54\nPDU INFO:")
    assert "SNMPv2-MIB::snmpTrapOID.0 IF-MIB::linkDown" in out
    assert "IF-MIB::ifOperStatus.2 down" in out


def test_format_cold_start_trap() -> None:
    text = """V1-vios
UDP: [192.0.2.50]:41000->[192.0.2.128]:162
SNMPv2-MIB::snmpTrapOID.0 SNMPv2-MIB::coldStart
"""
    out = format_zabbix_trap(text, datetime(2026, 9, 24, 10, 0, 0))
    assert "ZBXTRAP 192.0.2.50" in out
    assert "coldStart" in out


def test_format_cisco_config_change_trap_with_numeric_enterprise_oids() -> None:
    text = """A2-viosl2
UDP: [192.0.2.55]:52034->[192.0.2.128]:162
SNMPv2-MIB::snmpTrapOID.0 1.3.6.1.4.1.9.9.43.2.0.1
1.3.6.1.4.1.9.9.43.1.1.6.1.3.7 2
1.3.6.1.4.1.9.9.43.1.1.6.1.8.7 5
"""
    out = format_zabbix_trap(text, datetime(2026, 9, 24, 10, 0, 1))
    assert "ZBXTRAP 192.0.2.55" in out
    assert "1.3.6.1.4.1.9.9.43.2.0.1" in out
    assert "1.3.6.1.4.1.9.9.43.1.1.6.1.8.7 5" in out


def test_handle_appends_to_requested_file(tmp_path) -> None:
    trap_log = tmp_path / "snmptrap.log"
    assert handle("h\nUDP: [192.0.2.56]:1->[192.0.2.128]:162\nOID value\n", trap_log) == 0
    assert "ZBXTRAP 192.0.2.56" in trap_log.read_text(encoding="utf-8")


def test_iso_root_is_normalized_to_numeric_oid() -> None:
    from deploy.trap.zabbix_trap_handler import format_zabbix_trap

    text = "h\nUDP: [192.0.2.50]:1->[192.0.2.128]:162\niso.3.6.1.6.3.1.1.4.1.0 iso.3.6.1.6.3.1.1.5.3\n"
    out = format_zabbix_trap(text)
    assert "1.3.6.1.6.3.1.1.4.1.0 1.3.6.1.6.3.1.1.5.3" in out
    assert "iso." not in out
