# Playbook (SOP) format / 剧本格式规范

A playbook is a YAML file in `playbooks/` that tells the investigating agent *where to look next* for a known class of alert. **It is advice, not automation**: `sop_lookup` returns the matching playbook; the engine never executes a step. The agent still calls tools itself, and what it actually did is recorded as `sop_usage` in the alert record (which steps ran, which were skipped, deviations). If a playbook contradicts what the device or monitoring says, the evidence wins.

Three examples ship with the repo: `interface-link-down.yaml`, `ospf-adjacency.yaml`, `bgp-session.yaml`. Lint them with `python tools/sop_lint.py` (add paths to lint your own).

## Top level

```yaml
name: interface-link-down            # unique id
description: >                       # what it covers
  ...
applicability: "one sentence: when it applies / does not apply"     # optional
limits:                               # optional
  max_main_steps: 5                   # default 5
  max_tokens: 6000                    # default 1500
match:
  vendor: cisco                       # optional; a mismatch rules the playbook out (it is not a score penalty)
  trigger_name_contains:              # any substring of the alert name; the longest hit scores
    - Link down
    - linkDown
  tags:                               # optional: [{tag: component, value: network}, ...]; only enforced when the alert has tags
start: triage_interface               # id of the first step
steps: [...]
```

Matching: playbooks whose `match` fails are dropped; the rest are ranked by specificity (vendor +30, tag +12 each, plus the length of the longest matching name substring). An empty `match` matches everything with score 0 — almost always a mistake, and the lookup says so.

## Steps

```yaml
- id: triage_interface
  why: "What this step answers."                       # shown to the agent
  expect: "What you expect to see."                    # optional; lets the run be judged against the expectation
  main: true                                           # default true; non-main steps are branch detail
  action:
    tool: device                                       # see "Actions"
    command: "show interfaces {alert_interface}"
  branches:
    - when: "output_contains('administratively down')"
      goto: local_shutdown_evidence
    - when: default
      goto: __ai__
  provenance: {source: human, date: "2026-10-01"}      # human | ai_proposed_human_approved
```

### Branch conditions

Deliberately tiny — unknown expressions evaluate to false so a later `default` still carries on:

| Expression | True when |
|---|---|
| `default` | always (put it last) |
| `error` | the step failed or the command was denied |
| `output_contains('text')` | the output contains `text` |
| `value == x` / `value != x` | the tool produced a value equal / not equal to `x` (false if the step produced no value) |

### Special `goto` targets

- `__ai__` — hand control back to the model with what has been gathered so far
- `__end__` — the playbook is finished

### Actions

`action.tool` must be one of: `device` (a `show` command — the same name `device_show` as the agent's tool), `zabbix_history`, `zabbix_reachability`, `topology_neighbors`, `zbx_syslog`, `zbx_items`. For the `zbx_*` and `topology_neighbors` tools the other keys must be that tool's real parameter names (`since`, `until`, `interface`, …). The linter checks all of this, **and** that every rendered `device` command passes the command whitelist — a playbook cannot smuggle in a command the gate would refuse.

### Placeholders

| Placeholder | Filled with |
|---|---|
| `{alert_interface}` | the interface named in the alert (a render warning is recorded if the alert names none) |
| `{alert_window_from}`, `{alert_window_to}` | the fault window around the alert time |
| `{alert_log_prefix}` | a timestamp prefix for `show logging \| begin …` |
| `<接口>`, `<对端地址>` | left for the **agent** to fill in at run time |

### Intent steps

Instead of a literal command a step can name an *intent* (`intent: interface_state`); `playbooks/catalog/cisco_ios.yaml` maps each intent to the concrete command for that OS family. The linter validates intent steps against the catalog. Adding another OS means adding a catalog file and whitelist rules — it is deliberately not automatic.

---

## 中文摘要

剧本是 `playbooks/` 下的 YAML，告诉取证的 agent「这类告警下一步该往哪儿看」。**它是建议，不是自动化**：`sop_lookup` 返回命中的剧本，引擎从不执行步骤；agent 仍然自己调工具，实际走了哪几步记成告警记录里的 `sop_usage`。剧本和设备/监控的实测矛盾时，以证据为准。

- **顶层**：`name`、`description`、`applicability`、`limits`（`max_main_steps` 默认 5、`max_tokens` 默认 1500）、`match`（`vendor` 对不上直接出局；`trigger_name_contains` 告警名子串；`tags` 仅在告警带 tag 时才强制）、`start`、`steps`。
- **步骤**：`id`、`why`、`expect`、`main`、`action{tool, …}`、`branches[{when, goto}]`、`provenance`。
- **分支条件**只有四种：`default`、`error`、`output_contains('…')`、`value ==/!= …`；未知表达式按 false，后面的 `default` 照常兜底。特殊去向 `__ai__`（交回给模型）、`__end__`（剧本结束）。
- **动作工具**：`device`（只读 `show` 命令）、`zabbix_history`、`zabbix_reachability`、`topology_neighbors`、`zbx_syslog`、`zbx_items`；参数名必须是该工具的真实参数名。`python tools/sop_lint.py` 会逐个核对，并确认每条渲染出来的设备命令都能过白名单——剧本不可能夹带闸门会拒的命令。
- **占位符**：`{alert_interface}`、`{alert_window_from/to}`、`{alert_log_prefix}`；`<接口>`、`<对端地址>` 留给 agent 运行时填。**意图步骤**用 `intent:` 引用 `playbooks/catalog/cisco_ios.yaml` 里的命令目录。
