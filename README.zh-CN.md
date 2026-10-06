# netops-ai

[English](README.md) | **简体中文**

**告警驱动的只读 AI 网络排障。** Zabbix 告警进来，AI 用只读命令去查设备，给出根因结论并引用它依据的原文，推到飞书卡片和网页看板。

[部署](docs/INSTALL.zh-CN.md) · [实验环境](docs/LAB.zh-CN.md) · [架构](docs/ARCHITECTURE.zh-CN.md) · [剧本](docs/PLAYBOOK-FORMAT.zh-CN.md) · [贡献](CONTRIBUTING.zh-CN.md)

<p align="center"><img src="docs/images/architecture.png" alt="architecture" width="1000"></p>

## 它做什么

1. Zabbix 把告警 `POST` 到 `/webhooks/zabbix`。
2. 相关告警合并成一个事件。
3. agent 只用只读工具取证：查 Zabbix、在设备上敲 `show` 命令、查拓扑邻居、读你的剧本。
4. 给出结构化结论：根因、置信度、引用的证据原文；判不出来时，写清还差什么、用哪条命令能判。
5. 结果推到飞书卡片和网页看板。

**它什么都不会改。** 设备命令先过代码里的白名单（只放行写全的 `show …`），你还要给它一个只读的设备账号。被拒的命令根本不会发出去。

## 工作原理

- **合并。** 时间上靠近的告警在一个短窗口里（`ALERT_WINDOW_*`）攒起来合成一个事件，一条链路故障带出十条连带告警，只给一个结论，不是十个。
- **取证。** 一个工具循环：有 token 预算、调用次数上限、重复调用拦截、无进展就停。剧本（YAML）只给 agent **建议**，从不执行。
- **两层只读守卫。** 第一层是代码：每条设备命令发出前先过白名单；第二层是你在设备上建的只读账号。Zabbix 客户端同样有方法白名单。

<p align="center"><img src="docs/images/guard.png" alt="两层只读守卫" width="900"></p>

- **结论。** 模型填一份严格的结构：六类假设（本端操作、本机硬件或资源、对端或上游、链路质量、管理面、监控采集自身问题）逐个表态，支持 / 已排除（要给反证）/ 暂时判不了；判不了的要写清还差什么数据、用哪条命令能判。

## 功能清单

- 告警流水线：webhook、合并窗口、事件去重、飞书卡片
- 通过 SSH/Telnet 只读访问设备（Cisco IOS），命令白名单在前；只读的 Zabbix 客户端
- 结构化结论：六类假设清单，判不出时老实说「判不出」
- 剧本（SOP）只做建议，带 linter；六个示例：接口 down、OSPF 邻接、BGP 会话、设备重启、CPU 高、接口错误
- 定时巡检：读 Zabbix 历史的趋势检测 + 只读登设备的实时状态检查，有历史、与上次对比、Markdown/HTML 导出
- 命令审计：AI 跑过的每条命令、被拒的每条命令
- 本地文档检索（索引随仓库是空的，用 `tools/kb_ingest.py` 灌库）
- 中英文网页看板：总览、告警与结论、拓扑、巡检、审计、知识库、设置；演示模式自动遮住 IP 和编号，方便截图
- 离线回放和回归工具（`tools/demo_replay.py`、`tools/run_regression.py`）

## 工具

**agent 能调用的工具**——全部只读、统一注册；完整列表见 [docs/ARCHITECTURE.zh-CN.md](docs/ARCHITECTURE.zh-CN.md#agent-能用的工具)。

| 工具 | 作用 |
|---|---|
| `zbx_*` | Zabbix 只读查询：主机、监控项、历史、趋势、syslog、问题、流量排行、图表 |
| `device_show` | 过命令白名单后，在设备上执行 `show` 命令 |
| `topology_neighbors` | 从 `topology.yaml` 查设备或接口的邻居 |
| `sop_lookup` | 查找匹配的剧本——只给建议，从不执行 |
| `doc_search` | 对你自己的文档做关键词（BM25）检索 |
| `run_inspection`、`get_analysis` | 跑一次巡检、查之前的结论 |

**我们带的命令行工具**，除标注的外都能离线运行：

| 命令 | 用途 |
|---|---|
| `python zbx-cli.py hosts` | 只读 Zabbix 命令行，和 agent 共用同一份工具定义；`python zbx-cli.py tools` 会打印它们 |
| `python tools/demo_replay.py` | 回放落盘的告警记录并渲染飞书卡片，不需要网络 |
| `python tools/seed_demo.py` | 往 `records/` 灌 6 条合成告警，让看板有数据 |
| `python tools/sop_lint.py` | 检查剧本：工具真实、参数真实、命令能过白名单 |
| `python tools/kb_ingest.py <目录>` | 用 `.md/.txt/.html/.pdf` 建本地文档索引（SQLite FTS5） |
| `python tools/llm_doctor.py` | 用多轮工具调用回放检查你的大模型接口（会调模型） |
| `python -m tools.run_regression <用例>` | 把一个录下来的用例多次送进模型回放（会调模型） |

## 效果

离线演示（不需要网络、设备、模型）：

<p align="center"><img src="docs/images/demo-replay.png" alt="离线回放" width="860"></p>

网页看板：

| | |
|---|---|
| 系统总览<br><img src="docs/images/ui-overview.png" width="460"> | 告警与结论<br><img src="docs/images/ui-incident.png" width="460"> |
| 命令审计<br><img src="docs/images/ui-audit.png" width="460"> | 设备与拓扑<br><img src="docs/images/ui-topology.png" width="460"> |

### 看板和卡片的更多页面

| | |
|---|---|
| 飞书卡片——推到群里的样子。左：找到了根因；右：老实说「判不出」并给出下一条该敲的命令<br><img src="docs/images/feishu-card.png" width="460"> | 自动化巡检——趋势发现加对每台设备的只读状态检查<br><img src="docs/images/ui-inspection.png" width="460"> |
| 知识库——检索你自己的文档；图里是 `examples/kb/` 的三篇示例笔记<br><img src="docs/images/ui-knowledge.png" width="460"> | 系统设置——在页面里改 `.env`，密钥掩码显示<br><img src="docs/images/ui-settings.png" width="460"> |

卡片图是用两条合成示例的卡片 JSON 渲染的示意图（不是飞书客户端截图）。`python tools/seed_demo.py` 会灌入六条合成告警，不用任何配置就能把所有页面点一遍。

## 怎么部署

三个层级，互不依赖。完整步骤见 [docs/INSTALL.zh-CN.md](docs/INSTALL.zh-CN.md)。

```bash
git clone https://github.com/qifawu/netops-ai.git && cd netops-ai
python -m venv .venv && . .venv/bin/activate     # Windows：.venv\Scripts\activate
pip install -r requirements.txt                   # Python 3.13
```

1. **离线体验** —— `python tools/demo_replay.py`（也可以 `python -m pytest tests -q`）。
2. **看板** —— `cd web && npm ci && npm run build && cd ..`，（可选）`python tools/seed_demo.py` 灌入六条合成告警，再 `python -m uvicorn netops_ai.api.app:app --host 127.0.0.1 --port 8000`，打开 http://127.0.0.1:8000 。
3. **完整链路** —— `cp env.example .env` 后填写：Zabbix（只读用户）、设备（**只读账号**）、大模型接口，飞书可选；在 `topology.yaml` 里写你的设备；Zabbix 里加一个 webhook 媒介类型，POST 到 `/webhooks/zabbix`。

接口和看板**没有登录**：uvicorn 只监听 `127.0.0.1`，前面放一个带认证的反向代理（nginx 示例见 [INSTALL.zh-CN.md](docs/INSTALL.zh-CN.md#保护接口和看板)）。

## 开发和测试用的环境

EVE-NG 里的一套虚拟网络：三层 7 台 Cisco 节点（2 台核心 + 2 台汇聚 IOSv，3 台接入 IOSv-L2），跑 OSPF 和一条 BGP 会话；一台 Zabbix 7.0 用 SNMP 轮询并接收 trap。故障是在设备上手工制造的（接口 shutdown、OSPF 邻居丢失、BGP 会话 shutdown、reload），再检查 agent 的结论。仅支持 Cisco IOS，没有在生产网络跑过。仓库不含设备镜像和配置。详情和自己怎么搭一个：[docs/LAB.zh-CN.md](docs/LAB.zh-CN.md)。

## 接下来可能的方向

欢迎一起做的方向，都不承诺时间。

- **更多厂商。** 每个厂商是一套命令白名单规则、一份命令目录和几个剧本，Cisco IOS 就是模板。
- **更多剧本。** 现在带 6 个。剧本是一个小 YAML，[docs/PLAYBOOK-FORMAT.zh-CN.md](docs/PLAYBOOK-FORMAT.zh-CN.md) 里有十分钟上手指南。
- **飞书之外的通知渠道**（群聊 webhook、邮件）。
- **内置登录**，让反向代理变成可选。
- **更丰富的巡检规则**和状态检查（STP、端口聚合、电源风扇）。

## 许可证

Apache-2.0，见 [LICENSE](LICENSE)。
