from __future__ import annotations

from deploy.trap.zabbix_items import (
    FALLBACK_KEY,
    enabled_hosts_with_snmp,
    ensure_items_and_triggers,
    item_defs,
    recovery_expression,
    regexp_matches,
    rollback,
    trap_key,
    trigger_expression,
)


class FakeZabbix:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []
        self.items: dict[str, dict[str, str]] = {}
        self.triggers: dict[str, dict[str, str]] = {}

    def call(self, method: str, params):
        self.calls.append((method, params))
        if method == "host.get":
            return [
                {
                    "hostid": "101",
                    "host": "A1-viosl2",
                    "status": "0",
                    "interfaces": [{"type": "2", "ip": "192.0.2.54"}],
                },
                {
                    "hostid": "102",
                    "host": "NoSnmp",
                    "status": "0",
                    "interfaces": [{"type": "1", "ip": "192.0.2.99"}],
                },
            ]
        if method == "item.get":
            return list(self.items.values())
        if method == "trigger.get":
            return list(self.triggers.values())
        if method == "item.create":
            key = params["key_"]
            itemid = str(200 + len(self.items))
            self.items[key] = {"itemid": itemid, "name": params["name"], "key_": key}
            return {"itemids": [itemid]}
        if method == "trigger.create":
            triggerid = str(300 + len(self.triggers))
            self.triggers[params["description"]] = {
                "triggerid": triggerid,
                "description": params["description"],
                "expression": params["expression"],
                "tags": params["tags"],
                "status": "0",
            }
            return {"triggerids": [triggerid]}
        if method in {"item.update", "trigger.update"}:
            return {"ok": True}
        if method == "trigger.delete":
            return params
        if method == "item.delete":
            return params
        raise AssertionError(method)


def test_enabled_hosts_with_snmp_filters_to_enabled_snmp_interfaces() -> None:
    api = FakeZabbix()
    hosts = enabled_hosts_with_snmp(api)
    assert [host["host"] for host in hosts] == ["A1-viosl2"]


def test_item_definitions_include_name_and_numeric_oid_regexes_and_fallback() -> None:
    defs = item_defs()
    keys = [definition["key_"] for definition in defs]
    assert any(regexp_matches(key.removeprefix("snmptrap[").removesuffix("]"), "IF-MIB::linkDown") for key in keys)
    assert any(regexp_matches(key.removeprefix("snmptrap[").removesuffix("]"), "1.3.6.1.6.3.1.1.5.3") for key in keys)
    assert any("ospfIfStateChange" in key for key in keys)
    assert any("ospfNbrStateChange" in key for key in keys)
    assert FALLBACK_KEY in keys
    config = next(definition for definition in defs if definition["event"] == "configChange")
    assert config["trigger"] is False


def test_ensure_items_and_triggers_dry_run_creates_no_api_objects() -> None:
    api = FakeZabbix()
    actions = ensure_items_and_triggers(api, apply=False)
    assert any(action.kind == "item" and action.action == "create" for action in actions)
    assert any(action.kind == "trigger" and action.action == "create" for action in actions)
    assert not api.items
    assert not api.triggers


def test_ensure_items_and_triggers_apply_is_idempotent_update_on_second_run() -> None:
    api = FakeZabbix()
    first = ensure_items_and_triggers(api, apply=True)
    second = ensure_items_and_triggers(api, apply=True)
    assert any(action.action == "create" for action in first)
    assert any(action.action == "update" for action in second)
    assert not any(method == "item.update" for method, _ in api.calls)  # 已有的 item 不动（模板继承项 update 会报 readonly）
    assert any(method == "trigger.update" for method, _ in api.calls)
    assert not any("configChange" in params.get("description", "") for method, params in api.calls if method == "trigger.create")


def test_existing_config_trigger_is_disabled_not_deleted() -> None:
    api = FakeZabbix()
    api.triggers["Trap: configChange received on {HOST.NAME}"] = {
        "triggerid": "399",
        "description": "Trap: configChange received on {HOST.NAME}",
        "expression": "old",
        "tags": [{"tag": "source", "value": "trap"}],
        "status": "0",
    }
    actions = ensure_items_and_triggers(api, apply=True)
    assert any(action.action == "disable" and "configChange" in action.name for action in actions)
    assert ("trigger.update", {"triggerid": "399", "status": 1}) in api.calls
    assert not any(method == "trigger.delete" for method, _ in api.calls)


def test_trigger_expressions_use_90s_nodata_window() -> None:
    key = trap_key("linkDown")
    assert trigger_expression("A1-viosl2", key) == "nodata(/A1-viosl2/snmptrap[linkDown],90s)=0"
    assert recovery_expression("A1-viosl2", key) == "nodata(/A1-viosl2/snmptrap[linkDown],90s)=1"


def test_rollback_dry_run_targets_source_trap_triggers_and_snmptrap_items() -> None:
    api = FakeZabbix()
    ensure_items_and_triggers(api, apply=True)
    actions = rollback(api, apply=False)
    assert any(action.kind == "trigger" and action.action == "delete" for action in actions)
    assert any(action.kind == "item" and action.action == "delete" for action in actions)
    assert not any(method == "trigger.delete" for method, _ in api.calls)
