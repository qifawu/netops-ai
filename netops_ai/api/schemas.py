"""webhook 收到的 Zabbix 事件负载。字段名照 Zabbix 宏对应关系 里 Media type 消息模板的配置。

"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ZabbixWebhookPayload(BaseModel):
    eventid: str
    name: str
    severity: str = Field(default="")
    clock: str = Field(default="")
    hostid: str = Field(default="")
    host: str = Field(default="")


class ChatRequest(BaseModel):
    """对话请求体。

    **走请求体而不是 URL query**：运维问的问题里常带主机名、接口名、IP，
    放在 query 里会落进 uvicorn access log、反向代理日志和浏览器历史。
    """

    q: str = Field(min_length=1, max_length=2000)
    host: str = Field(default="", max_length=200)
    session: str = Field(default="default", max_length=200)


class PlaybookLintRequest(BaseModel):
    yaml_text: str = Field(default="", max_length=400_000)


class PlaybookDraftRequest(BaseModel):
    yaml_text: str = Field(max_length=400_000)
    based_on: str | None = None
    note: str | None = Field(default=None, max_length=2000)
    replace_file: str | None = None  # 覆盖保存已有的待审草稿，而不是再生成一份
    rebase: bool = False  # 解决完合并冲突再存：基线快照换成现在的正式文件


class PlaybookFileRequest(BaseModel):
    file: str


class PlaybookReviewRequest(BaseModel):
    file: str
    replay_limit: int | None = Field(default=None, ge=1, le=100_000)  # 告警回放条数；缺省 300，上限 600


class PlaybookApproveRequest(BaseModel):
    file: str
    merge: bool = False  # 基线已变、三方合并无冲突时，确认「按合并结果批准」


class PlaybookRollbackRequest(BaseModel):
    file: str
    archive: str


class PlaybookRejectRequest(BaseModel):
    file: str
    reason: str = Field(default="", max_length=4000)


class TranslateRequest(BaseModel):
    """AI 生成内容的按需翻译。跟界面静态字典（`web/src/lib/i18n.ts` 的 `dict`）是两回事：
    这里翻的是模型自己写的中文（结论/理由等），不是前端写死的 UI 文案。"""

    text: str = Field(min_length=1, max_length=4000)
    lang: str = Field(pattern=r"^(zh|en)$")


class InspectionConfigRequest(BaseModel):
    """只收 detectors 和 scope 两块，多余字段直接 422。校验（类型、范围、未知键）在 `inspection/config.py`。
    **没有任何路径参数**：写哪个文件由后端固定，不由请求决定。"""

    model_config = ConfigDict(extra="forbid")
    detectors: dict | None = None
    scope: dict | None = None


class InspectionIgnoreRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    host: str = Field(min_length=1, max_length=300)
    item_key: str = Field(min_length=1, max_length=300)
    reason: str = Field(default="", max_length=200)


class PlaybookValidateRequest(BaseModel):
    yaml_text: str = Field(default="", max_length=400_000)


class PlaybookGraphRequest(BaseModel):
    """yaml_text 和 form 二选一。"""

    yaml_text: str | None = Field(default=None, max_length=400_000)
    form: dict | None = None
    base_yaml: str | None = Field(default=None, max_length=400_000)


class PlaybookFormToYamlRequest(BaseModel):
    form: dict
    base_yaml: str | None = Field(default=None, max_length=400_000)


class PlaybookProposeRequest(BaseModel):
    """人写的 SOP 提交为待审草稿。yaml_text 和 form 二选一。"""

    yaml_text: str | None = Field(default=None, max_length=400_000)
    form: dict | None = None
    based_on: str | None = None
    note: str | None = Field(default=None, max_length=2000)
    replace_file: str | None = None
    rebase: bool = False
