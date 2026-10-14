"""Web GUI 的唯一后端对接层（见 DESIGN.md 第 2 节）。

职责：单任务执行、消融套件程序化调用（复用 main.AblationTestSuite）、
provider 信息派生、历史存储。约束：不出现 HTTP 概念、不做线程管理。
"""

import os
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    from agent import ContextAwareAgent, ContextMode
    from config import (
        PROVIDERS,
        SUPPORTED_PROVIDERS,
        canonical_provider,
        resolve_backend,
    )
    from grounding import assess_groundedness, observation_quantities
except ImportError:  # 从仓库根目录等位置启动时的兜底
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from agent import ContextAwareAgent, ContextMode
    from config import (
        PROVIDERS,
        SUPPORTED_PROVIDERS,
        canonical_provider,
        resolve_backend,
    )
    from grounding import assess_groundedness, observation_quantities

# 历史条目 FIFO 上限（消融记录体量大，较 web-search-agent 收紧），见 SPEC 第 10 节
HISTORY_LIMIT = 50

CONTEXT_MODES = [mode.value for mode in ContextMode]
DEFAULT_PROVIDER = "doubao"

# 结果字段截断上限：tool result 可能携带整份 PDF 文本，GUI 展示不需要全文
_RESULT_SNIPPET_LIMIT = 4000


class MissingKeyError(RuntimeError):
    """provider 无法解析（key 缺失且无兜底），消息由注册表提供。"""


def ensure_provider_ready(provider: str, model: Optional[str] = None) -> None:
    """预检 provider 可用性，不可用抛 MissingKeyError（消息含缺失变量名）。"""
    try:
        resolve_backend(provider, model=model)
    except ValueError as exc:
        raise MissingKeyError(str(exc))


def resolve_api_key(provider: str, model: Optional[str] = None):
    """解析执行用的 api_key 实参（与 main.py 交互模式同规则）。

    OpenRouter 兜底路径返回 None，让 ContextAwareAgent 内部自行解析，
    避免把 OpenRouter key 当作 provider 自有 key 发出去。
    """
    backend = resolve_backend(provider, model=model)
    return None if backend.using_openrouter else backend.api_key


def providers_info() -> List[Dict[str, Any]]:
    """provider 清单，完全派生自注册表；只回传布尔，绝不回传 key 值。"""
    items = []
    for name in SUPPORTED_PROVIDERS:
        spec = PROVIDERS.get(canonical_provider(name))
        if spec is None:
            continue
        items.append(
            {
                "name": name,
                "default_model": spec.default_model,
                "requires_key": bool(spec.requires_key),
                "key_vars": list(spec.key_vars),
                "key_set": bool(spec.api_key()) if spec.requires_key else True,
            }
        )
    return items


def defaults() -> Dict[str, Any]:
    return {
        "provider": DEFAULT_PROVIDER,
        "model": None,
        "context_mode": ContextMode.FULL.value,
        "context_modes": list(CONTEXT_MODES),
        "num_cases": 1,
    }


def _snapshot(value: Any, limit: int = _RESULT_SNIPPET_LIMIT) -> Any:
    """JSON 化并截断任意工具结果，保证可序列化且体量可控。"""
    try:
        text = json_dumps(value)
    except (TypeError, ValueError):
        text = str(value)
    if len(text) > limit:
        text = text[:limit] + f"…（截断，共 {len(text)} 字符）"
    try:
        return json_loads(text)
    except ValueError:
        return text


def json_dumps(value: Any) -> str:
    import json

    return json.dumps(value, ensure_ascii=False, default=str)


def json_loads(text: str):
    import json

    return json.loads(text)


def _trim_results(results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """消融结果的 GUI 裁剪视图（DESIGN 第 3 节）。"""
    trimmed = []
    for r in results:
        trimmed.append(
            {
                "test_name": r.get("test_name"),
                "case_name": r.get("case_name"),
                "context_mode": r.get("context_mode"),
                "completed": bool(r.get("completed")),
                "grounding_verdict": r.get("grounding_verdict"),
                "unsupported_quantities": r.get("unsupported_quantities") or [],
                "iterations": r.get("iterations", 0),
                "num_tool_calls": r.get("num_tool_calls", 0),
                "execution_time": r.get("execution_time", 0),
                "error": r.get("error"),
            }
        )
    return trimmed


def run_single_task(
    task: str, context_mode: str, provider: str, model: Optional[str] = None
) -> Dict[str, Any]:
    """执行单任务并组装记录（调用方持有执行权，见 job_manager.EXECUTION_LOCK）。"""
    ensure_provider_ready(provider, model)
    mode = ContextMode(context_mode)
    api_key = resolve_api_key(provider, model)
    agent = ContextAwareAgent(api_key, mode, provider=provider, model=model)

    started = time.monotonic()
    result = agent.execute_task(task)
    elapsed = round(time.monotonic() - started, 2)

    # grounding 评估：从实发请求的消息里取观察值（与 main.run_single_test 同法）
    sent = [
        message
        for turn in result["trajectory"].api_turns
        for message in (turn.get("request") or {}).get("messages", [])
    ]
    grounding = assess_groundedness(
        result.get("final_answer"), task, observation_quantities(sent)
    )

    trajectory = result["trajectory"]
    error = result.get("error")
    return {
        "kind": "single",
        "task": task,
        "context_mode": context_mode,
        "final_answer": result.get("final_answer"),
        "completed": bool(result.get("completed")),
        "status": "error" if error else "success",
        "error": error,
        "elapsed_seconds": elapsed,
        "iterations": result.get("iterations", 0),
        "grounding": {
            "verdict": grounding["verdict"],
            "unsupported_quantities": grounding["unsupported_quantities"],
            "observation_count": grounding["observation_count"],
        },
        "trajectory": {
            "tool_calls": [
                {
                    "tool_name": tc.tool_name,
                    "arguments": _snapshot(tc.arguments),
                    "result": _snapshot(tc.result),
                    "timestamp": tc.timestamp,
                }
                for tc in trajectory.tool_calls
            ],
            "reasoning_steps_count": len(trajectory.reasoning_steps),
            "api_turns_count": len(trajectory.api_turns),
        },
    }


def make_cases(num_cases: int) -> Optional[List[Dict[str, str]]]:
    """与 CLI 语义一致：1 = 套件默认（Multinational Budget），>1 = 示例任务子集。"""
    if num_cases and num_cases > 1:
        from main import get_ablation_cases

        return get_ablation_cases(num_cases)
    return None


def run_ablation_job(
    provider: str,
    model: Optional[str],
    context_modes: List[str],
    num_cases: int,
    suite_out: Dict[str, Any],
) -> Dict[str, Any]:
    """执行消融套件（在 job 线程中运行；执行权由调用方持有）。

    suite_out 是 job_manager 传入的可变容器：本函数把 AblationTestSuite 实例
    放进去，轮询端点据此用 len(suite.test_results) 推导实时进度（套件无回调
    钩子，见 DESIGN 第 6 节）。
    """
    ensure_provider_ready(provider, model)
    from io import StringIO
    import contextlib

    from main import AblationTestSuite

    modes = [ContextMode(m) for m in context_modes]
    cases = make_cases(num_cases)
    total = len(modes) * (len(cases) if cases else 1)

    api_key = resolve_api_key(provider, model)
    suite = AblationTestSuite(api_key, provider=provider, model=model)
    suite_out["suite"] = suite
    suite_out["total"] = total

    # 套件会 print 表格与进度（run_ablation_study 内部），捕获为 job 日志，
    # 避免混入 uvicorn 输出，同时给前端"运行日志"面板提供内容。
    log_buffer = StringIO()
    with contextlib.redirect_stdout(log_buffer):
        results = suite.run_ablation_study(modes, cases=cases)
        analysis = suite.analyze_results(results)
        report = suite.generate_report(results, analysis)

    suite_out["log"] = log_buffer.getvalue()
    return {
        "results": _trim_results(results),
        "analysis": analysis,
        "report_md": report,
        "params": {
            "provider": provider,
            "model": model,
            "context_modes": context_modes,
            "num_cases": num_cases,
        },
    }


def _new_id(prefix: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{prefix}_{stamp}_{os.urandom(2).hex()}"


class HistoryStore:
    """内存历史：list 保时序（FIFO 淘汰），dict 按 id 索引（同 web-search-agent 模式）。"""

    def __init__(self, limit: int = HISTORY_LIMIT):
        self._limit = limit
        self._items: List[Dict[str, Any]] = []
        self._index: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()

    def add(self, record: Dict[str, Any]) -> Dict[str, Any]:
        entry = dict(record)
        entry["id"] = _new_id("r")
        entry["created_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with self._lock:
            self._items.append(entry)
            self._index[entry["id"]] = entry
            while len(self._items) > self._limit:
                oldest = self._items.pop(0)
                self._index.pop(oldest["id"], None)
        return entry

    def list_summaries(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [
                {
                    "id": e["id"],
                    "kind": e.get("kind"),
                    "title": (
                        e.get("task", "")[:60]
                        if e.get("kind") == "single"
                        else f"消融 {len(e.get('params', {}).get('context_modes', []))} 模式×{e.get('params', {}).get('num_cases', 1)} 案例"
                    ),
                    "status": e.get("status"),
                    "created_at": e.get("created_at"),
                }
                for e in reversed(self._items)
            ]

    def get(self, entry_id: str) -> Optional[Dict[str, Any]]:
        return self._index.get(entry_id)

    def clear(self) -> None:
        with self._lock:
            self._items.clear()
            self._index.clear()

    def __len__(self) -> int:
        return len(self._items)


STORE = HistoryStore()
