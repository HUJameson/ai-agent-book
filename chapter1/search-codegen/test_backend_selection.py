"""Offline coverage for explicit backend selection and entry-point propagation."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from agent import GPT5NativeAgent
from config import Config
import main
import run_experiment_1_3 as experiment

SHARED = "https://dashscope.aliyuncs.com/compatible-mode/v1"
WORKSPACE = "https://ws-example.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"


@pytest.mark.parametrize("backend", ["openai", "openrouter", "dashscope"])
def test_explicit_backend_overrides_url(backend):
    agent = GPT5NativeAgent("key", SHARED, "model", backend=backend)
    assert agent.provider == backend
    request = agent._build_responses_request("task", verbosity="high")
    if backend != "dashscope":
        assert request["reasoning"] == {"effort": "low"}
        assert request["text"] == {"verbosity": "high"}
        assert "stream" not in request


def test_workspace_and_shared_use_identical_dashscope_requests():
    agents = [GPT5NativeAgent("key", url, "model", backend="dashscope")
              for url in (SHARED, WORKSPACE)]
    requests = [a._build_responses_request("task", verbosity="high") for a in agents]
    assert requests[0] == requests[1]
    assert agents[1].base_url == WORKSPACE
    assert requests[1]["stream"] is True
    assert requests[1]["tools"] == [{"type": "web_search"}, {"type": "code_interpreter"}]
    assert not {"reasoning", "background", "store", "text"} & requests[1].keys()


@pytest.mark.parametrize("url,provider", [
    ("https://api.openai.com/v1", "openai"),
    ("https://openrouter.ai/api/v1", "openrouter"),
    (SHARED, "dashscope"), (WORKSPACE, "custom"),
    ("https://example.com/v1", "custom"),
])
def test_legacy_positional_url_inference(url, provider):
    assert GPT5NativeAgent("key", url, "model").provider == provider


@pytest.mark.parametrize("backend", ["", "custom", "unknown"])
def test_invalid_backend_fails_at_construction(backend):
    with pytest.raises(ValueError, match="backend must be"):
        GPT5NativeAgent("key", backend=backend)


@pytest.mark.parametrize("selected", [None, "dashscope"])
def test_interactive_passes_configured_backend(monkeypatch, selected):
    monkeypatch.setattr(Config, "BACKEND", "dashscope")
    monkeypatch.setattr(Config, "validate", Mock(return_value=True))
    resolve = Mock(return_value=("key", WORKSPACE, "model"))
    monkeypatch.setattr(Config, "resolve", resolve)
    factory = Mock()
    monkeypatch.setattr(main, "GPT5NativeAgent", factory)
    main.InteractiveCLI(selected)
    resolve.assert_called_once_with("dashscope", None)
    assert factory.call_args.kwargs["backend"] == "dashscope"


@pytest.mark.parametrize("dry_run", [True, False])
def test_single_passes_backend(monkeypatch, dry_run):
    monkeypatch.setattr(Config, "resolve", Mock(return_value=("key", WORKSPACE, "model")))
    factory = Mock()
    factory.return_value.process_request.return_value = {"success": True, "response": "ok"}
    monkeypatch.setattr(main, "GPT5NativeAgent", factory)
    args = SimpleNamespace(backend="dashscope", model=None, dry_run=dry_run,
                           request="task", no_tools=False, reasoning="low",
                           verbosity=None, output=None)
    main._run_single(args)
    assert factory.call_args.kwargs["backend"] == "dashscope"


def test_experiment_passes_backend_to_both_agents(monkeypatch):
    monkeypatch.setattr(Config, "resolve", Mock(return_value=("key", WORKSPACE, "model")))
    factory = Mock()
    factory.return_value.process_request.return_value = {}
    factory.return_value.api_turns = []
    monkeypatch.setattr(experiment, "GPT5NativeAgent", factory)
    monkeypatch.setattr(experiment, "validate_asean", lambda *a: {})
    monkeypatch.setattr(experiment, "validate_clarification", lambda *a: {})
    monkeypatch.setattr(experiment, "is_clarifying_question", lambda *a: False)
    experiment.run_backend("dashscope", "low")
    assert factory.call_count == 2
    assert all(call.kwargs["backend"] == "dashscope" for call in factory.call_args_list)
