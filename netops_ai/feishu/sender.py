"""把卡片 POST 到飞书群机器人 webhook。纯标准库；失败返回 `(False, 错误)`，不抛。"""

from __future__ import annotations

import json
import urllib.error
import urllib.request


def send_card(webhook_url: str, card: dict, *, timeout: float = 10.0) -> tuple[bool, str]:
    """返回 `(是否成功, 出错信息)`。`webhook_url` 为空或明显是占位值时直接
    拒绝发送，不真的发出去——占位值判断很宽松（只要不是 `http` 开头就算），
    调用方（`report.py`）在真发之前也会自己做一次更明确的检查，这里是
    最后一道兜底，不是唯一的判断点。
    """
    if not webhook_url or not webhook_url.startswith("http"):
        return False, f"webhook_url 看起来不是一个真实地址，拒绝发送：{webhook_url!r}"

    payload = {"msg_type": "interactive", "card": card}
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        webhook_url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
    except urllib.error.URLError as exc:
        return False, f"{type(exc).__name__}: {exc}"

    try:
        parsed = json.loads(body)
    except json.JSONDecodeError:
        return False, f"响应不是合法 JSON：{body[:200]!r}"

    # 飞书自定义机器人成功时返回 {"code": 0, ...} / {"StatusCode": 0, ...}
    # （两种历史格式都出现过），失败时 code 非 0 且带 msg
    code = parsed.get("code", parsed.get("StatusCode"))
    if code not in (0, None):
        return False, f"飞书返回失败：{parsed}"
    return True, ""
