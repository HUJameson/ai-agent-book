"""Kimi 上下文消融实验的 Web GUI 服务端（FastAPI）。

启动：python -m webui.app   （默认 http://127.0.0.1:8000）
职责边界见 DESIGN.md：本文件只做路由/状态码映射/静态挂载。
"""

import argparse
import logging
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

try:
    from webui import agent_runner
    from webui import job_manager
    from webui.agent_runner import MissingKeyError, STORE
    from webui.job_manager import EXECUTION_LOCK, JOBS, JobBusyError, spawn_ablation_job
except ImportError:  # 直接以脚本方式运行时的兜底
    import agent_runner
    import job_manager
    from agent_runner import MissingKeyError, STORE
    from job_manager import EXECUTION_LOCK, JOBS, JobBusyError, spawn_ablation_job

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

STATIC_DIR = Path(__file__).resolve().parent / "static"
CONTEXT_MODES = tuple(agent_runner.CONTEXT_MODES)

app = FastAPI(
    title="Context Ablation Agent GUI",
    description="上下文消融实验（实验 1-1）本地 Web 界面：单任务、消融套件与 provider 切换",
)


# --------------------------------------------------------------------------
# 请求模型（校验失败 → 422 + 中文 detail）
# --------------------------------------------------------------------------
class RunRequest(BaseModel):
    task: str = Field(min_length=1, max_length=8000)
    context_mode: Literal[
        "full", "no_history", "no_reasoning", "no_tool_calls", "no_tool_results"
    ] = "full"
    provider: str = Field(
        default=agent_runner.DEFAULT_PROVIDER, min_length=1, max_length=50
    )
    model: Optional[str] = Field(default=None, max_length=100)

    @field_validator("task")
    @classmethod
    def _strip_task(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("任务不能为空")
        return value


class AblationRequest(BaseModel):
    context_modes: List[
        Literal[
            "full", "no_history", "no_reasoning", "no_tool_calls", "no_tool_results"
        ]
    ] = Field(default_factory=lambda: list(CONTEXT_MODES), min_length=1)
    num_cases: int = Field(default=1, ge=1, le=4)
    provider: str = Field(
        default=agent_runner.DEFAULT_PROVIDER, min_length=1, max_length=50
    )
    model: Optional[str] = Field(default=None, max_length=100)


def _validate_provider(provider: str) -> None:
    if provider not in agent_runner.SUPPORTED_PROVIDERS:
        raise HTTPException(
            status_code=400,
            detail=f"未知 provider: {provider}。可用：{', '.join(agent_runner.SUPPORTED_PROVIDERS)}",
        )


# --------------------------------------------------------------------------
# 路由（错误映射表见 DESIGN 第 5 节）
# --------------------------------------------------------------------------
@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/defaults")
def get_defaults() -> Dict[str, Any]:
    return agent_runner.defaults()


@app.get("/api/providers")
def get_providers() -> List[Dict[str, Any]]:
    return agent_runner.providers_info()


@app.get("/api/samples")
def get_samples() -> List[Dict[str, Any]]:
    from main import get_sample_tasks

    return [
        {"name": s["name"], "description": s["description"], "task": s["task"]}
        for s in get_sample_tasks()
    ]


@app.get("/api/state")
def get_state() -> Dict[str, Any]:
    """执行权状态：前端据此禁用执行入口（DESIGN D2 的 UI 义务）。"""
    current = JOBS.current_id()
    return {
        "busy": JOBS.busy() or EXECUTION_LOCK.locked(),
        "current_job": current,
        "job_progress": (
            (JOBS.snapshot(current) or {}).get("progress") if current else None
        ),
    }


@app.post("/api/run")
def run_single(req: RunRequest) -> Dict[str, Any]:
    _validate_provider(req.provider)
    if not EXECUTION_LOCK.acquire(blocking=False):
        current = JOBS.current_id()
        detail = (
            f"消融任务正在运行（job {current}），完成前无法执行单任务。"
            if current
            else "另一个任务正在执行，请稍候。"
        )
        raise HTTPException(status_code=409, detail=detail)
    try:
        try:
            record = agent_runner.run_single_task(
                req.task, req.context_mode, req.provider, req.model
            )
        except MissingKeyError as exc:
            raise HTTPException(status_code=400, detail=f"无法执行：{exc}")
        except Exception as exc:  # 未预期异常统一 500
            logger.exception("单任务执行出错")
            raise HTTPException(status_code=500, detail=f"处理任务时出错: {exc}")
    finally:
        EXECUTION_LOCK.release()
    return STORE.add(record)


@app.post("/api/ablation")
def start_ablation(req: AblationRequest) -> Dict[str, Any]:
    _validate_provider(req.provider)
    params = {
        "provider": req.provider,
        "model": req.model,
        "context_modes": list(req.context_modes),
        "num_cases": req.num_cases,
    }
    try:
        job = spawn_ablation_job(params)
    except JobBusyError as exc:
        raise HTTPException(
            status_code=409,
            detail=f"已有消融任务在运行（job {exc}），完成前不能启动新任务。",
        )
    except Exception as exc:
        logger.exception("消融任务创建失败")
        raise HTTPException(status_code=500, detail=f"创建消融任务失败: {exc}")
    return {
        "job_id": job["job_id"],
        "status": job["status"],
        "progress": job["progress"],
    }


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str) -> Dict[str, Any]:
    snap = JOBS.snapshot(job_id)
    if snap is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return snap


@app.get("/api/report/{job_id}")
def get_report(job_id: str) -> Any:
    job = JOBS.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    if job["status"] != "completed" or not job.get("report_md"):
        raise HTTPException(status_code=409, detail="任务尚未完成，报告不可下载")
    from fastapi.responses import Response

    return Response(
        content=job["report_md"],
        media_type="text/markdown; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="ablation_report_{job_id}.md"'
        },
    )


@app.get("/api/history")
def get_history() -> Any:
    return STORE.list_summaries()


@app.get("/api/history/{entry_id}")
def get_history_detail(entry_id: str) -> Dict[str, Any]:
    entry = STORE.get(entry_id)
    if entry is None:
        raise HTTPException(
            status_code=404, detail="历史记录不存在（可能已被清空或淘汰）"
        )
    return entry


@app.delete("/api/history")
def clear_history() -> Dict[str, Any]:
    STORE.clear()
    return {"cleared": True}


@app.exception_handler(Exception)
async def unhandled_exception_handler(request, exc: Exception) -> JSONResponse:
    logger.exception("未处理异常")
    return JSONResponse(status_code=500, content={"detail": f"服务器内部错误: {exc}"})


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


def main() -> None:
    parser = argparse.ArgumentParser(description="上下文消融实验本地 Web GUI")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址（默认仅本机）")
    parser.add_argument("--port", type=int, default=8000, help="监听端口（默认 8000）")
    args = parser.parse_args()

    import uvicorn

    print(f"\n🧪 Context Ablation Agent GUI: http://{args.host}:{args.port}\n")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
