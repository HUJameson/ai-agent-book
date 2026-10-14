"""context webui API 层测试（全程不触网，见 DESIGN.md 第 7 节）。

覆盖：providers/samples/defaults 契约、单任务参数透传与状态映射、
无 key 400、消融 job 生命周期（进度/报告/409 单飞）、参数校验与历史。
LLM 调用一律 monkeypatch，不 mock 底层 HTTP。
"""

import time

import pytest
from fastapi.testclient import TestClient

from webui import agent_runner
from webui.agent_runner import HistoryStore, MissingKeyError
from webui.job_manager import JOBS


@pytest.fixture(autouse=True)
def clean_state():
    """每条用例独享空历史与空任务表，避免串扰。"""
    agent_runner.STORE.clear()
    JOBS._jobs.clear()
    JOBS._current_id = None
    yield
    agent_runner.STORE.clear()
    JOBS._jobs.clear()
    JOBS._current_id = None


@pytest.fixture()
def client():
    return TestClient(app := __import__("webui.app", fromlist=["app"]).app)


@pytest.fixture()
def ready(monkeypatch):
    """跳过 provider 预检（不触网、不看环境变量）。"""
    monkeypatch.setattr(
        agent_runner, "ensure_provider_ready", lambda provider, model=None: None
    )


def _run_payload(**overrides):
    payload = {
        "task": "把 1000 美元换算成欧元",
        "context_mode": "full",
        "provider": "doubao",
        "model": None,
    }
    payload.update(overrides)
    return payload


def _ablation_payload(**overrides):
    payload = {
        "context_modes": ["full", "no_history"],
        "num_cases": 2,
        "provider": "doubao",
        "model": None,
    }
    payload.update(overrides)
    return payload


def _wait_job(job_id, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        snap = JOBS.snapshot(job_id)
        assert snap is not None
        if snap["status"] != "running":
            return snap
        time.sleep(0.05)
    raise AssertionError("job 未在超时前结束")


# ---------------------------------------------------------------------------
# 契约端点
# ---------------------------------------------------------------------------
def test_providers_shape(client):
    res = client.get("/api/providers")
    assert res.status_code == 200
    items = res.json()
    assert items and all(
        {"name", "default_model", "requires_key", "key_vars", "key_set"} <= set(i)
        for i in items
    )
    # 绝不回传 key 值：任何字符串字段都不是 sk- 开头的长串
    for item in items:
        assert all(not str(v).startswith("sk-") for v in item.values())


def test_samples_endpoint(client):
    res = client.get("/api/samples")
    assert res.status_code == 200
    samples = res.json()
    assert len(samples) == 5
    assert all({"name", "description", "task"} <= set(s) for s in samples)


def test_defaults(client):
    data = client.get("/api/defaults").json()
    assert data["context_mode"] == "full"
    assert data["num_cases"] == 1
    assert len(data["context_modes"]) == 5


def test_state_idle(client):
    assert client.get("/api/state").json()["busy"] is False


# ---------------------------------------------------------------------------
# 单任务
# ---------------------------------------------------------------------------
def test_run_single_params_passthrough(client, monkeypatch, ready):
    captured = {}

    def fake_run(task, context_mode, provider, model=None):
        captured.update(
            task=task, context_mode=context_mode, provider=provider, model=model
        )
        return {
            "kind": "single",
            "task": task,
            "context_mode": context_mode,
            "final_answer": "928,000.00 EUR",
            "completed": True,
            "status": "success",
            "error": None,
            "elapsed_seconds": 1.2,
            "iterations": 3,
            "grounding": {
                "verdict": "grounded",
                "unsupported_quantities": [],
                "observation_count": 4,
            },
            "trajectory": {
                "tool_calls": [],
                "reasoning_steps_count": 2,
                "api_turns_count": 4,
            },
        }

    monkeypatch.setattr(agent_runner, "run_single_task", fake_run)
    res = client.post(
        "/api/run",
        json=_run_payload(
            task="转换任务", context_mode="no_history", provider="kimi", model="kimi-k3"
        ),
    )
    assert res.status_code == 200
    entry = res.json()
    assert captured == {
        "task": "转换任务",
        "context_mode": "no_history",
        "provider": "kimi",
        "model": "kimi-k3",
    }
    assert entry["status"] == "success"
    assert entry["id"].startswith("r_")
    assert entry["grounding"]["verdict"] == "grounded"
    # 入史
    assert client.get("/api/history").json()[0]["id"] == entry["id"]


def test_run_single_missing_key_400(client, monkeypatch):
    def refuse(provider, model=None):
        raise MissingKeyError("ARK_API_KEY / OPENROUTER_API_KEY 均未设置")

    monkeypatch.setattr(agent_runner, "ensure_provider_ready", refuse)
    res = client.post("/api/run", json=_run_payload())
    assert res.status_code == 400
    assert "ARK_API_KEY" in res.json()["detail"]


def test_unknown_provider_400(client, ready):
    res = client.post("/api/run", json=_run_payload(provider="nope"))
    assert res.status_code == 400
    assert "nope" in res.json()["detail"]


@pytest.mark.parametrize(
    "overrides",
    [
        {"task": "   "},
        {"task": "x", "context_mode": "bogus"},
        {"task": "x" * 8001},
    ],
)
def test_run_validation_422(client, overrides):
    body = _run_payload(**overrides)
    assert client.post("/api/run", json=body).status_code == 422


# ---------------------------------------------------------------------------
# 消融 job
# ---------------------------------------------------------------------------
class _FakeSuite:
    """模拟 AblationTestSuite 的进度可见面：test_results 随执行增长。"""

    def __init__(self, results):
        self.test_results = results


def _fake_ablation_results(context_modes, num_cases):
    results = []
    for mode in context_modes:
        for c in range(1, num_cases + 1):
            results.append(
                {
                    "test_name": f"Ablation - {mode}",
                    "case_name": f"case{c}",
                    "context_mode": mode,
                    "completed": True,
                    "grounding_verdict": "grounded",
                    "unsupported_quantities": [],
                    "iterations": 2,
                    "num_tool_calls": 1,
                    "execution_time": 0.5,
                    "error": None,
                }
            )
    return results


def _patch_fast_job(monkeypatch, sleep=0.0):
    def fake_run(provider, model, context_modes, num_cases, suite_out):
        results = _fake_ablation_results(context_modes, num_cases)
        suite_out["suite"] = _FakeSuite(list(results))
        suite_out["total"] = len(context_modes) * num_cases
        time.sleep(sleep)
        return {
            "results": agent_runner._trim_results(results),
            "analysis": {
                "total_tests": len(results),
                "completed_tests": len(results),
                "unsupported_number_tests": 0,
                "context_mode_impact": {},
            },
            "report_md": "# Context Ablation Study Report\n\nfake report",
            "params": {
                "provider": provider,
                "model": model,
                "context_modes": context_modes,
                "num_cases": num_cases,
            },
        }

    monkeypatch.setattr(agent_runner, "run_ablation_job", fake_run)


def test_ablation_lifecycle(client, monkeypatch, ready):
    _patch_fast_job(monkeypatch)
    res = client.post("/api/ablation", json=_ablation_payload())
    assert res.status_code == 200
    job_id = res.json()["job_id"]

    snap = _wait_job(job_id)
    assert snap["status"] == "completed"
    assert snap["progress"]["done"] == snap["progress"]["total"] == 4
    assert len(snap["results"]) == 4
    assert snap["results"][0]["grounding_verdict"] == "grounded"

    # 消融结果统一入史（SPEC F4）
    history = client.get("/api/history").json()
    assert history and history[0]["kind"] == "ablation"
    detail = client.get(f"/api/history/{history[0]['id']}").json()
    assert detail["job_id"] == job_id
    assert len(detail["results_summary"]) == 4

    # 报告下载
    report = client.get(f"/api/report/{job_id}")
    assert report.status_code == 200
    assert "Ablation Study Report" in report.text
    # 未完成任务不可下载
    assert client.get("/api/report/job_none").status_code == 404


def test_ablation_single_flight_409(client, monkeypatch, ready):
    _patch_fast_job(monkeypatch, sleep=0.5)
    first = client.post("/api/ablation", json=_ablation_payload())
    assert first.status_code == 200
    job_id = first.json()["job_id"]

    # 第二个消融请求与单任务请求都被拒绝
    assert client.post("/api/ablation", json=_ablation_payload()).status_code == 409
    assert client.post("/api/run", json=_run_payload()).status_code == 409

    snap = _wait_job(job_id)
    assert snap["status"] == "completed"
    # 释放后恢复可用
    assert client.get("/api/state").json()["busy"] is False


def test_job_404(client):
    assert client.get("/api/jobs/job_none").status_code == 404


# ---------------------------------------------------------------------------
# 校验与历史
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "payload",
    [
        _ablation_payload(num_cases=0),
        _ablation_payload(num_cases=5),
        _ablation_payload(context_modes=[]),
        _ablation_payload(context_modes=["bogus"]),
    ],
)
def test_ablation_validation_422(client, payload):
    assert client.post("/api/ablation", json=payload).status_code == 422


def test_history_fifo_limit():
    store = HistoryStore(limit=3)
    ids = [store.add({"kind": "single", "status": "success"})["id"] for _ in range(5)]
    assert len(store) == 3
    assert store.get(ids[0]) is None
    assert store.get(ids[4]) is not None


# ---------------------------------------------------------------------------
# 逐轮实发上下文视图（实验 1-1 本体证据，DESIGN D9）
# ---------------------------------------------------------------------------
class _FakeTrajectory:
    def __init__(self, api_turns):
        self.api_turns = api_turns


def test_trajectory_view_exposes_sent_messages():
    from webui.agent_runner import trajectory_view

    trajectory = _FakeTrajectory(
        [
            {
                "iteration": 1,
                "request": {
                    "model": "kimi-k3",
                    "tools": [{"type": "function"}],
                    "messages": [
                        {"role": "system", "content": "You are an assistant."},
                        {"role": "user", "content": "把 1000 美元换算成欧元"},
                        {
                            "role": "assistant",
                            "content": "",
                            "tool_calls": [
                                {
                                    "function": {
                                        "name": "convert_currency",
                                        "arguments": "{}",
                                    }
                                }
                            ],
                        },
                        {"role": "tool", "tool_call_id": "x", "content": ""},
                    ],
                },
            },
        ]
    )
    view = trajectory_view(trajectory)
    assert len(view) == 1
    turn = view[0]
    assert turn["iteration"] == 1
    assert turn["tools_included"] is True
    roles = [m["role"] for m in turn["messages"]]
    assert roles == ["system", "user", "assistant", "tool"]
    # 空 tool 消息必须原样暴露（no_tool_results 模式的铁证）
    assert turn["messages"][3]["content"] is None
    # 助手消息附带工具调用与思考标记
    assert turn["messages"][2]["tool_calls"][0]["name"] == "convert_currency"


def test_trajectory_view_truncates_and_flags_reasoning():
    from webui.agent_runner import trajectory_view, _MESSAGE_VIEW_LIMIT

    long_text = "x" * (_MESSAGE_VIEW_LIMIT + 500)
    trajectory = _FakeTrajectory(
        [
            {
                "iteration": 1,
                "request": {
                    "messages": [
                        {
                            "role": "assistant",
                            "content": long_text,
                            "reasoning_content": "thinking...",
                        },
                    ]
                },
            },
        ]
    )
    view = trajectory_view(trajectory)
    msg = view[0]["messages"][0]
    assert msg["truncated"] is True
    assert len(msg["content"]) < _MESSAGE_VIEW_LIMIT + 100
    assert msg["has_reasoning"] is True


def test_trajectory_view_caps_turns():
    from webui.agent_runner import trajectory_view, _MAX_TURNS_VIEW

    trajectory = _FakeTrajectory(
        [
            {"iteration": i, "request": {"messages": []}}
            for i in range(_MAX_TURNS_VIEW + 5)
        ]
    )
    assert len(trajectory_view(trajectory)) == _MAX_TURNS_VIEW


def test_clear_history_and_404(client, monkeypatch, ready):
    monkeypatch.setattr(
        agent_runner,
        "run_single_task",
        lambda *a, **k: {
            "kind": "single",
            "status": "success",
            "task": "t",
            "context_mode": "full",
            "final_answer": "a",
            "completed": True,
            "error": None,
            "elapsed_seconds": 0,
            "iterations": 1,
            "grounding": {},
            "trajectory": {"tool_calls": []},
        },
    )
    entry = client.post("/api/run", json=_run_payload()).json()
    assert client.get(f"/api/history/{entry['id']}").status_code == 200
    assert client.delete("/api/history").json() == {"cleared": True}
    assert client.get(f"/api/history/{entry['id']}").status_code == 404
    assert client.get("/api/history").json() == []
