from __future__ import annotations

from pathlib import Path

from netops_ai.devices.whitelist import check
from netops_ai.playbooks.engine import load_playbooks
from netops_ai.playbooks.lookup import _step_for_agent


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_playbooks_do_not_contain_show_running_config() -> None:
    for path in (REPO_ROOT / "playbooks").glob("*.yaml"):  # 只查现役 SOP；_proposed/_approved_archive/_rejected 是历史记录，不改
        assert "show running-config" not in path.read_text(encoding="utf-8"), str(path)


def test_formal_playbook_device_commands_are_whitelisted_for_cisco() -> None:
    for playbook in load_playbooks(REPO_ROOT / "playbooks"):
        for step in playbook.get("steps") or []:
            rendered = _step_for_agent(
                step,
                alert_clock=None,
                alert_interface=("GigabitEthernet0/1", "Gi0/1"),
            )
            action = rendered.get("action") or {}
            if action.get("tool") != "device":
                continue
            command = action.get("command")
            if not command:
                continue
            verdict = check("cisco", command)
            assert verdict.allowed, f"{playbook.get('name')}:{step.get('id')} {command}: {verdict.reason}"


def test_interface_link_down_commands_render_from_alert_interface_and_are_whitelisted() -> None:
    playbook = next(pb for pb in load_playbooks(REPO_ROOT / "playbooks") if pb.get("name") == "interface-link-down")
    commands = []
    for step in playbook.get("steps") or []:
        rendered = _step_for_agent(
            step,
            alert_clock=None,
            alert_interface=("GigabitEthernet0/1", "Gi0/1"),
        )
        action = rendered.get("action") or {}
        if action.get("tool") not in ("device", "device_show") or not action.get("command"):
            continue
        command = action["command"]
        commands.append(command)
        assert "{" not in command
        verdict = check("cisco", command)
        assert verdict.allowed, f"{step.get('id')} {command}: {verdict.reason}"

    assert "show interfaces GigabitEthernet0/1" in commands


def test_formal_playbook_start_and_gotos_are_valid() -> None:
    terminal = {"__end__", "__ai__"}
    for playbook in load_playbooks(REPO_ROOT / "playbooks"):
        step_ids = {step.get("id") for step in playbook.get("steps") or []}
        assert playbook.get("start") in step_ids, playbook.get("name")
        for step in playbook.get("steps") or []:
            for branch in step.get("branches") or []:
                goto = branch.get("goto")
                assert goto in step_ids or goto in terminal, f"{playbook.get('name')}:{step.get('id')} -> {goto}"
