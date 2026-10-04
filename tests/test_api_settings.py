"""系统设置页后端：字段清单/敏感字段判定、.env 原子写、测试连接接口。

**全用临时目录**：每条用例都把 `settings.ENV_PATH` monkeypatch 成 `tmp_path` 下的文件，
绝不碰真实仓库根的 `.env`。测试连接接口一律 mock 掉真实 Zabbix/NetBox/LLM 客户端，
不发真实网络请求。
"""

from __future__ import annotations

from unittest import mock

import pytest
from fastapi.testclient import TestClient

import netops_ai.api.settings as settings_mod
from netops_ai.api.app import app

client = TestClient(app)


@pytest.fixture()
def env_file(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    monkeypatch.setattr(settings_mod, "ENV_PATH", path)
    return path


def _write(path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


# ---------------------------------------------------------------------------
# 敏感字段判定
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "key",
    ["ZABBIX_PASSWORD", "NETBOX_TOKEN", "LLM_API_KEY", "FEISHU_APP_SECRET", "DEVICE_PASSWORD", "netbox_token", "Zabbix_Password"],
)
def test_is_sensitive_true(key):
    assert settings_mod.is_sensitive(key) is True


@pytest.mark.parametrize("key", ["ZABBIX_URL", "DEVICE_HOST", "DEVICE_PORT", "ANALYSIS_TOKEN_BUDGET", "DOC_SEARCH"])
def test_is_sensitive_false(key):
    assert settings_mod.is_sensitive(key) is False


def test_all_known_credential_fields_marked_sensitive():
    """任务书点名的敏感字段清单必须完整、一个都不能漏标。"""
    must_be_sensitive = {
        "ZABBIX_PASSWORD",
        "LLM_API_KEY",
        "FEISHU_APP_SECRET", "DEVICE_PASSWORD",
    }
    for key in must_be_sensitive:
        assert key in settings_mod.FIELDS_BY_KEY, f"{key} 没有登记在字段清单里"
        assert settings_mod.is_sensitive(key) is True


# ---------------------------------------------------------------------------
# .env 原子写
# ---------------------------------------------------------------------------

def test_write_dotenv_preserves_untouched_lines_and_order(env_file):
    _write(env_file, "# comment\nZABBIX_URL=http://old\nDEVICE_HOST=1.2.3.4\n\nLLM_MODEL=old-model\n")
    settings_mod.write_dotenv_updates({"ZABBIX_URL": "http://new"}, path=env_file)
    text = env_file.read_text(encoding="utf-8")
    lines = text.splitlines()
    assert lines[0] == "# comment"
    assert "ZABBIX_URL=http://new" in lines
    assert "DEVICE_HOST=1.2.3.4" in lines
    assert "LLM_MODEL=old-model" in lines
    # 顺序不打乱：ZABBIX_URL 仍在 DEVICE_HOST 前面
    assert lines.index("ZABBIX_URL=http://new") < lines.index("DEVICE_HOST=1.2.3.4")


def test_write_dotenv_appends_new_key(env_file):
    _write(env_file, "ZABBIX_URL=http://old\n")
    settings_mod.write_dotenv_updates({"NETBOX_URL": "http://netbox"}, path=env_file)
    lines = env_file.read_text(encoding="utf-8").splitlines()
    assert lines[-1] == "NETBOX_URL=http://netbox"
    assert lines[0] == "ZABBIX_URL=http://old"


def test_write_dotenv_overwrites_existing_key(env_file):
    _write(env_file, "ANALYSIS_TOKEN_BUDGET=60000\n")
    settings_mod.write_dotenv_updates({"ANALYSIS_TOKEN_BUDGET": "80000"}, path=env_file)
    d = settings_mod.load_dotenv_dict(env_file)
    assert d["ANALYSIS_TOKEN_BUDGET"] == "80000"


def test_write_dotenv_creates_file_when_missing(env_file):
    assert not env_file.exists()
    settings_mod.write_dotenv_updates({"ZABBIX_URL": "http://x"}, path=env_file)
    assert env_file.exists()
    assert settings_mod.load_dotenv_dict(env_file) == {"ZABBIX_URL": "http://x"}


# ---------------------------------------------------------------------------
# GET /api/settings：不回显敏感值
# ---------------------------------------------------------------------------

def test_get_settings_sensitive_field_only_returns_configured_bool(env_file, monkeypatch):
    monkeypatch.delenv("ZABBIX_PASSWORD", raising=False)
    _write(env_file, "ZABBIX_PASSWORD=S3cr3tPassw0rd123\nZABBIX_URL=http://zbx\n")
    data = client.get("/api/settings").json()
    by_key = {x["key"]: x for x in data["connection"]}
    pw = by_key["ZABBIX_PASSWORD"]
    assert pw["sensitive"] is True
    assert pw["configured"] is True
    assert "value" not in pw
    url = by_key["ZABBIX_URL"]
    assert url["sensitive"] is False
    assert url["value"] == "http://zbx"


def test_get_settings_response_never_contains_real_secret_string(env_file, monkeypatch):
    """硬红线断言：构造一个明显的假密码，确认它不会出现在响应体任何地方。"""
    for key in ("ZABBIX_PASSWORD", "NETBOX_TOKEN", "LLM_API_KEY", "FEISHU_APP_SECRET", "DEVICE_PASSWORD"):
        monkeypatch.delenv(key, raising=False)
    secret = "S3cr3tPassw0rd123"
    _write(
        env_file,
        "\n".join(
            f"{key}={secret}"
            for key in ("ZABBIX_PASSWORD", "NETBOX_TOKEN", "LLM_API_KEY", "FEISHU_APP_SECRET", "DEVICE_PASSWORD")
        )
        + "\n",
    )
    resp = client.get("/api/settings")
    assert secret not in resp.text


def test_get_settings_unconfigured_sensitive_field_is_false(env_file, monkeypatch):
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    _write(env_file, "ZABBIX_URL=http://zbx\n")
    data = client.get("/api/settings").json()
    by_key = {x["key"]: x for x in data["connection"]}
    assert by_key["LLM_API_KEY"]["configured"] is False


def test_get_settings_source_reports_process_env_override(env_file, monkeypatch):
    _write(env_file, "ANALYSIS_TOKEN_BUDGET=60000\n")
    monkeypatch.setenv("ANALYSIS_TOKEN_BUDGET", "99999")
    data = client.get("/api/settings").json()
    by_key = {x["key"]: x for x in data["runtime"]}
    assert by_key["ANALYSIS_TOKEN_BUDGET"]["source"] == "env_override"
    monkeypatch.delenv("ANALYSIS_TOKEN_BUDGET", raising=False)


# ---------------------------------------------------------------------------
# POST /api/settings：空值语义、数字校验
# ---------------------------------------------------------------------------

def test_post_settings_empty_sensitive_value_does_not_clear(env_file, monkeypatch):
    monkeypatch.delenv("ZABBIX_PASSWORD", raising=False)
    _write(env_file, "ZABBIX_PASSWORD=oldpass\n")
    resp = client.post("/api/settings", json={"updates": {"ZABBIX_PASSWORD": ""}})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert "ZABBIX_PASSWORD" in body["skipped_sensitive_empty"]
    assert settings_mod.load_dotenv_dict(env_file)["ZABBIX_PASSWORD"] == "oldpass"


def test_post_settings_empty_non_sensitive_value_clears(env_file):
    _write(env_file, "ANALYSIS_TOKEN_BUDGET=60000\n")
    resp = client.post("/api/settings", json={"updates": {"ANALYSIS_TOKEN_BUDGET": ""}})
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    assert settings_mod.load_dotenv_dict(env_file)["ANALYSIS_TOKEN_BUDGET"] == ""


def test_post_settings_sensitive_non_empty_overwrites(env_file, monkeypatch):
    monkeypatch.delenv("ZABBIX_PASSWORD", raising=False)
    _write(env_file, "ZABBIX_PASSWORD=oldpass\n")
    resp = client.post("/api/settings", json={"updates": {"ZABBIX_PASSWORD": "newpass"}})
    assert resp.json()["ok"] is True
    assert settings_mod.load_dotenv_dict(env_file)["ZABBIX_PASSWORD"] == "newpass"


def test_post_settings_rejects_non_numeric_for_numeric_field(env_file):
    _write(env_file, "ANALYSIS_TOKEN_BUDGET=60000\n")
    resp = client.post("/api/settings", json={"updates": {"ANALYSIS_TOKEN_BUDGET": "abc"}})
    body = resp.json()
    assert body["ok"] is False
    assert body["rejected"][0]["key"] == "ANALYSIS_TOKEN_BUDGET"
    # 拒绝时不应该写坏文件
    assert settings_mod.load_dotenv_dict(env_file)["ANALYSIS_TOKEN_BUDGET"] == "60000"


def test_post_settings_rejects_unknown_key(env_file):
    resp = client.post("/api/settings", json={"updates": {"SOME_RANDOM_KEY": "x"}})
    body = resp.json()
    assert body["ok"] is False
    assert body["rejected"][0]["key"] == "SOME_RANDOM_KEY"


def test_post_settings_response_never_contains_submitted_secret(env_file, monkeypatch):
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    secret = "S3cr3tPassw0rd123"
    resp = client.post("/api/settings", json={"updates": {"LLM_API_KEY": secret}})
    assert secret not in resp.text


# ---------------------------------------------------------------------------
# POST /api/settings/test：mock 客户端，zabbix/netbox 各一条成功一条失败
# ---------------------------------------------------------------------------

def test_test_connection_zabbix_success(env_file, monkeypatch):
    monkeypatch.delenv("ZABBIX_URL", raising=False)
    monkeypatch.delenv("ZABBIX_USER", raising=False)
    monkeypatch.delenv("ZABBIX_PASSWORD", raising=False)
    _write(env_file, "ZABBIX_URL=http://zbx\nZABBIX_USER=admin\nZABBIX_PASSWORD=pass\n")

    fake_client = mock.MagicMock()
    fake_client._call.return_value = "6.4.0"
    fake_client.list_hosts.return_value = [{"hostid": "1"}]

    with mock.patch("netops_ai.zabbix.client.ZabbixClient", return_value=fake_client):
        resp = client.post("/api/settings/test", json={"target": "zabbix"})
    body = resp.json()
    assert body["ok"] is True
    assert "6.4.0" in body["message"]
    assert body["elapsed_ms"] >= 0


def test_test_connection_zabbix_failure(env_file, monkeypatch):
    from netops_ai.zabbix.client import ZabbixAPIError

    monkeypatch.delenv("ZABBIX_URL", raising=False)
    monkeypatch.delenv("ZABBIX_USER", raising=False)
    monkeypatch.delenv("ZABBIX_PASSWORD", raising=False)
    _write(env_file, "ZABBIX_URL=http://zbx\nZABBIX_USER=admin\nZABBIX_PASSWORD=pass\n")

    fake_client = mock.MagicMock()
    fake_client._call.side_effect = ZabbixAPIError("连不上 Zabbix：timeout")

    with mock.patch("netops_ai.zabbix.client.ZabbixClient", return_value=fake_client):
        resp = client.post("/api/settings/test", json={"target": "zabbix"})
    body = resp.json()
    assert body["ok"] is False
    assert "连不上" in body["message"] or "失败" in body["message"]








def test_test_connection_llm_uses_minimal_prompt_and_reports_token_usage(env_file, monkeypatch):
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("LLM_MODEL", raising=False)
    _write(env_file, "LLM_BASE_URL=http://llm\nLLM_API_KEY=sk-test\nLLM_MODEL=test-model\n")

    from netops_ai.llm.client import LLMResponse

    fake_resp = LLMResponse(content="pong", parsed=None, usage={"total_tokens": 7}, finish_reason="stop", raw={})

    captured = {}

    def fake_complete(self, messages, **kwargs):
        captured["messages"] = messages
        captured["max_completion_tokens"] = kwargs.get("max_completion_tokens")
        return fake_resp

    with mock.patch("netops_ai.llm.client.LLMClient.complete", fake_complete):
        resp = client.post("/api/settings/test", json={"target": "llm"})
    body = resp.json()
    assert body["ok"] is True
    assert "7" in body["message"]
    assert captured["max_completion_tokens"] <= 8


def test_test_connection_unsupported_target(env_file):
    resp = client.post("/api/settings/test", json={"target": "feishu"})
    body = resp.json()
    assert body["ok"] is False


def test_llm_route_reports_vendor_label_and_no_backup(env_file, monkeypatch):
    """维护者要求：把 LLM 接入信息（哪家、有没有主备）透出，token 打码。当前架构确实没有
    主备/多端点切换，`has_backup` 必须如实是 False，不能编一个假的"备用"出来。"""
    for key in ("LLM_API_KEY",):
        monkeypatch.delenv(key, raising=False)
    _write(
        env_file,
        "LLM_BASE_URL=https://ws-fake.ap-southeast-1.maas.aliyuncs.com/compatible-mode/v1\n"
        "LLM_API_KEY=sk-ThisIsASecretKey999\n"
        "LLM_MODEL=qwen3.8-flash\n",
    )
    data = client.get("/api/settings").json()
    route = data["llm_route"]
    assert route["configured"] is True
    assert route["vendor_label"] == "阿里云百炼（DashScope）"
    assert route["model"] == "qwen3.8-flash"
    assert route["has_backup"] is False
    assert "备" in route["note"]
    # 真实 key 不能出现在这个块，也不能出现在整个响应体任何地方。
    resp = client.get("/api/settings")
    assert "sk-ThisIsASecretKey999" not in resp.text
    assert "sk-ThisIsASecretKey999" not in str(route)


def test_llm_route_unknown_host_says_so_not_made_up(env_file, monkeypatch):
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    _write(env_file, "LLM_BASE_URL=https://example.internal/v1\nLLM_API_KEY=x\nLLM_MODEL=m\n")
    route = client.get("/api/settings").json()["llm_route"]
    assert "看不出" in route["vendor_label"]


def test_llm_route_not_configured_is_honest(env_file, monkeypatch):
    for key in ("LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL"):
        monkeypatch.delenv(key, raising=False)
    _write(env_file, "ZABBIX_URL=http://zbx\n")
    route = client.get("/api/settings").json()["llm_route"]
    assert route["configured"] is False
    assert route["vendor_label"] == "未配置"
