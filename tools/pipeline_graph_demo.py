"""同一条告警管道的两种写法：顺序代码 vs LangGraph 的图。**这是对照用的样例，不在生产路径上。**

生产路径是 `netops_ai/api/pipeline.py`：取证 → 研判 → 校验 → 落盘，顺着往下写，分支用 `if`。
这里把同一套流程用 `StateGraph` 画一遍，节点是假的（不调模型、不登设备），
好让人看清楚换成图之后**多出来的到底是什么**：

1. **状态是显式的**：`PipelineState` 一眼看完这条流程有哪些数据在流动；
   顺序代码里这些是散在函数里的七八个局部变量。
2. **分支是边，不是代码**：`_enough_evidence()` 返回走哪条边，重试策略是数据不是逻辑。
3. **断点续跑**：checkpointer 存状态，进程重启能接着跑——
   现在后端一重启，攒批窗口里没处理完的告警就真丢了。
4. **人工打断**：`interrupt()` 能把流程停在某个节点等人点头（SOP 审批那条线要用）。

跑一下看：

    .venv/bin/python -m tools.pipeline_graph_demo          # 正常跑一遍，打印每步
    .venv/bin/python -m tools.pipeline_graph_demo --resume # 中途断电，从 checkpoint 接着跑
"""

from __future__ import annotations

import argparse
import sys
from typing import Annotated, Literal, TypedDict

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph


def _append(left: list, right: list) -> list:
    """两个节点都往同一个字段写时怎么合并。顺序代码里这件事是 `list.append`，图里要显式说明。"""
    return [*left, *right]


class PipelineState(TypedDict, total=False):
    """一条告警在流程里的全部状态。**顺序代码里这些是局部变量，图里必须写出来。**"""

    eventid: str
    zabbix_text: str
    evidence: str          # ① 取证拿到的设备/监控证据
    missing: str           # 模型自己说还缺什么
    probes: int            # 补查过几轮
    analysis: dict         # ② 研判的结论
    violations: list       # 校验抓到的自相矛盾
    steps: Annotated[list, _append]   # 走过哪些节点，给演示看的


# ---- 节点：都是普通函数，签名一律 (state) -> 要更新的字段 ----------------------
# **节点里不许出现 LangGraph 的类型**。保持这条，哪天不用图了，节点原样搬走。

def fetch_evidence(state: PipelineState) -> dict:
    """① 取证。生产里这里起 agent 循环（`run_agent_loop`），模型自己决定查什么。"""
    probes = state.get("probes", 0)
    if probes == 0:
        return {"evidence": "接口 Et0/1 down；syslog 有 LINK-5-CHANGED",
                "missing": "对端 A1 那一侧的状态", "probes": 1, "steps": ["取证"]}
    # 第二轮：按模型自己说缺的东西补查（A15 之后可以去邻居设备上查）
    return {"evidence": state["evidence"] + "；对端 A1 Et0/1 也是 down",
            "missing": "", "probes": probes + 1, "steps": ["补查"]}


def judge(state: PipelineState) -> dict:
    """② 研判。生产里是一次 strict schema 调用，不给工具。"""
    return {"analysis": {"root_cause": "链路两端都被人为 shutdown", "confidence": "high"},
            "steps": ["研判"]}


def check(state: PipelineState) -> dict:
    """校验：引文逐字核 + 业务规则。纯代码，模型不参与。"""
    return {"violations": [], "steps": ["校验"]}


def persist(state: PipelineState) -> dict:
    """落盘 + 发卡。"""
    return {"steps": ["落盘发卡"]}


def record_violations(state: PipelineState) -> dict:
    """有矛盾就原样记下来端到界面上——**不让模型重来**，跟生产里的行为一致。"""
    return {"steps": ["记下矛盾"]}


# ---- 边：分支判断单独写成函数，返回走哪条 ------------------------------------

MAX_PROBES = 2


def _enough_evidence(state: PipelineState) -> Literal["judge", "fetch_evidence"]:
    """证据够不够。**这就是「判不出就按它自己说缺什么再查一轮」那条待办**——
    在图里它是一条边，改策略只改这个函数，不用动节点。
    """
    if state.get("missing") and state.get("probes", 0) < MAX_PROBES:
        return "fetch_evidence"
    return "judge"


def _has_violations(state: PipelineState) -> Literal["record_violations", "persist"]:
    return "record_violations" if state.get("violations") else "persist"


def build_graph(checkpointer=None):
    g = StateGraph(PipelineState)
    for name, fn in (("fetch_evidence", fetch_evidence), ("judge", judge), ("check", check),
                     ("persist", persist), ("record_violations", record_violations)):
        g.add_node(name, fn)

    g.add_edge(START, "fetch_evidence")
    g.add_conditional_edges("fetch_evidence", _enough_evidence)   # 够了往前，不够回自己
    g.add_edge("judge", "check")
    g.add_conditional_edges("check", _has_violations)
    g.add_edge("record_violations", "persist")
    g.add_edge("persist", END)
    return g.compile(checkpointer=checkpointer)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--resume", action="store_true", help="演示断点续跑：跑到一半模拟进程挂掉，再接着跑")
    args = ap.parse_args(argv)

    start: PipelineState = {"eventid": "93555", "zabbix_text": "Interface Et0/1 Link down"}

    if not args.resume:
        final = build_graph().invoke(start)
        print("走过的节点：", " → ".join(final["steps"]))
        print("结论：", final["analysis"])
        print("\n注意「补查」那一步：模型说还缺对端状态，边把它送回了取证节点。")
        return 0

    # 断点续跑：同一个 thread_id 就是同一条告警的现场
    saver = MemorySaver()
    graph = build_graph(checkpointer=saver)
    cfg = {"configurable": {"thread_id": "alert-93555"}}
    for chunk in graph.stream(start, cfg, stream_mode="updates"):
        node = next(iter(chunk))
        print("  跑完节点：", node)
        if node == "judge":
            print("  —— 假装这里进程被杀了 ——")
            break
    print("状态还在 checkpoint 里：", saver.get(cfg)["channel_values"].get("steps"))
    final = graph.invoke(None, cfg)   # 传 None = 从上次的地方接着跑
    print("接着跑完：", " → ".join(final["steps"]))
    print("\n这就是换成图最实在的那个好处：后端重启，没处理完的告警接得上。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
