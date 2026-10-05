# 架构

[English](ARCHITECTURE.md) | **简体中文**

这份文档说明一条告警是怎么变成结论的，以及各部分为什么是现在这个样子。

## 告警管道

```
Zabbix 触发器 ──► POST /webhooks/zabbix ──► 立刻返回 200（Zabbix webhook 60 秒硬超时）
                          │
                          ▼  后台任务  (netops_ai/api/pipeline.py)
        ┌─────────────────────────────────────────────────────────────┐
        │ 1. 收集窗口   时间上靠近的告警先攒一小会儿，                  │
        │               合并成一个事件 (netops_ai/incident/)            │
        │ 2. 取证       一个带只读工具的工具循环                        │
        │               (netops_ai/graph/agent_loop.py)                 │
        │ 3. 结论       严格 schema 的结构化输出                        │
        │               (netops_ai/analysis/schema.py)                  │
        │ 4. 上报       飞书卡片 + 落盘记录                             │
        │               (netops_ai/feishu/, records/alert-*.json)       │
        └─────────────────────────────────────────────────────────────┘
                          │
                          ▼
                 看板读取记录 (netops_ai/api/dashboard.py → web/)
```

模型说完之后的上报这一步是确定性代码，所以能离线重放——见 `tools/demo_replay.py`。

## agent 能用的工具

所有工具都是只读的，统一注册在一个地方（`netops_ai/graph/chat_agent.py` 的 `build_chat_tools`；名字在 `graph/tool_names.py`）：

| 工具 | 作用 |
|---|---|
| `zbx_*` | Zabbix 只读查询——主机、监控项、历史、趋势、syslog、问题、流量排行、图表（由 `zabbix/cli.py` 生成，命令行和工具共用同一张规格表） |
| `device_show`、`device_show_many` | 过白名单后在设备上执行 `show` 命令 |
| `topology_neighbors` | 从 `topology.yaml` 查某台设备/接口的邻居 |
| `sop_lookup` | 查找匹配的剧本（只给建议，从不执行） |
| `doc_search` | 对你本地文档索引做 BM25 检索 |
| `run_inspection`、`list_analyses`、`get_analysis` | 跑/读巡检，查之前的结论 |

工具结果只返回事实。结果为空就说为空并回显查询条件；被截断就说被截断。「下一步试试 X」这类路由建议只放在系统提示或剧本里，从不放进工具结果，这样不管哪个 agent 来调用，工具都保持中立。

### 两层只读守卫

1. **命令白名单**（`netops_ai/devices/whitelist.py`）——在 `DeviceAdapter.run()` 里判断，所以被拒的命令根本不会发出。规则：只放行写全的命令（不认缩写，`sh`、`conf t` 都会被拒）；先查禁令动词再查放行；管道最多一个，后面只允许 `include/exclude/begin/section/count`；拒绝 `; & | > < \` $ " '` 等 shell 元字符、控制字符、非 ASCII 字符、超过 200 字符的命令；`ping`/`traceroute` 默认关闭，需要显式启用；`terminal length 0` 和唯一一种 `more system:running-config` 写法是特例放行。查看配置类命令的输出会被标成 `sensitive`。
2. **设备侧只读账号。** 这一层代码替你保证不了。IOS 上 `enable` 没设 secret 时，低权限等级不是边界：要在真实 VTY（不是 console 代理）上验证写命令确实被拒。

Zabbix 客户端也是同样的结构：每个调用发出前都先过方法白名单。

## 结构化结论

`analysis/schema.py` 定义了一份模型必须填的严格 JSON schema：

- `root_cause`、`headline`、`confidence`
- `evidence[]`——每条是 `{claim, source, source_from}`，`source` 是从原始数据里复制的原文，`source_from` 说明它来自哪份输入（`zabbix` 监控数据或 `device` 设备输出）
- `hypothesis_checklist`——六类假设，每类取值 `supported | ruled_out | cannot_determine`；标 `ruled_out` 的必须带上直接反驳它的 `counter_evidence`（没找到证据不算反证，一个当前状态的快照也不能排除已经结束的故障）
- `undistinguishable_candidates[]`——有两类以上假设没分出来时，要点名、写清为什么分不开、什么数据能分开、拿数据的具体命令、谁去拿（agent 再试一次，还是人）
- `alert_roles`、`grouping`——哪条告警是根、哪些是连带

## 工具循环

`graph/agent_loop.py` 是唯一的循环。告警取证和其它要调工具的场景共用它，带有：token 预算和工具调用/迭代次数上限（一跳做完才检查预算，所以上限是软的，最多超冲一跳）；重复调用拦截（同一工具同样参数，直接用前一次的结果回答并记为「没有新信息」）；重复多次后无进展就中止；对相同只读调用的可选短时缓存；以及被截断时让模型总结已有信息的收尾调用。

## 剧本（SOP）

`netops_ai/playbooks/` 加载 YAML 剧本，并按厂商、触发器名子串、标签把它们匹配到告警。剧本是**给 agent 的建议**：`sop_lookup` 返回匹配到的步骤，每步有 `why`、`expect`、动作（工具加参数）和分支。引擎不执行步骤；agent 仍然自己调工具，实际做了什么记成 `sop_usage`（走了哪几步、哪里偏离）。`playbooks/lint.py` 检查每个动作用的都是已注册的只读工具、参数名真实、命令能过白名单。格式见 [PLAYBOOK-FORMAT.zh-CN.md](PLAYBOOK-FORMAT.zh-CN.md)。

## 巡检（告警之前）

两种互补的检查（`netops_ai/inspection/`）：

- **趋势巡检**只读 Zabbix 历史，跑三个纯函数检测器：持续单向变化、周期性冲高、反复抖动又自愈。阈值和扫描范围放在 `inspection.yaml`（可在界面里改）。
- **状态巡检**只读登录每台设备，按固定规则（不用模型）检查接口状态、OSPF 邻居、BGP 会话和错误计数增长。每条发现都带着设备自己的输出当证据。

可选的一次模型调用，把发现整理成「今晚要处理什么 / 可以忽略什么（写理由）/ 判不了」。每次运行都紧凑地存下来，页面能显示和上次相比新增了什么、消失了什么，报告可以导出成 Markdown/HTML。

## 网页看板的结构

`web/` 是 Vite + React + TypeScript 应用，构建后（`web/dist`）由 FastAPI 进程提供。它只读 `netops_ai/api/` 里的 JSON 接口；页面有：总览、告警与结论、设备与拓扑、巡检、命令审计、知识库、设置。演示模式开关会遮住 IP 和编号，方便截图。

## 改东西之前值得知道的设计规则

- 只有超了才能发现的上限不叫上限：预算在两跳之间检查，并如实标成软上限。
- 出错就说为什么。兜底（比如某个台账来源连不上）要记录原因，而不是悄悄用旧数据。
- 不猜：不认识的厂商直接抛错，没有已知价格的模型不给成本估算，而不是编一个。
- 测试不能碰真实的记录、账本或凭据。测试包在任何 import 之前就把这些位置重定向到临时目录。
