# 示例

[English](README.md) | **简体中文**

`alerts/` 下是六条**合成**的告警记录（手写，不涉及任何真实网络、主机或地址），形状与流水线落盘的 `records/alert-*.json` 一致：01 接口被人为 shutdown（高置信）；02 OSPF 邻居丢失，老实说「判不出」并写明还差什么、用哪条命令能判；03 BGP 邻居被管理性关闭，agent 想 `clear ip bgp` 被白名单拒绝；04 设备被 reload 重启；05 CPU 高，流量被送到 CPU，环路还是单台主机分不出；06 接口 CRC 错误加 late collision，疑似双工不匹配，对端口待查。`python tools/demo_replay.py` 打印并渲染飞书卡片；`python tools/seed_demo.py` 把它们灌进 `records/`（时间戳平移到最近几小时，不覆盖已有文件），看板就有数据了。
