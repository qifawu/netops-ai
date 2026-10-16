# Two-layer inspection analysis and the safety boundary for assistant-written scripts

Chinese version: [SCRIPT-SANDBOX.zh-CN.md](SCRIPT-SANDBOX.zh-CN.md). Code entry points are listed at the end.

## 1. Two layers

| Layer | When | How | Kept? |
|---|---|---|---|
| General | The same kind of result keeps recurring, or the judgement can be written as a deterministic rule (entry-by-entry table diff, threshold, count) | Fixed analysis: existing templates / generic rules (including the table diff) first; if they cannot express it, the assistant writes a script, which is reviewed and then reused on a schedule | Script is stored, versioned |
| Special | One-off, different every time, needs contextual reasoning | AI analysis in real time | The conclusion goes into this run's report only; **no reusable script is produced** |

`layer_router.decide` routes in this order:

1. an approved script already exists for this kind of result → reuse it;
2. an existing template or generic rule expresses it → use that, no script and no AI call;
3. the judgement is rule-shaped, or the same kind of result occurred ≥ N times within a 30-day window (default N = 3) → distil a script, which goes to review;
4. otherwise → real-time AI analysis.

"The same kind" is a fingerprint of (command, judgement category); the command is lower-cased with whitespace folded. N and the window are adjustable.
Templates rank above scripts because they are already reviewed and have no execution surface: cheapest and most auditable.

## 2. Safety boundary

Threat model: this guards against a script the assistant wrote going wrong (a bug, being steered by text in the sample, overreach). It is not a
boundary against a hostile person with local access. Whoever approves scripts must be the owner or an administrator.

**a. Read-only.** A script receives one dictionary, `ctx`: this run's output, the previous output, the device name and parameters. Commands are run by
the system through the read-only whitelist; a script cannot execute any. It cannot reach environment variables, credentials or other files: the
child process starts with an empty environment and an empty temporary working directory.

**b. Sandboxed execution.**
- The source passes an AST allowlist first: only `re / json / csv / ipaddress / collections / datetime / statistics / math / itertools` may be imported;
  `open / eval / exec / compile / __import__ / getattr` and similar names are rejected, as are dunder attributes and `.format / .format_map`
  (format-string attribute traversal bypasses the attribute check).
- A separate `python -I` child process; 5 s timeout; caps on output and findings; a 256 MB memory cap (Windows Job Object, POSIX `RLIMIT_AS`);
  restricted builtins and a guarded import at runtime.
- *Adjustment to the original proposal:* a system-level "no network" is not available as a lightweight option on Windows. Network access is prevented
  indirectly — no socket-capable module is allowlisted, the static check rejects such imports, and the runtime import guard repeats it. This is
  defence in depth, **not OS-level isolation**; for untrusted users run it inside a container or a low-privilege account.

**c. Structured output only.** The return value has a fixed schema: `status ∈ {pass, fail, baseline}`, `summary`, numeric `metrics`, at most 20
`findings`. Anything else is an error. A script has no channel to change a device or send an alert; its verdict is only the result of that check
and then follows the normal check → alert path, exactly like a template rule.

**d. Human review, then a library.** `script_registry` stores each script with an id and versions; each version records the source, its sha256,
**the original request text** that produced it, the model, a self-test summary, the state history, approver and time, and an HMAC signature.
- `pending → approved → revoked`, or `pending → rejected`;
- approval requires: static check passes, self-tests pass (including cases derived from the user's goal, at least one expected to alarm), the hash matches, and a named approver;
- on every use, state + hash + signature + static check are verified again; any mismatch counts as not approved (also if the file was edited by hand);
- changing the source creates a new version in `pending`; the previously approved version stays valid until the new one is approved and the old one revoked;
- the signing key lives only on this machine (`~/.netops-ai/script-signing-key`); someone who gets the stored files cannot produce a valid signature.

**e. Redaction before the model sees data.** Lines holding passwords, SNMP communities or keys are redacted (everything after the keyword becomes
`<REDACTED>`, line breaks kept); commands whose output may contain secrets (`running-config`, `snmp-agent community`, `local-user`, …) are never used
to author a script; self-tests use the same redacted sample. Only credentials are redacted by default — IPs and hostnames are not, because scripts parse the real
format. A stable pseudonymisation of identifiers is future work.

**f. Routing.** See section 1. Recurrence counts are kept in `occurrences.json` inside the script library directory.

## 3. Known limitations

1. Not OS-level isolation (see b).
2. The dashboard has no login by default: whoever can open the page can approve a script. Restrict approval with a login, a reverse proxy or network isolation.
   The approver is taken from the request's `approver` field, `dashboard` by default.
3. If the Job Object cannot be set (rare) the sandbox falls back to the static check plus the timeout.
4. After losing the signing key or moving machines, approved scripts must be approved again (the safe direction to fail).
5. The "≥ N occurrences" counter currently exposes `record / count` only; it is not yet called from each inspection run.

## 4. Switches

- `NETOPS_SCRIPT_AUTHORING=1` turns on assistant-written scripts (off by default; when off, the prompt contains no script rule and script requests from the model are ignored).
- `NETOPS_SCRIPT_LIB_DIR` — script library (default `records/check-scripts/`); `NETOPS_SCRIPT_KEY_FILE` — signing key file.

## 5. Files

`netops_ai/inspection/check_script.py` (static check, sandbox, signing, self-test) · `script_author.py` (authoring, redaction) ·
`script_registry.py` (library) · `layer_router.py` (routing and recurrence) · tests: `test_check_script.py`, `test_script_author.py`,
`test_script_registry.py`, `test_inspection_plan_scripts.py`.
