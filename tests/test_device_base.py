"""DeviceAdapter 基类：白名单闸门统一在 run() 里，子类不用也不许重复判断。"""

import unittest

from netops_ai.devices.base import CommandResult, DeviceAdapter


class _EchoAdapter(DeviceAdapter):
    """测试用最小子类：直接把命令回显回去，不连任何真东西。"""

    def __init__(self, vendor="cisco", **kw):
        super().__init__(vendor, **kw)
        self.executed: list[str] = []

    def _execute_one(self, command: str) -> str:
        self.executed.append(command)
        return f"echo: {command}"


class _BoomAdapter(DeviceAdapter):
    def _execute_one(self, command: str) -> str:
        raise ConnectionError("设备不在线")


class TestGate(unittest.TestCase):
    def test_放行命令真的执行了(self):
        a = _EchoAdapter()
        result = a.run("show version")
        self.assertTrue(result.allowed)
        self.assertEqual(result.output, "echo: show version")
        self.assertEqual(a.executed, ["show version"])

    def test_被拒命令根本没执行(self):
        a = _EchoAdapter()
        result = a.run("configure terminal")
        self.assertFalse(result.allowed)
        self.assertEqual(result.output, "")
        self.assertTrue(result.denial_reason)
        self.assertEqual(a.executed, [])  # 闸门拦下来了，_execute_one 压根没被调

    def test_被拒的命令记进denied_log(self):
        a = _EchoAdapter()
        a.run("system-view")
        a.run("show version")  # 放行的不进这个日志
        self.assertEqual(len(a.denied_log), 1)
        self.assertEqual(a.denied_log[0].command, "system-view")

    def test_run_many批量(self):
        a = _EchoAdapter()
        results = a.run_many(["show version", "reload", "show ip route"])
        self.assertEqual([r.allowed for r in results], [True, False, True])

    def test_ok属性要求既放行又没出错(self):
        self.assertTrue(CommandResult("show version", allowed=True, output="x").ok)
        self.assertFalse(CommandResult("show version", allowed=True, error="连接超时").ok)
        self.assertFalse(CommandResult("configure terminal", allowed=False).ok)

    def test_执行时抛异常变成CommandResult的error不炸穿(self):
        a = _BoomAdapter("cisco")
        result = a.run("show version")
        self.assertTrue(result.allowed)  # 白名单放行了，是执行阶段失败
        self.assertFalse(result.ok)
        self.assertIn("设备不在线", result.error)

    def test_可以当上下文管理器用(self):
        with _EchoAdapter() as a:
            self.assertEqual(a.run("show version").output, "echo: show version")


if __name__ == "__main__":
    unittest.main()
