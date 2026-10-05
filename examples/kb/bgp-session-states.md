# BGP session states and common reasons a session is not Established

These notes are original, written for the netops-ai sample knowledge base. They summarize widely documented BGP behavior; check your vendor documentation for details.

## The state sequence

Idle, Connect, Active, OpenSent, OpenConfirm, Established. Only Established exchanges routes.

## What each non-Established state suggests

- **Idle (Admin).** The neighbor was shut down on purpose with `neighbor <ip> shutdown`. The syslog normally shows `%BGP-5-ADJCHANGE: neighbor <ip> Down Admin. shutdown`, and the configuration change appears as `%SYS-5-CONFIG_I`.
- **Idle** without "(Admin)". The router is waiting before the next connection attempt, or has no route to the neighbor address. Check `show ip route <neighbor>`.
- **Active.** The router is trying to open a TCP connection and failing. Reachability, an access list on TCP/179, or the neighbor not being configured back are the usual causes.
- **OpenSent / OpenConfirm that never completes.** The OPEN message was rejected: wrong remote AS, mismatched authentication (MD5), or unsupported capabilities. The notification code in the log names the reason.
- **Session drops with "Hold Timer Expired".** Keepalives stopped arriving. The path is dropping packets or the neighbor's CPU is overloaded.

## eBGP specifics

eBGP neighbors that are not directly connected need `ebgp-multihop`, otherwise the TTL of 1 makes the TCP connection fail. Loopback-based sessions also need `update-source`.

## Useful read-only commands

`show ip bgp summary`, `show ip bgp neighbors <ip>`, `show logging | include BGP`, `show ip route <neighbor>`.
