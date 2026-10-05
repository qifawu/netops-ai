# 参考实验环境

[English](LAB.md) | **简体中文**

netops-ai 是在一个小型虚拟网络上开发和验证的。这页说明它长什么样，让你能判断「实验室验证过」到底指什么，也能自己搭一个类似的。

**仓库里有什么、没有什么。** 有：拓扑文件（`topology.yaml`）、Zabbix 的 compose 文件（`deploy/zabbix/`）、SNMP trap 实验脚本（`deploy/trap/`）、剧本、少量脱敏的设备抓包（`labs/captures/`）。**没有**：EVE-NG、任何设备镜像、任何设备配置——路由器/交换机镜像是厂商授权的，要你自己准备。

**结构。** 三层、7 台 Cisco 节点：
- **核心** V1、V2（IOSv）：与汇聚层跑 OSPF；V1、V2 之间直连链路上有一条 BGP 会话。
- **汇聚** D1、D2（IOSv）：向两台核心做三层上联；`bridge irb` + BVI 接口给接入层当 VLAN 网关。
- **接入** A1、A2、A3（IOSv-L2）：纯二层，管理 IP 配在 `Vlan1` SVI 上（不是物理口）。

7 台都通过带外管理网（`Gi0/0`）可达，`topology.yaml` 里是具体连线，地址用的是文档保留段 `192.0.2.0/24`（原实验环境里是虚拟化软件的 NAT 网络）。接入层后面挂了一台普通 Linux 测试机用来打流量，它不纳管，故意**不写进** `topology.yaml`（登记了但查不到的设备会在拓扑页上显示成一个死节点）。

**监控**：一台 Zabbix 7.0（`deploy/zabbix/`）用 SNMP 轮询所有节点（*Cisco IOS by SNMP* 类模板），同时接收 SNMP trap（`deploy/trap/`）。Zabbix 主机名带镜像后缀（`V1-vios`、`A1-viosl2`），所以 `topology.yaml` 要显式写 `aliases`。**syslog**：设备把日志发到模拟器 guest 虚拟机上的 rsyslog（每台设备一个文件），Zabbix 的 `logrt[]` 监控项读这些文件，agent 通过 Zabbix 查，不去登录日志服务器。**宿主**：桌面虚拟化软件里的 EVE-NG Community 6.2，16 GB 内存的机器上，8 台节点加测试机大约占 3 GB。没有把硬件虚拟化透传给虚拟机时 QEMU 节点启动很慢（要几分钟），别急着判断节点坏了。

**用过的故障场景**（都是人或脚本在**设备上用有写权限的账号**制造的，netops-ai 自己没有任何写路径）：接口 down（设备上 `shutdown`，对应 `interface-link-down`）；OSPF 邻居丢失（关邻居侧接口或改 timer，对应 `ospf-adjacency`）；BGP 会话 down（V1/V2 上 `neighbor … shutdown`，对应 `bgp-session`）；设备重启（`reload`，表现为冷启动 trap + uptime 清零）。

**踩过的坑**：
- **断链路用设备上的 `shutdown`，别去虚拟化软件里拔虚拟网线（tap）。** 拔 tap 会让 IOSv 节点卡死，而且不产生有用的告警。
- Zabbix 的 *LLD* 会自动禁用管理性 down 接口的监控项/触发器，所以这类 PROBLEM 可能不出现在 Zabbix「问题」页，需要手工启用触发器。
- Zabbix 页面默认显示 UTC，卡片和记录可能是本地时间，判断告警「是否延迟」前先看时区。
- 只读账号要在**真实 VTY** 上验证，不要拿虚拟化软件的 console 代理测（它走 `line con 0`，不遵守 VTY 的登录规则）。

**自己搭一个（提纲）**：①装一个网络模拟器（EVE-NG、CML、GNS3 都行），加上你有授权的 Cisco IOSv / IOSv-L2 镜像；②按 `topology.yaml` 的 `links:` 建 7 个节点并连线，每个节点第一个接口接到 API 主机能到的管理网；③每台配置：管理 IP、一个能 SSH 的**低权限账号**加 `enable secret`、指向 Zabbix 的 SNMP（v2c community，trap 脚本就是这么配的）、`logging host`、核心与汇聚之间的 OSPF、两台核心之间一条 BGP；④起 Zabbix（`deploy/zabbix/README.md`），把节点加成主机、挂 SNMP 模板，需要的话配 trap（`deploy/trap/`）；⑤填 `.env` 和 `topology.yaml`（你自己的地址、把你的 Zabbix 主机名写成 aliases），再照 [INSTALL.zh-CN.md](INSTALL.zh-CN.md) 的第 3 层走。

想试一试并不需要实验室：离线回放和看板（第 1、2 层）用合成数据就能跑。
