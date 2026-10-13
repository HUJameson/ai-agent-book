"""Web GUI 的唯一后端对接层（见 DESIGN.md 第 2 节）。

职责：按参数构建 WebSearchAgent、执行问答、组装响应、错误转换、历史存储。
约束：允许 import agent/config；不允许出现 HTTP 概念（状态码由 app.py 映射）。
"""

import os
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    from agent import WebSearchAgent, is_failure_answer, run_offline_demo
    from config import Config
except ImportError:  # 从仓库根目录等位置启动时的兜底
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from agent import WebSearchAgent, is_failure_answer, run_offline_demo
    from config import Config

# 与 main.py --provider 的取值保持一致
PROVIDERS = ("kimi", "offline-demo")

# 历史条目 FIFO 上限，见 DESIGN.md D2
HISTORY_LIMIT = 100

# 问答串行锁：WebSearchAgent 实例含可变状态，禁止并发（DESIGN.md D1）
RUN_LOCK = threading.Lock()


class MissingKeyError(RuntimeError):
    """provider=kimi 但没有任何可用 API Key（由 app.py 映射为 400）。"""


def has_any_key() -> bool:
    """预检是否存在任一可用凭据（与 agent 的解析顺序一致）。"""
    return bool(
        os.environ.get("MOONSHOT_API_KEY")
        or os.environ.get("KIMI_API_KEY")
        or os.environ.get("OPENROUTER_API_KEY")
    )


def default_options() -> Dict[str, Any]:
    """返回与 Config/agent 现状一致的默认参数（供 /api/defaults）。"""
    return {
        "provider": "kimi",
        "model": Config.DEFAULT_MODEL,
        "max_steps": Config.MAX_SEARCH_ITERATIONS,
        "temperature": 0.6,  # 与 agent.temperature 初值一致
        "timeout": Config.SEARCH_TIMEOUT,
    }


def run_question(question: str, options: Dict[str, Any]) -> Dict[str, Any]:
    """执行一次问答，返回不含 HTTP 概念的原始记录（id/created_at 由 HistoryStore 补齐）。

    调用方需持有 RUN_LOCK（offline-demo 分支不依赖锁，但统一加锁最简单安全）。
    """
    provider = options["provider"]
    started = time.monotonic()

    if provider == "offline-demo":
        result = run_offline_demo(question, verbose=False)
        answer = result["answer"]
        trace = result["trace"]
    else:
        if not has_any_key():
            raise MissingKeyError()
        # timeout 在 agent __init__ 时从 Config 读取，故先写回再构建（DESIGN.md D5）。
        # 仅在锁内修改，避免与其他请求的构建过程交错。
        Config.SEARCH_TIMEOUT = float(options["timeout"])
        agent = WebSearchAgent(model=options["model"], verbose=False)
        agent.set_temperature(options["temperature"])
        answer = agent.search_and_answer(
            question, max_iterations=int(options["max_steps"])
        )
        trace = agent.get_trace()

    elapsed = round(time.monotonic() - started, 3)
    status = "error" if is_failure_answer(answer) else "success"
    return {
        "question": question,
        "answer": answer,
        "trace": list(trace),
        "status": status,
        "elapsed_seconds": elapsed,
        "params": dict(options),
    }


def _new_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"q_{stamp}_{os.urandom(2).hex()}"


class HistoryStore:
    """内存历史：list 保时序（FIFO 淘汰），dict 按 id 索引（DESIGN.md D2）。"""

    def __init__(self, limit: int = HISTORY_LIMIT):
        self._limit = limit
        self._items: List[Dict[str, Any]] = []
        self._index: Dict[str, Dict[str, Any]] = {}

    def add(self, record: Dict[str, Any]) -> Dict[str, Any]:
        entry = dict(record)
        entry["id"] = _new_id()
        entry["created_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self._items.append(entry)
        self._index[entry["id"]] = entry
        while len(self._items) > self._limit:
            oldest = self._items.pop(0)
            self._index.pop(oldest["id"], None)
        return entry

    def list_summaries(self) -> List[Dict[str, Any]]:
        return [
            {
                "id": e["id"],
                "question": e["question"],
                "status": e["status"],
                "created_at": e["created_at"],
            }
            for e in reversed(self._items)  # 时间倒序
        ]

    def get(self, entry_id: str) -> Optional[Dict[str, Any]]:
        return self._index.get(entry_id)

    def clear(self) -> None:
        self._items.clear()
        self._index.clear()

    def __len__(self) -> int:
        return len(self._items)


# 全局共享存储（进程内单例）
STORE = HistoryStore()
