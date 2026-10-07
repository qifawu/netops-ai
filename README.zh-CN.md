# netops-ai

[English](README.md) | **简体中文**

**告警驱动的只读 AI 网络排障。** Zabbix 告警进来，AI 用只读命令去查设备，给出根因结论并引用它依据的原文，推到飞书卡片和网页看板。

[部署](docs/INSTALL.zh-CN.md) · [接入 Zabbix 和 NetBox](docs/DEPLOY.zh-CN.md) · [实验环境](docs/LAB.zh-CN.md) · [架构](docs/ARCHITECTURE.zh-CN.md) · [剧本](docs/PLAYBOOK-FORMAT.zh-CN.md) · [贡献](CONTRIBUTING.zh-CN.md)

<p align="center"><img src="docs/images/architecture.zh-CN.png" alt="architecture" width="1000"></p>

## 功能

- **告警流水线。** Zabbix 把告警 `POST` 到 `/webhooks/zabbix`；相关告警合并为一个事件；agent 只用只读工具取证；结论推送到飞书卡片和网页看板。
- **只读由代码保证。** 设备命令先过代码层白名单（只放行写全的 `show …`），并使用设备上的只读账号执行；Zabbix 客户端同样有方法白名单。
- **结构化结论。** 根因、置信度和引用的证据原文，并按六类假设逐项核对；无法判定时，写明缺少什么数据、用哪条命令可以确定。
- **拓扑。** 来自 NetBox（可选、只读，支持 webhook 刷新）或 YAML 文件。
- **剧本（SOP）。** YAML 格式的建议，只供 agent 参考，带 linter；六个示例：接口 down、OSPF 邻接、BGP 会话、设备重启、CPU 高、接口错误。
- **定时巡检。** 基于 Zabbix 历史的趋势检测、只读登录设备的实时状态检查、历史记录、与上次结果对比、Markdown/HTML 导出。
- **命令审计。** 记录 agent 执行过的每条命令和被拒绝的每条命令。
- **网页看板。** 中英文界面：总览、告警与结论、拓扑、巡检、审计、设置；演示模式会遮盖 IP 地址和编号。
- **演示脚本。** `tools/demo_replay.py` 和 `tools/seed_demo.py`，无需实验环境即可填充看板。

## 工作原理

- **合并。** 时间上相近的告警在一个短窗口（`ALERT_WINDOW_*`）内汇集并合并为一个事件，因此一条链路故障及其衍生的多条告警只产出一个结论；拓扑上相邻设备的告警也可以合并。
- **取证。** 一个工具调用循环：设有 token 预算、调用次数上限、重复调用拦截和无进展终止。剧本（YAML）只向 agent 提供建议，不执行任何操作。
- **两层只读守卫。** 第一层是代码：每条设备命令发出前先经过白名单检查；第二层是在设备上创建的只读账号。Zabbix 客户端同样设有方法白名单。

<p align="center"><img src="docs/images/guard.zh-CN.png" alt="两层只读守卫" width="900"></p>

- **结论。** 模型按严格的结构输出。对六类假设（本端操作、本机硬件或资源、对端或上游、链路质量、管理面、监控采集自身问题）逐项给出：支持、已排除（须附反证）或无法判定；无法判定时须写明缺少哪些数据、用哪条命令可以确定。
- **巡检（没告警时）。** 两类检查，都由固定规则判定，不交给模型。*趋势巡检*只读 Zabbix 历史，跑三个检测器：持续单向趋势（如错包计数一直在涨）、周期性冲高、反复自愈的抖动。*状态巡检*用只读账号登录每台设备，检查接口 up/up、OSPF 邻居全部 FULL、BGP 会话 Established，以及错误计数相对上次的增量；每条发现都引用设备输出原文。之后可选调一次模型，把发现分成「今晚处理 / 可忽略（附理由）/ 无法判断」。每次结果都会存档，页面显示相比上次新增和消失的问题，报告可导出为 Markdown/HTML。阈值和扫描范围写在 `inspection.yaml`。

## 工具

**agent 能调用的工具**——全部只读、统一注册；完整列表见 [docs/ARCHITECTURE.zh-CN.md](docs/ARCHITECTURE.zh-CN.md#agent-能用的工具)。

| 工具 | 作用 |
|---|---|
| `zbx_*` | Zabbix 只读查询：主机、监控项、历史、趋势、syslog、问题、流量排行、图表 |
| `device_show` | 过命令白名单后，在设备上执行 `show` 命令 |
| `topology_neighbors` | 查设备或接口的邻居——配了 NetBox 就读 NetBox，否则读 `topology.yaml` |
| `nb_devices`、`nb_topology` | NetBox 台账（只读，配了 NetBox 才有） |
| `sop_lookup` | 查找匹配的剧本——只给建议，从不执行 |
| `run_inspection`、`get_analysis` | 跑一次巡检、查之前的结论 |

**我们带的命令行工具**，除标注的外都能离线运行：

| 命令 | 用途 |
|---|---|
| `python zbx-cli.py hosts` | 只读 Zabbix 命令行，和 agent 共用同一份工具定义；`python zbx-cli.py tools` 会打印它们 |
| `python -m netops_ai.netbox_cli devices` | 只读 NetBox 台账命令行（`devices`、`neighbors <设备>`、`topology`）；需要 `NETBOX_URL` 和 `NETBOX_TOKEN` |
| `python tools/demo_replay.py` | 回放落盘的告警记录并渲染飞书卡片，不需要网络 |
| `python tools/seed_demo.py` | 往 `records/` 灌 6 条合成告警，让看板有数据 |
| `python tools/sop_lint.py` | 检查剧本：工具真实、参数真实、命令能过白名单 |
| `python tools/llm_doctor.py` | 用多轮工具调用回放检查你的大模型接口（会调模型） |

## 效果

下面所有图都来自真实运行：一套虚拟 Cisco 实验网络（7 台、三层），故障是在设备上手工制造的，用的是真实的 Zabbix、NetBox 和大模型，没有任何模拟数据。看板的演示模式把 IP 地址遮住了。

### 一次真实故障，从头到尾

注入的故障：在 V1 上执行 `neighbor 10.0.0.2 shutdown`。会话两端共产生 6 条告警（SNMP trap、syslog、Zabbix 触发器），合并为**一个事件**，经只读工具取证后，输出一张卡片和一个看板页面。

**推到群里的卡片**（用这次事件的卡片 JSON 渲染）：

<p align="center"><img src="docs/images/real-card.png" alt="飞书卡片" width="640"></p>

**同一个事件在看板上**——结论、6 条引用的证据原文、两台设备的告警时间线、取证步骤、六类假设清单：

<p align="center"><img src="docs/images/real-incident.png" alt="告警与结论页" width="900"></p>

### 看板的其它页面

**系统总览**——处理了多少故障、多快、多少次老实转人工、白名单拦下多少条命令：

<p align="center"><img src="docs/images/real-overview.png" alt="系统总览" width="900"></p>

**设备与拓扑**——从 NetBox 读的分层拓扑，叠加近期故障：

<p align="center"><img src="docs/images/real-topology.png" alt="设备与拓扑" width="900"></p>

**命令审计**——agent 跑过的每条命令和被白名单拒绝的每条命令：

<p align="center"><img src="docs/images/real-audit.png" alt="命令审计" width="900"></p>

**自动化巡检**——趋势发现加对每台设备的只读状态检查：

<p align="center"><img src="docs/images/ui-inspection.png" alt="自动化巡检" width="900"></p>

上面的截图多数是英文界面；看板也有中文界面。

### 我们注入了哪些故障、它怎么说

每个故障都是在实验设备上手工制造、事后复位的。典型结果如下；由于取证由大模型完成，同一故障多次运行的结论会有波动。

| 故障 | 系统的结论 |
|---|---|
| 接口被手工 shutdown | 控制台执行 shutdown，高置信 |
| BGP 邻居被 shutdown | 一个事件覆盖两端：V1 上配置了 shutdown |
| 同一条链路的两端先后被 shutdown | 合并为一个事件，指出两个接口是同一条链路的两端 |
| OSPF hello / 认证 / area 不一致、passive 接口 | 合并为一个事件并指出不一致项；早期运行中对端的卡片偶尔会漏掉 |
| 设备 `reload` | 判定为 reload；由人还是脚本发起，结论为无法判定 |
| 接口抖动 | 控制台上反复 shutdown / no shutdown |
| 45 秒内自行恢复的故障 | 报告曾被关闭，并注明已经恢复 |
| 两个不相关的故障同时发生（一个接入口、一个 BGP 邻居） | 保持为两个独立事件 |
| 整台设备断电 | 各邻居侧均判定对端不可达，并列出无法判定的部分；各邻居的卡片目前尚未合并为一个事件 |

仅在实验环境（Cisco 虚拟拓扑）中验证，未经过生产环境验证。

## 部署步骤

三个层级，互不依赖。命令见 [docs/INSTALL.zh-CN.md](docs/INSTALL.zh-CN.md)；**Zabbix 和 NetBox 逐步配置（带图）见 [docs/DEPLOY.zh-CN.md](docs/DEPLOY.zh-CN.md)。**

<p align="center"><img src="docs/images/deployment.zh-CN.png" alt="部署总览" width="1000"></p>

```bash
git clone https://github.com/qifawu/netops-ai.git && cd netops-ai
python -m venv .venv && . .venv/bin/activate     # Windows：.venv\Scripts\activate
pip install -r requirements.txt                   # Python 3.13
```

1. **确认装好了** —— `python -m pytest tests -q`（不需要设备、模型和凭证）。
2. **看板** —— `cd web && npm ci && npm run build && cd ..`，（可选）`python tools/seed_demo.py` 灌入六条合成告警，再 `python -m uvicorn netops_ai.api.app:app --host 127.0.0.1 --port 8000`，打开 http://127.0.0.1:8000 。
3. **完整链路** —— `cp env.example .env` 后填写：Zabbix（只读用户）、设备（**只读账号**）、大模型接口，飞书可选；在 `topology.yaml` 里写你的设备；Zabbix 里加一个 webhook 媒介类型，POST 到 `/webhooks/zabbix`。Zabbix 和 NetBox 里具体怎么点：[docs/DEPLOY.zh-CN.md](docs/DEPLOY.zh-CN.md)。

接口和看板**没有认证**：uvicorn 仅监听 `127.0.0.1`，前面应放置带认证的反向代理（nginx 示例见 [INSTALL.zh-CN.md](docs/INSTALL.zh-CN.md#保护接口和看板)）。

## 实验环境

<p align="center"><img src="docs/images/lab-topology.zh-CN.png" alt="参考实验环境" width="1000"></p>

在 EVE-NG 虚拟实验环境中开发和测试：三层共 7 台 Cisco IOS 节点，运行 OSPF 和 BGP，由 Zabbix 7.0 监控；故障在设备上手工注入。仅支持 Cisco IOS，未经生产环境测试；仓库不含设备镜像和配置。详情见 [docs/LAB.zh-CN.md](docs/LAB.zh-CN.md)。

## 可拓展方向

代码是照这些方向留的扩展点：

- **更多厂商。** 每个厂商是一套命令白名单规则、一份命令目录和几个剧本，Cisco IOS 就是模板。
- **更多剧本。** 现在带 6 个。剧本是一个小 YAML，[docs/PLAYBOOK-FORMAT.zh-CN.md](docs/PLAYBOOK-FORMAT.zh-CN.md) 里有十分钟上手指南。
- **飞书之外的通知渠道**（群聊 webhook、邮件）。
- **内置登录**，让反向代理变成可选。
- **更丰富的巡检规则**和状态检查（STP、端口聚合、电源风扇）。

## 许可证

Apache-2.0，见 [LICENSE](LICENSE)。
