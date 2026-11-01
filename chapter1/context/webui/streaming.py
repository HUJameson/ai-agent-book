"""GUI-only model streaming adapter; the core Agent still receives a complete response."""
from types import SimpleNamespace
from openai.types.chat import ChatCompletion


def streaming_client(client, emit):
    iteration = 0

    def create(**kwargs):
        nonlocal iteration
        iteration += 1
        emit({"type": "turn", "iteration": iteration})
        content, reasoning, calls = [], [], {}
        last, finish, usage = {}, None, None
        with client.chat.completions.create(**kwargs, stream=True) as stream:
            for chunk in stream:
                last = chunk.model_dump()
                if last.get("usage"):
                    usage = last["usage"]
                for choice in last.get("choices", []):
                    if choice.get("index", 0) != 0:
                        continue
                    delta = choice.get("delta") or {}
                    for field, parts in (("content", content), ("reasoning_content", reasoning)):
                        if delta.get(field):
                            parts.append(delta[field])
                            emit({"type": "delta", "iteration": iteration,
                                  "field": "reasoning" if field == "reasoning_content" else "content",
                                  "text": delta[field]})
                    for tc in delta.get("tool_calls") or []:
                        call = calls.setdefault(tc["index"], {"id": "", "type": "function",
                                                            "function": {"name": "", "arguments": ""}})
                        if tc.get("id"):
                            call["id"] = tc["id"]
                        for field in ("name", "arguments"):
                            call["function"][field] += (tc.get("function") or {}).get(field) or ""
                    finish = choice.get("finish_reason") or finish
        if finish is None:
            raise RuntimeError("模型流提前结束，未收到完成标记")
        return ChatCompletion.model_validate({
            "id": last.get("id", "stream"), "object": "chat.completion",
            "created": last.get("created", 0), "model": last.get("model", kwargs.get("model", "")),
            "usage": usage,
            "choices": [{"index": 0, "finish_reason": finish, "logprobs": None,
                         "message": {"role": "assistant", "content": "".join(content),
                                     "reasoning_content": "".join(reasoning),
                                     "tool_calls": [calls[i] for i in sorted(calls)] or None}}],
        })

    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
