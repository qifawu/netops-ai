# Install & run

**English** | [简体中文](INSTALL.zh-CN.md)

Three levels, each works without the next: **(1)** tests and the offline replay demo — nothing external needed; **(2)** the web dashboard against sample data; **(3)** the full pipeline against Zabbix, devices and an LLM.

## Requirements

- Python **3.13** (see `pyproject.toml`)
- Node.js 20+ and npm — only to build the dashboard
- For level 3: a Zabbix 7.0 server, SSH/Telnet reachability to devices with a **read-only account**, an OpenAI-compatible (or Gemini/Anthropic) LLM endpoint

## 1. Tests and the offline replay (5 minutes)

```bash
git clone https://github.com/qifawu/netops-ai.git && cd netops-ai
python -m venv .venv
. .venv/bin/activate                 # Windows: .venv\Scripts\activate
pip install -r requirements.txt

python -m pytest tests -q            # no devices, models or credentials needed
python tools/demo_replay.py          # replays 6 synthetic alerts and renders the Feishu card
python tools/demo_replay.py --card   # also print the Feishu card JSON
```

Expected from the replay: six records and a summary line (`汇总：6 条记录。`). Some of them honestly say "could not determine" and name what is missing; those cards are not green.

To replay your own record, pass the path of an `alert-*.json` produced by the pipeline: `python tools/demo_replay.py records/alert-123.json`.

## 2. Dashboard

```bash
cd web
npm ci
npm run build                        # produces web/dist, served by the API process
cd ..
python tools/seed_demo.py            # optional: fill the dashboard with 6 synthetic incidents
python -m uvicorn netops_ai.api.app:app --host 127.0.0.1 --port 8000
# open http://127.0.0.1:8000
```

Without a configured Zabbix/LLM the pages show empty states. `GET /healthz` returns `{"status":"ok"}`. For frontend development run `npm run dev` in `web/` (Vite proxies `/api` to `http://127.0.0.1:8000`, so start the API first).

**The API and dashboard have no authentication. Bind to localhost or put them behind an authenticating reverse proxy** (next section).

## Securing the API and dashboard

Anyone who can reach the port can read incident conclusions, and the **Settings** page can rewrite `.env` (secrets are masked on screen, but webhook and model endpoints are editable). So:

1. Keep uvicorn on `--host 127.0.0.1`, and publish it only through a reverse proxy that authenticates users.
2. Zabbix cannot log in through a browser prompt, so expose `/webhooks/zabbix` separately and restrict it by source address instead.

Example (nginx, TLS settings omitted):

```nginx
server {
    listen 443 ssl;
    server_name netops.example.internal;
    # ssl_certificate / ssl_certificate_key ...

    # Dashboard and API: password required.  htpasswd -c /etc/nginx/netops.htpasswd alice
    location / {
        auth_basic           "netops-ai";
        auth_basic_user_file /etc/nginx/netops.htpasswd;
        proxy_pass           http://127.0.0.1:8000;
    }

    # Zabbix webhook: no password, only your Zabbix server may call it
    location = /webhooks/zabbix {
        allow 203.0.113.10;        # your Zabbix server
        deny  all;
        proxy_pass http://127.0.0.1:8000;
    }
}
```

With this setup, point the Zabbix media type's `url` parameter at `https://netops.example.internal/webhooks/zabbix`, and the API process itself stays on localhost. If you use a different proxy, keep the same two rules: authenticate everything except the webhook, and restrict the webhook by source address.

## 3. Full pipeline

### 3.1 Configure

```bash
cp env.example .env       # then edit; .env is git-ignored
```

You can also edit most values on the dashboard's **Settings** page, which reads and writes `.env` (secrets are masked and never echoed back).

| Group | Keys | Notes |
|---|---|---|
| Zabbix | `ZABBIX_URL`, `ZABBIX_USER`, `ZABBIX_PASSWORD` | Use a read-only Zabbix user. Only whitelisted API methods are ever called |
| Devices | `DEVICE_USERNAME`, `DEVICE_PASSWORD`, `DEVICE_VENDOR=cisco`, `DEVICE_TRANSPORT=ssh\|telnet`, `DEVICE_PORT` | **Must be a read-only account.** The command whitelist is the first layer, not the only one |
| LLM | `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL` (`LLM_PROVIDER` optional) | `python tools/llm_doctor.py` checks the transport with a multi-turn tool replay |
| Feishu | `FEISHU_WEBHOOK_URL`, or `FEISHU_APP_ID` / `FEISHU_APP_SECRET` / `FEISHU_CHAT_ID` | Optional; without it conclusions are still stored and shown on the dashboard |
| Budgets | `ANALYSIS_TOKEN_BUDGET` (default 60000) | Soft cap per investigation |
| Topology | `topology.yaml` | Devices, management IPs, aliases (e.g. the Zabbix host name `D1-vios` for device `D1`) and links. **Aliases must be listed explicitly — there is no fuzzy matching** |

### 3.2 Devices: the read-only account

On each IOS device create a privilege-1/low-privilege account for SSH, **and verify it on a real VTY session**: write commands must be rejected. If `enable` has no secret configured, privilege-1 users can reach privileged mode without a password on IOS — set an `enable secret`. Do not test through an EVE-NG console proxy: it uses `line con 0`, which does not follow your VTY login rules.

Smoke-test the whitelist (no device needed): `python -c "from netops_ai.devices.whitelist import check; print(check('cisco','show version').allowed, check('cisco','configure terminal').allowed)"` prints `True False`. Smoke-test Zabbix access with `python zbx-cli.py hosts` (read-only CLI that shares the agent's tool definitions).

### 3.3 Zabbix → webhook

Create a Zabbix **webhook media type** whose script POSTs JSON to your API:

```
POST http://<api-host>:8000/webhooks/zabbix
{"eventid": "...", "name": "...", "severity": "...", "clock": "...", "hostid": "...", "host": "..."}
```

Only `eventid` and `name` are required. A minimal Zabbix JS script:

```js
var p = JSON.parse(value);
var req = new HttpRequest();
req.addHeader('Content-Type: application/json');
req.post(p.url, JSON.stringify({eventid: p.eventid, name: p.name, severity: p.severity, hostid: p.hostid, host: p.host}));
if (req.getStatus() !== 200) { throw 'webhook returned ' + req.getStatus(); }
return 'OK';
```

with media-type parameters `url`, `eventid={EVENT.ID}`, `name={EVENT.NAME}`, `severity={EVENT.NSEVERITY}`, `hostid={HOST.ID}`, `host={HOST.NAME}`. Then add a trigger **action** (event source: trigger problems) whose operation sends to that media type. Things that bit us in the lab:

- A media type created through the API is **disabled by default**; set `status: 0` or enable it in the UI, or the first alert silently never arrives.
- The webhook URL must be reachable from the Zabbix server: either the API listens on an address Zabbix can reach (`--host 0.0.0.0` or a specific interface — only on a trusted network, see [Securing](#securing-the-api-and-dashboard)), or Zabbix posts to the reverse proxy in front of it.
- Zabbix webhook scripts time out at 60 s; the endpoint returns immediately and analyses in the background.

Trigger a test: `curl -X POST http://127.0.0.1:8000/webhooks/zabbix -H 'content-type: application/json' -d '{"eventid":"1","name":"Interface Gi0/1(): Link down","host":"A1"}'`. A record is written to `records/alert-1.json` (it will carry errors for whatever you have not configured yet) and shows up on the dashboard.

### 3.4 Optional pieces

- **Zabbix in docker**: `deploy/zabbix/` — see its README.
- **SNMP traps** (faster than polling for link/OSPF/BGP events): `deploy/trap/` contains lab scripts to install `snmptrapd` and add trap items. They write device/guest configuration in a lab; read them before running anything.
- **A lab to try it on**: [LAB.md](LAB.md) describes the reference lab (7 Cisco nodes, Zabbix, traps, the faults we injected) and how to rebuild something similar.
- **Documentation search**: the index ships empty. Try it with the three sample notes: `python tools/kb_ingest.py examples/kb`, then open the Knowledge page. For your own use, ingest documents you are licensed to use (`python tools/kb_ingest.py --help`); the agent's `doc_search` tool then returns passages labelled as vendor documentation (never as device evidence).
- **Regression runs**: `python -m tools.run_regression spike-demo` replays a case directory (`experiments/spike/<case>/input-zabbix.md` + `input-device.md`) through the model several times. A synthetic `spike-demo` case is included; this one calls your LLM.
- **Scheduled inspection**: runs every `SCHEDULE_INTERVAL_MINUTES` (default 60) while the API process is up.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| Alerts never arrive | Media type disabled; API bound to localhost; firewall |
| Analysis says "connection error" | LLM endpoint unreachable — run `tools/llm_doctor.py`. Failed analyses are recorded with `status: analysis_failed` and are **not retried automatically** |
| Everything "unknown host" | Alias missing in `topology.yaml` — add the Zabbix host name as an alias |
| Commands rejected | Working as intended — check the audit page for the denial reason |
