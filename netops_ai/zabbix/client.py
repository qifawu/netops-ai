"""只读 Zabbix API 客户端。每次调用都先过 `whitelist.assert_allowed()`，闸门在 `_call()` 里。纯标准库。"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .whitelist import assert_allowed

#: 日志/trap 转发一行开头常带的接收时间前缀，例如
#: `Sep 26 15:58:47 192.0.2.51 186: *Sep 26 15:43:35.728: %BGP-5-ADJCHANGE: ...`——
#: 冒号前那段是转发/采集时打的时间，不带年份。SNMP trap 那种 `20260926.171505 PDU INFO:...`
#: 格式不匹配这个正则，天然不受影响（那是一整条 trap，不该被当成"补发的旧行"处理）。
_RECEIVE_TS_RE = re.compile(r"^([A-Za-z]{3}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2})")

#: 猜年份时，算出来的时间跟参照时刻差出去多少天就认为猜错了年份（跨年边界）。
_YEAR_GUESS_TOLERANCE_DAYS = 200


def parse_line_receive_time(line: str, near_epoch: int) -> int | None:
    """从一行日志/trap 转发文本开头的接收时间前缀解析出真实 epoch。

 背景（一条真实告警 的 RCA）：Zabbix 记一条日志型监控项历史值的 `clock`，反映的是
 "这条历史记录几时写进 Zabbix"，不是"这行日志描述的事几时真的发生"——设备到采集之间
 的传输一旦中断过又补发，会出现好几条历史记录**同一个 `clock`**（补发抵达的那一刻），
 但每条的文本内容各自带着旧得多的真实时间。**这个函数返回的才是後者。**

 前缀不带年份，借 `near_epoch`（通常就是这条历史记录自己的 `clock`）推算年份；
 算出来的时间如果离 `near_epoch` 差出去超过 `_YEAR_GUESS_TOLERANCE_DAYS` 天，
 说明是年份猜错了（跨年边界），换前一年/后一年再试。解析不出来前缀就返回 `None`，
 调用方应该退回用 Zabbix 自己的 `clock`——这个函数只在能读出真实时间时才更正，
 不能读出来不代表这行是可疑的，只是格式不认识。
    """
    match = _RECEIVE_TS_RE.match(str(line or "").strip())
    if not match:
        return None
    near_dt = datetime.fromtimestamp(near_epoch, tz=timezone.utc)
    for year in (near_dt.year, near_dt.year - 1, near_dt.year + 1):
        try:
            parsed = datetime.strptime(f"{year} {match.group(1)}", "%Y %b %d %H:%M:%S").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if abs((parsed - near_dt).days) <= _YEAR_GUESS_TOLERANCE_DAYS:
            return int(parsed.timestamp())
    return None


class ZabbixAPIError(RuntimeError):
    """Zabbix JSON-RPC 返回了 error，或者请求本身没发出去（连不上/超时）。"""


@dataclass
class ZabbixClient:
    """一个只读 Zabbix 会话。配置默认读 `ZABBIX_URL`/`ZABBIX_USER`/`ZABBIX_PASSWORD`。"""

    url: str = field(default_factory=lambda: os.environ.get("ZABBIX_URL", ""))
    user: str = field(default_factory=lambda: os.environ.get("ZABBIX_USER", ""))
    password: str = field(default_factory=lambda: os.environ.get("ZABBIX_PASSWORD", ""))
    timeout: float = 10.0

    _token: str | None = field(default=None, init=False, repr=False)
    _request_id: int = field(default=0, init=False, repr=False)

    # -- 会话 -----------------------------------------------------------

    @property
    def _endpoint(self) -> str:
        if not self.url:
            raise ZabbixAPIError("没配 ZABBIX_URL，读一下 env.example 该填什么")
        return self.url.rstrip("/") + "/api_jsonrpc.php"

    def login(self) -> str:
        """`user.login` 是白名单里唯二不需要先登录的方法之一，走同一条 `_call()`。"""
        token = self._call("user.login", {"username": self.user, "password": self.password}, authed=False)
        if not isinstance(token, str):
            raise ZabbixAPIError(f"user.login 返回的不是 token 字符串：{token!r}")
        self._token = token
        return token

    def logout(self) -> None:
        if self._token is None:
            return
        try:
            self._call("user.logout", {})
        finally:
            self._token = None

    def ensure_login(self) -> None:
        if self._token is None:
            self.login()

    def __enter__(self) -> "ZabbixClient":
        self.ensure_login()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.logout()

    # -- 高层只读能力（S4 要求的最小集合）--------------------------------

    def list_hosts(self, **filters: Any) -> list[dict]:
        """列主机。"""
        self.ensure_login()
        params = {"output": "extend", **filters}
        return self._call("host.get", params)

    def list_items(self, host_id: str, **filters: Any) -> list[dict]:
        """列某主机的监控项。"""
        self.ensure_login()
        params = {"output": "extend", "hostids": host_id, **filters}
        return self._call("item.get", params)

    def get_item_value_type(self, item_id: str) -> int | None:
        """查监控项的 `value_type`，取不到返回 None。get_history 必须传对类型。"""
        self.ensure_login()
        rows = self._call("item.get", {"output": ["itemid", "value_type"], "itemids": item_id})
        if not rows:
            return None
        try:
            return int(rows[0].get("value_type"))
        except (TypeError, ValueError):
            return None

    def get_history(
        self,
        item_id: str,
        *,
        history_type: int = 0,
        time_from: int | None = None,
        time_till: int | None = None,
        limit: int = 100,
    ) -> list[dict]:
        """取监控项历史。`history_type` 照 item 的 `value_type` 传（0 float/1 char/2 log/3 uint/4 text），传错只会返回空。"""
        self.ensure_login()
        params: dict[str, Any] = {
            "output": "extend",
            "itemids": item_id,
            "history": history_type,
            "sortfield": "clock",
            "sortorder": "DESC",
            "limit": limit,
        }
        if time_from is not None:
            params["time_from"] = time_from
        if time_till is not None:
            params["time_till"] = time_till
        return self._call("history.get", params)

    def get_trends(
        self,
        item_id: str,
        *,
        time_from: int | None = None,
        time_till: int | None = None,
        limit: int = 100,
    ) -> list[dict]:
        """取某监控项的趋势数据。

        Zabbix history 保存原始点，通常保留时间短；trends 是按小时汇总的
        `value_min/value_avg/value_max`，适合"过去一周/一个月"这类长窗口查询。
        """
        self.ensure_login()
        params: dict[str, Any] = {
            "output": "extend",
            "itemids": item_id,
            "sortfield": "clock",
            "sortorder": "DESC",
            "limit": limit,
        }
        if time_from is not None:
            params["time_from"] = time_from
        if time_till is not None:
            params["time_till"] = time_till
        return self._call("trend.get", params)

    def get_active_problems(self, **filters: Any) -> list[dict]:
        """取当前告警（未恢复的 problem）。"""
        self.ensure_login()
        params = {"output": "extend", "recent": False, **filters}
        return self._call("problem.get", params)

    def _call(self, method: str, params: dict, *, authed: bool = True) -> Any:
        """真正发请求的地方。闸门在第一行，改哪个上层方法都绕不过去。"""
        method = assert_allowed(method)

        self._request_id += 1
        payload = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
            "id": self._request_id,
        }
        headers = {"Content-Type": "application/json-rpc"}
        if authed and self._token:
            # Zabbix >= 6.4 推荐用 Authorization header 带 token，不再塞进 params
            headers["Authorization"] = f"Bearer {self._token}"

        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(self._endpoint, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8")
        except urllib.error.URLError as exc:
            raise ZabbixAPIError(f"连不上 Zabbix（{self._endpoint}）：{exc}") from exc

        try:
            body = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ZabbixAPIError(f"{method} 返回的不是合法 JSON：{raw[:200]!r}") from exc

        if "error" in body:
            err = body["error"]
            raise ZabbixAPIError(
                f"{method} 失败：{err.get('message')} / {err.get('data')}"
            )
        return body["result"]
