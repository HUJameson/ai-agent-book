"""Public projections, secret redaction and evidence exports."""

import json
from collections.abc import Mapping
from config import Config

SENSITIVE = {
    "api_key",
    "apikey",
    "authorization",
    "proxy_authorization",
    "access_token",
    "password",
    "headers",
}


def redact(value):
    secrets = [
        getattr(Config, name, "")
        for name in ("OPENAI_API_KEY", "OPENROUTER_API_KEY", "DASHSCOPE_API_KEY")
    ]

    def clean(item):
        if isinstance(item, Mapping):
            return {
                str(k): clean(v)
                for k, v in item.items()
                if not str(k).startswith("_")
                and str(k).lower().replace("-", "_") not in SENSITIVE
            }
        if isinstance(item, (tuple, list)):
            return [clean(v) for v in item]
        if isinstance(item, str):
            for key in secrets:
                if key:
                    item = item.replace(key, "[REDACTED]")
            return item
        return item

    return clean(value)


def json_text(value):
    return json.dumps(redact(value), ensure_ascii=False, indent=2)


def byte_size(value):
    return len(json_text(value).encode("utf-8"))


def markdown(owner, turn_id=None):
    turns = owner.get("turns", [])
    if turn_id:
        turns = [t for t in turns if t["id"] == turn_id]
    parts = ["# 搜索与代码计算\n"]
    for turn in turns:
        parts.extend(
            [
                "## 用户任务\n",
                turn.get("input", ""),
                "\n## 模型回复\n",
                (turn.get("result") or {}).get("response") or "（无可用答案）",
            ]
        )
    return redact("\n\n".join(parts))


def usage_summary(runs):
    values = []
    for run in runs:
        if not run.get("started"):
            continue
        clarification = run.get("clarification") or {}
        for result in (
            run.get("asean"),
            clarification.get("first"),
            clarification.get("second"),
        ):
            if result is not None:
                values.append(result.get("usage") or {})
    fields = {}
    partial = False
    for name in ("input_tokens", "output_tokens", "total_tokens"):
        present = [u[name] for u in values if isinstance(u.get(name), (int, float))]
        fields[name] = sum(present) if present else None
        partial |= len(present) < len(values)
    fields["partial"] = partial
    return fields
