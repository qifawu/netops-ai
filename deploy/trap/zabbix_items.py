from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from deploy.trap.device_trap import CRED_PATH, parse_creds

ITEM_TYPE_SNMP_TRAP = 17
VALUE_TYPE_TEXT = 4
INTERFACE_TYPE_SNMP = 2
HOST_STATUS_ENABLED = 0
TRAP_RECOVERY_SECONDS = 90  # Greater than the 60s polling interval, so pollers get a chance to confirm state.

EVENTS = [
    ("linkDown", "linkDown", r"(?:linkDown|1\.3\.6\.1\.6\.3\.1\.1\.5\.3)", 2),
    ("linkUp", "linkUp", r"(?:linkUp|1\.3\.6\.1\.6\.3\.1\.1\.5\.4)", 2),
    ("bgpBackwardTransition", "bgpBackwardTransition", r"(?:bgpBackwardTransition|1\.3\.6\.1\.2\.1\.15\.7\.2)", 3),
    ("ospfIfStateChange", "ospfIfStateChange", r"(?:ospfIfStateChange|1\.3\.6\.1\.2\.1\.14\.16\.2\.16)", 3),
    ("ospfNbrStateChange", "ospfNbrStateChange", r"(?:ospfNbrStateChange|1\.3\.6\.1\.2\.1\.14\.16\.2\.2)", 3),
    ("coldStart", "coldStart", r"(?:coldStart|1\.3\.6\.1\.6\.3\.1\.1\.5\.1)", 2),
    ("warmStart", "warmStart", r"(?:warmStart|1\.3\.6\.1\.6\.3\.1\.1\.5\.2)", 2),
    ("configChange", "configChange", r"(?:configChange|ciscoConfigManEvent|1\.3\.6\.1\.4\.1\.9\.9\.43\.2\.0\.1)", 2),
]

FALLBACK_KEY = "snmptrap.fallback"
FALLBACK_REGEX = r".*"


class ZabbixAPI(Protocol):
    def call(self, method: str, params: dict[str, Any] | list[Any]) -> Any: ...


@dataclass
class PlanAction:
    action: str
    kind: str
    host: str
    name: str
    detail: str = ""


class JsonRpcZabbixAPI:
    def __init__(self, url: str, user: str, password: str) -> None:
        self.url = url.rstrip("/") + "/api_jsonrpc.php"
        self.auth: str | None = None
        self._id = 0
        self.auth = self.call("user.login", {"username": user, "password": password})

    def call(self, method: str, params: dict[str, Any] | list[Any]) -> Any:
        self._id += 1
        payload = {"jsonrpc": "2.0", "method": method, "params": params, "id": self._id}
        if self.auth:
            payload["auth"] = self.auth
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(self.url, data=data, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as response:
            result = json.loads(response.read().decode("utf-8"))
        if "error" in result:
            raise RuntimeError(result["error"])
        return result["result"]


def read_creds(path: str | Path) -> dict[str, str]:
    return parse_creds(Path(path).read_text(encoding="utf-8"))


def trap_key(regex: str) -> str:
    return f"snmptrap[{regex}]"


def item_defs() -> list[dict[str, Any]]:
    items = [
        {
            "name": f"Trap: {name}",
            "key_": trap_key(regex),
            "event": event,
            "trigger": trigger and event != "configChange",
            "priority": priority,
        }
        for event, name, regex, priority in EVENTS
        for trigger in [event != "linkUp"]
    ]
    items.append({"name": "Trap: fallback", "key_": FALLBACK_KEY, "event": "fallback", "trigger": False, "priority": 0})
    return items


def enabled_hosts_with_snmp(api: ZabbixAPI) -> list[dict[str, Any]]:
    hosts = api.call(
        "host.get",
        {
            "output": ["hostid", "host", "name", "status"],
            "selectInterfaces": ["interfaceid", "type", "ip", "dns", "main"],
            "filter": {"status": str(HOST_STATUS_ENABLED)},
        },
    )
    result = []
    for host in hosts:
        interfaces = host.get("interfaces", [])
        if any(str(interface.get("type")) == str(INTERFACE_TYPE_SNMP) for interface in interfaces):
            result.append(host)
    return result


def existing_items(api: ZabbixAPI, hostid: str) -> dict[str, dict[str, Any]]:
    items = api.call(
        "item.get",
        {
            "output": ["itemid", "name", "key_"],
            "hostids": hostid,
            "search": {"key_": "snmptrap"},
        },
    )
    return {item["key_"]: item for item in items}


def existing_triggers(api: ZabbixAPI, hostid: str) -> dict[str, dict[str, Any]]:
    triggers = api.call(
        "trigger.get",
        {
            "output": ["triggerid", "description", "expression", "status"],
            "hostids": hostid,
            "selectTags": "extend",
            "filter": {"tags": [{"tag": "source", "value": "trap"}]},
        },
    )
    return {trigger["description"]: trigger for trigger in triggers}


def snmp_interface_id(host: dict[str, Any]) -> str:
    """SNMP trap 监控项必须挂在主机的 SNMP 接口上（真机实测：缺 interfaceid 会被 API 拒）。"""
    interfaces = [i for i in host.get("interfaces", []) if str(i.get("type")) == "2"]
    interfaces.sort(key=lambda i: str(i.get("main")) != "1")
    if not interfaces:
        raise RuntimeError(f"host {host.get('host')} has no SNMP interface")
    return str(interfaces[0].get("interfaceid", ""))


def item_payload(hostid: str, definition: dict[str, Any], interfaceid: str = "") -> dict[str, Any]:
    return {
        "hostid": hostid,
        "interfaceid": interfaceid,
        "name": definition["name"],
        "key_": definition["key_"],
        "type": ITEM_TYPE_SNMP_TRAP,
        "value_type": VALUE_TYPE_TEXT,
    }


def trigger_expression(host: str, key: str) -> str:
    return f"nodata(/{host}/{key},{TRAP_RECOVERY_SECONDS}s)=0"


def recovery_expression(host: str, key: str) -> str:
    return f"nodata(/{host}/{key},{TRAP_RECOVERY_SECONDS}s)=1"


def trigger_payload(host: str, definition: dict[str, Any]) -> dict[str, Any]:
    event = definition["event"]
    return {
        "description": f"Trap: {event} received on {{HOST.NAME}}",
        "expression": trigger_expression(host, definition["key_"]),
        "recovery_mode": 1,
        "recovery_expression": recovery_expression(host, definition["key_"]),
        "priority": definition["priority"],
        "tags": [{"tag": "source", "value": "trap"}],
    }


def is_config_trigger(description: str) -> bool:
    return description.startswith("Trap: configChange received on ")


def disable_config_triggers(
    api: ZabbixAPI, host_name: str, triggers: dict[str, dict[str, Any]], apply: bool
) -> list[PlanAction]:
    actions: list[PlanAction] = []
    for trigger in triggers.values():
        if not is_config_trigger(trigger.get("description", "")):
            continue
        if str(trigger.get("status", "0")) == "1":
            actions.append(PlanAction("skip", "trigger", host_name, trigger["description"], "already disabled"))
            continue
        actions.append(PlanAction("disable", "trigger", host_name, trigger["description"], "status=1"))
        if apply:
            api.call("trigger.update", {"triggerid": trigger["triggerid"], "status": 1})
    return actions


def ensure_items_and_triggers(api: ZabbixAPI, apply: bool = False) -> list[PlanAction]:
    actions: list[PlanAction] = []
    for host in enabled_hosts_with_snmp(api):
        hostid = host["hostid"]
        host_name = host["host"]
        by_key = existing_items(api, hostid)
        by_trigger = existing_triggers(api, hostid)
        actions.extend(disable_config_triggers(api, host_name, by_trigger, apply))
        for definition in item_defs():
            key = definition["key_"]
            if key in by_key:
                actions.append(PlanAction("skip", "item", host_name, definition["name"], key))
                # 已存在（含模板继承的 snmptrap.fallback，item.update 会报 readonly）就不动，重复跑只补缺的
                itemid = by_key[key]["itemid"]
            else:
                actions.append(PlanAction("create", "item", host_name, definition["name"], key))
                if apply:
                    created = api.call("item.create", item_payload(hostid, definition, snmp_interface_id(host)))
                    itemid = created["itemids"][0]
                else:
                    itemid = ""
            del itemid
            if not definition["trigger"]:
                continue
            payload = trigger_payload(host_name, definition)
            if payload["description"] in by_trigger:
                actions.append(PlanAction("skip", "trigger", host_name, payload["description"], "source=trap"))
                if apply:
                    api.call(
                        "trigger.update",
                        {"triggerid": by_trigger[payload["description"]]["triggerid"], **payload},
                    )
                    actions[-1].action = "update"
            else:
                actions.append(PlanAction("create", "trigger", host_name, payload["description"], "source=trap"))
                if apply:
                    api.call("trigger.create", payload)
    return actions


def rollback(api: ZabbixAPI, apply: bool = False) -> list[PlanAction]:
    actions: list[PlanAction] = []
    for host in enabled_hosts_with_snmp(api):
        hostid = host["hostid"]
        host_name = host["host"]
        triggers = existing_triggers(api, hostid)
        trigger_ids = [trigger["triggerid"] for trigger in triggers.values()]
        for trigger in triggers.values():
            actions.append(PlanAction("delete", "trigger", host_name, trigger["description"], "source=trap"))
        if apply and trigger_ids:
            api.call("trigger.delete", trigger_ids)
        items = existing_items(api, hostid)
        item_ids = [
            item["itemid"]
            for key, item in items.items()
            if key == FALLBACK_KEY or key.startswith("snmptrap[") or key.startswith("snmptrap")
        ]
        for key, item in items.items():
            if item["itemid"] in item_ids:
                actions.append(PlanAction("delete", "item", host_name, item.get("name", key), key))
        if apply and item_ids:
            api.call("item.delete", item_ids)
    return actions


def print_actions(actions: list[PlanAction]) -> None:
    print("action\tkind\thost\tname\tdetail")
    for action in actions:
        print(f"{action.action}\t{action.kind}\t{action.host}\t{action.name}\t{action.detail}")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Create or roll back Zabbix SNMP trap items and triggers.")
    parser.add_argument("--creds", default=str(CRED_PATH))
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--rollback", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    creds = read_creds(args.creds)
    missing = [key for key in ("ZABBIX_URL", "ZABBIX_USER", "ZABBIX_PASSWORD") if not creds.get(key)]
    if missing:
        print(f"zabbix_items: missing credentials: {', '.join(missing)}", file=sys.stderr)
        return 2
    api = JsonRpcZabbixAPI(creds["ZABBIX_URL"], creds["ZABBIX_USER"], creds["ZABBIX_PASSWORD"])
    actions = rollback(api, args.apply) if args.rollback else ensure_items_and_triggers(api, args.apply)
    print_actions(actions)
    return 0


def regexp_matches(pattern: str, text: str) -> bool:
    return re.search(pattern, text) is not None


if __name__ == "__main__":
    raise SystemExit(main())
