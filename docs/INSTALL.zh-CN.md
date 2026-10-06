# 安装与跑通指南

[English](INSTALL.md) | **简体中文**

三个层级，互不依赖：**①** 测试和离线回放演示——不需要任何外部系统；**②** 用示例数据看网页看板；**③** 完整链路，接 Zabbix、设备和大模型。

## 环境要求

- Python **3.13**（见 `pyproject.toml`）
- Node.js 20+ 和 npm——只用来构建看板
- 第 ③ 层还需要：一台 Zabbix 7.0 服务器；能用 **只读账号** SSH/Telnet 登录的设备；一个 OpenAI 兼容（或 Gemini/Anthropic）的大模型接口

## 1. 测试和离线回放（5 分钟）

```bash
git clone https://github.com/qifawu/netops-ai.git && cd netops-ai
python -m venv .venv
. .venv/bin/activate                 # Windows：.venv\Scripts\activate
pip install -r requirements.txt

python -m pytest tests -q            # 不需要设备、模型和凭据
python tools/demo_replay.py          # 回放 6 条合成告警并渲染飞书卡片
python tools/demo_replay.py --card   # 同时打印飞书卡片 JSON
```

预期：打印 6 条记录和一行汇总（`汇总：6 条记录。`）。其中几条老实说「判不出」并写明还差什么，这几张卡片不是绿色。

回放你自己的记录：传入流水线落盘的 `alert-*.json` 路径，例如 `python tools/demo_replay.py records/alert-123.json`。

## 2. 网页看板

```bash
cd web
npm ci
npm run build                        # 生成 web/dist，由 API 进程提供
cd ..
python tools/seed_demo.py            # 可选：灌入 6 条合成告警，让看板有数据
python -m uvicorn netops_ai.api.app:app --host 127.0.0.1 --port 8000
# 打开 http://127.0.0.1:8000
```

没配置 Zabbix 和大模型时，页面显示空状态。`GET /healthz` 返回 `{"status":"ok"}`。前端开发时在 `web/` 里跑 `npm run dev`（Vite 把 `/api` 代理到 `http://127.0.0.1:8000`，所以先起 API）。

**接口和看板没有鉴权，只绑本机，或放在带认证的反向代理后面**（见下一节）。

## 保护接口和看板

谁能访问这个端口，谁就能看告警结论，而且 **系统设置** 页能改写 `.env`（密钥在页面上是掩码，但 webhook 地址、模型地址是可改的）。所以：

1. uvicorn 始终用 `--host 127.0.0.1`，只通过带认证的反向代理对外。
2. Zabbix 没法在浏览器里输密码，所以把 `/webhooks/zabbix` 单独放出来，用来源地址限制代替密码。

nginx 示例（省略 TLS 配置）：

```nginx
server {
    listen 443 ssl;
    server_name netops.example.internal;
    # ssl_certificate / ssl_certificate_key ...

    # 看板和接口：必须输密码。 htpasswd -c /etc/nginx/netops.htpasswd alice
    location / {
        auth_basic           "netops-ai";
        auth_basic_user_file /etc/nginx/netops.htpasswd;
        proxy_pass           http://127.0.0.1:8000;
    }

    # Zabbix webhook：不要密码，只允许你的 Zabbix 服务器调用
    location = /webhooks/zabbix {
        allow 203.0.113.10;        # 你的 Zabbix 服务器
        deny  all;
        proxy_pass http://127.0.0.1:8000;
    }
}
```

这样配置后，把 Zabbix 媒介类型的 `url` 参数填成 `https://netops.example.internal/webhooks/zabbix`，API 进程本身一直只监听本机。换成别的代理也守同样两条：除 webhook 外一律要认证，webhook 按来源地址限制。

## 3. 完整链路

### 3.1 配置

```bash
cp env.example .env       # 然后编辑；.env 已被 git 忽略
```

大部分配置也可以在看板的 **系统设置** 页改，它直接读写 `.env`（密钥只掩码显示、永不回显）。

| 分组 | 配置项 | 说明 |
|---|---|---|
| Zabbix | `ZABBIX_URL`、`ZABBIX_USER`、`ZABBIX_PASSWORD` | 用 Zabbix 只读用户。只会调用白名单里的 API 方法 |
| 设备 | `DEVICE_USERNAME`、`DEVICE_PASSWORD`、`DEVICE_VENDOR=cisco`、`DEVICE_TRANSPORT=ssh\|telnet`、`DEVICE_PORT` | **必须是只读账号。** 命令白名单是第一层，不是唯一一层 |
| 大模型 | `LLM_BASE_URL`、`LLM_API_KEY`、`LLM_MODEL`（可选 `LLM_PROVIDER`） | `python tools/llm_doctor.py` 用多轮工具回放检查链路 |
| 飞书 | `FEISHU_WEBHOOK_URL`，或 `FEISHU_APP_ID` / `FEISHU_APP_SECRET` / `FEISHU_CHAT_ID` | 可选；不配的话结论照样落盘并显示在看板上 |
| 预算 | `ANALYSIS_TOKEN_BUDGET`（默认 60000） | 单次取证的软上限 |
| 拓扑 | `topology.yaml` | 设备、管理 IP、别名（例如设备 `D1` 在 Zabbix 里叫 `D1-vios`）和连线。**别名必须显式写，没有模糊匹配** |

### 3.2 设备：只读账号

在每台 IOS 设备上建一个 privilege-1 / 低权限的 SSH 账号，**并在真实 VTY 会话上验证**：写命令必须被拒。IOS 上如果 `enable` 没设 secret，privilege-1 用户可以无密码进特权模式——要设 `enable secret`。不要拿 EVE-NG 的 console 代理来测：它走 `line con 0`，不遵守你的 VTY 登录规则。

冒烟测试白名单（不需要设备）：`python -c "from netops_ai.devices.whitelist import check; print(check('cisco','show version').allowed, check('cisco','configure terminal').allowed)"` 应打印 `True False`。冒烟测试 Zabbix 访问：`python zbx-cli.py hosts`（只读命令行，和 agent 共用同一份工具定义）。

### 3.3 Zabbix → webhook

在 Zabbix 里建一个 **webhook 媒介类型**，脚本把 JSON `POST` 到你的 API：

```
POST http://<api-host>:8000/webhooks/zabbix
{"eventid": "...", "name": "...", "severity": "...", "clock": "...", "hostid": "...", "host": "..."}
```

只有 `eventid` 和 `name` 必填。一个最小的 Zabbix JS 脚本：

```js
var p = JSON.parse(value);
var req = new HttpRequest();
req.addHeader('Content-Type: application/json');
req.post(p.url, JSON.stringify({eventid: p.eventid, name: p.name, severity: p.severity, hostid: p.hostid, host: p.host}));
if (req.getStatus() !== 200) { throw 'webhook returned ' + req.getStatus(); }
return 'OK';
```

媒介类型参数填 `url`、`eventid={EVENT.ID}`、`name={EVENT.NAME}`、`severity={EVENT.NSEVERITY}`、`hostid={HOST.ID}`、`host={HOST.NAME}`。再建一个触发器 **动作**（事件源：触发器问题），操作里发给这个媒介类型。实验室里踩过的坑：

- 通过 API 建的媒介类型 **默认是禁用的**，要设 `status: 0` 或在界面里启用，否则第一条告警静默丢失。
- webhook 地址必须能被 Zabbix 服务器访问：要么 API 监听 Zabbix 够得着的地址（`--host 0.0.0.0` 或指定网卡——只能在可信网络里，见[保护接口和看板](#保护接口和看板)），要么让 Zabbix 去 POST 前面的反向代理。
- Zabbix webhook 脚本 60 秒超时；接口立即返回，分析在后台做。

发一条测试告警：`curl -X POST http://127.0.0.1:8000/webhooks/zabbix -H 'content-type: application/json' -d '{"eventid":"1","name":"Interface Gi0/1(): Link down","host":"A1"}'`。会在 `records/alert-1.json` 生成一条记录（没配好的部分会带错误信息），并出现在看板上。

### 3.4 可选组件

- **Docker 里的 Zabbix**：`deploy/zabbix/`，见它的 README。
- **SNMP trap**（比轮询更快发现链路 / OSPF / BGP 事件）：`deploy/trap/` 里是实验室脚本，用来装 `snmptrapd` 和加 trap 监控项。它们会改实验室里的设备和虚拟机配置，先读再跑。
- **试验用实验室**：[LAB.zh-CN.md](LAB.zh-CN.md) 描述了参考实验环境（7 台 Cisco、Zabbix、trap、制造过的故障）以及怎么自己搭一个类似的。
- **文档检索**：索引随仓库是空的。先用 3 篇示例笔记试一下：`python tools/kb_ingest.py examples/kb`，然后打开知识库页。自己用的话，灌你有权使用的文档（`python tools/kb_ingest.py --help`）；agent 的 `doc_search` 工具返回的片段会标成厂商文档，不会当作设备证据。
- **定时巡检**：API 进程运行期间，每 `SCHEDULE_INTERVAL_MINUTES`（默认 60）分钟跑一次。

## 排障

| 现象 | 可能原因 |
|---|---|
| 告警一直收不到 | 媒介类型被禁用；API 只绑了本机；防火墙 |
| 分析报「connection error」 | 大模型接口连不上——跑 `tools/llm_doctor.py`。失败的分析会记成 `status: analysis_failed`，**不会自动重试** |
| 到处报「unknown host」 | `topology.yaml` 缺别名——把 Zabbix 主机名加进别名 |
| 命令被拒 | 符合预期——去命令审计页看拒绝原因 |
