"""后台消融任务：JobStore + 线程 worker + 单飞控制（DESIGN D1-D3）。

执行权模型：EXECUTION_LOCK 是唯一的"执行权"令牌——单任务与消融 job
互斥。job 的占用在 create() 时同步登记（busy 标志），避免"检查时空闲、
线程尚未拿到锁"的创建竞态；单任务用非阻塞 acquire。
"""

import threading
from datetime import datetime, timezone
from typing import Any, Dict, Optional

try:
    from webui import agent_runner
except ImportError:  # 直接以脚本方式运行时的兜底
    import agent_runner


EXECUTION_LOCK = threading.Lock()

# 轮询进度时的快照条数上限（防止极端多案例把响应撑爆）
_SNAPSHOT_RESULTS_LIMIT = 80


class JobBusyError(RuntimeError):
    """已有执行流占用（单任务或消融 job）。"""


class JobStore:
    """进程内单飞任务存储：同一时刻至多一个 running job。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._jobs: Dict[str, Dict[str, Any]] = {}
        self._current_id: Optional[str] = None

    # -- 创建与查询 ----------------------------------------------------------
    def create(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """同步登记 busy 并返回 job 记录；已有任务时抛 JobBusyError。"""
        with self._lock:
            if self._current_id is not None:
                raise JobBusyError(self._current_id)
            job_id = _new_job_id()
            job = {
                "job_id": job_id,
                "status": "running",
                "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "params": params,
                "progress": {"done": 0, "total": 0, "current": None},
                "results": None,
                "analysis": None,
                "report_md": None,
                "log": "",
                "error": None,
                "_suite_out": {},  # agent_runner 写入 suite 引用，供进度推导
            }
            self._jobs[job_id] = job
            self._current_id = job_id
            return job

    def get(self, job_id: str) -> Optional[Dict[str, Any]]:
        return self._jobs.get(job_id)

    def busy(self) -> bool:
        return self._current_id is not None

    def current_id(self) -> Optional[str]:
        return self._current_id

    # -- 终态与快照 ----------------------------------------------------------
    def _finish(self, job_id: str, updates: Dict[str, Any], status: str) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            job.update(updates)
            job["status"] = status
            if self._current_id == job_id:
                self._current_id = None

    def complete(self, job_id: str, payload: Dict[str, Any]) -> None:
        self._finish(job_id, payload, "completed")

    def fail(self, job_id: str, error: str) -> None:
        self._finish(job_id, {"error": error}, "failed")

    def snapshot(self, job_id: str) -> Optional[Dict[str, Any]]:
        """轮询用的可序列化快照；running 时从 suite 引用推导实时进度。"""
        job = self._jobs.get(job_id)
        if job is None:
            return None
        snap = {
            "job_id": job["job_id"],
            "status": job["status"],
            "created_at": job["created_at"],
            "params": job["params"],
            "progress": dict(job["progress"]),
            "results": job["results"],
            "analysis": job["analysis"],
            "log": job["log"][-4000:],
            "error": job["error"],
        }
        if job["status"] == "running":
            suite = job["_suite_out"].get("suite")
            total = job["_suite_out"].get("total", 0)
            results = getattr(suite, "test_results", []) if suite else []
            snap["progress"] = {
                "done": len(results),
                "total": total,
                "current": _last_cell(results),
            }
            snap["results"] = agent_runner._trim_results(
                results[-_SNAPSHOT_RESULTS_LIMIT:]
            )
        else:
            done = len(job["results"] or [])
            snap["progress"] = {"done": done, "total": done, "current": None}
        return snap


def _last_cell(results):
    """最近一条结果的 (case, mode)，前端用它高亮"正在运行"的网格单元。"""
    if not results:
        return None
    latest = results[-1]
    return {
        "case_name": latest.get("case_name"),
        "context_mode": latest.get("context_mode"),
    }


def _new_job_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"job_{stamp}_{threading.get_ident() % 10000:04d}"


JOBS = JobStore()


def spawn_ablation_job(params: Dict[str, Any]) -> Dict[str, Any]:
    """创建并启动消融 job（同步登记 busy；线程内再取执行权）。"""
    job = JOBS.create(params)

    def worker():
        # busy 标志已在 create() 同步占用，这里阻塞等锁只会与单个单任务
        # 竞争极短窗口；执行期间持锁，保证与单任务互斥（DESIGN D3）。
        with EXECUTION_LOCK:
            try:
                payload = agent_runner.run_ablation_job(
                    provider=params["provider"],
                    model=params.get("model"),
                    context_modes=params["context_modes"],
                    num_cases=params["num_cases"],
                    suite_out=job["_suite_out"],
                )
                JOBS.complete(
                    job["job_id"],
                    {
                        "results": payload["results"],
                        "analysis": payload["analysis"],
                        "report_md": payload["report_md"],
                        "log": job["_suite_out"].get("log", ""),
                    },
                )
                # 消融结果统一入史（SPEC F4）：单任务与消融共用一条时间线
                agent_runner.STORE.add(
                    {
                        "kind": "ablation",
                        "status": "success",
                        "job_id": job["job_id"],
                        "params": params,
                        "results_summary": payload["results"],
                        "analysis": payload["analysis"],
                        "elapsed_seconds": None,
                    }
                )
            except Exception as exc:  # noqa: BLE001 - job 边界必须兜住一切
                JOBS.fail(job["job_id"], f"{type(exc).__name__}: {exc}")
                agent_runner.STORE.add(
                    {
                        "kind": "ablation",
                        "status": "error",
                        "job_id": job["job_id"],
                        "params": params,
                        "results_summary": [],
                        "analysis": None,
                        "error": f"{type(exc).__name__}: {exc}",
                        "elapsed_seconds": None,
                    }
                )

    thread = threading.Thread(
        target=worker, name=f"ablation-{job['job_id']}", daemon=True
    )
    thread.start()
    return job
