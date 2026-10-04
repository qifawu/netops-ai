from __future__ import annotations

from pathlib import Path
from unittest import mock

import yaml

from netops_ai.playbooks.lint import has_errors, lint_paths, lint_playbook


REPO_ROOT = Path(__file__).resolve().parents[1]


def _codes(playbook: dict) -> set[str]:
    return {issue.code for issue in lint_playbook(playbook)}


def _good_playbook(**overrides):
    data = {
        "name": "good",
        "description": "good",
        "applicability": "Cisco test SOP",
        "limits": {"max_main_steps": 5, "max_tokens": 1500},
        "match": {"vendor": "cisco", "trigger_name_contains": ["Test"]},
        "start": "s1",
        "steps": [
            {
                "id": "s1",
                "why": "collect evidence",
                "expect": "show version output",
                "main": True,
                "action": {"tool": "device", "command": "show version"},
                "branches": [{"when": "default", "goto": "__ai__"}],
                "provenance": {"source": "human", "date": "2026-09-25"},
            }
        ],
    }
    data.update(overrides)
    return data


def test_formal_playbooks_have_no_lint_errors() -> None:
    issues = lint_paths([REPO_ROOT / "playbooks"])
    assert not has_errors(issues), "\n".join(f"{i.location} {i.code}: {i.message}" for i in issues)


def test_lint_reports_bad_start_goto_cycle_no_exit_unknown_when_empty_match() -> None:
    playbook = _good_playbook(
        match={},
        start="missing",
        steps=[
            {
                "id": "s1",
                "why": "x",
                "expect": "x",
                "action": {"tool": "device", "command": "show version"},
                "branches": [{"when": "made_up()", "goto": "missing_step"}],
                "provenance": {"source": "human"},
            },
            {
                "id": "s2",
                "why": "x",
                "expect": "x",
                "action": {"tool": "device", "command": "show clock"},
                "branches": [{"when": "default", "goto": "s1"}],
                "provenance": {"source": "human"},
            },
        ],
    )
    codes = _codes(playbook)
    assert {"bad-start", "bad-goto", "unknown-when", "empty-match"}.issubset(codes)

    cycle_playbook = _good_playbook(
        steps=[
            {
                "id": "s1",
                "why": "x",
                "expect": "x",
                "action": {"tool": "device", "command": "show version"},
                "branches": [{"when": "default", "goto": "s2"}],
                "provenance": {"source": "human"},
            },
            {
                "id": "s2",
                "why": "x",
                "expect": "x",
                "action": {"tool": "device", "command": "show clock"},
                "branches": [{"when": "default", "goto": "s1"}],
                "provenance": {"source": "human"},
            },
        ]
    )
    assert {"cycle", "no-exit"}.issubset(_codes(cycle_playbook))


def test_lint_reports_whitelist_and_template_errors() -> None:
    playbook = _good_playbook(
        steps=[
            {
                "id": "bad_command",
                "why": "x",
                "expect": "x",
                "action": {"tool": "device", "command": "reload in 5"},
                "branches": [{"when": "default", "goto": "__ai__"}],
                "provenance": {"source": "human"},
            },
            {
                "id": "bad_template",
                "why": "x",
                "expect": "x",
                "action": {"tool": "device", "command": "show interface {unknown_field}"},
                "branches": [{"when": "default", "goto": "__ai__"}],
                "provenance": {"source": "human"},
            },
        ]
    )
    assert {"whitelist-denied", "unrendered-template"}.issubset(_codes(playbook))


def test_lint_reports_main_step_and_token_limits() -> None:
    steps = []
    for index in range(6):
        steps.append(
            {
                "id": f"s{index}",
                "why": "x",
                "expect": "x",
                "main": True,
                "action": {"tool": "device", "command": "show version"},
                "branches": [{"when": "default", "goto": "__ai__"}],
                "provenance": {"source": "human"},
            }
        )
    playbook = _good_playbook(limits={"max_main_steps": 5, "max_tokens": 1}, steps=steps)
    assert {"too-many-main-steps", "too-many-tokens", "duplicate-command"}.issubset(_codes(playbook))


def test_cli_path_scan_skips_archives_by_default(tmp_path: Path) -> None:
    root = tmp_path / "playbooks"
    archive = root / "_proposed"
    archive.mkdir(parents=True)
    (root / "good.yaml").write_text(yaml.safe_dump(_good_playbook(), sort_keys=False), encoding="utf-8")
    (archive / "bad.yaml").write_text(yaml.safe_dump(_good_playbook(match={}), sort_keys=False), encoding="utf-8")

    assert not has_errors(lint_paths([root]))
    assert has_errors(lint_paths([root], include_archive=True))


def test_fault_type_wildcard_requires_generic_true() -> None:
    playbook = _good_playbook(
        match={"fault_types": ["*"]},
        steps=[
            {
                "id": "s1",
                "why": "x",
                "expect": "x",
                "intent": "device_uptime",
                "branches": [{"when": "default", "goto": "__ai__"}],
                "provenance": {"source": "human"},
            }
        ],
    )

    assert "wildcard-requires-generic" in _codes(playbook)


def _action_step(action: dict, **extra) -> dict:
    step = {
        "id": "s1",
        "why": "x",
        "expect": "x",
        "action": action,
        "branches": [{"when": "default", "goto": "__ai__"}],
        "provenance": {"source": "human"},
    }
    step.update(extra)
    return step


def test_w35_old_local_shutdown_step_is_rejected() -> None:
    """外部 agent 反馈第 1 条：`tool=zbx_history; command=syslog`。zbx_history 要 item_id，没有 key_contains。"""
    codes = _codes(_good_playbook(steps=[_action_step({"tool": "zabbix_history", "key_contains": "syslog"})]))
    assert {"bad-action-param", "missing-action-param"}.issubset(codes)


def test_unknown_action_tool_is_rejected() -> None:
    assert "unknown-action-tool" in _codes(_good_playbook(steps=[_action_step({"tool": "config_diff"})]))


def test_real_agent_tool_names_and_params_pass() -> None:
    steps = [
        _action_step({"tool": "zbx_syslog", "since": "{alert_window_from}", "until": "{alert_window_to}"}),
    ]
    codes = _codes(_good_playbook(steps=steps))
    assert not codes & {"unknown-action-tool", "bad-action-param", "missing-action-param", "unrendered-template"}


def test_unknown_template_in_non_device_param_is_rejected() -> None:
    codes = _codes(_good_playbook(steps=[_action_step({"tool": "zbx_syslog", "since": "{nope}"})]))
    assert "unrendered-template" in codes


def test_unknown_intent_is_rejected() -> None:
    playbook = _good_playbook(
        match={"fault_types": ["interface_down"]},
        steps=[
            {
                "id": "s1",
                "why": "x",
                "expect": "x",
                "intent": "made_up_intent",
                "branches": [{"when": "default", "goto": "__ai__"}],
                "provenance": {"source": "human"},
            }
        ],
    )
    assert "unknown-intent" in _codes(playbook)


def test_template_in_why_is_rejected() -> None:
    """模板变量只在 action 里渲染；前 flap_history 的 why 把 `{alert_interface_short}` 原样摆给了模型。"""
    step = _action_step({"tool": "device", "command": "show version"}, why="过滤 {alert_interface_short} 的行")
    assert "template-in-prose" in _codes(_good_playbook(steps=[step]))


# ---- 验证 改动后 外部 agent 指出的遗留问题 ----


def test_cycle_outside_start_path_is_rejected() -> None:
    """原来只从 start 找环，走不到的那部分里有环不报。"""
    steps = [
        _action_step({"tool": "device", "command": "show version"}, id="s1"),
        _action_step({"tool": "device", "command": "show clock"}, id="a",
                     branches=[{"when": "output_contains('x')", "goto": "b"}, {"when": "default", "goto": "__ai__"}]),
        _action_step({"tool": "device", "command": "show users"}, id="b",
                     branches=[{"when": "default", "goto": "a"}]),
    ]
    codes = _codes(_good_playbook(steps=steps))
    assert "cycle" in codes
    assert "unreachable-step" in codes


def test_int_reading_of_rendered_number_must_pass_tool_schema() -> None:
    """验证 外部 agent 反馈第 2 条：计划里是 `since=1790395561`，模型照字面传整数，schema 只收字符串就 ValidationError。
 lint 要按「字符串」和「整数」两种读法都过一遍工具的参数校验。
    """
    from pydantic import create_model

    from netops_ai.playbooks import lint

    string_only = create_model("zbx_syslog_args", host=(str, ...), since=(str, "-30m"), until=(str, ""))
    with mock.patch.object(lint, "_agent_tool_schemas", return_value={"zbx_syslog": string_only}):
        issues = lint._param_type_issues("zbx_syslog", {"tool": "zbx_syslog", "since": "1790395561"}, "p", "s1")
    assert [i.code for i in issues] == ["bad-action-param-type"]
    assert "1790395561" in issues[0].message

    # 真实注册的 zbx_syslog：整数、数字字符串都收
    assert lint._param_type_issues("zbx_syslog", {"since": "1790395561", "until": "1790396761"}, "p", "s1") == []


def test_steps_runtime_cannot_tell_apart_are_warned() -> None:
    """运行时靠工具名+命令认步骤。一步没有可比的参数时，同工具的任何调用都会被认成它（验证的回环就是这么来的）。"""
    steps = [
        _action_step({"tool": "topology_neighbors", "interface": "{alert_interface}"}, id="s1"),
        _action_step({"tool": "topology_neighbors"}, id="s2"),
    ]
    assert "ambiguous-step-match" in _codes(_good_playbook(steps=steps))


def test_interface_link_down_log_buffer_has_explicit_exit() -> None:
    """验证 外部 agent 反馈第 1 条：local_log_buffer 找到 CONFIG_I 就走完，不回 local_shutdown_evidence。"""
    data = yaml.safe_load((REPO_ROOT / "playbooks" / "interface-link-down.yaml").read_text(encoding="utf-8"))
    step = next(s for s in data["steps"] if s["id"] == "local_log_buffer")
    gotos = {b["when"]: b["goto"] for b in step["branches"]}
    assert gotos["output_contains('%SYS-5-CONFIG_I')"] == "__end__"
    assert set(gotos.values()) <= {"__end__", "__ai__"}
    # 截断是事实陈述，不是命令式建议
    assert "8000" in step["why"] and "| include" in step["why"]


# ── SOP 是方向性文件，不写死实例值 ──

def _literal_messages(playbook: dict) -> list[str]:
    return [issue.message for issue in lint_playbook(playbook) if issue.code == "instance-literal"]


def test_w36c_old_bgp_and_ospf_steps_are_rejected() -> None:
    """前 bgp-session / ospf-adjacency 的原样步骤，每个写死的值都要报。"""
    steps = [
        _action_step({"tool": "device", "command": "show ip route 192.0.2.51"}, id="s1",
                     expect="看到到 peer 192.0.2.51 的路由是否存在",
                     branches=[{"when": "default", "goto": "s2"}]),
        _action_step({"tool": "device", "command": "show ip ospf interface GigabitEthernet0/1"}, id="s2",
                     branches=[{"when": "default", "goto": "s3"}]),
        _action_step({"tool": "topology_neighbors", "device": "V1", "interface": "GigabitEthernet0/1"}, id="s3"),
    ]
    messages = _literal_messages(_good_playbook(steps=steps))
    joined = "\n".join(messages)
    assert "action.command hard-codes IPv4 address '192.0.2.51'" in joined
    assert "expect hard-codes IPv4 address '192.0.2.51'" in joined
    assert "action.command hard-codes interface 'GigabitEthernet0/1'" in joined
    assert "action.device hard-codes topology device 'V1'" in joined
    assert "action.interface hard-codes interface 'GigabitEthernet0/1'" in joined
    assert len(messages) == 5


def test_instance_literal_catches_short_names_aliases_and_branch_conditions() -> None:
    steps = [
        _action_step(
            {"tool": "device", "command": "show interfaces status err-disabled"},
            why="看 Gi0/1、Et0/2、Eth1/1、Fa0/1、Vlan11 和 V2-vios 的状态，对端是 D1。",
            branches=[{"when": "output_contains('10.0.0.2')", "goto": "__end__"}, {"when": "default", "goto": "__ai__"}],
        )
    ]
    joined = "\n".join(_literal_messages(_good_playbook(steps=steps)))
    for literal in ("'Gi0/1'", "'Et0/2'", "'Eth1/1'", "'Fa0/1'", "'Vlan11'", "'V2-vios'", "'D1'"):
        assert literal in joined, literal
    assert "branches.when hard-codes IPv4 address '10.0.0.2'" in joined


def test_instance_literal_does_not_flag_keywords_timestamps_log_tags_or_protocol_constants() -> None:
    """不误伤：命令关键字、Unix 秒、日志助记符、协议常量、模板变量和 agent 填的占位符。"""
    steps = [
        _action_step(
            {"tool": "device", "command": "show logging | include CONFIG_I|{alert_interface}"}, id="s1",
            why=(
                "show ip route / show ip ospf interface / show ip bgp neighbors 都是通用命令；"
                "%LINK-5-CHANGED、%LINEPROTO-5-UPDOWN、%LINK-3-UPDOWN、%SYS-5-CONFIG_I 是日志助记符；"
                "OSPF hello 发往 224.0.0.5 / 224.0.0.6，默认路由 0.0.0.0，掩码 255.255.255.252、反掩码 0.0.0.3；"
                "IOS 15.9，单次返回上限 8000 字符，zabbix[host,snmp,available]，lastvalue 1=通、0=不通，"
                "命令里的 <接口> / <对端地址> 由 agent 填。"
            ),
            expect="窗口 1790391000~1790396761 内的日志",
            branches=[{"when": "default", "goto": "s2"}],
        ),
        _action_step({"tool": "zbx_syslog", "since": "{alert_window_from}", "until": 1790396761}, id="s2",
                     branches=[{"when": "default", "goto": "s3"}]),
        _action_step({"tool": "device", "command": "show ip route <对端地址>"}, id="s3",
                     branches=[{"when": "output_contains('not in table')", "goto": "__end__"},
                               {"when": "default", "goto": "s4"}]),
        _action_step({"tool": "topology_neighbors", "interface": "{alert_interface}"}, id="s4"),
    ]
    playbook = _good_playbook(steps=steps)
    assert _literal_messages(playbook) == []
    assert not has_errors(lint_playbook(playbook))


def test_peer_address_placeholder_is_whitelist_checked_with_a_representative_address() -> None:
    ok = _good_playbook(steps=[_action_step({"tool": "device", "command": "show ip route <对端地址>"})])
    assert "whitelist-denied" not in _codes(ok)
    bad = _good_playbook(steps=[_action_step({"tool": "device", "command": "ping <对端地址>"})])
    assert "whitelist-denied" in _codes(bad)


def test_live_sops_have_no_instance_literal() -> None:
    issues = lint_paths([REPO_ROOT / "playbooks"])
    assert [i for i in issues if i.code == "instance-literal"] == []
    assert not has_errors(issues)


def test_value_branch_on_tool_without_value_is_rejected() -> None:
    """`value ==` 只有 engine.BRANCH_VALUE_FIELDS 里的工具才取得到值，其它工具写了永远不触发。"""
    value_branches = [{"when": "value == '0'", "goto": "__end__"}, {"when": "default", "goto": "__ai__"}]
    on_device = _good_playbook(steps=[_action_step({"tool": "device", "command": "show version"}, branches=value_branches)])
    assert "value-branch-unsupported" in _codes(on_device)
    on_items = _good_playbook(steps=[_action_step({"tool": "zbx_items", "key": "icmpping"}, branches=value_branches)])
    assert "value-branch-unsupported" not in _codes(on_items)
