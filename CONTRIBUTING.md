# Contributing / 贡献指南

Thanks for considering a contribution. This project has a small number of non-negotiable constraints; reading them first saves everyone a round trip. 中文见文末。

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

---

## 中文

几条不能松的底线，先读能省一轮来回：

1. **永远只读。** 任何改动都不能引入往设备写配置、往 Zabbix 调写方法的路径。命令白名单和 Zabbix 方法白名单是闸门，**代码迁就闸门，不是闸门迁就代码。** 放宽白名单的改动，必须有测试精确说明哪条新命令被放行、为什么它在我们支持的平台上是只读的，并且改动要小。
2. **不猜。** 不认识的厂商就抛错，不认识的值就说不认识，兜底失败要记原因；宁要响亮具体的报错，不要看着合理的默认值。
3. **测试不碰真实状态。** 不读写真实 `records/`、账本、`.env`、凭据，也不依赖在线设备/Zabbix/模型。`tests/__init__.py` 会在任何 import 之前把可写位置重定向到临时目录，别破坏这点。
4. **不放密钥、不放真实地址。** 示例里用 RFC 5737 文档地址段（`192.0.2.0/24` 等），别提交凭据、令牌、真实主机名/IP，或来自生产网络的抓包。

开发流程：建虚拟环境 → `pip install -r requirements.txt` → `python -m pytest tests -q` → `python tools/demo_replay.py` → `cd web && npm ci && npx tsc --noEmit && npm run build`。依赖版本是**故意钉死**的（agent 循环的行为取决于 LangChain/LangGraph 的确切版本）；升级要单独评审，并在前后各跑一遍全量测试和一条真实告警。PR 一次只做一件事，先说问题再说改动；改行为就改测试，修 bug 要有「不修就红」的测试；界面文案在 `web/src/lib/i18n.ts` 里中英文都要加。

安全问题（尤其是任何可能绕过命令白名单的问题），请通过 GitHub 的 *Security → Report a vulnerability*（私密漏洞报告）提交，不要直接开公开 issue。
