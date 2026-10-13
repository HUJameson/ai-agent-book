"""webui API 层测试（全程不触网，见 DESIGN.md 第 7 节）。

覆盖：/api/defaults、/api/ask（offline-demo 全链路 + kimi 参数透传 + 无 key 400 +
参数校验 422）、历史增删查与 FIFO 淘汰。不 mock 底层 HTTP。
"""

import pytest
from fastapi.testclient import TestClient

from webui import agent_runner
from webui.agent_runner import HistoryStore
from webui.app import app


@pytest.fixture(autouse=True)
def clean_store():
    """每条用例独享空历史，避免用例间串扰。"""
    agent_runner.STORE.clear()
    yield
    agent_runner.STORE.clear()


@pytest.fixture()
def client():
    return TestClient(app)


def _ask_payload(question="测试问题", **option_overrides):
    options = {
        "provider": "offline-demo",
        "model": "kimi-k3",
        "max_steps": 5,
        "temperature": 0.6,
        "timeout": 180,
    }
    options.update(option_overrides)
    return {"question": question, "options": options}


# ---------------------------------------------------------------------------
# /api/defaults
# ---------------------------------------------------------------------------
def test_defaults_matches_agent_constants(client):
    from config import Config

    res = client.get("/api/defaults")
    assert res.status_code == 200
    data = res.json()
    assert data["provider"] == "kimi"
    assert data["model"] == Config.DEFAULT_MODEL
    assert data["max_steps"] == Config.MAX_SEARCH_ITERATIONS
    assert data["timeout"] == Config.SEARCH_TIMEOUT


# ---------------------------------------------------------------------------
# POST /api/ask —— offline-demo 全链路（不触网）
# ---------------------------------------------------------------------------
def test_ask_offline_demo_success(client):
    res = client.post("/api/ask", json=_ask_payload("Context Caching 是什么？"))
    assert res.status_code == 200
    entry = res.json()

    assert entry["status"] == "success"
    assert entry["question"] == "Context Caching 是什么？"
    assert entry["id"].startswith("q_")
    assert entry["created_at"]
    assert entry["elapsed_seconds"] >= 0
    assert entry["params"]["provider"] == "offline-demo"
    # ReAct 轨迹：含思考/行动/观察/最终答案，且 answer 步骤在最后
    types = [step["type"] for step in entry["trace"]]
    assert {"thought", "action", "observation", "answer"} <= set(types)
    assert types[-1] == "answer"

    # 问答即进入历史
    summaries = client.get("/api/history").json()
    assert [s["id"] for s in summaries] == [entry["id"]]

    # 详情与 ask 响应一致
    detail = client.get(f"/api/history/{entry['id']}")
    assert detail.status_code == 200
    assert detail.json()["answer"] == entry["answer"]


# ---------------------------------------------------------------------------
# POST /api/ask —— kimi 路径（mock run_question，不触网）
# ---------------------------------------------------------------------------
class _FakeRunner:
    """替换 agent_runner.run_question，断言参数透传并注入指定回答。"""

    def __init__(self, answer):
        self.answer = answer
        self.calls = []

    def __call__(self, question, options):
        self.calls.append((question, dict(options)))
        return {
            "question": question,
            "answer": self.answer,
            "trace": [{"iteration": 1, "type": "answer", "content": self.answer}],
            "status": "success",
            "elapsed_seconds": 0.01,
            "params": dict(options),
        }


@pytest.fixture()
def no_keys(monkeypatch):
    for name in ("MOONSHOT_API_KEY", "KIMI_API_KEY", "OPENROUTER_API_KEY"):
        monkeypatch.delenv(name, raising=False)


def test_ask_kimi_without_key_returns_400(client, no_keys):
    res = client.post("/api/ask", json=_ask_payload(provider="kimi"))
    assert res.status_code == 400
    assert "API Key" in res.json()["detail"]


def test_ask_kimi_params_passthrough(client, monkeypatch):
    fake = _FakeRunner(answer="正常回答")
    monkeypatch.setattr(agent_runner, "has_any_key", lambda: True)
    monkeypatch.setattr(agent_runner, "run_question", fake)

    payload = _ask_payload(
        "比特币现价", provider="kimi", model="kimi-k3",
        max_steps=3, temperature=0.9, timeout=60,
    )
    res = client.post("/api/ask", json=payload)
    assert res.status_code == 200
    entry = res.json()
    assert entry["status"] == "success"

    question, options = fake.calls[0]
    assert question == "比特币现价"
    assert options == {
        "provider": "kimi", "model": "kimi-k3", "max_steps": 3,
        "temperature": 0.9, "timeout": 60,
    }


def test_ask_kimi_failure_answer_maps_to_error_status(client, monkeypatch):
    """业务兜底文案（如超过最大迭代）应映射为 status=error 而非 500（DESIGN.md D6）。"""
    from agent import MAX_ITERATIONS_MESSAGE

    class _StubAgent:
        def __init__(self, **kwargs):
            pass

        def set_temperature(self, temperature):
            pass

        def search_and_answer(self, question, max_iterations=5):
            return MAX_ITERATIONS_MESSAGE

        def get_trace(self):
            return [{"iteration": 1, "type": "answer",
                     "content": MAX_ITERATIONS_MESSAGE}]

    monkeypatch.setattr(agent_runner, "has_any_key", lambda: True)
    monkeypatch.setattr(agent_runner, "WebSearchAgent", _StubAgent)

    res = client.post("/api/ask", json=_ask_payload(provider="kimi"))
    assert res.status_code == 200
    entry = res.json()
    assert entry["status"] == "error"
    assert entry["answer"] == MAX_ITERATIONS_MESSAGE


# ---------------------------------------------------------------------------
# 参数校验（422）
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "payload",
    [
        {"question": "   "},                       # 空白问题
        {"question": "x" * 4001},                  # 超长问题
        _ask_payload(provider="bogus"),            # 非法 provider
        _ask_payload(max_steps=0),                 # max_steps 下界
        _ask_payload(max_steps=11),                # max_steps 上界
        _ask_payload(temperature=2.5),             # temperature 上界
        _ask_payload(timeout=0),                   # timeout 必须为正
    ],
)
def test_ask_validation_rejected(client, payload):
    assert client.post("/api/ask", json=payload).status_code == 422


# ---------------------------------------------------------------------------
# 历史接口
# ---------------------------------------------------------------------------
def test_history_404_for_unknown_id(client):
    assert client.get("/api/history/q_not_exist").status_code == 404


def test_clear_history(client):
    client.post("/api/ask", json=_ask_payload())
    assert len(client.get("/api/history").json()) == 1
    assert client.delete("/api/history").json() == {"cleared": True}
    assert client.get("/api/history").json() == []


def test_history_fifo_limit():
    store = HistoryStore(limit=3)
    ids = [store.add({"question": f"q{i}", "answer": "a", "status": "success",
                      "trace": [], "params": {}, "elapsed_seconds": 0})["id"]
           for i in range(5)]
    assert len(store) == 3
    assert store.get(ids[0]) is None and store.get(ids[1]) is None
    assert store.get(ids[2]) is not None and store.get(ids[4]) is not None
