# Zabbix 7.0 for the lab

**English** | [简体中文](README.zh-CN.md)

`docker-compose.yml` starts a Zabbix **7.0 LTS** server (PostgreSQL, server, web UI, agent2 and an SNMP-trap receiver) so you can try netops-ai without an existing Zabbix. 7.0 is an LTS release; pick the current LTS if you read this later.

## Start

```bash
cd deploy/zabbix
export POSTGRES_USER=zabbix POSTGRES_PASSWORD='<choose-a-password>' POSTGRES_DB=zabbix   # see the variables referenced in docker-compose.yml
docker compose up -d
docker compose ps                     # wait until all services are running / healthy
```

Published ports (override with env vars): web UI `ZBX_WEB_PORT` (default 8080), server `ZBX_SERVER_PORT` (default 10051), SNMP traps `ZBX_TRAP_PORT` (default 162/udp — privileged on most hosts). `TZ` defaults to `Asia/Shanghai`; set your own. Open the web UI on `ZBX_WEB_PORT`. Zabbix ships with a default `Admin` account — **change its password immediately**, then create a separate **read-only** user for netops-ai (`ZABBIX_USER` / `ZABBIX_PASSWORD` in `.env`).

## Verify the API

```bash
python3 verify_api.py --url http://localhost:<port> --user <readonly-user> --password '<password>'
```

It logs in, reads real hosts/items/history and checks that the answers are real data, not empty lists. Run it **before** pointing netops-ai at the server.

## Next

Add your devices as Zabbix hosts (SNMP template such as *Cisco IOS by SNMP*), then wire the alert webhook — see [../../docs/INSTALL.md](../../docs/INSTALL.md#33-zabbix--webhook). For SNMP traps instead of polling, see `deploy/trap/`.
