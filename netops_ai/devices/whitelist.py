"""设备命令白名单——两层只读保险的第一层。

这是闸门。上游是网工可编辑的诊断剧本（不可信输入），下游是设备侧的只读账号（兜底）。
剧本过闸门，不是闸门迁就剧本。

设计上的几个决定，改之前先看 内部文档：

- **只认全命令，不认缩写。** IOS 上 `sh` 等于 `show`、`conf t` 等于 `configure terminal`，
 缩写正是绕过前缀匹配最省事的办法。剧本里必须写全。
- **先查禁令再查放行。** 放行清单已经够严了，禁令是防我们自己哪天往放行清单里
 加错一条。两道都过才放行。
- **ping / tracert 默认不放行。** 它们是只读的，但会真往网上发包，属于「测试」不属于
 「查询」。要用得显式打开 `allow_active_tests`，让这件事是个有意识的决定。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: 命令长度上限。正常的 display 命令远到不了这个数，超了就是有人在拼东西
MAX_COMMAND_LEN = 200

#: 结构上直接毙掉的字符。设备 CLI 用不着这些，出现就是想拼命令或者往文件里写
FORBIDDEN_CHARS = {
    "\n": "换行",
    "\r": "回车",
    "\t": "制表符",
    ";": "命令分隔符",
    "&": "后台/连接符",
    "`": "反引号",
    "$": "变量展开",
    ">": "重定向",
    "<": "重定向",
    "\\": "转义符",
    '"': "引号",
    "'": "引号",
}

#: 禁令。命中首个词就拒，不管放行清单怎么写
#: 这里列的是「一旦执行就改变设备状态」的动词
FORBIDDEN_VERBS = frozenset(
    {
        # 进配置态
        "system-view", "sys", "configure", "config", "conf",
        # 改配置
        "undo", "no", "interface", "aaa", "user-interface", "acl",
        "ip", "route-static", "bgp", "ospf", "isis", "vlan", "stp",
        # 清计数、清会话
        "reset", "clear", "erase",
        # 存盘、重启、文件操作
        "save", "write", "reboot", "restart", "startup", "delete", "rmdir",
        "mkdir", "format", "copy", "move", "rename", "patch", "upgrade",
        # 往外连
        "telnet", "ssh", "ftp", "tftp", "sftp",
        # 调试（会拖垮设备）
        "debugging", "debug", "terminal",
    }
)

#: 放行的命令前缀。按厂商分。(前缀, 输出是否可能带敏感信息)
_ALLOWED: dict[str, tuple[tuple[str, bool], ...]] = {
    "cisco": (
        ("show", False),
    ),
}

#: 关分页。技术上算设置，但只影响本次会话、不落配置，不放行的话所有长输出都会卡在
#: `---- More ----`。单独列出来，不走通用前缀
_PAGING: dict[str, frozenset[str]] = {
    "cisco": frozenset({"terminal length 0"}),
}

#: 逐条精确放行，不走前缀匹配。`show running-config` 这批设备上内容渲染
#: 被 IOS 硬挡（`privilege exec level` 只解开执行权限，渲染那层解不开，试过
#: 4 种机制都不行），换成 `file privilege` + `more system:running-config`
#: 读文件系统视图拿到完整配置。**只放这一条精确命令，不放 `more` 整个前缀**——
#: `more flash:`/`more nvram:` 这类会碰文件系统其它内容，不在这次要开的范围。
_EXACT_ALLOWED: dict[str, frozenset[str]] = {
    "cisco": frozenset({"more system:running-config"}),
}


#: 会真往网上发包的「测试」类命令。默认不放行，见模块注释
_ACTIVE_TESTS: dict[str, tuple[str, ...]] = {
    "cisco": ("ping", "traceroute"),
}

#: 输出里可能带口令哈希、社区名、密钥的命令片段。命中就标 sensitive，
#: 交给上层决定要不要脱敏后再喂给模型
_SENSITIVE_MARKERS = (
    "current-configuration",
    "saved-configuration",
    "startup-configuration",
    "running-config",
    "startup-config",
    "this",
    "snmp-agent community",
    "local-user",
)

#: 管道后面只允许这几个过滤子句
_PIPE_FILTERS = frozenset({"include", "exclude", "begin", "section", "count"})

#: 管道后面可能出现的全部 IOS 动词，**不只是我们放行的那几个**。
#: 这张表的用途是「认出参数里的 `|` 后面是不是又起了一个子句」，
#: 所以要把 `redirect`/`tee`/`append` 这些**我们不放行的**也列进来——
#: 漏一个就等于给它开了后门。
_PIPE_VERBS = _PIPE_FILTERS | frozenset({
    "redirect", "tee", "append", "format", "exclude", "sort", "uniq", "last",
})

#: 过滤子句的参数。`$` `|` `\` 这些在结构检查那关就被毙了，到不了这里，
#: 所以字符类里不列它们，免得看的人以为放行了
#:
#: 放宽了三个字符（维护者：「白名单管道符放松点」）：
#: - `%`：Cisco 的日志类别就写成 `%OSPF-5-ADJCHG`，按类别过滤是最自然的用法，
#:   不放行等于逼着模型去 grep 全量日志
#: - `,` `=`：接口描述、`Nbr 1.1.1.1 on Ethernet0/1` 这类串里常见
#:
#: **这三个在管道参数里是纯文本**——它们是拿去做正则匹配的，不会被设备当命令解释。
#: 真正危险的拼接字符（`;` `&` `` ` `` `$` `>` `<` `\` 引号）在 `FORBIDDEN_CHARS`
#: 那关整条命令就被毙了，到不了这里。
_PIPE_ARG_RE = re.compile(r"^[A-Za-z0-9_.:/ \-\[\]()*+?^{}%,=|]{1,80}$")

SUPPORTED_VENDORS = tuple(_ALLOWED)


@dataclass(frozen=True)
class Verdict:
    """一条命令的裁决结果。

    拒绝的时候 `reason` 一定有内容——调用方要把它原样记下来，
    「模型想跑什么被拦了」本身就是实验素材。
    """

    allowed: bool
    command: str
    vendor: str
    reason: str = ""
    rule: str = ""
    sensitive: bool = False

    def __bool__(self) -> bool:
        return self.allowed


class CommandWhitelist:
    """某个厂商的命令闸门。

        >>> wl = CommandWhitelist("cisco")
        >>> wl.check("show ip interface brief").allowed
        True
        >>> wl.check("configure terminal").allowed
        False
    """

    def __init__(self, vendor: str, *, allow_active_tests: bool = False) -> None:
        vendor = vendor.strip().lower()
        if vendor not in _ALLOWED:
            raise ValueError(
                f"不认识的厂商 {vendor!r}，目前支持：{', '.join(SUPPORTED_VENDORS)}"
            )
        self.vendor = vendor
        self.allow_active_tests = allow_active_tests

    # -- 对外 ---------------------------------------------------------------

    def check(self, command: str) -> Verdict:
        """裁决一条命令。任何拿不准的情况一律拒绝。"""
        raw = command
        if not isinstance(command, str):
            return self._deny(str(raw), "命令不是字符串")

        # 结构检查放最前面。这一关拦的是注入，跟命令本身是什么无关
        structural = self._check_structure(command)
        if structural is not None:
            return structural

        cmd = " ".join(command.split())  # 折叠多余空格，统一成单空格
        low = cmd.lower()

        # 关分页单独放行，不走前缀
        if low in _PAGING[self.vendor]:
            return Verdict(True, cmd, self.vendor, rule="paging")

        # 精确放行的整条命令，不走前缀匹配——避免连带放开同前缀下的其它命令
        if low in _EXACT_ALLOWED.get(self.vendor, frozenset()):
            return Verdict(True, cmd, self.vendor, rule="exact-allow", sensitive=True)

        head, pipe = self._split_pipe(cmd)
        if head is None:
            return self._deny(cmd, pipe)  # 这时 pipe 装的是拒绝理由

        # 精确放行的命令后面接只读管道过滤（`| section` / `| include` / `| begin`），管道内容照常过上面的检查。
        # 在 V1 上实测 `more system:running-config | section router ospf` 可用；agent 想只看一段配置时
        # 这是 `show running-config | section ...` 的等价写法，以前被当成「换壳」拒掉。
        if "|" in cmd and head.lower() in _EXACT_ALLOWED.get(self.vendor, frozenset()):
            return Verdict(True, cmd, self.vendor, rule="exact-allow", sensitive=True)

        tokens = head.split()
        if not tokens:
            return self._deny(cmd, "空命令")
        verb = tokens[0].lower()

        # 禁令先于放行：防我们自己往放行清单里加错东西
        if verb in FORBIDDEN_VERBS:
            return self._deny(cmd, f"命中禁令动词 {verb!r}", rule="forbidden-verb")

        # 测试类命令：默认拒
        if verb in _ACTIVE_TESTS[self.vendor]:
            if not self.allow_active_tests:
                return self._deny(
                    cmd,
                    f"{verb!r} 会真往网上发包，属于测试不属于查询，默认不放行",
                    rule="active-test",
                )
            return Verdict(True, cmd, self.vendor, rule="active-test")

        for prefix, _ in _ALLOWED[self.vendor]:
            if verb == prefix:
                if len(tokens) == 1:
                    return self._deny(
                        cmd, f"{prefix!r} 后面什么都没跟，不是一条完整命令"
                    )
                return Verdict(
                    True,
                    cmd,
                    self.vendor,
                    rule=f"allow:{prefix}",
                    sensitive=self._is_sensitive(low),
                )

        return self._deny(
            cmd,
            f"{verb!r} 不在放行清单里（本厂商只放行 "
            f"{', '.join(p for p, _ in _ALLOWED[self.vendor])}，且不接受缩写）",
            rule="not-allowed",
        )

    def check_many(self, commands) -> list[Verdict]:
        return [self.check(c) for c in commands]

    def filter_allowed(self, commands) -> tuple[list[str], list[Verdict]]:
        """分成能跑的和被拒的。被拒的整条 Verdict 返回，给上层记日志。"""
        ok: list[str] = []
        denied: list[Verdict] = []
        for v in self.check_many(commands):
            (ok.append(v.command) if v.allowed else denied.append(v))
        return ok, denied

    # -- 内部 ---------------------------------------------------------------

    def _deny(self, command: str, reason: str, rule: str = "structural") -> Verdict:
        return Verdict(False, command, self.vendor, reason=reason, rule=rule)

    def _check_structure(self, command: str) -> Verdict | None:
        if not command.strip():
            return self._deny(command, "空命令")
        if len(command) > MAX_COMMAND_LEN:
            return self._deny(
                command[:80] + "...",
                f"命令超过 {MAX_COMMAND_LEN} 字符，正常的 display 到不了这个长度",
            )
        for ch, name in FORBIDDEN_CHARS.items():
            if ch in command:
                return self._deny(command, f"命令里有{name}（{ch!r}）")
        if not command.isascii():
            return self._deny(command, "命令里有非 ASCII 字符")
        if any(ord(c) < 0x20 or ord(c) == 0x7F for c in command):
            return self._deny(command, "命令里有控制字符")
        return None

    def _split_pipe(self, cmd: str) -> tuple[str | None, str]:
        """拆管道。返回 (命令主体, "")；不合法时返回 (None, 拒绝理由)。"""
        if "|" not in cmd:
            return cmd, ""
        head, _, tail = cmd.partition("|")
        head = head.strip()
        tail = tail.strip()
        if not head:
            return None, "管道前面没有命令"
        parts = tail.split(None, 1)
        if not parts:
            return None, "管道后面没有过滤子句"
        clause = parts[0].lower()
        if clause not in _PIPE_FILTERS:
            return None, (
                f"管道后面只允许 {', '.join(sorted(_PIPE_FILTERS))}，不允许 {clause!r}"
            )
        # `count` 接不接 pattern 是**厂商差异**：
        # IOS 上 `show logging | count %OSPF` 统计匹配行数，接 pattern 合法；
        # 其它厂商的 `| count` 语法不同，**没有设备可验的就不放宽**——
        # 按仓库纪律，没实测过的行为不当成已知。
        if clause == "count":
            if len(parts) == 1:
                return head, ""
        if len(parts) == 1:
            return None, f"{clause} 后面没有参数"
        # **参数里的 `|` 是正则的「或」，不是第二个管道。**
        # `show version | include uptime|reload|System image` 是合法的 IOS 写法，
        # 以前一律按「只允许一个管道」拒掉——Win 在审计页上看到的
        # 那 10 次「白名单拦截」全是这条，而且是**平台自己的巡检命令**被拦，
        # 不是 AI 越界。
        #
        # 区分办法：真正的第二个子句一定以一个 IOS 管道动词开头。
        for seg in parts[1].split("|")[1:]:
            first = seg.strip().split(None, 1)[:1]
            if first and first[0].lower() in _PIPE_VERBS:
                return None, f"只允许一个过滤子句，{first[0]!r} 是第二个"
        if not _PIPE_ARG_RE.match(parts[1]):
            return None, f"{clause} 的参数里有不允许的字符"
        return head, ""

    @staticmethod
    def _is_sensitive(low_cmd: str) -> bool:
        return any(m in low_cmd for m in _SENSITIVE_MARKERS)


def check(vendor: str, command: str, *, allow_active_tests: bool = False) -> Verdict:
    """一次性裁决，不用自己建对象。"""
    return CommandWhitelist(vendor, allow_active_tests=allow_active_tests).check(command)
