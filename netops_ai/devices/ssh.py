"""真连设备。每条命令先过白名单（在 `DeviceAdapter.run()` 里，这个文件不重复判断），
被拒就不发给设备。

这里调用系统 OpenSSH 客户端，而不是把 SSH 协议栈锁死在某个 Python 依赖版本上。
V1 这台 IOSv 15.9 只提供老的 SHA-1 系 SSH 算法，现代 OpenSSH 默认关闭但没有删除；
显式用 ``+`` 打开这些算法，是网工在跳板机上兼容老设备的常见做法。现代设备
（IOS-XE 17.x 等）通常不需要这些兼容开关。
"""

from __future__ import annotations

import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

from .base import DeviceAdapter

_ASKPASS_ENV = "NETOPS_AI_SSH_PASSWORD"
_SSH_OPTIONS = [
    "-o",
    "KexAlgorithms=+diffie-hellman-group14-sha1",
    "-o",
    "HostKeyAlgorithms=+ssh-rsa",
    "-o",
    "PubkeyAcceptedAlgorithms=+ssh-rsa",
    "-o",
    "StrictHostKeyChecking=accept-new",
    "-o",
    "PreferredAuthentications=password",
    "-o",
    "NumberOfPasswordPrompts=1",
]


class SSHDeviceAdapter(DeviceAdapter):
    """配置字段名照 `env.example`：`DEVICE_HOST` / `DEVICE_PORT` / `DEVICE_USERNAME` /
 `DEVICE_PASSWORD` / `DEVICE_VENDOR`。这个账号必须是设备侧的只读账号——
 两层只读保险的第二层，这个类本身不做也做不到强制校验
 "对方账号是不是真的只读"，那件事在设备上配置，代码层面只能守住命令白名单这一层。
    """

    def __init__(
        self,
        vendor: str | None = None,
        *,
        host: str | None = None,
        port: int | None = None,
        username: str | None = None,
        password: str | None = None,
        timeout: float = 10.0,
        allow_active_tests: bool = False,
    ) -> None:
        super().__init__(vendor or os.environ.get("DEVICE_VENDOR", ""), allow_active_tests=allow_active_tests)
        self.host = host or os.environ.get("DEVICE_HOST", "")
        self.port = int(port if port is not None else os.environ.get("DEVICE_PORT", "22"))
        self.username = username or os.environ.get("DEVICE_USERNAME", "")
        self.password = password or os.environ.get("DEVICE_PASSWORD", "")
        self.timeout = timeout
        self._askpass_path: Path | None = None

    def _ensure_ready(self) -> None:
        if not self.host:
            raise RuntimeError("没配 DEVICE_HOST，读一下 env.example 该填什么")
        if not self.username:
            raise RuntimeError("没配 DEVICE_USERNAME，读一下 env.example 该填什么")
        if not self.password:
            raise RuntimeError("没配 DEVICE_PASSWORD，读一下 env.example 该填什么")

    def _askpass_helper(self) -> str:
        """OpenSSH 不从普通 stdin 读密码；用 askpass 从环境变量取，避免密码进 argv。"""
        if self._askpass_path is not None:
            return str(self._askpass_path)
        suffix = ".cmd" if os.name == "nt" else ".sh"
        fd, path = tempfile.mkstemp(prefix="netops-ai-ssh-askpass-", suffix=suffix, text=True)
        helper = Path(path)
        if os.name == "nt":
            content = f'@echo off\r\n"{sys.executable}" -c "import os; print(os.environ.get(\'{_ASKPASS_ENV}\', \'\'))"\r\n'
        else:
            content = (
                "#!/bin/sh\n"
                f"exec {sys.executable!r} -c "
                f"\"import os; print(os.environ.get('{_ASKPASS_ENV}', ''))\"\n"
            )
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(content)
        if os.name != "nt":
            helper.chmod(helper.stat().st_mode | stat.S_IXUSR)
        self._askpass_path = helper
        return str(helper)

    def _ssh_command(self, command: str) -> list[str]:
        return [
            "ssh",
            *(_SSH_OPTIONS),
            "-p",
            str(self.port),
            f"{self.username}@{self.host}",
            command,
        ]

    def _execute_one(self, command: str) -> str:
        self._ensure_ready()
        # 用一次性 ssh exec 而不是交互会话：
        # 白名单一次只放行一条完整命令，不需要维护分页/提示符这些交互状态，
        # IOS 对 SSH exec 通道执行单条只读命令本身就是标准用法。
        env = {
            **os.environ,
            _ASKPASS_ENV: self.password,
            "SSH_ASKPASS": self._askpass_helper(),
            "SSH_ASKPASS_REQUIRE": "force",
            "DISPLAY": os.environ.get("DISPLAY", "netops-ai"),
        }
        proc = subprocess.run(
            self._ssh_command(command),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=self.timeout,
            env=env,
            shell=False,
        )
        out = proc.stdout
        err = proc.stderr
        if proc.returncode != 0 and not out.strip():
            raise RuntimeError(f"设备返回错误：{err.strip()}")
        return out

    def close(self) -> None:
        if self._askpass_path is not None:
            try:
                self._askpass_path.unlink(missing_ok=True)
            finally:
                self._askpass_path = None
