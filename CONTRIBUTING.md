# Contributing

**English** | [简体中文](CONTRIBUTING.zh-CN.md)

Thanks for considering a contribution. This project has a small number of non-negotiable constraints; reading them first saves everyone a round trip.

## Non-negotiables

1. **Read-only, always.** No change may introduce a code path that writes configuration to a device or a write method to Zabbix. The command whitelist (`netops_ai/devices/whitelist.py`) and the Zabbix method whitelist are the gate; **the code adapts to the gate, never the other way round.** A change that loosens a whitelist needs a test that shows exactly which new command becomes allowed and why it is read-only on the platforms we support — and it should be small.
2. **Don't guess.** Unknown vendor → raise. Unknown value → say so. Failed fallback → record why. Prefer a loud, specific error over a plausible default.
3. **Tests never touch real state.** They must not read or write real `records/`, ledgers, `.env` or credentials, and must not need a live device, Zabbix or model. The test package (`tests/__init__.py`) redirects the writable locations to temp directories before anything is imported — keep it that way.
4. **No secrets, no real addresses.** Don't commit credentials, tokens, real hostnames/IPs, or captures from a production network. Use RFC 5737 documentation addresses (`192.0.2.0/24`, `198.51.100.0/24`, `203.0.113.0/24`) in examples.

## Development setup

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python -m pytest tests -q
python tools/demo_replay.py                  # offline end-to-end sanity check
cd web && npm ci && npx tsc --noEmit && npm run build
```

Requirements are **pinned** on purpose: the agent loop's behaviour depends on the exact LangChain/LangGraph versions, so upgrading is a change to review, not a drive-by. If you bump one, run the full suite and a real alert before and after, and say so in the PR.

## Pull requests

- One logical change per PR; describe the problem first, then the change.
- Add or update tests next to the behaviour you change. Bug fixes need a test that fails without the fix.
- UI text has Chinese and English entries in `web/src/lib/i18n.ts`; add both.
- Code comments in this repository are mostly Chinese, explaining *why* (including the incident that caused a rule). New comments may be English or Chinese — explain the reason, not the line.
- Run `python -m pytest tests -q` and `npx tsc --noEmit` before pushing.

## Reporting problems

Open an issue with: what you ran, what you expected, what happened, and (if relevant) the alert record with hostnames and addresses redacted. **Do not paste credentials or unredacted device output.** For a security problem — especially anything that could let a command bypass the whitelist — please report it privately through GitHub's *Security → Report a vulnerability* (private vulnerability reporting) instead of opening a public issue.
