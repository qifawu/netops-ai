# 贡献指南

[English](CONTRIBUTING.md) | **简体中文**

几条不能松的底线，先读能省一轮来回：

1. **永远只读。** 任何改动都不能引入往设备写配置、往 Zabbix 调写方法的路径。命令白名单和 Zabbix 方法白名单是闸门，**代码迁就闸门，不是闸门迁就代码。** 放宽白名单的改动，必须有测试精确说明哪条新命令被放行、为什么它在我们支持的平台上是只读的，并且改动要小。
2. **不猜。** 不认识的厂商就抛错，不认识的值就说不认识，兜底失败要记原因；宁要响亮具体的报错，不要看着合理的默认值。
3. **测试不碰真实状态。** 不读写真实 `records/`、账本、`.env`、凭据，也不依赖在线设备/Zabbix/模型。`tests/__init__.py` 会在任何 import 之前把可写位置重定向到临时目录，别破坏这点。
4. **不放密钥、不放真实地址。** 示例里用 RFC 5737 文档地址段（`192.0.2.0/24` 等），别提交凭据、令牌、真实主机名/IP，或来自生产网络的抓包。

开发流程：建虚拟环境 → `pip install -r requirements.txt` → `python -m pytest tests -q` → `python tools/demo_replay.py` → `cd web && npm ci && npx tsc --noEmit && npm run build`。依赖版本是**故意钉死**的（agent 循环的行为取决于 LangChain/LangGraph 的确切版本）；升级要单独评审，并在前后各跑一遍全量测试和一条真实告警。PR 一次只做一件事，先说问题再说改动；改行为就改测试，修 bug 要有「不修就红」的测试；界面文案在 `web/src/lib/i18n.ts` 里中英文都要加。

安全问题（尤其是任何可能绕过命令白名单的问题），请通过 GitHub 的 *Security → Report a vulnerability*（私密漏洞报告）提交，不要直接开公开 issue。
