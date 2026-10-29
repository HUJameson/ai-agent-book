"""The only bridge between the WebUI and existing model/experiment code."""

from copy import deepcopy
from agent import GPT5NativeAgent
from config import Config
import run_experiment_1_3 as experiment
from .state import StateError, new_id, now
from .serialization import usage_summary

KEY_VARS = {
    "openai": "OPENAI_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "dashscope": "DASHSCOPE_API_KEY",
}


def backends():
    items = []
    for backend, variable in KEY_VARS.items():
        key, url, model = Config.resolve(backend)
        items.append(
            {
                "name": backend,
                "key_var": variable,
                "key_set": bool(key),
                "model": model,
                "base_url": url,
                "supports_reasoning": backend != "dashscope",
                "supports_verbosity": backend != "dashscope",
            }
        )
    return items


def defaults():
    return {
        "backend": Config.BACKEND,
        "model": None,
        "use_tools": True,
        "reasoning": "low",
        "verbosity": None,
        "max_output_tokens": Config.DEFAULT_MAX_TOKENS,
        "poll_interval_ms": 1000,
        "max_records": 50,
    }


def resolve(settings, require_key=True):
    config = settings.model_dump()
    key, url, model = Config.resolve(settings.backend, settings.model)
    if require_key and not key:
        raise StateError("missing_key", f"请配置 {KEY_VARS[settings.backend]}")
    config.update(requested_model=model, base_url=url)
    config["max_output_tokens"] = (
        settings.max_output_tokens
        if settings.max_output_tokens is not None
        else Config.DEFAULT_MAX_TOKENS
    )
    return config, key


def make_agent(config, key):
    return GPT5NativeAgent(
        key or "PREVIEW-PLACEHOLDER",
        base_url=config["base_url"],
        model=config["requested_model"],
        backend=config["backend"],
    )


def options(config):
    kwargs = {
        "use_tools": config["use_tools"],
        "max_tokens": config["max_output_tokens"],
    }
    if config["backend"] != "dashscope":
        kwargs.update(
            reasoning_effort=config["reasoning"], verbosity=config["verbosity"]
        )
    return kwargs


def preview(store, data):
    if data.session_id:
        with store.lock:
            owner = store.get(data.session_id)
            if owner["kind"] != "conversation" or owner.get("evidence_limited"):
                raise StateError("invalid_session", "该记录不能续接或预览", 409)
            if owner["active_job_id"]:
                raise StateError("busy", "会话正在运行", 409)
            config = deepcopy(owner["config"])
            previous = owner["_agent"].previous_response_id
    else:
        config, _ = resolve(data.config, require_key=False)
        previous = None
    agent = make_agent(config, "PREVIEW-PLACEHOLDER")
    agent.previous_response_id = previous
    result = agent.process_request(data.input, dry_run=True, **options(config))
    return {**result, "base_url": config["base_url"]}


def run_turn(store, owner_id, job_id, text):
    with store.lock:
        owner = store.get(owner_id)
        agent = owner["_agent"]
        config = deepcopy(owner["config"])
        previous = agent.previous_response_id
        history = deepcopy(agent.conversation_history)
        offset = len(agent.api_turns)
        turn = {
            "id": new_id("turn"),
            "job_id": job_id,
            "input": text,
            "status": "running",
            "started_at": now(),
        }
        owner["turns"].append(turn)
        owner["title"] = text[:60]
    try:
        result = agent.process_request(text, **options(config))
    except Exception:
        agent.previous_response_id = previous
        agent.conversation_history = history
        raise
    if not result.get("success"):
        raw = result.get("raw_response")
        if isinstance(raw, dict) and raw.get("raw_text") == "":
            result["error"] = {
                "type": "empty_response",
                "message": "服务端返回空响应，请检查接入地址及服务状态",
            }
        if agent.api_turns[offset:]:
            result["http_status"] = agent.api_turns[-1].get("http_status")
        agent.previous_response_id = previous
        agent.conversation_history = history
    with store.lock:
        turn.update(
            result=result,
            api_turns=deepcopy(agent.api_turns[offset:]),
            finished_at=now(),
            status="completed" if result.get("success") else "error",
        )
    return (
        None
        if result.get("success")
        else (
            result.get("error")
            or {"code": "incomplete_response", "message": "响应未完成或没有答案"}
        )
    )


def run_experiment(store, owner_id, job_id, data):
    runs = []
    had_error = False
    for index, backend in enumerate(data.backends):
        store.progress(job_id, index, backend)
        try:
            run = experiment.run_backend(backend, data.reasoning)
        except Exception as exc:
            run = {
                "backend": backend,
                "started": False,
                "error": {"class": type(exc).__name__, "message": str(exc)},
            }
        runs.append(run)
        clarification = run.get("clarification") or {}
        results = [
            run.get("asean"),
            clarification.get("first"),
            clarification.get("second"),
        ]
        had_error |= not run.get("started") or any(
            r is not None and not r.get("success") for r in results
        )
        with store.lock:
            store.get(owner_id)["runs"] = deepcopy(runs)
        store.progress(job_id, index + 1, None)
    with store.lock:
        store.get(owner_id).update(
            acceptance=experiment.acceptance(runs), usage_summary=usage_summary(runs)
        )
    return (
        {"code": "backend_failed", "message": "部分后端执行失败，请查看完整实验报告"}
        if had_error
        else None
    )
