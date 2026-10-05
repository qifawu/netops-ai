# The reference lab

**English** | [简体中文](LAB.zh-CN.md)

netops-ai was developed and tested against a small virtual network. This page describes it so you can judge what "lab-validated" means, and rebuild something similar.

**What is and is not included.** The repository contains the topology file (`topology.yaml`), the Zabbix compose file (`deploy/zabbix/`), the SNMP-trap lab scripts (`deploy/trap/`), the playbooks and a few sanitized device captures (`labs/captures/`). It does **not** contain EVE-NG, any device image, or device configurations — router/switch images are vendor-licensed and you have to supply them yourself.

## Shape

```
                    V1 ────── V2          core         (Cisco IOSv)
                   /  \  \  /  /  \
                  /    \  \/  /    \      routed /30 links
                 D1 ────────────── D2     aggregation  (Cisco IOSv)
                / \                 \
              A1   A2               A3    access       (Cisco IOSv-L2)
```

| Layer | Nodes | Image | Role in the scenarios |
|---|---|---|---|
| Core | V1, V2 | IOSv | OSPF with the aggregation layer; a BGP session between V1 and V2 over their direct link |
| Aggregation | D1, D2 | IOSv | Routed uplinks to both cores; `bridge irb` + BVI interfaces are the VLAN gateways for the access layer |
| Access | A1, A2, A3 | IOSv-L2 | Layer 2 only; management IP on the `Vlan1` SVI (not on a physical port) |

- 7 managed nodes, all reachable out-of-band on a management network (`Gi0/0`); `topology.yaml` lists the exact links and uses documentation addresses (`192.0.2.0/24`) — in the original lab this is a hypervisor NAT network.
- A plain Linux test host sits behind the access layer to generate traffic. It is not managed and is deliberately **not** listed in `topology.yaml` (a listed device that cannot be queried would show up on the topology page as a dead node).
- **Monitoring**: one Zabbix 7.0 server (`deploy/zabbix/`) polls every node over SNMP (a *Cisco IOS by SNMP*-style template) and also receives SNMP traps (`deploy/trap/`). Zabbix host names carry an image suffix (`V1-vios`, `A1-viosl2`), which is why `topology.yaml` has explicit `aliases`.
- **Syslog**: devices send syslog to an rsyslog instance on the emulator's guest VM (one file per device), a Zabbix `logrt[]` item tails those files, and the agent reads them through Zabbix instead of logging in to a log host.
- **Hypervisor**: EVE-NG Community 6.2 inside a desktop hypervisor on a 16 GB machine. The eight IOSv/IOSv-L2 nodes plus the test host take roughly 3 GB of RAM. Without hardware virtualization passed through to the VM, QEMU nodes boot slowly (minutes) — be patient before concluding a node is broken.

## Scenarios used

Faults were injected **on the device by a human or a script with write access** — never by netops-ai, which has no write path:

| Scenario | How it was triggered | What the agent should find |
|---|---|---|
| Interface down | `shutdown` on a routed interface | `interface-link-down` playbook: administratively down vs. line protocol down |
| OSPF neighbor loss | shut a neighbor-facing interface or change a timer | `ospf-adjacency`: which side, which stage |
| BGP session down | `neighbor … shutdown` on V1 or V2 | `bgp-session`: administrative vs. transport vs. policy |
| Device restart | `reload` | cold-start trap + uptime reset |

Lessons from running it:

- **Break links with `shutdown` on the device, not by pulling the virtual cable (tap) on the hypervisor.** Pulling the tap wedged IOSv nodes in our setup and produced no useful alert.
- Zabbix *LLD* automatically disables items/triggers of interfaces that are administratively down, so the PROBLEM may not appear on the Zabbix Problems page — enable the trigger by hand when you need to see it.
- Zabbix pages show UTC by default; cards and records may show local time. Check the time zone before concluding that an alert is "late".
- Test the read-only account through a **real VTY session**, not the hypervisor's console proxy (it uses `line con 0` and ignores your VTY login rules).

## Rebuild it (outline)

1. Install a network emulator (EVE-NG, CML, GNS3 …) and add Cisco IOSv / IOSv-L2 images you are licensed to use.
2. Create the 7 nodes and wire them as in `topology.yaml` (`links:`). Put each node's first interface on a management network the API host can reach.
3. Configure on every node: management IP, an SSH-capable **low-privilege** account plus an `enable secret`, SNMP (v2c community — what the trap scripts configure) pointing at Zabbix, `logging host`, OSPF between core and aggregation, one BGP session between the cores.
4. Start Zabbix (`deploy/zabbix/README.md`), add the nodes as hosts, link the SNMP template, optionally set up traps (`deploy/trap/`).
5. Fill `.env` and `topology.yaml` (your addresses and your Zabbix host names as aliases), then follow [INSTALL.md](INSTALL.md) level 3.

You do not need a lab to try the project: the offline replay and the dashboard (levels 1–2) run on synthetic data.
