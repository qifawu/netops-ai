"""分析结果上报飞书。两条通道：群机器人 webhook（`sender.py`）或应用身份（`app_sender.py`），
`report.py` 按 `.env` 自动挑，webhook 优先。卡片只写「建议人去做什么」。
"""

from .card import build_card, build_inspection_card, build_receipt_card
from .app_sender import get_tenant_access_token, send_card_as_app
from .report import is_configured_app, is_configured_webhook, report_alert_group, report_alert_receipt, report_single_alert
from .sender import send_card

__all__ = [
    "build_card",
    "build_inspection_card",
    "build_receipt_card",
    "get_tenant_access_token",
    "is_configured_app",
    "is_configured_webhook",
    "report_alert_group",
    "report_alert_receipt",
    "report_single_alert",
    "send_card",
    "send_card_as_app",
]
