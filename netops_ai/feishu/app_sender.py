"""用应用身份（tenant access token + `im/v1/messages`）把卡片发到指定群。

跟 webhook 那条（`sender.py`）接口一样：`(卡片) -> (是否成功, 出错信息)`，不抛异常。
`FEISHU_APP_SECRET` 不进仓库，但要存在本机固定的仓库外位置。
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

TENANT_TOKEN_URL = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
SEND_MESSAGE_URL = "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id"

#: 进程内的 token 缓存：`app_id -> (token, 过期时间戳)`。
#: tenant access token 官方有效期 7200 秒，频繁重新换没有意义，也会白白
#: 增加一次外部调用的失败面。**提前 300 秒过期**，避免"拿到时还有 2 秒有效"
#: 这种边界情况。进程重启就没了，这是有意的——不落盘，省得多一个装着凭据
#: 的文件要管。
_TOKEN_CACHE: dict[str, tuple[str, float]] = {}

_TOKEN_EARLY_EXPIRE_SECONDS = 300


def _post_json(url: str, payload: dict, headers: dict, timeout: float) -> tuple[dict | None, str]:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        # 飞书的业务错误（权限不足、chat_id 不存在）经常是 HTTP 400 带一个
        # JSON body，body 里的 code/msg 比 HTTP 状态码有用得多，要读出来
        detail = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
        return None, f"HTTP {exc.code}: {detail[:300]}"
    except urllib.error.URLError as exc:
        return None, f"{type(exc).__name__}: {exc}"

    try:
        return json.loads(body), ""
    except json.JSONDecodeError:
        return None, f"响应不是合法 JSON：{body[:200]!r}"


def get_tenant_access_token(
    app_id: str, app_secret: str, *, timeout: float = 10.0, force_refresh: bool = False
) -> tuple[str, str]:
    """返回 `(token, 出错信息)`。命中缓存时不发请求。"""
    if not app_id or not app_secret:
        return "", "FEISHU_APP_ID / FEISHU_APP_SECRET 没配全"

    if not force_refresh:
        cached = _TOKEN_CACHE.get(app_id)
        if cached and cached[1] > time.time():
            return cached[0], ""

    parsed, err = _post_json(
        TENANT_TOKEN_URL,
        {"app_id": app_id, "app_secret": app_secret},
        {"Content-Type": "application/json; charset=utf-8"},
        timeout,
    )
    if parsed is None:
        return "", f"换 tenant_access_token 失败：{err}"
    if parsed.get("code") != 0:
        # 这里**不要**把 app_secret 拼进错误信息——错误信息会落进
        # `records/alert-*.json`，那个目录不是放凭据的地方
        return "", f"换 tenant_access_token 被飞书拒绝：code={parsed.get('code')} msg={parsed.get('msg')!r}"

    token = parsed.get("tenant_access_token", "")
    if not token:
        return "", f"飞书返回里没有 tenant_access_token：{parsed}"

    expire = parsed.get("expire", 7200)
    _TOKEN_CACHE[app_id] = (token, time.time() + max(expire - _TOKEN_EARLY_EXPIRE_SECONDS, 60))
    return token, ""


def send_card_as_app(
    app_id: str, app_secret: str, chat_id: str, card: dict, *, timeout: float = 10.0
) -> tuple[bool, str]:
    """返回 `(是否成功, 出错信息)`。token 过期时强刷一次重试：进程跑得久，两次告警之间过期很正常。"""
    if not chat_id:
        return False, "FEISHU_CHAT_ID 没配，不知道该发到哪个群"

    for attempt in (1, 2):
        token, err = get_tenant_access_token(
            app_id, app_secret, timeout=timeout, force_refresh=(attempt == 2)
        )
        if not token:
            return False, err

        parsed, err = _post_json(
            SEND_MESSAGE_URL,
            {
                "receive_id": chat_id,
                "msg_type": "interactive",
                # 飞书这个接口要求 content 是**一个 JSON 字符串**，
                # 不是嵌套的 JSON 对象——传对象会被判成参数错误
                "content": json.dumps(card, ensure_ascii=False),
            },
            {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json; charset=utf-8",
            },
            timeout,
        )
        if parsed is None:
            return False, err
        if parsed.get("code") == 0:
            return True, ""

        code = parsed.get("code")
        if attempt == 1 and code in (99991663, 99991668, 99991661):
            # token 失效/过期，强刷一次再来
            continue
        return False, f"飞书返回失败：code={code} msg={parsed.get('msg')!r}"

    return False, "换了新 token 之后仍然发送失败"
