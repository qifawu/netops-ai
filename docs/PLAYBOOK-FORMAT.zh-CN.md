# 剧本（SOP）格式

[English](PLAYBOOK-FORMAT.md) | **简体中文**

剧本是 `playbooks/` 下的一个 YAML 文件，告诉取证的 agent：遇到某类已知告警，**下一步该往哪儿看**。**它是建议，不是自动化**：`sop_lookup` 返回匹配到的剧本，引擎从不执行任何一步。agent 仍然自己调工具，它实际做了什么，记在告警记录的 `sop_usage` 里（走了哪几步、跳过了哪几步、哪里偏离）。如果剧本和设备或监控的实测矛盾，以证据为准。

仓库带 6 个示例，都在 `playbooks/` 下：`interface-link-down`、`ospf-adjacency`、`bgp-session`、`device-reload`、`high-cpu`、`interface-errors`。先读最短的 `device-reload.yaml`。用 `python tools/sop_lint.py` 检查它们（加上路径可以检查你自己的）。

## 顶层字段

```yaml
name: interface-link-down            # 唯一 id
description: >                       # 覆盖什么场景
  ...
applicability: "一句话：什么时候适用 / 不适用"     # 可选
limits:                               # 可选
  max_main_steps: 5                   # 默认 5
  max_tokens: 6000                    # 默认 1500
match:
  vendor: cisco                       # 可选；对不上就直接出局（不是扣分）
  trigger_name_contains:              # 告警名的任意子串；命中最长的那个计分
    - Link down
    - linkDown
  tags:                               # 可选：[{tag: component, value: network}, ...]；只在告警带 tag 时才强制
start: triage_interface               # 第一步的 id
steps: [...]
```

匹配规则：`match` 不满足的剧本直接丢掉，其余的按具体程度排序（厂商 +30，每个 tag +12，再加上命中的最长告警名子串的长度）。`match` 为空会以 0 分匹配所有告警——几乎一定是写错了，查询时会提示。

## 步骤

```yaml
- id: triage_interface
  why: "这一步回答什么问题。"                           # 给 agent 看
  expect: "期望看到什么。"                              # 可选；让这次运行能对照期望来评判
  main: true                                           # 默认 true；非 main 步骤是分支细节
  action:
    tool: device                                       # 见「动作」
    command: "show interfaces {alert_interface}"
  branches:
    - when: "output_contains('administratively down')"
      goto: local_shutdown_evidence
    - when: default
      goto: __ai__
  provenance: {source: human, date: "2026-10-01"}      # human | ai_proposed_human_approved
```

### 分支条件

故意做得很小——不认识的表达式按 false 处理，后面的 `default` 照常兜底：

| 表达式 | 为真的条件 |
|---|---|
| `default` | 永远为真（放最后） |
| `error` | 这一步失败，或命令被拒 |
| `output_contains('text')` | 输出里包含 `text` |
| `value == x` / `value != x` | 工具产出的值等于 / 不等于 `x`（这一步没有值时为假） |

### 特殊的 `goto` 去向

- `__ai__` —— 把已经收集到的信息交回给模型
- `__end__` —— 剧本结束

### 动作

`action.tool` 必须是下面之一：`device`（一条 `show` 命令，对应 agent 的 `device_show` 工具）、`zabbix_history`、`zabbix_reachability`、`topology_neighbors`、`zbx_syslog`、`zbx_items`。`zbx_*` 和 `topology_neighbors` 的其它键必须是该工具的真实参数名（`since`、`until`、`interface` 等）。linter 会检查这些，**并且**确认每条渲染出来的 `device` 命令都能过命令白名单——剧本不可能夹带闸门会拒的命令。

### 占位符

| 占位符 | 填入的内容 |
|---|---|
| `{alert_interface}` | 告警里提到的接口（告警没提接口时会记一条渲染警告） |
| `{alert_window_from}`、`{alert_window_to}` | 告警时间前后的故障窗口 |
| `{alert_log_prefix}` | `show logging \| begin …` 用的时间戳前缀 |
| `<接口>`、`<对端地址>` | 留给 **agent** 在运行时填 |

### 意图步骤

步骤可以不写字面命令，而是写一个**意图**（`intent: interface_state`）；`playbooks/catalog/cisco_ios.yaml` 把每个意图映射到该系列系统的具体命令。linter 会按这份目录校验意图步骤。要支持另一种系统，就要加一份目录文件和白名单规则——故意不做成自动的。

## 十分钟写一个自己的剧本

1. 挑一条常见告警，记下它的 Zabbix 触发器名（例如 `Interface Gi0/1(): High error rate`），填进 `match.trigger_name_contains`。
2. 复制 `playbooks/device-reload.yaml`（最短的一个）为 `playbooks/my-playbook.yaml`，改 `name`、`description`、`match`。
3. 第一步写「第一个要问的问题」：一条 `show` 命令（或一个 `zbx_*` 工具），按输出决定下一步看什么。每种结果一个 `branches` 条目，最后一条 `when: default` → `__ai__`。
4. 最多再加两三步跟进。保持小：剧本是给 agent 的提示，不是脚本。
5. 运行 `python tools/sop_lint.py playbooks/my-playbook.yaml`。它会拒绝未知工具、错误参数名、白名单会拒的命令，以及运行时分不清的步骤。
6. 往 webhook 发一条同触发器名的告警（见 [INSTALL.zh-CN.md](INSTALL.zh-CN.md)），再看记录里的 `sop_usage`，确认 agent 实际用了哪几步。
