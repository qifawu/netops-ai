"""Engineer-editable diagnostic SOP playbooks."""

from .engine import find_matching_playbook, load_playbooks

__all__ = [
    "find_matching_playbook",
    "load_playbooks",
]
