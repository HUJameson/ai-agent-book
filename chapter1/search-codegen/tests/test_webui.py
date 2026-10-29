"""All model calls replaced offline. No real credentials or paid requests."""

from copy import deepcopy
from pathlib import Path
import sys
import threading
import time

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from webui import agent_runner as runner
from webui.app import create_app
from webui.job_manager import JobManager
from webui.state import Store, StateError
from webui.serialization import redact, usage_summary
from config import Config

WORKSPACE = "https://ws-example.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"


@pytest.fixture
def env(monkeypatch):
    def deny_network(*args, **kwargs):
        pytest.fail("unexpected live model call")

    monkeypatch.setattr("requests.post", deny_network)
    monkeypatch.setattr(Config, "DASHSCOPE_API_KEY", "TEST-SECRET-DO-NOT-RETURN")
    monkeypatch.setattr(Config, "DASHSCOPE_BASE_URL", WORKSPACE)
    monkeypatch.setattr(Config, "BACKEND", "dashscope")
    monkeypatch.setattr(Config, "DEFAULT_MAX_TOKENS", None)
    return monkeypatch


@pytest.fixture
def client(env):
    with TestClient(create_app()) as client:
        yield client


def session(client):
    response = client.post("/api/sessions", json={"config": {"backend": "dashscope"}})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def wait(client, job_id):
    for _ in range(200):
        response = client.get("/api/jobs/" + job_id).json()
        if response["status"] in ("completed", "error"):
            return response
        time.sleep(0.005)
    pytest.fail("fake job did not finish")


def fake_success(agent, text, **kwargs):
    request = agent._build_responses_request(
        text, use_tools=kwargs.get("use_tools", True)
    )
    agent.previous_response_id = "response-success"
    agent.conversation_history.append({"role": "assistant", "content": "ok"})
    result = {
        "success": True,
        "response": "连接成功",
        "request": request,
        "response_id": "response-success",
        "provider": agent.provider,
        "base_url": agent.base_url,
        "tool_calls": [],
        "usage": {"input_tokens": 3},
    }
    agent.api_turns.append({"request": request, "response": {"id": "response-success"}})
    return result


def test_defaults_static_and_key_status_are_safe(client):
    assert client.get("/").status_code == 200
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/api/defaults").json()["backend"] == "dashscope"
    backends = client.get("/api/backends")
    assert "TEST-SECRET" not in backends.text
    assert next(b for b in backends.json() if b["name"] == "dashscope")["key_set"]
    assert len(client.get("/api/samples").json()) == 3


def test_preview_without_key_has_correct_workspace_protocol(client, env):
    env.setattr(Config, "DASHSCOPE_API_KEY", "")
    response = client.post(
        "/api/preview", json={"config": {"backend": "dashscope"}, "input": "hello"}
    )
    assert response.status_code == 200
    data = response.json()
    assert data["base_url"] == WORKSPACE
    assert data["provider"] == "dashscope"
    assert data["request"]["stream"] is True
    assert "reasoning" not in data["request"]
    assert data["request"]["tools"] == [
        {"type": "web_search"},
        {"type": "code_interpreter"},
    ]
    assert not client.get("/api/history").json()
    response = client.post("/api/sessions", json={"config": {"backend": "dashscope"}})
    assert response.status_code == 400
    assert "DASHSCOPE_API_KEY" in response.text


@pytest.mark.parametrize(
    "body",
    [
        {"config": {"backend": "bad"}},
        {"config": {"backend": "dashscope", "max_output_tokens": 0}},
        {"config": {"backend": "dashscope", "base_url": "https://evil.test"}},
    ],
)
def test_bad_session_parameters(client, body):
    assert client.post("/api/sessions", json=body).status_code == 422


@pytest.mark.parametrize(
    "body",
    [
        {"input": " "},
        {"input": "a", "session_id": "x", "config": {"backend": "dashscope"}},
        {"input": "a"},
    ],
)
def test_preview_validation(client, body):
    assert client.post("/api/preview", json=body).status_code == 422


def test_multiturn_preview_does_not_mutate_and_failure_rolls_back(client, env):
    sid = session(client)
    store = client.app.state.store
    agent = store.get(sid)["_agent"]
    env.setattr(type(agent), "process_request", fake_success)
    first = client.post(f"/api/sessions/{sid}/turns", json={"input": "first"})
    assert first.status_code == 202
    assert wait(client, first.json()["job_id"])["status"] == "completed"
    before = (
        deepcopy(agent.api_turns),
        deepcopy(agent.conversation_history),
        agent.previous_response_id,
    )

    # Real dry_run implementation for the preview.
    def fail(agent, text, **kwargs):
        assert agent.previous_response_id == "response-success"
        agent.previous_response_id = "response-incomplete"
        agent.conversation_history.append({"role": "assistant", "content": "bad"})
        agent.api_turns.append({"request": {"input": text}, "error": "timeout"})
        return {
            "success": False,
            "error": {"class": "ReadTimeout", "message": "timeout"},
        }

    # Construct request through the actual preview path, without invoking fake network code.
    from unittest.mock import patch

    with patch.object(runner, "make_agent") as factory:
        preview_agent = factory.return_value
        preview_agent.process_request.return_value = {
            "dry_run": True,
            "request": {"input": "next"},
        }
        assert (
            client.post(
                "/api/preview", json={"session_id": sid, "input": "next"}
            ).status_code
            == 200
        )
        assert preview_agent.previous_response_id == "response-success"
    assert before == (
        agent.api_turns,
        agent.conversation_history,
        agent.previous_response_id,
    )
    env.setattr(type(agent), "process_request", fail)
    second = client.post(f"/api/sessions/{sid}/turns", json={"input": "second"})
    assert wait(client, second.json()["job_id"])["status"] == "error"
    assert agent.previous_response_id == "response-success"
    assert agent.conversation_history == before[1]
    owner = client.get("/api/history/" + sid).json()
    assert len(owner["turns"]) == 2
    assert len(owner["turns"][1]["api_turns"]) == 1
    exported = client.get(f"/api/exports/{sid}").json()
    assert exported["turns"][1]["result"]["error"]["class"] == "ReadTimeout"
    assert "TEST-SECRET" not in client.get(f"/api/exports/{sid}").text


def test_atomic_busy_and_refresh_recovery(client, env):
    entered, release = threading.Event(), threading.Event()

    def blocking(agent, text, **kwargs):
        if kwargs.get("dry_run"):
            return {"dry_run": True, "request": agent._build_responses_request(text)}
        entered.set()
        assert release.wait(3)
        return fake_success(agent, text, **kwargs)

    env.setattr(runner.GPT5NativeAgent, "process_request", blocking)
    sid = session(client)
    try:
        first = client.post(f"/api/sessions/{sid}/turns", json={"input": "task"})
        assert entered.wait(1)
        jid = first.json()["job_id"]
        assert client.get("/api/jobs").json()[0]["id"] == jid
        assert client.get("/api/jobs/" + jid).json()["status"] == "running"
        assert (
            client.post(
                f"/api/sessions/{sid}/turns", json={"input": "duplicate"}
            ).status_code
            == 409
        )
        assert (
            client.post(
                "/api/preview", json={"session_id": sid, "input": "preview"}
            ).status_code
            == 409
        )
        assert (
            client.post(
                "/api/preview",
                json={"config": {"backend": "dashscope"}, "input": "preview"},
            ).status_code
            == 200
        )
        assert (
            client.delete(
                "/api/history/" + sid, headers={"Content-Type": "application/json"}
            ).status_code
            == 409
        )
        cleared = client.delete(
            "/api/history", headers={"Content-Type": "application/json"}
        ).json()
        assert cleared["retained_active_ids"] == [sid]
        assert client.get("/api/exports/" + sid).status_code == 409
    finally:
        release.set()
    assert wait(client, jid)["status"] == "completed"


def test_start_failure_releases_slot(env):
    store = Store()
    owner = store.add("conversation", {}, object())

    def bad_thread(**kwargs):
        raise RuntimeError("thread unavailable")

    manager = JobManager(store, thread_factory=bad_thread)
    jid = manager.turn(owner["id"], "task")
    assert store.snapshot(jid)["status"] == "error"
    assert store.current_job_id is None
    assert store.get(owner["id"])["active_job_id"] is None


def test_unexpected_exception_restores_state(client, env):
    sid = session(client)
    agent = client.app.state.store.get(sid)["_agent"]
    agent.previous_response_id = "old"

    def crash(agent, *a, **kw):
        agent.previous_response_id = "bad"
        raise RuntimeError("TEST-SECRET-DO-NOT-RETURN")

    env.setattr(type(agent), "process_request", crash)
    response = client.post(f"/api/sessions/{sid}/turns", json={"input": "task"})
    job = wait(client, response.json()["job_id"])
    assert job["status"] == "error"
    assert "TEST-SECRET" not in str(job)
    assert agent.previous_response_id == "old"
    assert client.app.state.store.current_job_id is None


def test_history_fifo_capacity_and_job_cleanup():
    store = Store(max_records=2)
    first = store.add("conversation", {})
    jid = store.reserve("turn", first["id"])
    for _ in range(4):
        store.add("conversation", {})
    assert first["id"] in store.owners
    assert len(store.owners) == 3
    store.finish(jid)
    for _ in range(3):
        store.add("conversation", {})
    assert len(store.owners) == 2
    assert jid not in store.jobs


def test_oversize_evidence_is_explicit_not_fake_complete():
    store = Store(owner_budget=900)
    owner = store.add("conversation", {}, object())
    jid = store.reserve("turn", owner["id"])
    owner["turns"] = [{"result": {"response": "x" * 2000}}]
    store.finish(jid)
    assert store.snapshot(jid)["error"]["code"] == "evidence_limit_exceeded"
    assert store.detail(owner["id"])["evidence_limited"]
    assert "_agent" not in owner
    with pytest.raises(StateError):
        store.reserve("turn", owner["id"])


def test_experiment_partial_failure_continues(client, env):
    env.setattr(Config, "OPENAI_API_KEY", "fake-openai")
    seen = []

    def run(backend, reasoning):
        seen.append(backend)
        if backend == "openai":
            raise RuntimeError("unavailable")
        return {
            "backend": backend,
            "started": True,
            "asean": {"success": True, "usage": {"input_tokens": 2}},
            "asean_validation": {"passed": False},
            "clarification": {
                "first": {"success": True},
                "validation": {"passed": False},
            },
        }

    env.setattr(runner.experiment, "run_backend", run)
    result = client.post("/api/experiments", json={"backends": ["openai", "dashscope"]})
    assert result.status_code == 202
    job = wait(client, result.json()["job_id"])
    assert job["status"] == "error"
    assert job["progress"]["done"] == 2
    assert seen == ["openai", "dashscope"]
    report = client.get("/api/history/" + result.json()["experiment_id"]).json()
    assert len(report["runs"]) == 2
    assert report["acceptance"]["passed"] is False
    assert report["usage_summary"]["partial"]
    assert client.get("/api/exports/" + report["id"] + "?format=md").status_code == 422


def test_experiment_preflight_all_keys(client, env):
    env.setattr(Config, "OPENAI_API_KEY", "")
    assert (
        client.post(
            "/api/experiments", json={"backends": ["dashscope", "openai"]}
        ).status_code
        == 400
    )
    assert not client.get("/api/jobs").json()
    assert not client.get("/api/history").json()


def test_exports_and_not_found(client, env):
    env.setattr(runner.GPT5NativeAgent, "process_request", fake_success)
    sid = session(client)
    result = client.post(f"/api/sessions/{sid}/turns", json={"input": "task"})
    wait(client, result.json()["job_id"])
    owner = client.get("/api/history/" + sid).json()
    tid = owner["turns"][0]["id"]
    assert client.get(f"/api/exports/{sid}?format=md&turn_id={tid}").status_code == 200
    assert client.get(f"/api/exports/{sid}?turn_id=missing").status_code == 404
    assert client.get("/api/history/missing").status_code == 404
    assert (
        client.delete(
            "/api/history/" + sid, headers={"Content-Type": "application/json"}
        ).status_code
        == 204
    )
    assert client.get("/api/jobs/" + result.json()["job_id"]).status_code == 404


def test_same_origin_and_host_guards(client):
    assert (
        client.post(
            "/api/preview", json={}, headers={"Origin": "https://evil.test"}
        ).status_code
        == 403
    )
    assert client.get("/api/defaults", headers={"Host": "evil.test"}).status_code == 403
    assert client.delete("/api/history").status_code == 415
    assert client.get("/").headers["Content-Security-Policy"]


def test_redaction_and_usage_missing(env):
    value = {
        "message": "bad TEST-SECRET-DO-NOT-RETURN",
        "Authorization": "Bearer secret",
        "request": {"api_key": "secret", "input": "normal"},
        "_agent": object(),
    }
    assert redact(value) == {
        "message": "bad [REDACTED]",
        "request": {"input": "normal"},
    }
    assert (
        usage_summary([{"started": True, "asean": {"usage": {}}}])["total_tokens"]
        is None
    )
    assert (
        usage_summary([{"started": True, "asean": {"usage": {"input_tokens": 5}}}])[
            "input_tokens"
        ]
        == 5
    )


def test_real_preview_copies_id_without_changing_live_agent(client):
    sid = session(client)
    agent = client.app.state.store.get(sid)["_agent"]
    agent.previous_response_id = "previous-real"
    before = deepcopy(agent.api_turns)
    response = client.post("/api/preview", json={"session_id": sid, "input": "next"})
    assert response.status_code == 200
    assert response.json()["request"]["previous_response_id"] == "previous-real"
    assert agent.api_turns == before
    assert agent.previous_response_id == "previous-real"


def test_empty_response_is_explained(client, env):
    def empty(agent, *args, **kwargs):
        agent.api_turns.append({"http_status": 502})
        return {
            "success": False,
            "error": {"type": "http_error"},
            "raw_response": {"raw_text": ""},
        }

    env.setattr(runner.GPT5NativeAgent, "process_request", empty)
    sid = session(client)
    result = client.post(f"/api/sessions/{sid}/turns", json={"input": "task"})
    job = wait(client, result.json()["job_id"])
    assert job["error"]["type"] == "empty_response"
    turn = client.get("/api/history/" + sid).json()["turns"][0]
    assert turn["result"]["http_status"] == 502


def test_experiment_completed_but_acceptance_false(client, env):
    def run(backend, reasoning):
        return {
            "backend": backend,
            "started": True,
            "asean": {"success": True},
            "asean_validation": {"passed": False},
            "clarification": {
                "first": {"success": True},
                "validation": {"passed": False},
            },
        }

    env.setattr(runner.experiment, "run_backend", run)
    value = client.post("/api/experiments", json={"backends": ["dashscope"]}).json()
    assert wait(client, value["job_id"])["status"] == "completed"
    assert (
        client.get("/api/history/" + value["experiment_id"]).json()["acceptance"][
            "passed"
        ]
        is False
    )


def test_reservation_race_has_exactly_one_winner():
    store = Store()
    owners = [store.add("conversation", {}) for _ in range(2)]
    barrier = threading.Barrier(2)
    results = []

    def reserve(owner):
        barrier.wait()
        try:
            results.append(store.reserve("turn", owner["id"]))
        except StateError as error:
            results.append(error.code)

    threads = [threading.Thread(target=reserve, args=(owner,)) for owner in owners]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert results.count("busy") == 1
    assert len(store.jobs) == 1


def test_global_evidence_budget_evicts_old_records():
    store = Store(total_budget=1500, owner_budget=1000)
    first = store.add("conversation", {})
    first["turns"] = [{"result": "x" * 500}]
    second = store.add("conversation", {})
    second["turns"] = [{"result": "x" * 500}]
    jid = store.reserve("turn", second["id"])
    store.finish(jid)
    assert first["id"] not in store.owners
    assert second["id"] in store.owners


def test_preview_tools_disabled_does_not_send_definitions(client):
    value = client.post(
        "/api/preview",
        json={"input": "task", "config": {"backend": "dashscope", "use_tools": False}},
    ).json()
    assert "tools" not in value["request"]
