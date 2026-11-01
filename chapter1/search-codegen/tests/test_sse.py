"""Offline SSE tests; upstream and browser events are deterministic fixtures."""

import asyncio
import json
import threading
import time

import pytest
from agent import GPT5NativeAgent
from webui.streaming import StreamLog, event_frames
from webui.state import Store
from webui.app import create_app
from fastapi.testclient import TestClient
from config import Config


def test_agent_delivers_delta_before_terminal(monkeypatch):
    seen = []
    events = [
        {"type": "response.output_text.delta", "delta": "你好"},
        {"type": "response.output_text.done", "text": "你好"},
        {
            "type": "response.completed",
            "response": {
                "id": "r",
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": "你好"}],
                    }
                ],
            },
        },
    ]

    class Response:
        status_code = 200
        ok = True

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def iter_lines(self, **kwargs):
            assert kwargs["chunk_size"] == 1
            for i, event in enumerate(events):
                if i == 2:
                    assert seen[0]["delta"] == "你好"
                # Multiple data fields are a single SSE frame.
                raw = json.dumps(event, ensure_ascii=False)
                yield "data: " + raw[:1]
                yield "data: " + raw[1:]
                yield ""
            pytest.fail("must stop at terminal, without waiting for EOF")

    monkeypatch.setattr("requests.post", lambda *a, **kw: Response())
    result = GPT5NativeAgent("key", backend="dashscope").process_request(
        "task", stream=True, on_event=seen.append
    )
    assert result["success"]
    assert result["response"] == "你好"
    assert len(seen) == 3


def test_agent_failure_without_terminal(monkeypatch):
    class Response:
        status_code = 200
        ok = True

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def iter_lines(self, **kw):
            yield "data: [DONE]"
            yield ""

    monkeypatch.setattr("requests.post", lambda *a, **kw: Response())
    result = GPT5NativeAgent("key", backend="dashscope").process_request(
        "task", stream=True
    )
    assert not result["success"]
    assert result["error"]["type"] == "stream_incomplete"


def test_replay_reset_limits_and_redaction(monkeypatch):
    monkeypatch.setattr(Config, "DASHSCOPE_API_KEY", "a-secret-value")
    log = StreamLog("job", "owner", limit=2)
    for text in ["prefix a-sec", "ret-value suffix"]:
        log.observe(
            {"type": "response.output_text.delta", "delta": text},
            backend="dashscope",
            stage="turn",
        )
    log.observe(
        {"type": "response.output_text.done", "text": "prefix a-secret-value suffix"},
        stage="turn",
        backend="dashscope",
    )
    frames, _ = log.read(None)
    assert "a-secret-value" not in frames[0][1]
    assert "[REDACTED]" in frames[0][1]
    for _ in range(5):
        log.emit("job.state", status="running")
    assert len(log.events) == 2
    assert "stream.reset" in log.read(0)[0][0][1]
    assert len(log.read(log.seq - 1)[0]) == 1
    log.done("error", {"message": "a-secret-value"})
    assert log.read(None)[1]
    assert "a-secret-value" not in str(log.snapshot())


def test_generator_heartbeat_then_delta_before_finish():
    async def run():
        log = StreamLog("job", "owner")
        log.subscribers = 1

        class Request:
            async def is_disconnected(self):
                return False

        gen = event_frames(log, None, Request(), lambda: True, heartbeat=0)
        assert "stream.reset" in await anext(gen)
        assert ": heartbeat" in await anext(gen)
        log.observe(
            {"type": "response.output_text.delta", "delta": "partial"}, stage="turn"
        )
        assert "answer.snapshot" in await anext(gen)
        assert log.status == "queued"
        log.done("completed")
        rest = [frame async for frame in gen]
        assert any("job.done" in frame for frame in rest)
        assert log.subscribers == 0

    asyncio.run(run())


def test_route_terminal_cursor_and_unknown_job():
    store = Store()
    owner = store.add("conversation", {})
    jid = store.reserve("turn", owner["id"])
    store.start(jid)
    store.streams[jid].observe(
        {"type": "response.output_text.delta", "delta": "partial"}, stage="turn"
    )
    store.finish(jid)
    with TestClient(create_app(store)) as client:
        result = client.get(f"/api/jobs/{jid}/events")
        assert result.headers["content-type"].startswith("text/event-stream")
        assert "stream.reset" in result.text and "completed" in result.text
        assert client.get(f"/api/jobs/{jid}/events?after=no").status_code == 422
        assert client.get("/api/jobs/missing/events").status_code == 404
        assert store.streams[jid].subscribers == 0


def test_real_socket_first_frame_precedes_finish():
    import socket
    import requests
    import uvicorn

    store = Store()
    owner = store.add("conversation", {})
    jid = store.reserve("turn", owner["id"])
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(create_app(store), log_level="critical", lifespan="off")
    )
    thread = threading.Thread(target=lambda: server.run(sockets=[sock]), daemon=True)
    thread.start()
    try:
        for _ in range(100):
            if server.started:
                break
            time.sleep(0.01)
        with requests.get(
            f"http://127.0.0.1:{port}/api/jobs/{jid}/events", stream=True, timeout=3
        ) as response:
            lines = response.iter_lines(chunk_size=1, decode_unicode=True)
            assert next(lines).startswith("id:")
            assert store.jobs[jid]["status"] == "queued"
            store.streams[jid].observe(
                {"type": "response.output_text.delta", "delta": "live"}, stage="turn"
            )
            found = False
            for line in lines:
                if "answer.snapshot" in line:
                    found = True
                    break
            assert found
            assert store.jobs[jid]["status"] == "queued"
            store.finish(jid)
    finally:
        server.should_exit = True
        thread.join(3)
        sock.close()


def test_subscriber_limit_does_not_take_execution_slot():
    store = Store()
    owner = store.add("conversation", {})
    jid = store.reserve("turn", owner["id"])
    store.streams[jid].subscribers = 4
    with TestClient(create_app(store)) as client:
        assert client.get(f"/api/jobs/{jid}/events").status_code == 429
        assert store.current_job_id == jid
    store.streams[jid].subscribers = 0
    store.finish(jid)


def test_unknown_events_and_interleaved_tool_status():
    log = StreamLog("job", "owner")
    log.observe({"type": "unknown", "secret": "not a public field"})
    assert log.seq == 0
    log.observe(
        {
            "type": "response.output_item.added",
            "item": {"id": "tool", "type": "web_search_call", "status": "in_progress"},
        }
    )
    log.observe(
        {
            "type": "response.output_item.done",
            "item": {"id": "tool", "type": "web_search_call", "status": "completed"},
        }
    )
    assert len(log.tools) == 1
    assert list(log.tools.values())[0]["status"] == "completed"


def test_observer_failure_does_not_retry_model(monkeypatch):
    calls = []

    class Response:
        status_code = 200
        ok = True

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def iter_lines(self, **kw):
            for value in [
                {"type": "response.output_text.delta", "delta": "hello"},
                {
                    "type": "response.completed",
                    "response": {
                        "status": "completed",
                        "output": [
                            {
                                "type": "message",
                                "content": [{"type": "output_text", "text": "hello"}],
                            }
                        ],
                    },
                },
            ]:
                yield "data: " + json.dumps(value)
                yield ""

    def post(*args, **kw):
        calls.append(kw)
        return Response()

    def observer(event):
        raise ValueError("display failed")

    monkeypatch.setattr("requests.post", post)
    agent = GPT5NativeAgent("key", backend="dashscope")
    assert agent.process_request("task", on_event=observer)["success"]
    assert len(calls) == 1
    assert agent.api_turns[0]["stream_event_counts"]["observer_error"] == 1


def test_failed_partial_answer_is_saved_but_not_markdown_success():
    from webui.serialization import markdown

    store = Store()
    owner = store.add("conversation", {})
    jid = store.reserve("turn", owner["id"])
    owner["turns"] = [
        {
            "id": "turn",
            "job_id": jid,
            "status": "error",
            "input": "task",
            "result": {"success": False},
        }
    ]
    store.streams[jid].observe(
        {"type": "response.output_text.delta", "delta": "unfinished"}
    )
    store.finish(jid, {"message": "timeout"})
    saved = store.detail(owner["id"])
    assert saved["turns"][0]["stream_preview"]["blocks"][0]["text"] == "unfinished"
    assert "unfinished" not in markdown(saved)
    assert "失败" in markdown(saved)


def test_disconnect_releases_subscription_without_stopping_job():
    async def run():
        log = StreamLog("job", "owner")
        log.subscribers = 1

        class Request:
            async def is_disconnected(self):
                return True

        assert [
            frame async for frame in event_frames(log, None, Request(), lambda: True)
        ] == []
        assert log.subscribers == 0
        assert log.status == "queued"

    asyncio.run(run())
