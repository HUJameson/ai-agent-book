"""Offline coverage for streamed tool calls and endpoint lifecycle."""
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from webui.app import app
from webui import agent_runner
from webui.job_manager import EXECUTION_LOCK
from webui.streaming import streaming_client


def test_stream_adapter_reassembles_tools_and_reasoning():
    chunks = [
        {"delta": {"reasoning_content": "先计算", "tool_calls": [{"index": 0, "id": "call_1", "function": {"name": "calculator", "arguments": '{"x":'}}]}},
        {"delta": {"content": "好", "tool_calls": [{"index": 0, "function": {"arguments": '2}'}}]}},
        {"delta": {}, "finish_reason": "tool_calls"},
    ]
    class Stream:
        def __enter__(self):
            return iter(SimpleNamespace(model_dump=lambda c=c: {
                "id": "test", "model": "fake", "created": 0,
                "choices": [{"index": 0, **c}]
            }) for c in chunks)
        def __exit__(self, *args):
            pass
    def create(**kwargs):
        assert kwargs["stream"] is True
        return Stream()
    events = []
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    result = streaming_client(client, events.append).chat.completions.create(model="fake")
    message = result.choices[0].message
    assert message.reasoning_content == "先计算"
    assert message.content == "好"
    assert message.tool_calls[0].function.arguments == '{"x":2}'
    assert message.tool_calls[0].function.name == "calculator"
    assert [e["type"] for e in events] == ["turn", "delta", "delta"]


@pytest.mark.parametrize("fail", [False, True])
def test_stream_endpoint_lifecycle(monkeypatch, fail):
    import json
    monkeypatch.setattr(agent_runner, "ensure_provider_ready", lambda *a: None)
    def run(*args, emit):
        emit({"type": "turn", "iteration": 1})
        emit({"type": "delta", "iteration": 1, "field": "content", "text": "你好"})
        if fail:
            raise RuntimeError("fake failure")
        return {"kind": "single", "task": "hi", "status": "success", "final_answer": "你好"}
    monkeypatch.setattr(agent_runner, "run_single_task", run)
    with TestClient(app) as client:
        response = client.post("/api/run/stream", json={"task": "hi", "context_mode": "full", "provider": "kimi"})
        assert response.status_code == 200
        events = [json.loads(line) for line in response.text.splitlines()]
        assert events[-1]["type"] == ("error" if fail else "done")
        assert not EXECUTION_LOCK.locked()
        if not fail:
            entry_id = events[-1]["record"]["id"]
            assert client.get(f"/api/history/{entry_id}").json()["final_answer"] == "你好"
    agent_runner.STORE.clear()
