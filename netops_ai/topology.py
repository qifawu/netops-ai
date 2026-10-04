"""Static lab topology lookup for agent tools.

The topology file is versioned lab fact, not runtime incident evidence. This
module deliberately reads only that file and optionally runs one read-only
command on the recorded peer device through the existing DeviceAdapter gate.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import yaml

from netops_ai.devices.base import DeviceAdapter
from netops_ai.devices.ssh import SSHDeviceAdapter
from netops_ai.devices.telnet import TelnetDeviceAdapter
from netops_ai.graph.agent_loop import REPO_ROOT
from netops_ai.llm.factory import env

TOPOLOGY_PATH = REPO_ROOT / "topology.yaml"
EVIDENCE_COMMAND = "show ip interface brief"
LEGACY_HOST_ALIASES = {
    "A1-iol": "A1-viosl2",
    "A2-iol": "A2-viosl2",
    "A3-iol": "A3-viosl2",
}


@dataclass(frozen=True)
class TopologyLink:
    local_interface: str
    peer: str
    peer_interface: str


@dataclass(frozen=True)
class TopologyInterface:
    name: str
    peer: str = ""
    peer_interface: str = ""
    description: str = ""


@dataclass(frozen=True)
class TopologyDevice:
    name: str
    host: str
    links: tuple[TopologyLink, ...]
    #: 同一台设备在别的系统里的名字（Zabbix 叫 `V2-vios`、拓扑叫 `V2`）。
    #: 必须显式写，不做模糊匹配：猜错了没人知道。
    aliases: tuple[str, ...] = ()
    #: 设备在层级里的角色（core / aggregation / access）。file provider 留空，
    #: NetBox 那侧有 device-role。**影响面分析要的是层级，光有邻居答不了**
    #: 「汇聚挂一台影响下面几个接入」。
    role: str = ""
    model: str = ""
    serial: str = ""
    site: str = ""
    rack: str = ""
    platform: str = ""
    software_version: str = ""
    interfaces: tuple[TopologyInterface, ...] = ()


AdapterFactory = Callable[[TopologyDevice], DeviceAdapter]

#: 上一次 `load_topology()` 的数据是从哪来的，以及退回的原因。
#: 给 `topology_neighbors` 带进返回值，**让"我在读旧台账"这件事在输出里看得见**。
LAST_SOURCE = "topology.yaml"
LAST_NETBOX_ERROR = ""

#: NetBox 拓扑的进程内缓存。看板每次打开「设备与拓扑」都要现拉一遍（好几次分页请求），NetBox 一慢页面就等到超时；
#: 拓扑很少变，缓存 `TOPOLOGY_CACHE_TTL` 秒（默认 600，只是兜底；NetBox 有变更会 webhook 通知作废，也可以手动刷新），并且 NetBox 请求失败时退回**上一次成功的结果**
#: （比退回旧 yaml 新，而且不会突然只剩两台设备）。缓存键带上加载函数，单测里换掉加载函数就不会读到别的用例的缓存。
_NETBOX_CACHE: dict = {"key": None, "at": 0.0, "data": None}


def _cache_ttl() -> float:
    import os

    try:
        return max(0.0, float(os.environ.get("TOPOLOGY_CACHE_TTL", "600")))
    except ValueError:
        return 600.0


def invalidate_cache() -> None:
    """NetBox 通知有变更（webhook）或人手动刷新时调用：下一次 `load_topology()` 重新去 NetBox 取。"""
    _NETBOX_CACHE.update(key=None, at=0.0, data=None)


def cache_age() -> int | None:
    """当前缓存的台账是多少秒前读的；没有缓存（还没读过 / 刚被作废）返回 None。"""
    import time as _time

    if _NETBOX_CACHE["data"] is None:
        return None
    return int(_time.monotonic() - _NETBOX_CACHE["at"])


def _norm(value: str) -> str:
    return "".join(str(value or "").lower().split())


def load_topology(path: Path = TOPOLOGY_PATH) -> dict[str, TopologyDevice]:
    """拓扑真源。**配了 NetBox 就走 NetBox，没配就读版本化的 yaml。**

    两个 provider 返回完全一样的结构，`topology_neighbors` 一个字都不用改。
    离线、单测、以及 NetBox 挂了的时候，file provider 还在。
    """
    try:
        from netops_ai import netbox as _netbox
    except ImportError:  # 开源版：只读本地 topology.yaml
        _netbox = None

    global LAST_SOURCE, LAST_NETBOX_ERROR
    LAST_SOURCE, LAST_NETBOX_ERROR = "topology.yaml", ""
    if _netbox is not None and _netbox.netbox_enabled():
        import time as _time

        loader = _netbox.load_topology_from_netbox
        key = id(loader)
        now = _time.monotonic()
        ttl = _cache_ttl()
        if ttl and _NETBOX_CACHE["key"] == key and _NETBOX_CACHE["data"] is not None and now - _NETBOX_CACHE["at"] < ttl:
            LAST_SOURCE = "netbox"
            return _NETBOX_CACHE["data"]
        try:
            try:
                data = loader()
            except _netbox.NetBoxError:
                _time.sleep(0.5)  # 偶发的连接抖动重试一次；再失败才算失败
                data = loader()
            LAST_SOURCE = "netbox"
            _NETBOX_CACHE.update(key=key, at=now, data=data)
            return data
        except _netbox.NetBoxError as exc:
            if _NETBOX_CACHE["key"] == key and _NETBOX_CACHE["data"] is not None:
                age = int(now - _NETBOX_CACHE["at"])
                LAST_SOURCE = "netbox"
                LAST_NETBOX_ERROR = f"NetBox 暂时取不到，先用 {age} 秒前的台账。{exc}"
                return _NETBOX_CACHE["data"]
            # **NetBox 挂了不该让排查停摆**：退回版本化的 yaml，那份可能旧，
            # 但比没有强。**但退回必须留下原因**——早先这里静默 pass，
            # 结果一个 header 拼错让它一直读旧 yaml，只有两台设备，
            # 从外面看像是"NetBox 里就这么多"，排查半天。
            LAST_NETBOX_ERROR = str(exc)
    return _load_topology_file(path)


def _load_topology_file(path: Path = TOPOLOGY_PATH) -> dict[str, TopologyDevice]:
    """Load and minimally validate the versioned topology file."""
    if not path.exists():
        raise FileNotFoundError(f"没有拓扑记录：{path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    devices = data.get("devices")
    if not isinstance(devices, list):
        raise ValueError("topology.yaml 格式错误：devices 必须是列表")

    parsed: dict[str, TopologyDevice] = {}
    for index, item in enumerate(devices):
        if not isinstance(item, dict):
            raise ValueError(f"topology.yaml 格式错误：devices[{index}] 必须是对象")
        name = str(item.get("name", "")).strip()
        host = str(item.get("host", "")).strip()
        if not name or not host:
            raise ValueError(f"topology.yaml 格式错误：devices[{index}] 缺少 name 或 host")

        links: list[TopologyLink] = []
        for link_index, link in enumerate(item.get("links") or []):
            if not isinstance(link, dict):
                raise ValueError(f"topology.yaml 格式错误：{name}.links[{link_index}] 必须是对象")
            local_interface = str(link.get("local_interface", "")).strip()
            peer = str(link.get("peer", "")).strip()
            peer_interface = str(link.get("peer_interface", "")).strip()
            if not local_interface or not peer or not peer_interface:
                raise ValueError(f"topology.yaml 格式错误：{name}.links[{link_index}] 缺少链路字段")
            links.append(TopologyLink(local_interface, peer, peer_interface))

        aliases_raw = item.get("aliases") or []
        if not isinstance(aliases_raw, list):
            raise ValueError(f"topology.yaml 格式错误：{name}.aliases 必须是列表")
        aliases = tuple(str(a).strip() for a in aliases_raw if str(a).strip())

        parsed[name] = TopologyDevice(
            name=name,
            host=host,
            links=tuple(links),
            aliases=aliases,
            role=str(item.get("role") or "").strip().lower(),  # core / aggregation / access，可选
            interfaces=tuple(
                TopologyInterface(link.local_interface, link.peer, link.peer_interface)
                for link in links
            ),
        )

    _reject_ambiguous_aliases(parsed)
    return parsed


def _reject_ambiguous_aliases(topology: dict[str, TopologyDevice]) -> None:
    """一个名字只能指向一台设备，否则解析就是掷骰子。

    别名和正式名放在同一个命名空间里比，因为查询端不知道自己手上那个
    字符串是哪一种。
    """
    seen: dict[str, str] = {}
    for device in topology.values():
        for label in (device.name, *device.aliases):
            key = _norm(label)
            owner = seen.get(key)
            if owner is not None and owner != device.name:
                raise ValueError(
                    f"topology.yaml 命名冲突：{label!r} 同时指向 {owner} 和 {device.name}"
                )
            seen[key] = device.name


def resolve_device(topology: dict[str, TopologyDevice], name: str) -> TopologyDevice | None:
    """按设备名或别名找设备，大小写/空格不敏感。找不到返回 None，不猜。"""
    raw = str(name or "").strip()
    canonical = next((new for old, new in LEGACY_HOST_ALIASES.items() if _norm(old) == _norm(raw)), raw)
    wanted = _norm(canonical)
    if not wanted:
        return None
    for device in topology.values():
        if _norm(device.name) == wanted:
            return device
    for device in topology.values():
        if any(_norm(alias) == wanted for alias in device.aliases):
            return device
    return None


def known_device_labels(topology: dict[str, TopologyDevice]) -> list[str]:
    """拓扑里所有能用来查询的名字，失败时原样回给 agent 让它自纠。"""
    labels: list[str] = []
    for device in topology.values():
        labels.append(device.name)
        labels.extend(device.aliases)
    return labels


def _default_adapter_factory(device: TopologyDevice) -> DeviceAdapter:
    cfg = env()
    vendor = cfg.get("DEVICE_VENDOR", "cisco")
    transport = cfg.get("DEVICE_TRANSPORT", "ssh").strip().lower() or "ssh"
    if transport == "telnet":
        return TelnetDeviceAdapter(
            vendor,
            host=device.host,
            port=int(cfg.get("DEVICE_TELNET_PORT", "23")),
            username=cfg.get("DEVICE_USERNAME", ""),
            password=cfg.get("DEVICE_PASSWORD", ""),
        )
    return SSHDeviceAdapter(
        vendor,
        host=device.host,
        port=int(cfg.get("DEVICE_PORT", "22")),
        username=cfg.get("DEVICE_USERNAME", ""),
        password=cfg.get("DEVICE_PASSWORD", ""),
    )


def _find_links(device: TopologyDevice, interface: str) -> tuple[TopologyLink, ...]:
    if not interface.strip():
        return device.links
    wanted = _norm(interface)
    return tuple(link for link in device.links if _norm(link.local_interface) == wanted)


def _probe_peer(peer_device: TopologyDevice, adapter_factory: AdapterFactory) -> dict[str, object]:
    try:
        with adapter_factory(peer_device) as adapter:
            result = adapter.run(EVIDENCE_COMMAND)
        return {
            "reachable": bool(result.allowed and result.ok),
            "command": result.command,
            "allowed": result.allowed,
            "ok": result.ok,
            "output": result.output,
            "error": result.error,
            "denial_reason": result.denial_reason,
        }
    except Exception as exc:  # noqa: BLE001 - tool results should be readable evidence
        return {
            "reachable": False,
            "command": EVIDENCE_COMMAND,
            "allowed": None,
            "ok": False,
            "output": "",
            "error": f"{type(exc).__name__}: {exc}",
            "denial_reason": "",
        }


def topology_neighbors(
    device: str,
    interface: str = "",
    *,
    topology_path: Path = TOPOLOGY_PATH,
    adapter_factory: AdapterFactory | None = None,
) -> str:
    """Return recorded neighbor(s) and peer read-only evidence as JSON text."""
    topology = load_topology(topology_path)
    device_name = str(device or "").strip()
    current = resolve_device(topology, device_name)
    if current is None:
        # **查不到的时候必须把可选项摆出来。** 以前这里只说"不在
        # topology.yaml 中"，agent 拿到这句话没有任何可操作信息，只能
        # 放弃——S 段那次就是这么放弃的。把已知名字列出来，它才能换个
        # 名字重试一次。
        return json.dumps(
            {
                "found": False,
                "source": LAST_SOURCE,
                "netbox_error": LAST_NETBOX_ERROR,
                "device": device_name,
                "interface": interface,
                "known_devices": known_device_labels(topology),
                "note": (
                    f"没有拓扑记录：{device_name or '<empty>'} 不是{LAST_SOURCE}里的设备名或别名。"
                    "拓扑里可用的名字（含 Zabbix 主机名别名）见 known_devices。"
                ),
            },
            ensure_ascii=False,
        )

    links = _find_links(current, interface)
    if not links:
        return json.dumps(
            {
                "found": False,
                "device": current.name,
                "interface": interface,
                "known_interfaces": [link.local_interface for link in current.links],
                "note": (
                    f"没有拓扑记录：设备 {current.name} 的接口 {interface or '<empty>'} 没有登记对端。"
                    "该设备已登记的接口见 known_interfaces。"
                ),
            },
            ensure_ascii=False,
        )

    factory = adapter_factory or _default_adapter_factory
    # **本机没配设备账号的时候不要去探对端。** 探了只会给每个邻居挂一条
    # 一模一样的"没配 DEVICE_USERNAME"，D1 那种五条链路的设备光这堆重复
    # 报错就占掉响应的一半；更糟的是 agent 看到五个 `peer_reachable: false`
    # 很容易判成"这台的邻居全断了"——那是重大故障，实际只是本机没配置。
    # **"探不了"和"探过了不通"必须分得开。**
    probe = not (adapter_factory is None and not env().get("DEVICE_USERNAME"))
    neighbors: list[dict[str, object]] = []
    for link in links:
        peer_device = resolve_device(topology, link.peer)
        item: dict[str, object] = {
            "device": current.name,
            "local_interface": link.local_interface,
            "peer": link.peer,
            "peer_interface": link.peer_interface,
        }
        if peer_device is None:
            item["peer_reachable"] = False
            item["note"] = f"没有拓扑记录：对端设备 {link.peer} 未定义 host。"
        else:
            item["peer_host"] = peer_device.host
            if probe:
                item["evidence"] = _probe_peer(peer_device, factory)
                item["peer_reachable"] = bool(item["evidence"]["reachable"])  # type: ignore[index]
        neighbors.append(item)

    return json.dumps(
        {
            "found": True,
            "source": LAST_SOURCE,
            "netbox_error": LAST_NETBOX_ERROR,
            "device": current.name,
            "role": current.role,
            # 用别名查进来的，要让 agent 看见它查的名字被解析成了谁——
            # 否则结论里会写 Zabbix 主机名，跟拓扑对不上。
            "queried_as": device_name,
            "interface": interface,
            "neighbors": neighbors,
            # **真源写死成 topology.yaml 会被模型照抄。** 那次真对话，
            # 数据明明来自 NetBox，模型回答第一行写的却是"（来自 topology.yaml / netbox）"
            # ——它是照着这句话和工具描述复述的。说明必须跟着 provider 走。
            "note": (
                f"拓扑真源：{LAST_SOURCE}。"
                + (
                    "对端可达性通过 DeviceAdapter.run() 只读白名单执行 show ip interface brief 验证。"
                    if probe
                    else "本机没配设备账号（DEVICE_USERNAME），这次没有探测对端，"
                    "所以下面没有 peer_reachable 字段——**这不代表对端不可达，是根本没探**。"
                )
            ),
        },
        ensure_ascii=False,
    )
