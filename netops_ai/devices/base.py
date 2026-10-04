"""设备适配层的公共接口。

上层（将来的诊断剧本执行器）只认 `DeviceAdapter` 这个接口，不关心背后是
读 `labs/captures/` 的假数据（`mock.py`）还是真连设备（`ssh.py`）——
这是 内部文档 M1b 要求的"mock/ssh 可切换"。

白名单检查统一放在这里的 `run` 里，子类只需要实现 `_execute_one`，
不用也不许自己重新判断一遍某条命令能不能跑——闸门只有一处，
不然哪天 mock 和 ssh 两边的判断逻辑不一致就是隐患。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .whitelist import CommandWhitelist, Verdict


@dataclass(frozen=True)
class CommandResult:
    """一条命令的执行结果。

    `allowed=False` 时 `output` 一定是空的——被拒的命令根本没有机会执行，
    这是两层只读保险里"代码层白名单"这一层要保证的事，不是靠子类自觉。
    """

    command: str
    allowed: bool
    output: str = ""
    error: str = ""
    denial_reason: str = ""

    @property
    def ok(self) -> bool:
        return self.allowed and not self.error


class DeviceAdapter:
    """厂商 + 只读命令闸门 + 执行方式（mock/ssh），三者拼在一起才是一个可用的适配器。

        >>> class EchoAdapter(DeviceAdapter):
        ...     def _execute_one(self, command):
        ...         return f"echo: {command}"
        >>> a = EchoAdapter("cisco")
        >>> a.run("show version").output
        'echo: show version'
        >>> a.run("configure terminal").allowed
        False
    """

    def __init__(self, vendor: str, *, allow_active_tests: bool = False) -> None:
        self.vendor = vendor
        self._whitelist = CommandWhitelist(vendor, allow_active_tests=allow_active_tests)
        #: 被拒的命令全部记在这——"模型想跑越界命令被白名单拦下"的证据，
        #: 就是从这里拿的，不是靠翻日志文件
        self.denied_log: list[Verdict] = []

    def run(self, command: str) -> CommandResult:
        verdict = self._whitelist.check(command)
        if not verdict.allowed:
            self.denied_log.append(verdict)
            return CommandResult(command=command, allowed=False, denial_reason=verdict.reason)
        try:
            output = self._execute_one(verdict.command)
        except Exception as exc:  # 子类的连接/读取错误，不让它变成裸异常炸穿上层
            return CommandResult(command=command, allowed=True, error=str(exc))
        return CommandResult(command=command, allowed=True, output=output)

    def run_many(self, commands) -> list[CommandResult]:
        return [self.run(c) for c in commands]

    # -- 子类必须实现 -----------------------------------------------------

    def _execute_one(self, command: str) -> str:
        """`command` 已经过白名单规范化（折叠空格等），子类直接拿去用就行。"""
        raise NotImplementedError

    # -- 可选：需要连接/断开的子类（ssh）覆盖这两个 -------------------------

    def close(self) -> None:
        pass

    def __enter__(self) -> "DeviceAdapter":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()
