# Scheduled jobs

Reports, detections and reminders are one command per job; run them with the system scheduler:

```
python -m netops_ai.jobs <job>
```

| Job | What it does | Suggested frequency | Result |
|---|---|---|---|
| `isp-flap` | Checks the external links in `isp-links.yaml` (see `examples/isp-links.yaml`) for flapping | every 5 minutes | An email draft to the ISP when a link flaps; no repeat for the same link within 6 hours |
| `traffic-daily` | Yesterday's link traffic PDF + a management summary | every morning | One pending draft in the outbox |
| `traffic-weekly` | Last calendar week | Mondays | Same |
| `traffic-monthly` | Last calendar month | the 1st | Same |
| `version-reminder` | Device versions against `version-policy.yaml` (see `examples/version-policy.yaml`) | weekly | A reminder draft only when some device is behind |
| `cve` | Software version × CVE report (NVD + CISA KEV) | weekly | `reports/cve-<date>.html` |
| `topology` | CDP / LLDP topology discovery | weekly | `reports/topology-discovery.*` |

**Anything sent outside is only ever written as a draft**: the jobs never send mail; a person approves each draft on the dashboard's "Reports & Outbox" page.

Environment: `REPORT_CHANNEL` (`email` or `feishu`, default `feishu`), `REPORT_TO` (email recipients, comma-separated);
email also needs `SMTP_*` and the recipient allowlist `OUTBOX_ALLOWED_RECIPIENTS` (see `env.example`).

Exit code: `0` = finished (including "nothing to do"); `1` = error, reason on stderr, so the scheduler can alert on it.

## Windows (Task Scheduler)

Adjust the path to your checkout:

```bat
schtasks /Create /F /TN "netops-isp-flap"       /SC MINUTE /MO 5            /TR "cmd /c cd /d C:\netops-ai && .venv\Scripts\python.exe -m netops_ai.jobs isp-flap"
schtasks /Create /F /TN "netops-traffic-daily"  /SC DAILY  /ST 08:00        /TR "cmd /c cd /d C:\netops-ai && .venv\Scripts\python.exe -m netops_ai.jobs traffic-daily"
schtasks /Create /F /TN "netops-traffic-weekly" /SC WEEKLY /D MON /ST 08:10 /TR "cmd /c cd /d C:\netops-ai && .venv\Scripts\python.exe -m netops_ai.jobs traffic-weekly"
schtasks /Create /F /TN "netops-traffic-monthly" /SC MONTHLY /D 1 /ST 08:20 /TR "cmd /c cd /d C:\netops-ai && .venv\Scripts\python.exe -m netops_ai.jobs traffic-monthly"
```

## Linux / macOS (cron)

```cron
*/5 * * * *  cd /opt/netops-ai && .venv/bin/python -m netops_ai.jobs isp-flap
0 8 * * *    cd /opt/netops-ai && .venv/bin/python -m netops_ai.jobs traffic-daily
10 8 * * 1   cd /opt/netops-ai && .venv/bin/python -m netops_ai.jobs traffic-weekly
20 8 1 * *   cd /opt/netops-ai && .venv/bin/python -m netops_ai.jobs traffic-monthly
0 9 * * 1    cd /opt/netops-ai && .venv/bin/python -m netops_ai.jobs version-reminder
```

The account running the job must be able to read the `.env` at the repository root (Zabbix, device and model connection settings live there).
