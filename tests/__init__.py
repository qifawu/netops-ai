import os
import tempfile
from pathlib import Path

# **测试不许往真账本里记账。** 发现 records/token-ledger.jsonl 243 行里
# 210 行是测试写进去的假模型 "m"，命令审计页照着它算钱就全是错的。
# 个别测试自己 patch 这个变量的照旧生效。
_tmp = tempfile.mkdtemp(prefix="netops-test-ledger-")
os.environ["TOKEN_LEDGER_PATH"] = os.path.join(_tmp, "ledger.jsonl")
# 成本配置同理：测试不读也不写仓库根的真实 cost.yaml（对话闸门会读它）。
os.environ["NETOPS_COST_CONFIG"] = os.path.join(_tmp, "cost.yaml")
os.environ["AUTH_DB"] = os.path.join(_tmp, "auth.db")
os.environ.pop("NETOPS_AUTH", None)

# **同一个坑，第二次。** 查出 `records/chat-traces/` 里有 117 个测试
# 跑出来的假对话轨迹（问题都是「Gi0/1 怎么了」这类测试用例），混进了真实记录里，
# 维护者手工确认过才删掉。当时的修法是 `tests/conftest.py` 一个
# `@pytest.fixture(autouse=True)`——**但项目规定的跑法是
# `.venv/bin/python -m unittest discover`，unittest 不认识 conftest.py，
# 那个 fixture 一次都没跑过**。验证那次改动的提交也是拿 `python -m pytest`
# 跑的，不是仓库规定的命令，没人发现。
#
# 现在用跟上面 TOKEN_LEDGER_PATH 一样的办法接上：`tests` 包被导入时（也就是
# `unittest discover` 找到第一个测试模块之前）把落盘目录整体挪到临时目录。
# **这是全局一份，不是每个测试单独一份**——目的是不碰真实 records/，
# 不是让测试之间互相隔离；单个测试要自己的临时目录，照旧自己 `mock.patch`。
_records_tmp = Path(tempfile.mkdtemp(prefix="netops-test-records-"))
(_records_tmp / "chat-traces").mkdir()
os.environ.setdefault("SCOPE_GATE_PATH", str(_records_tmp / "scope-gate.jsonl"))

from netops_ai.api import dashboard as _dashboard  # noqa: E402
from netops_ai.api import pipeline as _pipeline  # noqa: E402
from netops_ai.api import schedule as _schedule  # noqa: E402
from netops_ai.graph import agent_loop as _agent_loop  # noqa: E402
import netops_ai.incident as _incident  # noqa: E402
from netops_ai.inspection import advise as _advise  # noqa: E402

_agent_loop.TRACE_DIR = _records_tmp / "chat-traces"
_dashboard.RECORDS_DIR = _records_tmp
_dashboard.CHAT_TRACE_DIR = _records_tmp / "chat-traces"
_dashboard.INSPECTION_LATEST_PATH = _records_tmp / "inspection-latest.json"
_dashboard.INSPECTION_STATUS_PATH = _records_tmp / "inspection-status-latest.json"
_pipeline.RECORDS_DIR = _records_tmp
_schedule.STATE_PATH = _records_tmp / "schedule-state.json"
_advise.ADVICE_PATH = _records_tmp / "inspection-advice.json"
_incident.DEFAULT_DB_PATH = _records_tmp / "incidents.db"
