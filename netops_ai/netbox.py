"""NetBox 只读客户端，拓扑真源（`topology.yaml` 是离线兜底）。

直连 REST，不走 MCP：官方那个是云托管，数据不出内网。只有 GET。
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request


class NetBoxError(RuntimeError):
    pass


def _get(base_url: str, auth: str, path: str, **params) -> dict:
    """`auth` 是**拼好的** Authorization 头（`auth_header()` 出的），不是裸 token。
    早先这里又拼了一次 `Token {token}`，结果是 `Token Bearer nbt_...`，
    401 之后静默退回 yaml，查出来只有两台设备——**静默退回会把配置错误吃掉**，
    所以现在退回那一步会把原因带出去（见 `topology.py`）。
    """
    url = base_url.rstrip("/") + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"Authorization": auth, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise NetBoxError(f"NetBox {path} 返回 {exc.code}：{exc.read()[:200]!r}") from exc
    except Exception as exc:  # noqa: BLE001 - 连不上也要给一句人能看懂的话
        raise NetBoxError(f"NetBox {path} 请求失败：{type(exc).__name__}: {exc}") from exc


def fetch_all(base_url: str, auth: str, path: str, **params) -> list[dict]:
    """翻页取全量。`limit=0` 在 NetBox 里是"不分页"，但有些部署会限制最大值，
    所以这里老老实实跟着 `next` 翻。
    """
    rows: list[dict] = []
    payload = _get(base_url, auth, path, limit=params.pop("limit", 200), **params)
    rows.extend(payload.get("results") or [])
    nxt = payload.get("next")
    while nxt:
        req = urllib.request.Request(nxt, headers={"Authorization": auth, "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001 - 翻页失败也要变成 NetBoxError，调用方才会走「退回旧台账并留原因」
            raise NetBoxError(f"NetBox 翻页 {nxt} 请求失败：{type(exc).__name__}: {exc}") from exc
        rows.extend(payload.get("results") or [])
        nxt = payload.get("next")
    return rows


def _env() -> dict[str, str]:
    """`.env` + 环境变量。**这里自己读，不 import 上层模块**——能力层不许反向依赖。"""
    from pathlib import Path

    cfg: dict[str, str] = {}
    path = Path(__file__).resolve().parents[1] / ".env"
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                cfg[k.strip()] = v.strip()
    cfg.update(os.environ)
    return cfg


def auth_header(token: str) -> str:
    """NetBox 4.7 起 API token 换成 v2：头是 `Bearer nbt_<key>.<plaintext>`。
    老的 v1 仍然是 `Token <key>`。**照旧文档写 v1 格式会一直 403**，
    返回体是 `{"detail":"Invalid v1 token"}`，真踩过，所以这里按前缀自动分辨。
    """
    token = token.strip()
    return f"Bearer {token}" if token.startswith("nbt_") else f"Token {token}"


def netbox_enabled(cfg: dict[str, str] | None = None) -> bool:
    cfg = cfg or _env()
    return bool(cfg.get("NETBOX_URL") and cfg.get("NETBOX_TOKEN"))


def load_topology_from_netbox(base_url: str = "", token: str = ""):
    """从 NetBox 取拓扑，返回跟 `topology.yaml` 完全一样的结构。

    调用方（`topology_neighbors`）一个字都不用改，换的只是数据从哪来。
    """
    from netops_ai.topology import TopologyDevice, TopologyLink

    cfg = _env()
    base_url = base_url or cfg.get("NETBOX_URL", "")
    header = auth_header(token or cfg.get("NETBOX_TOKEN", ""))

    devices = fetch_all(base_url, header, "/api/dcim/devices/")
    interfaces = fetch_all(base_url, header, "/api/dcim/interfaces/")
    return build_topology(devices, interfaces)


def build_topology(devices: list[dict], interfaces: list[dict]) -> dict:
    """把 NetBox 的 devices + interfaces 拼成拓扑。**纯函数，好测。**"""
    from netops_ai.topology import TopologyDevice, TopologyInterface, TopologyLink

    # 台账里状态不是「在网」的设备（offline / decommissioning / inventory / failed）不画进拓扑，
    # 也不当邻居。这样设备下架时，
    # 先置状态就能从图上摘掉，不用等删记录。
    inactive = {
        d.get("name") for d in devices
        if ((d.get("status") or {}).get("value") or "active") in ("offline", "decommissioning", "inventory", "failed")
    }
    devices = [d for d in devices if d.get("name") not in inactive]
    links: dict[str, list[TopologyLink]] = {}
    inventory: dict[str, list[TopologyInterface]] = {}
    for iface in interfaces:
        local_dev = ((iface.get("device") or {}).get("name")) or ""
        if not local_dev or local_dev in inactive:
            continue
        peers = [pr for pr in (iface.get("link_peers") or []) if ((pr.get("device") or {}).get("name")) not in inactive]
        peer_items = [
            (
                ((peer.get("device") or {}).get("name")) or "",
                peer.get("name", ""),
            )
            for peer in peers
        ]
        if not peer_items:
            inventory.setdefault(local_dev, []).append(
                TopologyInterface(name=iface.get("name", ""), description=iface.get("description", ""))
            )
        for peer_dev, peer_if in peer_items:
            inventory.setdefault(local_dev, []).append(
                TopologyInterface(
                    name=iface.get("name", ""),
                    peer=peer_dev,
                    peer_interface=peer_if,
                    description=iface.get("description", ""),
                )
            )
        if iface.get("link_peers_type") != "dcim.interface":
            continue
        for peer in peers:
            peer_dev = ((peer.get("device") or {}).get("name")) or ""
            if not local_dev or not peer_dev:
                continue
            links.setdefault(local_dev, []).append(
                TopologyLink(local_interface=iface.get("name", ""), peer=peer_dev, peer_interface=peer.get("name", ""))
            )

    out = {}
    for dev in devices:
        name = dev.get("name") or ""
        if not name:
            continue
        ip = ((dev.get("primary_ip4") or {}).get("display") or "").split("/")[0]
        # **别名是被真实故障逼出来的**：Zabbix 叫 D1-vios、台账里叫 D1，
        # 对不上就查不到对端。NetBox 侧用自定义字段 `zabbix_host` 显式登记，
        # 不做模糊匹配（剥后缀那是猜，猜错了没人知道）。
        zbx = str((dev.get("custom_fields") or {}).get("zabbix_host") or "").strip()
        custom = dev.get("custom_fields") or {}
        out[name] = TopologyDevice(
            name=name,
            host=ip,
            links=tuple(links.get(name, ())),
            aliases=tuple(x for x in (zbx,) if x),
            role=str((dev.get("role") or {}).get("slug") or ""),
            model=str((dev.get("device_type") or {}).get("model") or (dev.get("device_type") or {}).get("display") or ""),
            serial=str(dev.get("serial") or ""),
            site=str((dev.get("site") or {}).get("name") or (dev.get("site") or {}).get("display") or ""),
            rack=str((dev.get("rack") or {}).get("name") or (dev.get("rack") or {}).get("display") or ""),
            platform=str((dev.get("platform") or {}).get("name") or (dev.get("platform") or {}).get("display") or ""),
            software_version=str(custom.get("software_version") or custom.get("version") or ""),
            interfaces=tuple(inventory.get(name, ())),
        )
    return out
