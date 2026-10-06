# netops-ai

**English** | [简体中文](README.zh-CN.md)

**Alert-driven, read-only AI network troubleshooting.** A Zabbix alert comes in, an AI agent investigates the devices with read-only commands, and you get a root-cause conclusion that quotes the evidence it used — on a Feishu card and a web dashboard.

[Install](docs/INSTALL.md) · [Lab](docs/LAB.md) · [Architecture](docs/ARCHITECTURE.md) · [Playbooks](docs/PLAYBOOK-FORMAT.md) · [Contributing](CONTRIBUTING.md)

<p align="center"><img src="docs/images/architecture.png" alt="architecture" width="1000"></p>

## What it does

1. Zabbix sends an alert to `POST /webhooks/zabbix`.
2. Related alerts are merged into one incident.
3. The agent investigates with read-only tools only: Zabbix queries, `show` commands on the devices, topology neighbors, your playbooks.
4. It writes a structured conclusion: root cause, confidence, the evidence it quoted, and — when it can't tell — what is still unknown and which command would settle it.
5. The result goes to a Feishu card and the dashboard.

**It never changes anything.** Every device command passes a whitelist in code (only full `show …` commands), and you also give it a read-only device account. A refused command is never sent.

## How it works

- **Merge.** Alerts that arrive close together wait a short window (`ALERT_WINDOW_*`) and become one incident, so a link failure with ten knock-on alerts gives one conclusion, not ten.
- **Investigate.** One tool-calling loop with a token budget, call limits, duplicate-call blocking and a no-progress stop. Playbooks (YAML) only *advise* the agent; they never execute anything.
- **Two-layer read-only guard.** Layer 1 is code: every device command is checked against a whitelist before it is sent. Layer 2 is a read-only account you create on the device. The Zabbix client has a method whitelist the same way.

<p align="center"><img src="docs/images/guard.png" alt="two-layer read-only guard" width="900"></p>

- **Conclude.** The model fills a strict schema. For each of six hypothesis families (local action, local hardware/resource, remote/upstream, link/path quality, management plane, monitoring artifact) it must say supported, ruled out (with counter-evidence) or undetermined — and "can't tell" must say what data and which command would settle it.

## Features

- Alert pipeline: webhook, merge window, incident de-duplication, Feishu card
- Read-only device access over SSH/Telnet (Cisco IOS) behind the command whitelist; read-only Zabbix client
- Structured conclusions with a six-family hypothesis checklist and an honest "can't tell"
- Topology from NetBox (optional, read-only, with a webhook for instant refresh) or from a YAML file
- Playbooks (SOP) as advice, with a linter; six examples: interface down, OSPF adjacency, BGP session, device restart, high CPU, interface errors
- Scheduled inspection: trend detectors on Zabbix history plus live read-only status checks, history, diff against the last run, Markdown/HTML export
- Command audit: every command the AI ran and every one that was refused
- Local documentation search (the index ships empty; fill it with `tools/kb_ingest.py`)
- Web dashboard in Chinese and English: overview, incidents, topology, inspection, audit, knowledge base, settings; a demo mode that masks IPs and IDs for screenshots
- Offline replay and demo data (`tools/demo_replay.py`, `tools/seed_demo.py`)

## Tools

**What the agent can call** — all read-only, registered in one place; the full list is in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md#the-tools-the-agent-can-use).

| Tool | What it does |
|---|---|
| `zbx_*` | Read-only Zabbix queries: hosts, items, history, trends, syslog, problems, top talkers, chart |
| `device_show` | Run a `show` command on a device, after the command whitelist |
| `topology_neighbors` | Neighbors of a device or interface — from NetBox if configured, else `topology.yaml` |
| `nb_devices`, `nb_topology` | NetBox inventory (read-only, when NetBox is configured) |
| `sop_lookup` | Find the matching playbook — it advises, it never executes |
| `doc_search` | Keyword (BM25) search over your own documents |
| `run_inspection`, `get_analysis` | Run an inspection, look up an earlier conclusion |

**Command-line tools** we ship, all runnable offline unless noted:

| Command | What it is for |
|---|---|
| `python zbx-cli.py hosts` | Read-only Zabbix CLI that shares the agent's tool definitions; `python zbx-cli.py tools` prints them |
| `python -m netops_ai.netbox_cli devices` | Read-only NetBox inventory CLI (`devices`, `neighbors <dev>`, `topology`); needs `NETBOX_URL` and `NETBOX_TOKEN` |
| `python tools/demo_replay.py` | Replay saved alert records and render the Feishu card, no network needed |
| `python tools/seed_demo.py` | Fill `records/` with six synthetic incidents so the dashboard has data |
| `python tools/sop_lint.py` | Lint playbooks: real tools, real parameters, commands the whitelist accepts |
| `python tools/kb_ingest.py <dir>` | Build the local documentation index (SQLite FTS5) from `.md/.txt/.html/.pdf` |
| `python tools/llm_doctor.py` | Check your LLM endpoint with a multi-turn tool-call replay (calls the model) |

## What it looks like

Offline demo (no network, no devices, no model):

<p align="center"><img src="docs/images/demo-replay.png" alt="demo replay" width="860"></p>

Dashboard:

| | |
|---|---|
| Overview<br><img src="docs/images/ui-overview-en.png" width="460"> | Incident and conclusion<br><img src="docs/images/ui-incident.png" width="460"> |
| Command audit<br><img src="docs/images/ui-audit-en.png" width="460"> | Topology<br><img src="docs/images/ui-topology-en.png" width="460"> |

### More of the dashboard and the card

| | |
|---|---|
| Feishu card — what lands in the chat. Left: root cause found. Right: honest "can't tell" with the next command to run<br><img src="docs/images/feishu-card.png" width="460"> | Automated inspection — trend findings plus read-only status checks of every device<br><img src="docs/images/ui-inspection.png" width="460"> |
| Knowledge base — search your own documents; shown with the three sample notes in `examples/kb/`<br><img src="docs/images/ui-knowledge.png" width="460"> | Settings — edit `.env` from the page; secrets are masked<br><img src="docs/images/ui-settings.png" width="460"> |

The cards are rendered from the JSON of two synthetic examples (an illustration, not a screenshot of the Feishu client). `python tools/seed_demo.py` fills the dashboard with six synthetic incidents so you can click through everything without any setup.

## Deploy

Three levels; each works without the next. Full steps: [docs/INSTALL.md](docs/INSTALL.md).

```bash
git clone https://github.com/qifawu/netops-ai.git && cd netops-ai
python -m venv .venv && . .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt                   # Python 3.13
```

1. **Try it offline** — `python tools/demo_replay.py` (also `python -m pytest tests -q`).
2. **Dashboard** — `cd web && npm ci && npm run build && cd ..`, then (optional) `python tools/seed_demo.py` to fill it with six synthetic incidents, then `python -m uvicorn netops_ai.api.app:app --host 127.0.0.1 --port 8000` and open http://127.0.0.1:8000.
3. **Full pipeline** — `cp env.example .env` and fill in: Zabbix (read-only user), devices (**read-only account**), an LLM endpoint, optionally Feishu; list your devices in `topology.yaml`; in Zabbix add a webhook media type that posts to `/webhooks/zabbix`.

The API and dashboard have **no login**: keep uvicorn on `127.0.0.1` and put an authenticating reverse proxy in front (nginx example in [INSTALL.md](docs/INSTALL.md#securing-the-api-and-dashboard)).

## The environment it was built and tested on

A virtual network in EVE-NG: 7 Cisco nodes in three layers (2 core and 2 aggregation IOSv, 3 access IOSv-L2), running OSPF and a BGP session, one Zabbix 7.0 server polling over SNMP and receiving traps. Faults were injected by hand on the devices (interface shutdown, OSPF neighbor loss, BGP session shutdown, reload) and the agent was checked against them. Cisco IOS only; not run in production. No device images or configs are included. Details and how to rebuild it: [docs/LAB.md](docs/LAB.md).

## Where it could go next

Directions where help is welcome; none of them is promised on a date.

- **More vendors.** Each one is a command-whitelist ruleset, a command catalog and a few playbooks. Cisco IOS is the template.
- **More playbooks.** Six ship today. A playbook is a small YAML file; [docs/PLAYBOOK-FORMAT.md](docs/PLAYBOOK-FORMAT.md) has a ten-minute guide.
- **More notification channels** next to Feishu (chat webhooks, e-mail).
- **Built-in authentication** for the dashboard and webhook, so a reverse proxy is optional.
- **Richer inspection rules** and more status checks (STP, port-channel, power and fan).

## License

Apache-2.0 — [LICENSE](LICENSE).
