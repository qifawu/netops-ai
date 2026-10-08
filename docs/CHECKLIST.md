# Inspection checklists (prototype)

A checklist is a YAML or JSON file that lists read-only commands per device, what each output must look like, and numbers to track over time. It complements the built-in trend and status inspections: those use fixed checks, a checklist lets you define your own.

```bash
python tools/checklist_run.py validate examples/checklists/core-health.yaml   # format + read-only whitelist
python tools/checklist_run.py run      examples/checklists/core-health.yaml   # one run, stored as JSON
python tools/checklist_run.py run      examples/checklists/core-health.yaml --every 60 --count 24
python tools/checklist_run.py trend    core-health --last 12 --ask-llm        # trend analysis by the model
```

- **Format.** `name`, `vendor`, `devices` (`name`, `host`), and `checks`. Each check has an `id`, one `command`, optional `expect` rules (`contains`, `not_contains`, `regex`, `not_regex`, each with a `severity` of `info`, `warning` or `critical`) and optional `extract` rules (a regex with one capture group, cast to `int` or `float`). See `examples/checklists/core-health.yaml`.
- **Read-only.** Every command is checked against the command whitelist twice: by `validate` (so a write command is found while the file is written) and again by the device adapter on every run. A refused command is recorded as `denied` and never sent.
- **Fixed rules, no model in the verdict.** Pass / fail comes from the `expect` rules only. An unreachable device is recorded as such and the run continues with the next device.
- **Stored results.** Each run writes one JSON file to `records/checklist-runs/` (override with `--out`): per device and check the command, status, the output (capped at 6000 characters), the evaluations and the extracted metrics.
- **Scheduling.** `--every N --count M` repeats inside one process; for a real schedule call `run` from cron or Windows Task Scheduler (exit code 2 means at least one check failed, was unreachable or errored).
- **Trend analysis.** `trend` renders the last runs as a compact table and sends it with a fixed prompt (`TREND_SYSTEM_PROMPT` in `netops_ai/inspection/checklist.py`) to the configured LLM. The prompt requires run-id citations, separates a one-off spike from a trend, treats a counter reset as a reset, and asks for the data or command that would settle anything it cannot judge. Without `--ask-llm` the prompt is only printed.

Credentials come from `.env` (`DEVICE_USERNAME`, `DEVICE_PASSWORD`, `DEVICE_VENDOR`, `DEVICE_TRANSPORT`): use a read-only account. This is a prototype: Cisco IOS only, not wired into the dashboard.
