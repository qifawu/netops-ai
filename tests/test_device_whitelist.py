"""设备命令白名单的测试（开源版：Cisco IOS）。

这层是两层只读保险的第一层，闸门破了整个项目的论点就塌了，所以测试往狠里写：
注入、缩写、管道、大小写、边界，一样不放过。
"""

import unittest

from netops_ai.devices.whitelist import (
    SUPPORTED_VENDORS,
    CommandWhitelist,
    check,
)


class TestAllowedCommands(unittest.TestCase):
    def setUp(self):
        self.wl = CommandWhitelist("cisco")

    def test_常见的查询命令都放行(self):
        for cmd in [
            "show version",
            "show ip interface brief",
            "show ip route",
            "show ip ospf neighbor",
            "show cdp neighbors",
            "show processes cpu",
            "show memory statistics",
            "show logging",
            "show interfaces GigabitEthernet0/1",
        ]:
            with self.subTest(cmd=cmd):
                self.assertTrue(self.wl.check(cmd).allowed, cmd)

    def test_只认show系_不认其它厂商的写法(self):
        self.assertTrue(self.wl.check("show ip interface brief").allowed)
        self.assertFalse(self.wl.check("display version").allowed)

    def test_关分页单独放行(self):
        v = self.wl.check("terminal length 0")
        self.assertTrue(v.allowed)
        self.assertEqual(v.rule, "paging")
        self.assertFalse(self.wl.check("screen-length 0 temporary").allowed)

    def test_多余空格会被折叠(self):
        v = self.wl.check("  show    ip   interface   brief  ")
        self.assertTrue(v.allowed)
        self.assertEqual(v.command, "show ip interface brief")

    def test_只精确放行more_system_running_config不放整个more前缀(self):
        """`show running-config` 在只读账号下内容被 IOS 渲染层挡住，
        换成 `more system:running-config` 读文件系统视图。只精确放行这一条，
        `more flash:` / `more nvram:startup-config` 这类同前缀但没明确要开的命令必须继续被拒——
        不能因为放了一条 more 就等于开了前缀。
        """
        v = self.wl.check("more system:running-config")
        self.assertTrue(v.allowed)
        self.assertEqual(v.rule, "exact-allow")
        self.assertTrue(v.sensitive)
        self.assertFalse(self.wl.check("more flash:").allowed)
        self.assertFalse(self.wl.check("more nvram:startup-config").allowed)
        self.assertFalse(self.wl.check("more system:startup-config").allowed)


class TestForbidden(unittest.TestCase):
    def setUp(self):
        self.wl = CommandWhitelist("cisco")

    def test_进配置态一律拒(self):
        for cmd in ["configure terminal", "conf t", "configure"]:
            with self.subTest(cmd=cmd):
                v = self.wl.check(cmd)
                self.assertFalse(v.allowed)
                self.assertEqual(v.rule, "forbidden-verb")

    def test_改配置和清计数一律拒(self):
        for cmd in [
            "write memory",
            "reload",
            "delete flash:/config.cfg",
            "clear counters",
            "copy running-config startup-config",
            "debug ip packet",
            "terminal monitor",
            "enable",
        ]:
            with self.subTest(cmd=cmd):
                self.assertFalse(self.wl.check(cmd).allowed, cmd)

    def test_不接受缩写(self):
        # IOS 上 sh 就是 show、conf t 就是 configure terminal。
        # 缩写是绕过前缀匹配最省事的办法，所以全部拒掉，剧本里必须写全
        for cmd in ["sh version", "sho version", "conf t", "wr"]:
            with self.subTest(cmd=cmd):
                self.assertFalse(self.wl.check(cmd).allowed, cmd)

    def test_光一个show不算命令(self):
        self.assertFalse(self.wl.check("show").allowed)

    def test_空命令(self):
        self.assertFalse(self.wl.check("").allowed)
        self.assertFalse(self.wl.check("   ").allowed)


class TestInjection(unittest.TestCase):
    """注入类。这些是真会被模型或者剧本作者写出来的东西。"""

    def setUp(self):
        self.wl = CommandWhitelist("cisco")

    def test_换行拼第二条命令(self):
        v = self.wl.check("show version\nconfigure terminal")
        self.assertFalse(v.allowed)
        self.assertIn("换行", v.reason)

    def test_回车拼第二条命令(self):
        self.assertFalse(self.wl.check("show version\rconfigure terminal").allowed)

    def test_分号拼命令(self):
        self.assertFalse(self.wl.check("show version ; clear counters").allowed)

    def test_shell那套元字符全拒(self):
        for cmd in [
            "show version && reload",
            "show version `reload`",
            "show version $(reload)",
            "show version > flash:/a.txt",
            "show version < flash:/a.txt",
            'show version "x"',
            "show version 'x'",
            "show version \\n configure terminal",
        ]:
            with self.subTest(cmd=cmd):
                self.assertFalse(self.wl.check(cmd).allowed, cmd)

    def test_控制字符(self):
        self.assertFalse(self.wl.check("show version\x00").allowed)
        self.assertFalse(self.wl.check("show\x1b[2J version").allowed)

    def test_非ascii(self):
        self.assertFalse(self.wl.check("show 版本").allowed)

    def test_超长命令(self):
        self.assertFalse(self.wl.check("show " + "a" * 500).allowed)

    def test_不是字符串(self):
        self.assertFalse(self.wl.check(None).allowed)
        self.assertFalse(self.wl.check(123).allowed)


class TestPipe(unittest.TestCase):
    def setUp(self):
        self.wl = CommandWhitelist("cisco")

    def test_允许的过滤子句(self):
        for cmd in [
            "show running-config | include ospf",
            "show ip interface brief | exclude down",
            "show logging | begin Sep",
            "show ip route | count",
            "show running-config | section router",
        ]:
            with self.subTest(cmd=cmd):
                self.assertTrue(self.wl.check(cmd).allowed, cmd)

    def test_管道后面不是过滤子句就拒(self):
        for cmd in [
            "show version | reboot",
            "show version | redirect flash:x",  # IOS 的 redirect 会往设备文件系统写文件
            "show version | more",
        ]:
            with self.subTest(cmd=cmd):
                self.assertFalse(self.wl.check(cmd).allowed, cmd)

    def test_只允许一个管道(self):
        self.assertFalse(self.wl.check("show version | include a | include b").allowed)

    def test_过滤子句要有参数(self):
        self.assertFalse(self.wl.check("show version | include").allowed)

    def test_管道前面必须有命令(self):
        self.assertFalse(self.wl.check("| include ospf").allowed)


class TestActiveTests(unittest.TestCase):
    def test_默认不放行ping和traceroute(self):
        wl = CommandWhitelist("cisco")
        for cmd in ["ping 10.0.0.1", "traceroute 10.0.0.1"]:
            with self.subTest(cmd=cmd):
                v = wl.check(cmd)
                self.assertFalse(v.allowed)
                self.assertEqual(v.rule, "active-test")

    def test_显式打开才放行(self):
        wl = CommandWhitelist("cisco", allow_active_tests=True)
        self.assertTrue(wl.check("ping 10.0.0.1").allowed)
        # 打开了也不等于什么都能跑
        self.assertFalse(wl.check("configure terminal").allowed)


class TestSensitive(unittest.TestCase):
    def test_看配置的命令标敏感(self):
        wl = CommandWhitelist("cisco")
        for cmd in [
            "show running-config",
            "show startup-config",
            "show running-config | include snmp-server community",
        ]:
            with self.subTest(cmd=cmd):
                v = wl.check(cmd)
                self.assertTrue(v.allowed, cmd)
                self.assertTrue(v.sensitive, cmd)

    def test_普通查询不标敏感(self):
        self.assertFalse(CommandWhitelist("cisco").check("show ip interface brief").sensitive)


class TestApi(unittest.TestCase):
    def test_不认识的厂商直接抛(self):
        for vendor in ("juniper", "arista", "unknown"):  # 开源版只支持 Cisco IOS
            with self.subTest(vendor=vendor), self.assertRaises(ValueError):
                CommandWhitelist(vendor)

    def test_只支持cisco(self):
        self.assertEqual(SUPPORTED_VENDORS, ("cisco",))

    def test_厂商名大小写和空白无所谓(self):
        self.assertTrue(CommandWhitelist(" Cisco ").check("show version").allowed)

    def test_模块级快捷函数(self):
        self.assertTrue(check("cisco", "show version").allowed)
        self.assertFalse(check("cisco", "configure terminal").allowed)

    def test_verdict能当布尔用(self):
        self.assertTrue(bool(check("cisco", "show version")))
        self.assertFalse(bool(check("cisco", "configure terminal")))

    def test_拒绝一定带理由(self):
        v = check("cisco", "configure terminal")
        self.assertTrue(v.reason, "拒绝必须给理由，上层要原样记下来")

    def test_filter_allowed分拣(self):
        wl = CommandWhitelist("cisco")
        ok, denied = wl.filter_allowed(["show version", "configure terminal", "show ip interface brief"])
        self.assertEqual(ok, ["show version", "show ip interface brief"])
        self.assertEqual(len(denied), 1)
        self.assertEqual(denied[0].command, "configure terminal")

    def test_每个支持的厂商都能建起来(self):
        for v in SUPPORTED_VENDORS:
            with self.subTest(vendor=v):
                CommandWhitelist(v)


class Test管道参数放宽(unittest.TestCase):
    """维护者 「白名单管道符放松点」。

 起因：`show logging | include %OSPF` 被拒——但 Cisco 的日志类别就写成
 `%OSPF-5-ADJCHG`，不放行等于逼着模型去 grep 全量日志，白白烧 token。
 放宽的是 `%` `,` `=` 三个字符，以及 `count` 接 pattern 的合法写法。

 **放宽的只能是纯文本字符。** 能拼命令的那些（`;` `&` 反引号 `$` `>` `<`
 `\\` 引号）在 FORBIDDEN_CHARS 那关整条命令就被毙了，下面第四条锁住这一点。
    """

    def setUp(self):
        self.w = CommandWhitelist("cisco")

    def test_按日志类别过滤(self):
        self.assertTrue(self.w.check("show logging | include %OSPF").allowed)
        self.assertTrue(self.w.check("show logging | exclude %SSH").allowed)

    def test_count可以接pattern也可以不接(self):
        # IOS 上 `| count <pattern>` 统计匹配行数，不接则统计总行数，两种都合法
        self.assertTrue(self.w.check("show logging | count %LINK").allowed)
        self.assertTrue(self.w.check("show logging | count").allowed)

    def test_逗号等号在接口描述里常见(self):
        self.assertTrue(
            self.w.check("show interfaces | include Ethernet0/1, line protocol").allowed
        )

    def test_能拼命令的字符一个都没放开(self):
        for bad in ["a`b", "a;b", "a$b", "a>b", "a<b", "a&b", 'a"b', "a\'b", "a\\b"]:
            v = self.w.check(f"show logging | include {bad}")
            self.assertFalse(v.allowed, f"{bad!r} 不该放行")

    def test_过滤子句本身仍然是白名单(self):
        self.assertFalse(self.w.check("show logging | grep OSPF").allowed)

    def test_仍然只允许一个管道(self):
        self.assertFalse(self.w.check("show logging | include a | include b").allowed)


class Test正则的或不是第二个管道(unittest.TestCase):
    """Win 在审计页上看到 10 次「白名单拦截」，查下来全是
 **平台自己的巡检命令** `show version | include uptime|reload|System image`
 被拦，不是 AI 越界。根因：`include` 参数里的 `|` 是正则的「或」，
 被当成了第二个管道。

 区分办法：真正的第二个子句一定以一个 IOS 管道动词开头。
 `_PIPE_VERBS` 里**特意收了 redirect/tee/append 这些我们不放行的**——
 这张表是用来「认出第二个子句」的，漏一个就等于开后门。
    """

    def setUp(self):
        self.w = CommandWhitelist("cisco")

    def test_平台自己那条巡检命令要放行(self):
        self.assertTrue(
            self.w.check("show version | include uptime|reload|System image").allowed
        )

    def test_日志按多个类别过滤(self):
        self.assertTrue(self.w.check("show logging | include %OSPF|%LINK").allowed)

    def test_真的第二个子句仍然拒绝(self):
        for bad in ["show run | include x | exclude y",
                    "show run | include x | include y"]:
            self.assertFalse(self.w.check(bad).allowed, bad)

    def test_不放行的动词藏在正则里也要认出来(self):
        """`include x|redirect flash:a` 中间没空格，最像漏网的写法。"""
        for verb in ("redirect", "tee", "append", "format"):
            v = self.w.check(f"show run | include x|{verb} flash:a")
            self.assertFalse(v.allowed, verb)
            self.assertIn("第二个", v.reason)


class Testshow_running_config放行(unittest.TestCase):
    """维护者拍板：`show running-config` / `show startup-config` 是纯只读命令，不该被拒。

 原来拒它是因为怕它让 IOS 发 config-change trap 自激；trap 的 configChange 监控项
 和触发器后来已经关掉，而且 在 A1/V1 上用 ai-readonly（privilege 5）实测：这两条命令
 既不产生 `%SYS-5-CONFIG_I`，输出也只有 `Current configuration : 5 bytes` + `end`（设备侧渲染限制）。放行只是让命令能发出去，输出是设备的实际输出。
    """

    def setUp(self):
        self.cisco = CommandWhitelist("cisco")

    def test_完整写法和缩写全部放行(self):
        for cmd in ("show running-config", "show run", "show runn", "show startup-config", "show start"):
            with self.subTest(cmd=cmd):
                self.assertTrue(self.cisco.check(cmd).allowed, cmd)

    def test_带只读管道过滤放行(self):
        self.assertTrue(self.cisco.check("show running-config | include snmp").allowed)
        self.assertTrue(self.cisco.check("show running-config | section router ospf").allowed)

    def test_管道里的写动作仍然拒(self):
        for cmd in ("show running-config | redirect flash:a", "show run | tee flash:a", "show run | append flash:a"):
            with self.subTest(cmd=cmd):
                self.assertFalse(self.cisco.check(cmd).allowed, cmd)

    def test_输出可能带口令哈希_仍标sensitive(self):
        self.assertTrue(self.cisco.check("show running-config").sensitive)

    def test_more_system_running_config_仍然精确放行(self):
        self.assertTrue(self.cisco.check("more system:running-config").allowed)
        self.assertFalse(self.cisco.check("more flash:config.txt").allowed)

    def test_more_system_running_config_可接只读管道过滤(self):
        for cmd in ("more system:running-config | section router ospf",
                    "more system:running-config | include ^interface",
                    "more system:running-config | begin router bgp"):
            with self.subTest(cmd=cmd):
                v = self.cisco.check(cmd)
                self.assertTrue(v.allowed, cmd)
                self.assertTrue(v.sensitive)

    def test_more_system_running_config_管道里的写动作和其它文件仍然拒(self):
        for cmd in ("more system:running-config | redirect flash:a", "more system:running-config | tee flash:a",
                    "more flash:config.txt | include x", "more nvram:startup-config | section router"):
            with self.subTest(cmd=cmd):
                self.assertFalse(self.cisco.check(cmd).allowed, cmd)
