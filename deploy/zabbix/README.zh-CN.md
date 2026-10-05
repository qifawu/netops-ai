# 实验用 Zabbix 7.0

[English](README.md) | **简体中文**

`docker-compose.yml` 起一套 Zabbix **7.0 LTS**（PostgreSQL + server + web + agent2 + SNMP trap 接收），没有现成 Zabbix 也能试。启动：设好 `POSTGRES_*` 环境变量后 `docker compose up -d`。Zabbix 自带 `Admin` 账号，**立刻改密码**，再给 netops-ai 单独建一个**只读**用户。用 `verify_api.py` 验证：它会真读主机/监控项/历史，确认拿到的是真数据不是空列表——**先验再接**。之后把设备加成 Zabbix 主机、接 webhook（见 INSTALL.zh-CN.md）。
