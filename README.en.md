# netops-ai

**English** | [简体中文](README.md)

**Alert-driven, read-only AI network troubleshooting.** On a Zabbix alert, an AI agent investigates the affected devices with read-only commands and produces a root-cause conclusion that quotes its evidence, delivered as a Feishu card and on a web dashboard.

[Install](docs/INSTALL.md) · [Deploy: Zabbix & NetBox](docs/DEPLOY.md) · [Lab](docs/LAB.md) · [Architecture](docs/ARCHITECTURE.md) · [Playbooks](docs/PLAYBOOK-FORMAT.md) · [Contributing](CONTRIBUTING.md)

<p align="center"><img src="docs/images/architecture.png" alt="architecture" width="1000"></p>

## Features

- **Alert pipeline.** Zabbix posts to `POST /webhooks/zabbix`; related alerts are merged into one incident; an agent investigates with read-only tools; the conclusion is delivered as a Feishu card and on the dashboard.
- **Read-only by construction.** Device commands pass a code-level whitelist (full `show …` commands only) and run under a read-only device account. The Zabbix client has a method whitelist.
- **Structured conclusions.** Root cause, confidence and the quoted evidence, checked against six hypothesis families. If the cause cannot be determined, the result states what is missing and which command would settle it.
- **Topology.** From NetBox (optional, read-only, webhook refresh) or from a YAML file.
- **Playbooks (SOP).** YAML advice for the agent, with a linter; six examples: interface down, OSPF adjacency, BGP session, device restart, high CPU, interface errors.
- **Scheduled inspection.** Trend detectors on Zabbix history, live read-only status checks, history, diff against the previous run, Markdown/HTML export.
- **Command audit.** Every command the agent ran and every command that was refused.
- **Dashboard.** Chinese and English: overview, incidents, topology, inspection, audit, settings. A demo mode masks IP addresses and IDs.
- **Demo scripts.** `tools/demo_replay.py` and `tools/seed_demo.py` populate the dashboard without a lab.

## How it works

- **Merge.** Alerts that arrive close together wait for a short window (`ALERT_WINDOW_*`) and are merged into one incident, so a single link failure with many derived alerts yields one conclusion. Alerts from topologically adjacent devices can be merged as well.
- **Investigate.** One tool-calling loop with a token budget, call limits, duplicate-call blocking and a no-progress stop. Playbooks (YAML) only advise the agent; they never execute anything.
- **Two-layer read-only guard.** Layer 1 is code: every device command is checked against a whitelist before it is sent. Layer 2 is a read-only account created on the device. The Zabbix client has a method whitelist in the same way.

<p align="center"><img src="docs/images/guard.png" alt="two-layer read-only guard" width="900"></p>

- **Conclude.** The model fills a strict schema. For each of six hypothesis families (local action, local hardware/resource, remote/upstream, link/path quality, management plane, monitoring artifact) it must report supported, ruled out (with counter-evidence) or undetermined. An undetermined result must state which data and which command would settle it.
- **Inspect (before anything alerts).** Two checks, both decided by fixed rules rather than the model. *Trend inspection* reads only Zabbix history and runs three detectors: a sustained one-way trend (e.g. error counters climbing), a periodic spike, and a self-healing flap. *Status inspection* logs into each device read-only and checks interface up/up, OSPF neighbors all FULL, BGP sessions Established, and error-counter growth since the last run. Every finding quotes the device's own output. An optional model call then sorts findings into "handle tonight / ignore (with reason) / can't tell". Each run is stored so the page shows what is new or gone, and reports export to Markdown/HTML. Thresholds and scope live in `inspection.yaml`.

## Tools

**What the agent can call** — all read-only, registered in one place; the full list is in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md#the-tools-the-agent-can-use).

| Tool | What it does |
|---|---|
| `zbx_*` | Read-only Zabbix queries: hosts, items, history, trends, syslog, problems, top talkers, chart |
| `device_show` | Run a `show` command on a device, after the command whitelist |
| `topology_neighbors` | Neighbors of a device or interface — from NetBox if configured, else `topology.yaml` |
| `nb_devices`, `nb_topology` | NetBox inventory (read-only, when NetBox is configured) |
| `sop_lookup` | Find the matching playbook — it advises, it never executes |
| `run_inspection`, `get_analysis` | Run an inspection, look up an earlier conclusion |

**Command-line tools** we ship, all runnable offline unless noted:

| Command | What it is for |
|---|---|
| `python zbx-cli.py hosts` | Read-only Zabbix CLI that shares the agent's tool definitions; `python zbx-cli.py tools` prints them |
| `python -m netops_ai.netbox_cli devices` | Read-only NetBox inventory CLI (`devices`, `neighbors <dev>`, `topology`); needs `NETBOX_URL` and `NETBOX_TOKEN` |
| `python tools/demo_replay.py` | Replay saved alert records and render the Feishu card, no network needed |
| `python tools/seed_demo.py` | Fill `records/` with six synthetic incidents so the dashboard has data |
| `python tools/sop_lint.py` | Lint playbooks: real tools, real parameters, commands the whitelist accepts |
| `python tools/llm_doctor.py` | Check your LLM endpoint with a multi-turn tool-call replay (calls the model) |

## What it looks like

Everything below comes from real runs: a virtual Cisco lab (7 nodes, 3 layers), faults injected by hand on the devices, a real Zabbix, NetBox and LLM. No mock data. The dashboard's demo mode masks IP addresses.

### A real fault, end to end

Fault injected: `neighbor 10.0.0.2 shutdown` on V1. Six alerts arrived from both ends of the session (SNMP traps, syslog, Zabbix triggers). They were merged into **one incident**, investigated with read-only tools, and delivered as one card and one dashboard page.

**The card that lands in the chat** (rendered from the card JSON of that incident):

<p align="center"><img src="docs/images/real-card.png" alt="Feishu card" width="640"></p>

**The same incident on the dashboard** — conclusion, six pieces of quoted evidence, the alert timeline from both devices, the investigation steps, and the six-hypothesis checklist:

<p align="center"><img src="docs/images/real-incident-en.png" alt="Incident page" width="900"></p>

### The rest of the dashboard

**Overview** — how many incidents were handled, how fast, how many were escalated to a human, how many commands the whitelist blocked:

<p align="center"><img src="docs/images/real-overview-en.png" alt="Overview" width="900"></p>

**Devices and topology** — layered topology read from NetBox, recent faults overlaid:

<p align="center"><img src="docs/images/real-topology-en.png" alt="Topology" width="900"></p>

**Command audit** — every command the agent ran and every one the whitelist refused:

<p align="center"><img src="docs/images/real-audit-en.png" alt="Command audit" width="900"></p>

**Automated inspection** — trend findings plus read-only status checks of every device (screenshot in the Chinese UI):

<p align="center"><img src="docs/images/ui-inspection.png" alt="Inspection" width="900"></p>

The screenshots above are mostly the English UI; the dashboard also has a Chinese UI.

### What we injected and what it said

Each fault was injected by hand on the lab devices and restored afterwards. Typical outcomes are listed below; results vary between runs because the investigation is performed by an LLM.

| Fault | What the system concluded |
|---|---|
| Interface shut down manually | Shutdown from the console; high confidence |
| BGP neighbor shut down | One incident covering both ends: shutdown configured on V1 |
| Both ends of one link shut down in sequence | One incident naming both interfaces as the two ends of the same link |
| OSPF hello / authentication / area mismatch, passive interface | One incident naming the mismatch; in earlier runs the far-side card occasionally missed it |
| Device `reload` | Reload identified; whether a person or a script initiated it is reported as undetermined |
| Interface flapping | Repeated shutdown / no shutdown from the console |
| Fault that heals itself within 45 s | Shutdown reported, with the note that it has already recovered |
| Two unrelated faults at once (an access port and a BGP neighbor) | Kept as separate incidents |
| Whole device powered off | Each neighbor concludes that the remote device is unreachable, with the undetermined points listed; the neighbor cards are not yet merged into one incident |

Validated in a lab environment only (virtual Cisco topology); not tested in production.

## Deployment

Three levels, each usable without the next. Commands: [docs/INSTALL.md](docs/INSTALL.md). **Step-by-step Zabbix and NetBox setup, with diagrams: [docs/DEPLOY.md](docs/DEPLOY.md).**

<p align="center"><img src="docs/images/deployment.png" alt="deployment overview" width="1000"></p>

```bash
git clone https://github.com/qifawu/netops-ai.git && cd netops-ai
python -m venv .venv && . .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt                   # Python 3.13
```

1. **Check the install** — `python -m pytest tests -q` (no devices, model or credentials needed).
2. **Dashboard** — `cd web && npm ci && npm run build && cd ..`, then (optional) `python tools/seed_demo.py` to fill it with six synthetic incidents, then `python -m uvicorn netops_ai.api.app:app --host 127.0.0.1 --port 8000` and open http://127.0.0.1:8000.
3. **Full pipeline** — `cp env.example .env` and fill in: Zabbix (read-only user), devices (**read-only account**), an LLM endpoint, optionally Feishu; list your devices in `topology.yaml`; in Zabbix add a webhook media type that posts to `/webhooks/zabbix`. What exactly to click in Zabbix and NetBox: [docs/DEPLOY.md](docs/DEPLOY.md).

The API and dashboard have **no authentication**: bind uvicorn to `127.0.0.1` and place an authenticating reverse proxy in front (nginx example in [INSTALL.md](docs/INSTALL.md#securing-the-api-and-dashboard)).

## Lab environment

<p align="center"><img src="docs/images/lab-topology.png" alt="the reference lab" width="1000"></p>

Developed and tested on a virtual EVE-NG lab: 7 Cisco IOS nodes in three layers running OSPF and BGP, monitored by Zabbix 7.0. Faults were injected by hand on the devices. Cisco IOS only; not tested in production. Device images and configs are not included. Details: [docs/LAB.md](docs/LAB.md).

## Extension points

Where the code is built to be extended:

- **More vendors.** Each one is a command-whitelist ruleset, a command catalog and a few playbooks. Cisco IOS is the template.
- **More playbooks.** Six ship today. A playbook is a small YAML file; [docs/PLAYBOOK-FORMAT.md](docs/PLAYBOOK-FORMAT.md) has a ten-minute guide.
- **More notification channels** next to Feishu (chat webhooks, e-mail).
- **Built-in authentication** for the dashboard and webhook, so a reverse proxy is optional.
- **Richer inspection rules** and more status checks (STP, port-channel, power and fan).

## License

Apache-2.0 — [LICENSE](LICENSE).
