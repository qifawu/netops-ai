#!/usr/bin/env python3
"""M0 验收：确认 Zabbix API 读得到真数据。

只用标准库，那台机器上不用装任何东西。只读，不改任何配置。

    python3 verify_api.py --url http://localhost:8080 --user Admin --password xxx
"""
import argparse
import json
import sys
import urllib.error
import urllib.request

REQ_ID = [0]


def call(url, method, params, token=None):
    REQ_ID[0] += 1
    payload = {"jsonrpc": "2.0", "method": method, "params": params, "id": REQ_ID[0]}
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        url.rstrip("/") + "/api_jsonrpc.php",
        data=body,
        headers={"Content-Type": "application/json-rpc"},
    )
    if token:
        req.add_header("Authorization", "Bearer " + token)
    with urllib.request.urlopen(req, timeout=30) as r:
        resp = json.loads(r.read())
    if "error" in resp:
        raise RuntimeError(f"{method}: {resp['error'].get('data') or resp['error']}")
    return resp["result"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True, help="如 http://localhost:8080")
    ap.add_argument("--user", default="Admin")
    ap.add_argument("--password", required=True)
    a = ap.parse_args()

    fails = []

    ver = call(a.url, "apiinfo.version", {})
    print(f"API 版本: {ver}")

    token = call(a.url, "user.login", {"username": a.user, "password": a.password})
    print("登录: ok")

    hosts = call(a.url, "host.get", {"output": ["hostid", "host", "status"]}, token)
    print(f"主机 {len(hosts)} 台:")
    for h in hosts:
        print(f"  - {h['host']} (hostid={h['hostid']})")
    if not hosts:
        fails.append("主机列表是空的")

    # 找一个有数值的监控项，证明采集链路真的在动
    got_history = False
    for h in hosts:
        items = call(
            a.url,
            "item.get",
            {
                "hostids": h["hostid"],
                "output": ["itemid", "name", "key_", "value_type", "lastvalue"],
                "filter": {"value_type": [0, 3]},  # float / unsigned
                "limit": 50,
            },
            token,
        )
        for it in items:
            hist = call(
                a.url,
                "history.get",
                {
                    "itemids": it["itemid"],
                    "history": int(it["value_type"]),
                    "output": "extend",
                    "sortfield": "clock",
                    "sortorder": "DESC",
                    "limit": 3,
                },
                token,
            )
            if hist:
                print(f"历史数据: {h['host']} / {it['name']}")
                for p in hist:
                    print(f"  clock={p['clock']} value={p['value']}")
                got_history = True
                break
        if got_history:
            break
    if not got_history:
        fails.append("一条历史数据都没读到——采集链路没通，或者刚起还没到第一个采集周期")

    trig = call(a.url, "trigger.get", {"output": ["triggerid", "description"], "limit": 5}, token)
    print(f"触发器 {len(trig)} 条（只列前 5）")
    for t in trig:
        print(f"  - {t['description']}")
    if not trig:
        fails.append("一条触发器都没有——模板没挂上")

    probs = call(a.url, "problem.get", {"output": "extend", "limit": 10}, token)
    print(f"当前告警 {len(probs)} 条")
    for p in probs:
        print(f"  - [{p.get('severity')}] {p.get('name')}")

    call(a.url, "user.logout", {}, token)

    print()
    if fails:
        print("不通过:")
        for f in fails:
            print("  ✗ " + f)
        sys.exit(1)
    print("✓ API 读得到真数据，M0 的 API 这项过了")


if __name__ == "__main__":
    try:
        main()
    except (urllib.error.URLError, RuntimeError) as e:
        print(f"失败: {e}", file=sys.stderr)
        sys.exit(1)
