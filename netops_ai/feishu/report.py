"""分析结果 → 卡片 → 发送。按 `.env` 挑通道，webhook 优先（不用把 app secret 放这台机器）；都没配就跳过。"""

from __future__ import annotations

from .app_sender import send_card_as_app
from .card import build_card, build_receipt_card
from .sender import send_card

#: 飞书官方机器人 webhook 的真实前缀。用这个判断".env 里是不是随手留的
#: 空值/占位值"，比"随便判断是不是以 http 开头"更精确——占位值常见写法
#: 像 `https://your-webhook-url` 也是以 http 开头的，不能只靠这个判断。
_REAL_WEBHOOK_PREFIX = "https://open.feishu.cn/open-apis/bot/v2/hook/"

#: 飞书应用 id 的固定前缀，同样用来把占位值挡在外面
_REAL_APP_ID_PREFIX = "cli_"

#: 群 id 的固定前缀
_REAL_CHAT_ID_PREFIX = "oc_"


def is_configured_webhook(url: str) -> bool:
    return bool(url) and url.startswith(_REAL_WEBHOOK_PREFIX)


def is_configured_app(app_id: str, app_secret: str, chat_id: str) -> bool:
    """三个都得有，且 id 的前缀要对得上——只配一半（比如有 app_id 没
    secret）按"没配"处理，不要半路去发一个注定失败的请求。
    """
    return (
        bool(app_id)
        and app_id.startswith(_REAL_APP_ID_PREFIX)
        and bool(app_secret)
        and bool(chat_id)
        and chat_id.startswith(_REAL_CHAT_ID_PREFIX)
    )


def report_single_alert(
    eventid: str,
    analysis: dict | None,
    webhook_url: str = "",
    *,
    app_id: str = "",
    app_secret: str = "",
    chat_id: str = "",
) -> dict:
    """单条告警的上报，不抛异常。

    返回 `{attempted, sent, error, transport}`；`transport` 记进记录，两条通道的失败原因完全不同。
    """
    if analysis is None:
        return {"attempted": False, "sent": False, "error": "没有分析结果，跳过上报", "transport": ""}

    card = build_card({"eventids": [eventid], "analysis": analysis})

    if is_configured_webhook(webhook_url):
        ok, err = send_card(webhook_url, card)
        return {"attempted": True, "sent": ok, "error": err, "transport": "webhook"}

    if is_configured_app(app_id, app_secret, chat_id):
        ok, err = send_card_as_app(app_id, app_secret, chat_id, card)
        return {"attempted": True, "sent": ok, "error": err, "transport": "app"}

    return {
        "attempted": False,
        "sent": False,
        "error": (
            "飞书两条通道都没配，跳过发送："
            f"FEISHU_WEBHOOK_URL={webhook_url!r}；"
            f"FEISHU_APP_ID={app_id!r} / FEISHU_CHAT_ID={chat_id!r} / "
            f"FEISHU_APP_SECRET={'已配' if app_secret else '未配'}"
        ),
        "transport": "",
    }


def report_alert_receipt(
    eventid: str,
    name: str = "",
    webhook_url: str = "",
    *,
    app_id: str = "",
    app_secret: str = "",
    chat_id: str = "",
) -> dict:
    """收到告警后的轻量回执，不包含任何分析结论。"""
    card = build_receipt_card(eventid=eventid, name=name)

    if is_configured_webhook(webhook_url):
        ok, err = send_card(webhook_url, card)
        return {"attempted": True, "sent": ok, "error": err, "transport": "webhook"}

    if is_configured_app(app_id, app_secret, chat_id):
        ok, err = send_card_as_app(app_id, app_secret, chat_id, card)
        return {"attempted": True, "sent": ok, "error": err, "transport": "app"}

    return {
        "attempted": False,
        "sent": False,
        "error": (
            "飞书两条通道都没配，跳过发送："
            f"FEISHU_WEBHOOK_URL={webhook_url!r}；"
            f"FEISHU_APP_ID={app_id!r} / FEISHU_CHAT_ID={chat_id!r} / "
            f"FEISHU_APP_SECRET={'已配' if app_secret else '未配'}"
        ),
        "transport": "",
    }


def report_alert_group(
    group,
    webhook_url: str = "",
    *,
    app_id: str = "",
    app_secret: str = "",
    chat_id: str = "",
) -> dict:
    """上报一组已收敛的告警。返回结构跟 `report_single_alert()` 一致。"""
    analysis = getattr(group, "analysis", None) if not isinstance(group, dict) else group.get("analysis")
    if analysis is None:
        return {"attempted": False, "sent": False, "error": "没有分析结果，跳过上报", "transport": ""}

    card = build_card(group)

    if is_configured_webhook(webhook_url):
        ok, err = send_card(webhook_url, card)
        return {"attempted": True, "sent": ok, "error": err, "transport": "webhook"}

    if is_configured_app(app_id, app_secret, chat_id):
        ok, err = send_card_as_app(app_id, app_secret, chat_id, card)
        return {"attempted": True, "sent": ok, "error": err, "transport": "app"}

    return {
        "attempted": False,
        "sent": False,
        "error": (
            "飞书两条通道都没配，跳过发送："
            f"FEISHU_WEBHOOK_URL={webhook_url!r}；"
            f"FEISHU_APP_ID={app_id!r} / FEISHU_CHAT_ID={chat_id!r} / "
            f"FEISHU_APP_SECRET={'已配' if app_secret else '未配'}"
        ),
        "transport": "",
    }
