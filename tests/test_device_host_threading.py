"""`device_host` 必须是一路传下去的参数，不能是进程级全局状态。

**这组测试锁的是一个具体的设计决定，不只是一个函数的行为。**

以前告警管道是这么告诉设备工具"这次连哪台"的：临时改进程级环境变量
`DEVICE_HOST`，跑完再改回去。既然是全局可变状态，两条并发的告警就会互相
踩——A 把它改成 V1，B 紧接着改成 V2，A 的 SSH 就连到 V2 上去了。所以当时
必须拿一把大锁，把**整段 AI 探查循环**（多跳 LLM + SSH，几十秒到几分钟）
串起来。锁本身没写错，是那个设计逼出来的，代价是**所有告警的分析完全串行**。

改成一路传参之后锁就删掉了。下面这两组分别证明：

1. `test_no_global_mutation` —— 不再改环境变量（回归保护，防止哪天为了
   图省事又改回去）
2. `TestConcurrentHostsDoNotInterfere` —— 两个线程各自带着不同的
   `device_host` 并发跑，各自连到各自那台，一次都不串
"""

from __future__ import annotations

import os
import threading
import unittest
from unittest import mock

from netops_ai.graph.agent_loop import AgentLoopBudget, ChatRunTrace
from netops_ai.graph.chat_agent import _device_adapter, build_chat_tools, build_device_tools


def _trace() -> ChatRunTrace:
    return ChatRunTrace(trace_id="t", question="q", session_id="s", budget={}, plan="")


class TestDeviceHostIsAParameter(unittest.TestCase):
    def test_explicit_host_wins_over_env(self):
        with mock.patch.dict(os.environ, {"DEVICE_HOST": "10.0.0.1", "DEVICE_TRANSPORT": "ssh"}):
            adapter = _device_adapter("192.0.2.51")
        self.assertEqual(adapter.host, "192.0.2.51")

    def test_falls_back_to_env_when_not_given(self):
        """对话入口不传 host，行为必须和以前一样。"""
        with mock.patch.dict(os.environ, {"DEVICE_HOST": "10.0.0.1", "DEVICE_TRANSPORT": "ssh"}):
            adapter = _device_adapter()
        self.assertEqual(adapter.host, "10.0.0.1")

    def test_no_global_mutation(self):
        """构造适配器不许动 `os.environ`。

        这条是回归保护：一旦有人为了图省事又改回"临时改环境变量"，
        这里立刻红。
        """
        with mock.patch.dict(os.environ, {"DEVICE_HOST": "10.0.0.1", "DEVICE_TRANSPORT": "ssh"}):
            before = dict(os.environ)
            _device_adapter("192.0.2.51")
            self.assertEqual(dict(os.environ), before)

    def test_build_chat_tools_threads_host_through(self):
        """`functools.partial(build_chat_tools, device_host=...)` 这条路要通到底。"""
        from functools import partial

        factory = partial(build_chat_tools, device_host="192.0.2.51")
        with mock.patch.dict(os.environ, {"DEVICE_HOST": "10.0.0.1", "DEVICE_TRANSPORT": "ssh"}):
            tools = factory(_trace(), AgentLoopBudget())
        names = {t.name for t in tools}
        self.assertIn("device_show", names)
        self.assertIn("device_show_many", names)


class TestConcurrentHostsDoNotInterfere(unittest.TestCase):
    """两台设备的告警并发分析时，各连各的，一次都不许串。

    这是删掉那把全局锁之后真正要证明的事情。用一个假适配器把"这次连到了
    哪个 host"记下来，两个线程各跑若干轮并互相错开，最后核对每个线程看到的
    host 是不是自始至终都只有它自己那一个。
    """

    #: 记录「哪个线程连到了哪个 host」，一把锁护着。
    #:
    #: **注意这里为什么不在每个线程里各自 `mock.patch`**：`mock.patch` 替换的
    #: 是模块的全局属性，两个线程的 patch 会互相覆盖——测试脚手架本身就会犯它
    #: 要测的那个毛病，测出来的红是假红。第一版就是这么写的，真红了一次。
    #: 正确做法是**在主线程里只 patch 一次**，用一个线程安全的记录器收集结果。
    def _run_one(self, host: str, barrier: threading.Barrier, rounds: int = 12):
        trace = _trace()
        tools = {t.name: t for t in build_device_tools(trace, device_host=host)}
        show = tools["device_show"]
        for _ in range(rounds):
            barrier.wait()  # 每轮都在同一点集合，最大化两个线程交错的机会
            show.func(command="show version")

    def test_two_hosts_in_parallel(self):
        seen: dict[str, set[str]] = {}
        lock = threading.Lock()

        class _R:
            command = "show version"
            allowed = True
            ok = True
            output = ""
            error = ""
            denial_reason = ""

        class _Fake:
            def __init__(self, h: str):
                self.host = h

            def __enter__(self):
                with lock:
                    seen.setdefault(threading.current_thread().name, set()).add(self.host)
                return self

            def __exit__(self, *a):
                return False

            def run(self, command):
                return _R()

        barrier = threading.Barrier(2)
        env = {"DEVICE_HOST": "0.0.0.0", "DEVICE_TRANSPORT": "ssh"}
        with mock.patch.dict(os.environ, env), mock.patch(
            "netops_ai.graph.chat_agent._device_adapter", side_effect=lambda h="": _Fake(h)
        ):
            ta = threading.Thread(target=self._run_one, args=("192.0.2.50", barrier), name="A")
            tb = threading.Thread(target=self._run_one, args=("192.0.2.51", barrier), name="B")
            ta.start()
            tb.start()
            ta.join(timeout=30)
            tb.join(timeout=30)
            self.assertFalse(ta.is_alive() or tb.is_alive(), "线程没退出")

        # 核心断言：每个线程自始至终只连过它自己那台，一次都没串
        self.assertEqual(seen.get("A"), {"192.0.2.50"})
        self.assertEqual(seen.get("B"), {"192.0.2.51"})

    def test_env_fallback_is_what_would_have_collided(self):
        """反证：如果 host 还是从全局环境变量取，这个场景就会串。

        不传 `device_host` 时两个线程读的是同一个 `os.environ['DEVICE_HOST']`
        ——**这正是改造之前的行为**，也正是当年必须加那把大锁的原因。
        """
        with mock.patch.dict(os.environ, {"DEVICE_HOST": "10.0.0.9", "DEVICE_TRANSPORT": "ssh"}):
            a = _device_adapter()
            b = _device_adapter()
        self.assertEqual(a.host, b.host, "不传参时两者必然相同——这就是会互相踩的根源")


if __name__ == "__main__":
    unittest.main()
