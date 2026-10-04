"""Zabbix API 方法白名单的测试。

重点是：任何写方法都进不来，尤其 `script.execute`——那是这套 API 里唯一
能在主机上真跑命令的口子。
"""

import unittest

from netops_ai.zabbix.whitelist import (
    ALLOWED_METHODS,
    AUTH_METHODS,
    READ_METHODS,
    assert_allowed,
    check_method,
)


class TestAllowed(unittest.TestCase):
    def test_分析要用的只读方法都放行(self):
        for m in [
            "host.get",
            "hostgroup.get",
            "hostinterface.get",
            "item.get",
            "history.get",
            "trend.get",
            "trigger.get",
            "problem.get",
            "event.get",
            "maintenance.get",
        ]:
            with self.subTest(method=m):
                self.assertTrue(check_method(m).allowed, m)

    def test_登录和版本这三个例外(self):
        for m in ["apiinfo.version", "user.login", "user.logout"]:
            with self.subTest(method=m):
                self.assertTrue(check_method(m).allowed, m)


class TestDenied(unittest.TestCase):
    def test_写方法一个都进不来(self):
        for m in [
            "host.create",
            "host.update",
            "host.delete",
            "host.massupdate",
            "item.create",
            "trigger.update",
            "configuration.import",
            "hostinterface.replacehostinterfaces",
            "event.acknowledge",
            "user.update",
        ]:
            with self.subTest(method=m):
                v = check_method(m)
                self.assertFalse(v.allowed, m)

    def test_script_execute必须死堵(self):
        # 这是 Zabbix API 里唯一能在主机上真跑命令的方法。
        # 它不带 .create/.update 后缀，全靠放行清单拦
        v = check_method("script.execute")
        self.assertFalse(v.allowed)
        self.assertEqual(v.rule, "not-allowed")

    def test_没在清单里的只读方法也拒(self):
        # 严格白名单：没想清楚要它之前一律不放
        for m in ["usergroup.get", "mediatype.get", "proxy.get", "task.get"]:
            with self.subTest(method=m):
                self.assertFalse(check_method(m).allowed, m)

    def test_大小写不匹配一律拒(self):
        for m in ["Host.Get", "HOST.GET", "User.Login"]:
            with self.subTest(method=m):
                self.assertFalse(check_method(m).allowed, m)

    def test_两头有空白一律拒(self):
        for m in [" host.get", "host.get ", "\thost.get"]:
            with self.subTest(method=m):
                self.assertFalse(check_method(m).allowed, m)

    def test_空的和非字符串(self):
        self.assertFalse(check_method("").allowed)
        self.assertFalse(check_method(None).allowed)
        self.assertFalse(check_method(123).allowed)

    def test_拒绝一定带理由(self):
        self.assertTrue(check_method("host.create").reason)


class TestInvariants(unittest.TestCase):
    """清单本身的约束。这几条防的是我们自己哪天往清单里加错东西。"""

    def test_只读清单里每一条都以get结尾(self):
        for m in READ_METHODS:
            with self.subTest(method=m):
                self.assertTrue(m.endswith(".get"), f"{m} 在只读清单里但不是 .get")

    def test_清单里没有大写(self):
        for m in ALLOWED_METHODS:
            with self.subTest(method=m):
                self.assertEqual(m, m.lower())

    def test_例外只有三个(self):
        self.assertEqual(len(AUTH_METHODS), 3)

    def test_两个清单不重叠(self):
        self.assertFalse(AUTH_METHODS & READ_METHODS)


class TestAssertAllowed(unittest.TestCase):
    def test_放行的返回规范化方法名(self):
        self.assertEqual(assert_allowed("host.get"), "host.get")

    def test_不放行的抛异常(self):
        with self.assertRaises(PermissionError):
            assert_allowed("host.create")
        with self.assertRaises(PermissionError):
            assert_allowed("script.execute")


if __name__ == "__main__":
    unittest.main()
