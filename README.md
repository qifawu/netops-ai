# netops-ai

**Alert-driven, read-only AI network troubleshooting.** A Zabbix alert comes in, an AI agent investigates the devices with read-only commands, and you get a root-cause conclusion that quotes the evidence it used — on a Feishu card and a web dashboard.

**告警驱动的只读 AI 网络排障。** Zabbix 告警进来，AI 用只读命令去查设备，给出根因结论并引用它依据的原文，推到飞书卡片和网页看板。

[English](#english) · [中文](#中文) · [Install / 部署](docs/INSTALL.md) · [Lab / 实验环境](docs/LAB.md) · [Architecture / 架构](docs/ARCHITECTURE.md) · [Contributing / 贡献](CONTRIBUTING.md)

<p align="center"><img src="docs/images/architecture.png" alt="architecture / 架构" width="1000"></p>

---

# English

## What it does

1. Zabbix sends an alert to `POST /webhooks/zabbix`.
2. Related alerts are merged into one incident.
3. The agent investigates with read-only tools only: Zabbix queries, `show` commands on the devices, topology neighbors, your playbooks.
4. It writes a structured conclusion: root cause, confidence, the evidence it quoted, and — when it can't tell — what is still unknown and which command would settle it.
5. The result goes to a Feishu card and the dashboard.

**It never changes anything.** Every device command passes a whitelist in code (only full `show …` commands), and you also give it a read-only device account. A refused command is never sent.

## What it looks like

Offline demo (no network, no devices, no model):

<p align="center"><img src="docs/images/demo-replay.png" alt="demo replay" width="860"></p>

Dashboard:

| | |
|---|---|
| Overview<br><img src="docs/images/ui-overview-en.png" width="460"> | Incident and conclusion<br><img src="docs/images/ui-incident.png" width="460"> |
| Command audit<br><img src="docs/images/ui-audit-en.png" width="460"> | Topology<br><img src="docs/images/ui-topology-en.png" width="460"> |

## Deploy

Three levels; each works without the next. Full steps: [docs/INSTALL.md](docs/INSTALL.md).

```bash
git clone https://github.com/qifawu/netops-ai.git && cd netops-ai
python -m venv .venv && . .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt                   # Python 3.13
```

1. **Try it offline** — `python tools/demo_replay.py` (also `python -m pytest tests -q`).
2. **Dashboard** — `cd web && npm ci && npm run build && cd ..`, then `python -m uvicorn netops_ai.api.app:app --host 127.0.0.1 --port 8000` and open http://127.0.0.1:8000.
3. **Full pipeline** — `cp env.example .env` and fill in: Zabbix (read-only user), devices (**read-only account**), an LLM endpoint, optionally Feishu; list your devices in `topology.yaml`; in Zabbix add a webhook media type that posts to `/webhooks/zabbix`.

The API and dashboard have **no login**: keep uvicorn on `127.0.0.1` and put an authenticating reverse proxy in front (nginx example in [INSTALL.md](docs/INSTALL.md#securing-the-api-and-dashboard)).

## The environment it was built and tested on

A virtual network in EVE-NG: 7 Cisco nodes in three layers (2 core and 2 aggregation IOSv, 3 access IOSv-L2), running OSPF and a BGP session, one Zabbix 7.0 server polling over SNMP and receiving traps. Faults were injected by hand on the devices (interface shutdown, OSPF neighbor loss, BGP session shutdown, reload) and the agent was checked against them. Cisco IOS only; not run in production. No device images or configs are included. Details and how to rebuild it: [docs/LAB.md](docs/LAB.md).

## License

Apache-2.0 — [LICENSE](LICENSE).

---

# 中文

## 它做什么

1. Zabbix 把告警 `POST` 到 `/webhooks/zabbix`。
2. 相关告警合并成一个事件。
3. agent 只用只读工具取证：查 Zabbix、在设备上敲 `show` 命令、查拓扑邻居、读你的剧本。
4. 给出结构化结论：根因、置信度、引用的证据原文；判不出来时，写清还差什么、用哪条命令能判。
5. 结果推到飞书卡片和网页看板。

**它什么都不会改。** 设备命令先过代码里的白名单（只放行写全的 `show …`），你还要给它一个只读的设备账号。被拒的命令根本不会发出去。

## 效果

离线演示（不需要网络、设备、模型）：

<p align="center"><img src="docs/images/demo-replay.png" alt="离线回放" width="860"></p>

网页看板：

| | |
|---|---|
| 系统总览<br><img src="docs/images/ui-overview.png" width="460"> | 告警与结论<br><img src="docs/images/ui-incident.png" width="460"> |
| 命令审计<br><img src="docs/images/ui-audit.png" width="460"> | 设备与拓扑<br><img src="docs/images/ui-topology.png" width="460"> |

## 怎么部署

三个层级，互不依赖。完整步骤见 [docs/INSTALL.md](docs/INSTALL.md)。

```bash
git clone https://github.com/qifawu/netops-ai.git && cd netops-ai
python -m venv .venv && . .venv/bin/activate     # Windows：.venv\Scripts\activate
pip install -r requirements.txt                   # Python 3.13
```

1. **离线体验** —— `python tools/demo_replay.py`（也可以 `python -m pytest tests -q`）。
2. **看板** —— `cd web && npm ci && npm run build && cd ..`，再 `python -m uvicorn netops_ai.api.app:app --host 127.0.0.1 --port 8000`，打开 http://127.0.0.1:8000 。
3. **完整链路** —— `cp env.example .env` 后填写：Zabbix（只读用户）、设备（**只读账号**）、大模型接口，飞书可选；在 `topology.yaml` 里写你的设备；Zabbix 里加一个 webhook 媒介类型，POST 到 `/webhooks/zabbix`。

接口和看板**没有登录**：uvicorn 只监听 `127.0.0.1`，前面放一个带认证的反向代理（nginx 示例见 [INSTALL.md](docs/INSTALL.md#securing-the-api-and-dashboard)）。

## 开发和测试用的环境

EVE-NG 里的一套虚拟网络：三层 7 台 Cisco 节点（2 台核心 + 2 台汇聚 IOSv，3 台接入 IOSv-L2），跑 OSPF 和一条 BGP 会话；一台 Zabbix 7.0 用 SNMP 轮询并接收 trap。故障是在设备上手工制造的（接口 shutdown、OSPF 邻居丢失、BGP 会话 shutdown、reload），再检查 agent 的结论。仅支持 Cisco IOS，没有在生产网络跑过。仓库不含设备镜像和配置。详情和自己怎么搭一个：[docs/LAB.md](docs/LAB.md)。

## 许可证

Apache-2.0，见 [LICENSE](LICENSE)。
