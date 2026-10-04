# Install & run / 安装与跑通指南

Three levels, each works without the next: **(1)** tests and the offline replay demo — nothing external needed; **(2)** the web dashboard against sample data; **(3)** the full pipeline against Zabbix, devices and an LLM. 中文版在文末。

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
python tools/demo_replay.py          # replays 3 synthetic alerts through verification, rules and card rendering
python tools/demo_replay.py --card   # also print the Feishu card JSON
```

Expected from the replay: record 1 and 2 verify cleanly with no rule violations (record 2 honestly says "could not determine"); record 3 reports one `✗ fabricated` evidence item and one business-rule violation, and the summary line reads `3 条记录，6 条证据，其中 1 条被判编造`.

To replay your own record, pass the path of an `alert-*.json` produced by the pipeline: `python tools/demo_replay.py records/alert-123.json`.

## 2. Dashboard

```bash
cd web
npm ci
npm run build                        # produces web/dist, served by the API process
cd ..
python -m uvicorn netops_ai.api.app:app --host 127.0.0.1 --port 8000
# open http://127.0.0.1:8000
```

Without a configured Zabbix/LLM the pages show empty states. `GET /healthz` returns `{"status":"ok"}`. For frontend development run `npm run dev` in `web/` (Vite proxies `/api` to `http://127.0.0.1:8000`, so start the API first).

**The API and dashboard have no authentication. Bind to localhost or put them behind an authenticating reverse proxy.**

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
- The API process must listen on an address the Zabbix server can reach (`--host 0.0.0.0` or a specific interface, not `127.0.0.1`).
- Zabbix webhook scripts time out at 60 s; the endpoint returns immediately and analyses in the background.

Trigger a test: `curl -X POST http://127.0.0.1:8000/webhooks/zabbix -H 'content-type: application/json' -d '{"eventid":"1","name":"Interface Gi0/1(): Link down","host":"A1"}'`. A record is written to `records/alert-1.json` (it will carry errors for whatever you have not configured yet) and shows up on the dashboard.

### 3.4 Optional pieces

- **Zabbix in docker**: `deploy/zabbix/` — see its README.
- **SNMP traps** (faster than polling for link/OSPF/BGP events): `deploy/trap/` contains lab scripts to install `snmptrapd` and add trap items. They write device/guest configuration in a lab; read them before running anything.
- **Documentation search**: the index ships empty. `python tools/kb_ingest.py --help`, ingest documents you are licensed to use; the agent's `doc_search` tool then returns passages labelled as vendor documentation (never as device evidence).
- **Regression runs**: `python -m tools.run_regression spike-demo` replays a case directory (`experiments/spike/<case>/input-zabbix.md` + `input-device.md`) through the model several times and checks every output with the same verification and rules. A synthetic `spike-demo` case is included; this one calls your LLM.
- **Scheduled inspection**: runs every `SCHEDULE_INTERVAL_MINUTES` (default 60) while the API process is up.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| Alerts never arrive | Media type disabled; API bound to localhost; firewall |
| Analysis says "connection error" | LLM endpoint unreachable — run `tools/llm_doctor.py`. Failed analyses are recorded with `status: analysis_failed` and are **not retried automatically** |
| Everything "unknown host" | Alias missing in `topology.yaml` — add the Zabbix host name as an alias |
| Commands rejected | Working as intended — check the audit page for the denial reason |

---

## 中文版

**三个层级，互不依赖**：①测试 + 离线回放（不需要任何外部东西）；②网页看板；③完整链路（Zabbix + 设备 + 大模型）。

**①** Python 3.13；`python -m venv .venv`，激活后 `pip install -r requirements.txt`；`python -m pytest tests -q`；`python tools/demo_replay.py`（`--card` 同时打印飞书卡片）。预期：第 1、2 条证据全部逐字核过且无规则违反（第 2 条老实说「判不出」）；第 3 条报 1 条「编造」证据和 1 条业务规则违反，汇总行是「3 条记录，6 条证据，其中 1 条被判编造」。

**②** `cd web && npm ci && npm run build`，回到根目录 `python -m uvicorn netops_ai.api.app:app --host 127.0.0.1 --port 8000`，打开 `http://127.0.0.1:8000`。**接口和看板没有鉴权，只绑本机，或放在带认证的反向代理后面。**

**③** `cp env.example .env` 后填写（也可以在看板「系统设置」页改，密钥只掩码显示）：Zabbix 用只读账号；设备账号**必须是只读账号**，并且要在**真实 VTY** 上验证写命令被拒（IOS 上 `enable` 没设 secret 时低权限用户可以无密码进特权模式，所以要设 `enable secret`；别拿 EVE-NG 的 console 代理来测，那条线不走 VTY 的登录规则）；`topology.yaml` 里**别名要显式写**，没有模糊匹配。Zabbix 侧建一个 **webhook 媒介类型**，把 `{EVENT.ID}/{EVENT.NAME}/…` 包成 JSON `POST http://<api>:8000/webhooks/zabbix`（只有 `eventid`、`name` 必填），再建一个触发器 action 发给它。实验室里踩过的坑：API 建的媒介类型**默认是禁用的**（`status: 0` 才启用），否则第一条告警静默丢失；API 进程不能只绑 `127.0.0.1`；Zabbix webhook 60 秒超时，接口立即返回、后台分析。

失败的分析会记成 `status: analysis_failed`，**不会自动重试**。文档检索索引随仓库是空的，自己用 `tools/kb_ingest.py` 灌你有权使用的文档，`doc_search` 工具就能检索到（结果会标成厂商文档，不会当作设备证据）。
