"""真连设备（telnet）。跟 `ssh.py` 是同一个 `DeviceAdapter` 接口的另一种传输方式。

纯标准库 `socket`，不引入新依赖。**telnet 是明文协议**，生产环境里只应该出现在
console 线这类应急/带外场景，不是给 VTY 远程管理面的通用建议。
"""

from __future__ import annotations

import os
import re
import socket
import time

from .base import DeviceAdapter

_PAGING_COMMAND = {
    "cisco": "terminal length 0",
}
_PROMPT_RE = re.compile(r"(?:^|\r?\n)(?:[A-Za-z0-9_.:/()-]+[>#]|<[^>\r\n]+>|\[[^\]\r\n]+\])\s*$")
_LOGIN_PROMPT_RE = re.compile(r"(?:username|login|password)\s*:\s*$", re.IGNORECASE)


class TelnetDeviceAdapter(DeviceAdapter):
    """配置字段名照 `env.example`：`DEVICE_HOST`/`DEVICE_TELNET_PORT`/
    `DEVICE_USERNAME`/`DEVICE_PASSWORD`/`DEVICE_VENDOR`。跟 `SSHDeviceAdapter`
    一样，这个账号必须是设备侧的只读账号——闸门是 `DeviceAdapter.run()` 里的
    白名单，这个类不做也做不到验证"对方账号是不是真的只读"。
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
        self.port = int(port if port is not None else os.environ.get("DEVICE_TELNET_PORT", "23"))
        self.username = username or os.environ.get("DEVICE_USERNAME", "")
        self.password = password or os.environ.get("DEVICE_PASSWORD", "")
        self.timeout = timeout
        self._sock: socket.socket | None = None
        self._logged_in = False

    def _read_until_quiet(self, wait: float = 1.5) -> str:
        deadline = time.monotonic() + self.timeout
        data = b""
        old_timeout = self._sock.gettimeout() if hasattr(self._sock, "gettimeout") else self.timeout
        self._sock.settimeout(min(0.2, self.timeout))
        try:
            while time.monotonic() < deadline:
                try:
                    chunk = self._sock.recv(8192)
                except socket.timeout:
                    if data:
                        return data.decode(errors="replace")
                    continue
                if not chunk:
                    break
                data += chunk
                text = data.decode(errors="replace")
                if self._looks_complete(text):
                    return text
        finally:
            self._sock.settimeout(old_timeout)
        return data.decode(errors="replace")

    @staticmethod
    def _looks_complete(text: str) -> bool:
        tail = text[-200:]
        return bool(_PROMPT_RE.search(tail) or _LOGIN_PROMPT_RE.search(tail))

    def _ensure_connected(self) -> None:
        if self._logged_in:
            return
        if not self.host:
            raise RuntimeError("没配 DEVICE_HOST，读一下 env.example 该填什么")

        self._sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        self._sock.settimeout(self.timeout)
        self._read_until_quiet(1.0)  # 欢迎 banner / Username: 提示

        self._sock.sendall((self.username + "\r\n").encode())
        self._read_until_quiet(1.0)  # Password: 提示
        self._sock.sendall((self.password + "\r\n").encode())
        out = self._read_until_quiet(1.5)

        if ">" not in out and "#" not in out:
            self.close()
            raise RuntimeError(f"telnet 登录失败，没拿到期望的提示符：{out[-200:]!r}")

        self._logged_in = True

        paging_cmd = _PAGING_COMMAND.get(self.vendor)
        if paging_cmd:
            self._sock.sendall((paging_cmd + "\r\n").encode())
            self._read_until_quiet(1.0)

    def _execute_one(self, command: str) -> str:
        self._ensure_connected()
        self._sock.sendall((command + "\r\n").encode())
        raw = self._read_until_quiet(2.5)
        return self._strip_echo_and_prompt(raw, command)

    @staticmethod
    def _strip_echo_and_prompt(raw: str, command: str) -> str:
        """telnet 是真终端会回显敲的内容，输出形如：
        `<命令回显>\\n<设备真正的输出...>\\n<提示符>\\n<提示符>`——
        去掉首行的命令回显和结尾重复的空提示符行，只留设备真正的输出。
        """
        lines = raw.splitlines()
        if lines and command in lines[0]:
            lines = lines[1:]
        while lines and (not lines[-1].strip() or TelnetDeviceAdapter._is_prompt_line(lines[-1].strip())):
            if len(lines[-1].strip()) > 40:
                # 太长不像是单纯的提示符行，保留（避免误删真实输出）
                break
            lines.pop()
        return "\n".join(lines).strip("\n")

    @staticmethod
    def _is_prompt_line(line: str) -> bool:
        return bool(re.fullmatch(r"(?:[A-Za-z0-9_.:/()-]+[>#]|<[^>\r\n]+>|\[[^\]\r\n]+\])", line))

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            finally:
                self._sock = None
                self._logged_in = False
