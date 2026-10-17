# 定时任务

报表、检测和提醒都是「一条命令一件事」，用系统自带的计划任务定时跑：

```
python -m netops_ai.jobs <任务>
```

| 任务 | 做什么 | 建议频率 | 结果 |
|---|---|---|---|
| `isp-flap` | 检测 `isp-links.yaml`（见 `examples/isp-links.yaml`）里的外网链路是否翻滚 | 每 5 分钟 | 翻滚就出给运营商的邮件草稿；同一条链路 6 小时内不重复出 |
| `traffic-daily` | 昨天的链路流量日报 PDF + 给管理层的总结 | 每天早上 | 草稿箱里一份待确认草稿 |
| `traffic-weekly` | 上一个自然周的周报 | 每周一 | 同上 |
| `traffic-monthly` | 上一个自然月的月报 | 每月 1 日 | 同上 |
| `version-reminder` | 设备版本对照 `version-policy.yaml`（见 `examples/version-policy.yaml`） | 每周 | 有落后设备才出一份提醒草稿 |
| `cve` | 设备软件版本 × CVE 报告（NVD + CISA KEV） | 每周 | `reports/cve-<日期>.html` |
| `topology` | CDP / LLDP 拓扑自动发现 | 每周 | `reports/topology-discovery.*` |

**所有对外发送都只写草稿**：任务本身不发信，由人在看板「报表与发送」页确认后才发。

环境变量：`REPORT_CHANNEL`（`email` 或 `feishu`，默认 `feishu`）、`REPORT_TO`（邮件收件人，逗号分隔）；
邮件还需要 `SMTP_*` 和收件人白名单 `OUTBOX_ALLOWED_RECIPIENTS`（见 `env.example`）。

退出码：`0` = 跑完（包括「没有需要处理的」）；`1` = 出错，原因打印到标准错误，计划任务可据此告警。

## Windows（任务计划程序）

在仓库根目录对应的路径下执行（路径按实际修改）：

```bat
schtasks /Create /F /TN "netops-isp-flap"       /SC MINUTE /MO 5            /TR "cmd /c cd /d C:\netops-ai && .venv\Scripts\python.exe -m netops_ai.jobs isp-flap"
schtasks /Create /F /TN "netops-traffic-daily"  /SC DAILY  /ST 08:00        /TR "cmd /c cd /d C:\netops-ai && .venv\Scripts\python.exe -m netops_ai.jobs traffic-daily"
schtasks /Create /F /TN "netops-traffic-weekly" /SC WEEKLY /D MON /ST 08:10 /TR "cmd /c cd /d C:\netops-ai && .venv\Scripts\python.exe -m netops_ai.jobs traffic-weekly"
schtasks /Create /F /TN "netops-traffic-monthly" /SC MONTHLY /D 1 /ST 08:20 /TR "cmd /c cd /d C:\netops-ai && .venv\Scripts\python.exe -m netops_ai.jobs traffic-monthly"
```

## Linux / macOS（cron）

```cron
*/5 * * * *  cd /opt/netops-ai && .venv/bin/python -m netops_ai.jobs isp-flap
0 8 * * *    cd /opt/netops-ai && .venv/bin/python -m netops_ai.jobs traffic-daily
10 8 * * 1   cd /opt/netops-ai && .venv/bin/python -m netops_ai.jobs traffic-weekly
20 8 1 * *   cd /opt/netops-ai && .venv/bin/python -m netops_ai.jobs traffic-monthly
0 9 * * 1    cd /opt/netops-ai && .venv/bin/python -m netops_ai.jobs version-reminder
```

任务运行的账号需要能读到仓库根的 `.env`（Zabbix、设备、模型的连接信息都在里面）。
