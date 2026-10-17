"""拓扑自动发现：用 CDP（没有就用 LLDP）邻居信息还原设备之间的连线，对照已登记的拓扑，给出「多了什么、少了什么」，并画图。

只读命令（`show cdp neighbors detail` / `show lldp neighbors detail`，白名单放行），输出交给 `table_parse` 解析成行记录，
所以换厂商只需要有对应的解析模板。**发现结果只作为建议**：不改 `topology.yaml` / NetBox，差异由人确认后再去登记。

规则：
- 一条物理连线 = 两端互相看到对方（A 的 Gi0/1 看到 B 的 Gi0/1，B 也看到 A）；只有一端看到的也记，标「单向」；
- 一个本地接口下看到 3 台及以上邻居 = 共享网段（典型是带外管理口接在同一台交换机上），不是点到点连线，单独列出、不画进拓扑图；
- 邻居的设备名去掉域名后缀、接口名规整（Gi0/1 = GigabitEthernet0/1）再和登记的拓扑比；邻居不在已登记设备里的（外部设备）单独列。
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Callable

from netops_ai.inspection import table_parse as TP

COMMANDS = ("show cdp neighbors detail", "show lldp neighbors detail")
_SHORT = (("tengigabitethernet", "te"), ("gigabitethernet", "gi"), ("fastethernet", "fa"), ("port-channel", "po"), ("loopback", "lo"),
          ("ethernet", "eth"), ("serial", "se"))


def canon_if(name: str) -> str:
    n = re.sub(r"\s+", "", str(name).lower())
    for long, short in _SHORT:
        if n.startswith(long):
            return short + n[len(long):]
    return n


def short_name(name: str) -> str:
    """`A1.netops-ai.local` → `A1`；IP 形式的设备 ID 原样保留。"""
    n = str(name).strip()
    return n if re.fullmatch(r"[\d.]+", n) else n.split(".", 1)[0]


def collect(devices: dict[str, Any], adapter_factory: Callable[[Any], Any], *, vendor: str = "cisco") -> dict[str, dict[str, Any]]:
    """{设备: {"rows": [{local_if, peer, peer_if, mgmt_ip}], "source": "cdp"|"lldp", "error": ""}}。一台失败不影响其它。"""
    out: dict[str, dict[str, Any]] = {}
    for name, dev in sorted(devices.items()):
        res: dict[str, Any] = {"rows": [], "source": "", "error": ""}
        try:
            with adapter_factory(dev) as ad:
                for cmd in COMMANDS:
                    r = ad.run(cmd)
                    if not r.allowed:
                        res["error"] = str(r.denial_reason)[:120]
                        continue
                    if r.error or not (r.output or "").strip():
                        continue
                    parsed = TP.parse(cmd, r.output, vendor=vendor)
                    if parsed.ok and parsed.rows:
                        res.update(source="cdp" if "cdp" in cmd else "lldp", error="")
                        res["rows"] = [{"local_if": row.get("local_interface", ""), "peer": short_name(row.get("neighbor_name", "")),
                                        "peer_if": row.get("neighbor_interface", ""), "mgmt_ip": row.get("mgmt_address", "")} for row in parsed.rows]
                        break
        except Exception as exc:  # noqa: BLE001
            res["error"] = f"{type(exc).__name__}: {str(exc)[:120]}"
        out[name] = res
    return out


def build(observed: dict[str, dict[str, Any]], known: dict[str, Any], *, aliases: dict[str, str] | None = None) -> dict[str, Any]:
    """观测 → 连线集合，并对照 `known`（{设备名: TopologyDevice}）。返回 {links, shared, external, missing, extra, unreachable}。"""
    alias = {a.lower(): d for d, dev in known.items() for a in (d, *getattr(dev, "aliases", ()))}
    alias.update({k.lower(): v for k, v in (aliases or {}).items()})
    resolve = lambda n: alias.get(n.lower(), n)  # noqa: E731
    links: dict[frozenset, dict[str, Any]] = {}
    shared: list[dict[str, Any]] = []
    external: dict[str, set[str]] = {}
    for dev, o in observed.items():
        by_if: dict[str, list[dict[str, Any]]] = {}
        for r in o["rows"]:
            by_if.setdefault(canon_if(r["local_if"]), []).append(r)
        for lif, rs in by_if.items():
            if len(rs) >= 3:
                shared.append({"device": dev, "interface": lif, "neighbors": sorted(resolve(r["peer"]) for r in rs)})
                continue
            for r in rs:
                peer = resolve(r["peer"])
                if peer not in known:
                    external.setdefault(peer, set()).add(f"{dev} {lif}")
                    continue
                end_a, end_b = (dev, lif), (peer, canon_if(r["peer_if"]))
                key = frozenset([end_a, end_b])
                link = links.setdefault(key, {"a": min(end_a, end_b), "b": max(end_a, end_b), "seen_from": set()})
                link["seen_from"].add(dev)
    discovered = [{"a": f"{l['a'][0]} {l['a'][1]}", "b": f"{l['b'][0]} {l['b'][1]}", "both_sides": len(l["seen_from"]) == 2} for l in links.values()]
    # 已登记的连线（点到点，每条只算一次）
    recorded: dict[frozenset, tuple] = {}
    for name, dev in known.items():
        for lk in dev.links:
            if lk.peer in known:
                ends = sorted([(name, lk.local_interface), (lk.peer, lk.peer_interface)])  # 两端各登记一次：固定成名字小的在前，输出才稳定
                recorded[frozenset([(name, canon_if(lk.local_interface)), (lk.peer, canon_if(lk.peer_interface))])] = (*ends[0], *ends[1])
    seen_devs = {d for d, o in observed.items() if o["rows"]}
    missing = [f"{a} {ai} — {b} {bi}" for key, (a, ai, b, bi) in recorded.items() if key not in links and (a in seen_devs or b in seen_devs)]
    extra = [d for key, d in zip(links, discovered) if key not in recorded]
    return {"links": sorted(discovered, key=lambda d: (d["a"], d["b"])), "shared": shared,
            "external": {k: sorted(v) for k, v in sorted(external.items())}, "missing": sorted(missing), "extra": sorted(extra, key=lambda d: d["a"]),
            "unreachable": sorted(d for d, o in observed.items() if not o["rows"]),
            "unidirectional": [d for d in discovered if not d["both_sides"]]}


def to_blocks(result: dict[str, Any]) -> list[tuple]:
    blocks: list[tuple] = [("h1", "拓扑自动发现"),
                           ("p", f"发现 {len(result['links'])} 条设备间连线；与已登记拓扑相比：多出 {len(result['extra'])} 条、没看到 {len(result['missing'])} 条。"
                                 "发现结果只是建议，不会自动改拓扑登记，差异请人确认后再处理。")]
    blocks.append(("table", ["A 端", "B 端", "两端都看到", "是否已登记"],
                   [[l["a"], l["b"], "是" if l["both_sides"] else "仅一端", "否（新发现）" if l in result["extra"] else "是"] for l in result["links"]]))
    if result["missing"]:
        blocks += [("h3", "已登记但这次没看到的连线（可能断了、邻居协议没开，或登记有误）"), ("ul", result["missing"])]
    if result["shared"]:
        blocks += [("h3", "共享网段（一个接口下 3 台以上邻居，通常是带外管理网，不画进拓扑）"),
                   ("ul", [f"{s['device']} {s['interface']}：{', '.join(s['neighbors'])}" for s in result["shared"]])]
    if result["external"]:
        blocks += [("h3", "不在已登记设备里的邻居（外部设备或漏登记）"), ("ul", [f"{k}（经 {', '.join(v)}）" for k, v in result["external"].items()])]
    if result["unreachable"]:
        blocks += [("h3", "没取到邻居信息的设备"), ("ul", result["unreachable"])]
    return blocks


def to_yaml(result: dict[str, Any]) -> str:
    """建议登记的连线（YAML 片段，给人对照着改 `topology.yaml` 用，不会被程序读回去）。"""
    lines = ["# 自动发现的设备间连线（建议，需人工确认后再登记）"]
    for l in result["links"]:
        lines.append(f"- {{a: \"{l['a']}\", b: \"{l['b']}\", both_sides: {str(l['both_sides']).lower()}}}")
    return "\n".join(lines) + "\n"


def render_png(result: dict[str, Any], known: dict[str, Any], path: Path) -> Path:
    """分层画图：按角色（核心 / 汇聚 / 接入 / 其它）从上到下。绿 = 已登记且看到；橙 = 新发现；红虚线 = 已登记但没看到。"""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    order = {"core": 0, "aggregation": 1, "distribution": 1, "access": 2}
    names = sorted(known)
    layers: dict[int, list[str]] = {}
    for n in names:
        layers.setdefault(order.get(getattr(known[n], "role", ""), 3), []).append(n)
    pos: dict[str, tuple[float, float]] = {}
    for layer, ns in layers.items():
        for i, n in enumerate(ns):
            pos[n] = ((i + 1) / (len(ns) + 1), -layer)
    fig, ax = plt.subplots(figsize=(9, 1.8 + 1.6 * max(len(layers), 2)))

    def draw(a: str, b: str, color: str, style: str, label: str = "") -> None:
        (x1, y1), (x2, y2) = pos[a], pos[b]
        ax.plot([x1, x2], [y1, y2], color=color, linestyle=style, linewidth=1.6, zorder=1)
        if label:
            ax.text((x1 + x2) / 2, (y1 + y2) / 2, label, fontsize=5.5, ha="center", va="center", color="#444", zorder=3,
                    bbox={"fc": "white", "ec": "none", "pad": 0.4})
    extra = {(l["a"], l["b"]) for l in result["extra"]}
    for l in result["links"]:
        (a, ai), (b, bi) = l["a"].split(" ", 1), l["b"].split(" ", 1)
        if a in pos and b in pos:
            draw(a, b, "#ea580c" if (l["a"], l["b"]) in extra else "#16a34a", "-", f"{ai}–{bi}")
    for m in result["missing"]:
        left, right = m.split(" — ")
        (a, ai), (b, bi) = left.split(" ", 1), right.split(" ", 1)
        if a in pos and b in pos:
            draw(a, b, "#dc2626", "--", f"{ai}–{bi}")
    for n, (x, y) in pos.items():
        ax.text(x, y, n, ha="center", va="center", fontsize=9, zorder=4, bbox={"boxstyle": "round,pad=0.35", "fc": "#e0e7ff", "ec": "#6366f1"})
    ax.set_xlim(0, 1)
    ax.set_ylim(-max(layers) - 0.6, 0.6)
    ax.axis("off")
    ax.set_title("green: registered and seen   orange: newly discovered   red dashed: registered, not seen", fontsize=8)
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=130, bbox_inches="tight")
    plt.close(fig)
    return path


def main() -> None:
    """`python -m netops_ai.inspection.topology_discovery [--out 目录]`：登录拓扑里每台设备（只读）取邻居，出报告和图。"""
    import argparse

    from netops_ai import topology as topo
    from netops_ai.inspection.export import blocks_to_html, blocks_to_markdown

    ap = argparse.ArgumentParser(description="CDP/LLDP 拓扑自动发现（只读，结果仅作建议）")
    ap.add_argument("--out", default="reports")
    a = ap.parse_args()
    known = topo.load_topology()
    result = build(collect(known, topo._default_adapter_factory), known)
    blocks, out = to_blocks(result), Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "topology-discovery.md").write_text(blocks_to_markdown(blocks), encoding="utf-8")
    (out / "topology-discovery.html").write_text(blocks_to_html(blocks, "拓扑自动发现", classify=None), encoding="utf-8")
    (out / "topology-discovered.yaml").write_text(to_yaml(result), encoding="utf-8")
    print(render_png(result, known, out / "topology-discovery.png"))


if __name__ == "__main__":
    main()
