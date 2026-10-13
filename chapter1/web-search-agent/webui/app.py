"""Kimi Web Search Agent 的 Web GUI 服务端（FastAPI）。

启动：python -m webui.app   （默认 http://127.0.0.1:8000）
职责边界见 DESIGN.md：本文件只做路由/状态码映射/静态挂载，
agent 相关逻辑一律走 webui.agent_runner。
"""

import argparse
import logging
from pathlib import Path
from typing import Any, Dict, Literal

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

try:
    from webui import agent_runner
    from webui.agent_runner import RUN_LOCK, STORE, MissingKeyError
except ImportError:  # 直接以脚本方式运行时的兜底
    import agent_runner
    from agent_runner import RUN_LOCK, STORE, MissingKeyError

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

STATIC_DIR = Path(__file__).resolve().parent / "static"

app = FastAPI(
    title="Kimi Web Search Agent GUI",
    description="本地 Web 界面：ReAct 轨迹、参数设置与会话历史（详见 webui/SPEC.md）",
)


# --------------------------------------------------------------------------
# 请求/响应模型（校验失败由 FastAPI 自动映射为 422 + 中文 detail）
# --------------------------------------------------------------------------
class AskOptions(BaseModel):
    # 注意：此处的静态默认值仅兜底；前端初始化以 /api/defaults（Config 派生）为准
    provider: Literal["kimi", "offline-demo"] = "kimi"
    model: str = Field(default="kimi-k3", min_length=1, max_length=100)
    max_steps: int = Field(default=5, ge=1, le=10)
    temperature: float = Field(default=0.6, ge=0.0, le=2.0)
    timeout: float = Field(default=180, gt=0, le=3600)


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=4000)
    options: AskOptions = Field(default_factory=AskOptions)

    @field_validator("question")
    @classmethod
    def _strip_question(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("问题不能为空")
        return value


# --------------------------------------------------------------------------
# 路由（错误映射表见 DESIGN.md 第 5 节）
# --------------------------------------------------------------------------
@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/defaults")
def get_defaults() -> Dict[str, Any]:
    return agent_runner.default_options()


@app.post("/api/ask")
def ask(req: AskRequest) -> Dict[str, Any]:
    options = req.options.model_dump()
    if options["provider"] == "kimi" and not agent_runner.has_any_key():
        raise HTTPException(
            status_code=400,
            detail=(
                "未检测到可用 API Key（MOONSHOT_API_KEY / KIMI_API_KEY / "
                "OPENROUTER_API_KEY）。请配置环境变量后重启服务，"
                "或在参数面板切换为 offline-demo 模式体验流程。"
            ),
        )
    try:
        with RUN_LOCK:  # 串行执行，见 DESIGN.md D1
            record = agent_runner.run_question(req.question, options)
    except MissingKeyError:
        raise HTTPException(
            status_code=400,
            detail="未检测到可用 API Key，无法执行联网搜索。",
        )
    except Exception as exc:  # 未预期异常统一 500（DESIGN.md 第 5 节）
        logger.exception("处理问题时出错")
        raise HTTPException(status_code=500, detail=f"处理问题时出错: {exc}")
    return STORE.add(record)


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
    return JSONResponse(
        status_code=500, content={"detail": f"服务器内部错误: {exc}"}
    )


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Kimi Web Search Agent 本地 Web GUI"
    )
    parser.add_argument("--host", default="127.0.0.1", help="监听地址（默认仅本机）")
    parser.add_argument("--port", type=int, default=8000, help="监听端口（默认 8000）")
    args = parser.parse_args()

    import uvicorn

    print(f"\n🤖 Kimi Web Search Agent GUI: http://{args.host}:{args.port}\n")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
