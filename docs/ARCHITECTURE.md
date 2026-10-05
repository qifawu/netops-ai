# Architecture / 架构

This document describes how an alert becomes a checkable conclusion, and why the pieces are shaped the way they are. Chinese summary at the end.

## The alert pipeline

```
Zabbix trigger ──► POST /webhooks/zabbix ──► 200 immediately (Zabbix webhooks time out at 60 s)
                          │
                          ▼  background task  (netops_ai/api/pipeline.py)
        ┌─────────────────────────────────────────────────────────────┐
        │ 1. Collect window   alerts arriving close together are held  │
        │                     briefly and merged into ONE incident      │
        │                     (netops_ai/incident/)                     │
        │ 2. Investigate      one tool-calling loop with read-only      │
        │                     tools (netops_ai/graph/agent_loop.py)     │
        │ 3. Conclude         strict-schema structured output           │
        │                     (netops_ai/analysis/schema.py)            │
        │ 4. Report           Feishu card + a record on disk            │
        │                     (netops_ai/feishu/, records/alert-*.json) │
        │ Offline only: analysis/verify.py grades evidence quotes and   │
        │ check_business_rules flags contradictions, on saved records   │
        │ via tools/demo_replay.py (not run in the live path).          │
        └─────────────────────────────────────────────────────────────┘
                          │
                          ▼
                 dashboard reads the records (netops_ai/api/dashboard.py → web/)
```

Everything after the model has spoken (steps 4–6) is deterministic code, which is why it can be replayed offline — see `tools/demo_replay.py`.

## The tools the agent can use

All tools are read-only and registered in one place (`netops_ai/graph/chat_agent.py`, `build_chat_tools`; names in `graph/tool_names.py`):

| Tool | What it does |
|---|---|
| `zbx_*` | Zabbix read-only queries — hosts, items, history, trends, syslog, problems, top talkers, chart (generated from `zabbix/cli.py` so the CLI and the tools share one spec table) |
| `device_show`, `device_show_many` | Run `show` commands on a device through the whitelist |
| `topology_neighbors` | Neighbors of a device/interface from `topology.yaml` |
| `sop_lookup` | Find a matching SOP playbook (it *advises*; it never executes anything) |
| `doc_search` | BM25 search over your local documentation index |
| `run_inspection`, `list_analyses`, `get_analysis` | Run/read an inspection, look up earlier conclusions |

Tool results are returned as facts only. Empty results say they are empty and echo the query; truncation says it truncated. Routing advice ("try X next") lives in the system prompt or playbooks, never inside a tool result, so tools stay neutral for any agent that calls them.

### The two-layer read-only guard

1. **Command whitelist** (`netops_ai/devices/whitelist.py`) — runs inside `DeviceAdapter.run()`, so a rejected command is never sent. Rules: full commands only (no abbreviations — `sh` and `conf t` are rejected); forbidden verbs are checked before allowed ones; one optional pipe with `include/exclude/begin/section/count` only; no `; & | > < \` $ " '` metacharacters, no control characters, ASCII only, ≤ 200 characters; `ping`/`traceroute` are off unless explicitly enabled; `terminal length 0` and exactly one `more system:running-config` form are allowed as special cases. Outputs of configuration-viewing commands are flagged `sensitive`.
2. **Device-side read-only account.** The code cannot make this true for you. On IOS, a low privilege level is not a boundary if `enable` has no secret: verify over a real VTY (not a console proxy) that write commands are refused.

The Zabbix client has the same shape: every call goes through a method whitelist before it is sent.

## Structured conclusions

`analysis/schema.py` defines a strict JSON schema the model must fill:

- `root_cause`, `headline`, `confidence`
- `evidence[]` — each `{claim, source, source_from}` where `source` is text copied from the raw data and `source_from` says which input it came from (`zabbix` monitoring data or `device` output)
- `hypothesis_checklist` — six families, each `supported | ruled_out | cannot_determine`; a `ruled_out` family must carry `counter_evidence` that directly contradicts it (absence of evidence does not count, and a current-state snapshot does not rule out a fault that has already ended)
- `undistinguishable_candidates[]` — when two or more families are unresolved, name them, say why they can't be separated, which data would separate them, the exact command to get it, and who can fetch it (the agent again, or a human)
- `alert_roles`, `grouping` — which alert is the root and which are consequences

`verify.py` grades each evidence item: `verbatim` (found as-is in the named pool), `cross_source` (found, but in a different pool than claimed), `reformatted` (found after whitespace/render normalization), `fabricated` (not found). `check_business_rules` rejects combinations such as high confidence with unresolved families, or an incomplete grouping. **Neither runs in the live alert pipeline**: on real devices they produced too many false alarms, mostly from quotes the model assembled out of several separate lines. `tools/demo_replay.py`, the regression tools and the tests run them on saved records.

## The agent loop

`graph/agent_loop.py` is the only loop. Alert investigation and anything else that calls tools share it, with: a token budget and tool-call/iteration limits (a hop finishes before the budget is checked, so the cap is soft by up to one hop); duplicate-call interception (the same tool with the same arguments is answered from the earlier result and counted as "no new information"); a no-progress abort after repeated duplicates; an optional short-lived cache for identical read-only calls; and a wrap-up call that makes the model summarize what it has when it is cut off.

## Playbooks (SOPs)

`netops_ai/playbooks/` loads YAML playbooks and matches them to alerts (vendor, trigger-name substrings, tags). A playbook is **advice for the agent**: `sop_lookup` returns the matching steps, each with a `why`, an `expect`, an action (tool + arguments) and branches. The engine does not execute steps; the agent still calls tools itself, and what it did is recorded as `sop_usage` (which steps ran, deviations). `playbooks/lint.py` checks that every action uses a registered read-only tool with real parameter names and a command the whitelist allows. Format: [PLAYBOOK-FORMAT.md](PLAYBOOK-FORMAT.md).

## Inspection (before anything alerts)

Two complementary checks (`netops_ai/inspection/`):

- **Trend inspection** reads Zabbix history only and runs three pure-function detectors: sustained one-way trend, periodic spike, and self-healing flap. Thresholds and scan scope live in `inspection.yaml` (editable from the UI).
- **Status inspection** logs into each device read-only and checks interface state, OSPF neighbors, BGP sessions and error-counter growth with fixed rules (no model). Each finding carries the device's own output as evidence.

An optional model call turns findings into "what to handle tonight / what to ignore (with a reason) / can't tell", with citations verified the same way as alert evidence. Each run is stored compactly so the page can show what is new or gone since last time, and reports export to Markdown/HTML.

## Layout of the web UI

`web/` is a Vite + React + TypeScript app, served by the FastAPI process once built (`web/dist`). It reads only the JSON API in `netops_ai/api/`; the pages are overview, incidents & conclusions, device & topology, inspection, command audit, knowledge base and settings. A demo-mode toggle masks IPs and IDs for screenshots.

## Design rules worth knowing before you change things

- A cap you can only find out about after exceeding it is not a cap: budgets are checked between hops and reported as soft.
- When something fails, say why. Fallbacks (e.g. an unreachable inventory source) record the reason instead of silently using older data.
- Don't guess: unknown vendors raise, and a model with no known price gets no cost estimate rather than an invented one.
- Tests must not touch real records, ledgers or credentials. The test package redirects these to temp locations before anything imports.

---

## 中文摘要

**告警管道**：Zabbix 触发 → `POST /webhooks/zabbix` 立刻返回 200（Zabbix webhook 60 秒硬超时）→ 后台任务：①收集窗口（短时间内的相关告警合并成一个事件）②取证（一个带只读工具的工具循环）③结论（严格 schema 的结构化输出）④飞书卡片 + 落盘记录 → 看板读取记录。证据逐字核对和业务规则校验不在这条在线链路里，是确定性代码，对已保存的记录离线重放（`tools/demo_replay.py`）。

**两层只读**：①命令白名单（`devices/whitelist.py`）：只放行写全的命令，不认缩写，先查禁令再查放行，管道后只允许 `include/exclude/begin/section/count`，拒绝 shell 元字符/控制字符/非 ASCII/超过 200 字符，`ping`/`traceroute` 默认关闭；在 `DeviceAdapter.run()` 里判断，被拒的命令根本不会发出。②设备侧只读账号（代码替你保证不了；IOS 上 `enable` 没设 secret 时低权限等级不是边界，要在真实 VTY 上验证写命令被拒）。Zabbix 客户端同样先过方法白名单。

**结构化结论**：六类假设逐个表态（有证据支持 / 已排除且必须给直接反证 / 暂时无法判断）；多个方向分不开时必须写清为什么分不开、还差什么数据、具体用什么命令拿、谁去拿。`verify.py` 可以给每条证据分级（逐字 / 跨来源 / 重排版 / 编造），业务规则可以拒绝自相矛盾的输出——二者只在离线回放和回归里跑，在线流水线不跑（真机上误报偏多）。

**工具循环**：唯一的一个，带 token 预算（一跳做完才检查，所以上限是软的，最多超冲一跳）、重复调用拦截、无进展中止、可选缓存和收尾调用。**剧本**只是给 agent 的建议（`sop_lookup`），引擎不执行步骤，实际用了哪几步记为 `sop_usage`。**巡检**分趋势巡检（只读 Zabbix 历史，三类纯函数检测器）和状态巡检（只读登设备，固定规则，证据是设备原话）。
