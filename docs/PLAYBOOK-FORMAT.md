# Playbook (SOP) format

**English** | [简体中文](PLAYBOOK-FORMAT.zh-CN.md)

A playbook is a YAML file in `playbooks/` that tells the investigating agent *where to look next* for a known class of alert. **It is advice, not automation**: `sop_lookup` returns the matching playbook; the engine never executes a step. The agent still calls tools itself, and what it actually did is recorded as `sop_usage` in the alert record (which steps ran, which were skipped, deviations). If a playbook contradicts what the device or monitoring says, the evidence wins.

Six examples ship with the repo, all in `playbooks/`: `interface-link-down`, `ospf-adjacency`, `bgp-session`, `device-reload`, `high-cpu`, `interface-errors`. Start with `device-reload.yaml`, the shortest. Lint them with `python tools/sop_lint.py` (add paths to lint your own).

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

## Write your own in ten minutes

1. Pick an alert you see often and note its Zabbix trigger name (for example `Interface Gi0/1(): High error rate`). That text goes into `match.trigger_name_contains`.
2. Copy `playbooks/device-reload.yaml` (the shortest one) to `playbooks/my-playbook.yaml` and change `name`, `description` and `match`.
3. Write the *first question* as step one: one `show` command (or one `zbx_*` tool) whose output decides what to look at next. Give each outcome a `branches` entry and end with `when: default` -> `__ai__`.
4. Add at most two or three follow-up steps. Keep it small: a playbook is a hint for the agent, not a script.
5. Run `python tools/sop_lint.py playbooks/my-playbook.yaml`. It rejects unknown tools, wrong parameter names, commands the whitelist would refuse, and steps the runtime could not tell apart.
6. Send an alert with that trigger name to the webhook (see [INSTALL.md](INSTALL.md)) and look at `sop_usage` in the record to see which steps the agent actually used.
