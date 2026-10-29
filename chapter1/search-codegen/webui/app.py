"""Local, single-worker WebUI. Run from search-codegen: python -m webui.app."""

import argparse
from pathlib import Path
from urllib.parse import urlsplit
from fastapi import FastAPI, Request, Query
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, FileResponse, Response
from fastapi.staticfiles import StaticFiles
from .schemas import SessionInput, TurnInput, PreviewInput, ExperimentInput
from .state import Store, StateError
from .job_manager import JobManager
from . import agent_runner as runner
from .serialization import redact, json_text, markdown

STATIC = Path(__file__).parent / "static"
SAMPLES = [
    {
        "id": "capitals",
        "title": "教材十国首都距离",
        "input": "针对教材选定的文莱、柬埔寨、印度尼西亚、老挝、马来西亚、缅甸、菲律宾、新加坡、泰国、越南十国，哪两个首都之间距离最近？请搜索坐标并用 Python 计算全部 45 对距离。",
        "use_tools": True,
    },
    {
        "id": "bitcoin",
        "title": "多轮澄清：比特币分析",
        "input": "请分析比特币近一个月的走势，并用 Python 计算技术指标。",
        "use_tools": True,
    },
    {
        "id": "connect",
        "title": "基础连通性",
        "input": "请只回答：连接成功",
        "use_tools": False,
    },
]


def create_app(store=None, manager=None):
    store = store or Store()
    manager = manager or JobManager(store)
    app = FastAPI(
        title="搜索与代码计算", docs_url=None, redoc_url=None, openapi_url=None
    )
    app.state.store, app.state.manager = store, manager

    @app.middleware("http")
    async def local_only(request: Request, call_next):
        host = request.url.hostname
        if host not in {"127.0.0.1", "localhost", "::1", "testserver"}:
            return JSONResponse(
                {"error": {"code": "invalid_host", "message": "仅支持本地访问"}},
                status_code=403,
            )
        if request.method in {"POST", "DELETE", "PUT", "PATCH"}:
            origin = request.headers.get("origin")
            if origin:
                parsed = urlsplit(origin)
                if (
                    parsed.scheme != request.url.scheme
                    or parsed.netloc != request.url.netloc
                ):
                    return JSONResponse(
                        {
                            "error": {
                                "code": "invalid_origin",
                                "message": "拒绝跨站请求",
                            }
                        },
                        status_code=403,
                    )
            if (
                request.headers.get("content-type", "").split(";")[0].lower()
                != "application/json"
            ):
                return JSONResponse(
                    {
                        "error": {
                            "code": "content_type",
                            "message": "请使用 application/json",
                        }
                    },
                    status_code=415,
                )
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' https:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
        )
        return response

    @app.exception_handler(StateError)
    async def state_error(request, exc):
        return JSONResponse(
            redact(
                {
                    "error": {
                        "code": exc.code,
                        "message": exc.message,
                        "details": exc.details,
                    }
                }
            ),
            status_code=exc.status,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        # Do not echo arbitrary input, objects or credentials inside validation errors.
        return JSONResponse(
            {
                "error": {
                    "code": "validation_error",
                    "message": "参数无效，请检查输入",
                    "details": [
                        {"field": ".".join(map(str, e["loc"])), "message": e["msg"]}
                        for e in exc.errors()
                    ],
                }
            },
            status_code=422,
        )

    @app.exception_handler(Exception)
    async def unexpected(request, exc):
        return JSONResponse(
            {
                "error": {
                    "code": "internal_error",
                    "message": "服务异常，请检查本地服务状态",
                }
            },
            status_code=500,
        )

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/api/defaults")
    def defaults():
        return redact(runner.defaults())

    @app.get("/api/backends")
    def backends():
        return redact(runner.backends())

    @app.get("/api/samples")
    def samples():
        return SAMPLES

    @app.post("/api/preview")
    def preview(data: PreviewInput):
        return redact(runner.preview(store, data))

    @app.post("/api/sessions", status_code=201)
    def session(data: SessionInput):
        config, key = runner.resolve(data.config)
        agent = runner.make_agent(config, key)
        owner = store.add("conversation", config, agent)
        return store.detail(owner["id"])

    @app.post("/api/sessions/{owner_id}/turns", status_code=202)
    def turn(owner_id: str, data: TurnInput):
        with store.lock:
            owner = store.get(owner_id)
            if owner["kind"] != "conversation":
                raise StateError("invalid_session", "该记录不是对话会话", 409)
            job_id = manager.turn(owner_id, data.input)
        return {"job_id": job_id, "session_id": owner_id}

    @app.post("/api/experiments", status_code=202)
    def experiments(data: ExperimentInput):
        for backend in data.backends:
            from .schemas import Settings

            runner.resolve(Settings(backend=backend))
        job_id, owner_id = manager.experiment(data)
        return {"job_id": job_id, "experiment_id": owner_id}

    @app.get("/api/jobs")
    def jobs():
        with store.lock:
            return (
                [store.snapshot(store.current_job_id)] if store.current_job_id else []
            )

    @app.get("/api/jobs/{job_id}")
    def job(job_id: str):
        return store.snapshot(job_id)

    @app.get("/api/history")
    def history():
        return redact(store.history())

    @app.get("/api/history/{owner_id}")
    def detail(owner_id: str):
        return store.detail(owner_id)

    @app.delete("/api/history/{owner_id}", status_code=204)
    def delete(owner_id: str):
        store.delete(owner_id)
        return Response(status_code=204)

    @app.delete("/api/history")
    def clear():
        return store.clear()

    @app.get("/api/exports/{owner_id}")
    def export(
        owner_id: str,
        format: str = Query("json", pattern="^(json|md)$"),
        turn_id: str | None = None,
    ):
        owner = store.detail(owner_id)
        if owner.get("evidence_limited"):
            raise StateError("evidence_limit_exceeded", "证据未保留，无法完整导出", 409)
        if owner["active_job_id"]:
            raise StateError("busy", "请等待任务完成后导出", 409)
        if format == "md" and owner["kind"] == "experiment":
            raise StateError("unsupported_format", "实验报告仅支持 JSON", 422)
        if turn_id:
            turn = next((t for t in owner.get("turns", []) if t["id"] == turn_id), None)
            if turn is None:
                raise StateError("not_found", "轮次不存在", 404)
        payload = (
            markdown(owner, turn_id)
            if format == "md"
            else json_text(turn if turn_id else owner)
        )
        return Response(
            payload,
            media_type="text/markdown" if format == "md" else "application/json",
            headers={
                "Content-Disposition": f'attachment; filename="search-codegen-{owner_id}.{format}"'
            },
        )

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app


app = create_app()

if __name__ == "__main__":
    import uvicorn

    parser = argparse.ArgumentParser(description="search-codegen 本地 WebUI")
    parser.add_argument("--port", type=int, default=8001)
    args = parser.parse_args()
    uvicorn.run(app, host="127.0.0.1", port=args.port, workers=1, log_level="warning")
