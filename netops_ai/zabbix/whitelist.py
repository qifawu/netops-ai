"""Zabbix API 方法白名单，只读那一层的闸门。两道检查：在放行清单里，并且以 `.get` 结尾
（登录、版本这几个例外除外）。写操作没有一个以 `.get` 结尾。
"""

from __future__ import annotations

from dataclasses import dataclass

#: 不以 .get 结尾但必须放行的方法。只有这三个，加之前想清楚
AUTH_METHODS = frozenset(
    {
        "apiinfo.version",   # 不需要认证，探活和对版本用
        "user.login",
        "user.logout",
    }
)

#: 放行的只读方法。按用途分组，加的时候写清楚为什么要它
READ_METHODS = frozenset(
    {
        # 资产：哪些设备、分在哪个组、管理地址是什么
        "host.get",
        "hostgroup.get",
        "hostinterface.get",
        "template.get",
        # 指标：监控项定义、当前值
        "item.get",
        "valuemap.get",
        # 数据：历史点和趋势。分析靠这两个
        "history.get",
        "trend.get",
        # 告警：触发器定义、当前未恢复的问题、历史事件
        "trigger.get",
        "problem.get",
        "event.get",
        "alert.get",
        # 上下文：维护窗口（判断是不是计划内停机，很重要）
        "maintenance.get",
        # 动作定义：看告警是怎么被推出去的
        "action.get",
    }
)

ALLOWED_METHODS = AUTH_METHODS | READ_METHODS

#: 写操作的后缀。只用来给出更准的拒绝理由，不是靠它拦
_WRITE_SUFFIXES = (
    ".create", ".update", ".delete", ".massadd", ".massupdate", ".massremove",
    ".import", ".export", ".copy", ".replacehostinterfaces", ".push",
    ".acknowledge", ".unblock", ".resettotp", ".provision",
)


@dataclass(frozen=True)
class Verdict:
    allowed: bool
    method: str
    reason: str = ""
    rule: str = ""

    def __bool__(self) -> bool:
        return self.allowed


def check_method(method: str) -> Verdict:
    """裁决一个 API 方法。拿不准一律拒。"""
    if not isinstance(method, str):
        return Verdict(False, str(method), "方法名不是字符串")

    name = method.strip()
    if not name:
        return Verdict(False, method, "方法名是空的")
    if name != method:
        return Verdict(False, method, "方法名两头有空白")

    low = name.lower()
    if low != name:
        return Verdict(False, method, "Zabbix 的方法名是小写的，大小写不匹配一律拒")

    if low not in ALLOWED_METHODS:
        for suffix in _WRITE_SUFFIXES:
            if low.endswith(suffix):
                return Verdict(
                    False, method,
                    f"{low!r} 是写操作（{suffix}），本项目不做任何写入",
                    rule="write-method",
                )
        return Verdict(
            False, method,
            f"{low!r} 不在放行清单里。要加先想清楚为什么，并且它必须是只读的",
            rule="not-allowed",
        )

    # 防呆：清单里加错了东西也拦得住
    if low not in AUTH_METHODS and not low.endswith(".get"):
        return Verdict(
            False, method,
            f"{low!r} 在放行清单里但不以 .get 结尾——清单加错了，去看 whitelist.py",
            rule="sanity-check",
        )

    return Verdict(True, low, rule="allow")


def assert_allowed(method: str) -> str:
    """给客户端用：不放行就抛。返回规范化后的方法名。"""
    v = check_method(method)
    if not v.allowed:
        raise PermissionError(f"Zabbix 方法被白名单拒绝：{v.reason}")
    return v.method
