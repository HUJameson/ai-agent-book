"""Background task lifecycle; every exit path releases the execution slot."""

import threading
from . import agent_runner
from .state import now


class JobManager:
    def __init__(self, store, thread_factory=threading.Thread):
        self.store, self.thread_factory = store, thread_factory

    def launch(self, job_id, action):
        def worker():
            error = None
            try:
                self.store.start(job_id)
                error = action()
            except Exception as exc:
                error = {"class": type(exc).__name__, "message": str(exc)}
                with self.store.lock:
                    owner = self.store.get(self.store.jobs[job_id]["owner_id"])
                    for turn in owner.get("turns", []):
                        if turn["job_id"] == job_id and turn["status"] == "running":
                            turn.update(
                                status="error",
                                finished_at=now(),
                                result={"success": False, "error": error},
                            )
            finally:
                self.store.finish(job_id, error)

        try:
            thread = self.thread_factory(target=worker, daemon=True)
            thread.start()
        except Exception as exc:
            self.store.finish(
                job_id,
                {
                    "class": type(exc).__name__,
                    "message": "无法启动任务线程：" + str(exc),
                },
            )

    def turn(self, owner_id, text):
        job_id = self.store.reserve("turn", owner_id)
        self.launch(
            job_id, lambda: agent_runner.run_turn(self.store, owner_id, job_id, text)
        )
        return job_id

    def experiment(self, data):
        # Create and reserve atomically, so rejected submissions leave no orphan history.
        with self.store.lock:
            if self.store.current_job_id:
                from .state import StateError

                raise StateError(
                    "busy", "已有任务执行中", 409, {"job_id": self.store.current_job_id}
                )
            owner = self.store.add("experiment", data.model_dump())
            job_id = self.store.reserve("experiment", owner["id"], len(data.backends))
        self.launch(
            job_id,
            lambda: agent_runner.run_experiment(self.store, owner["id"], job_id, data),
        )
        return job_id, owner["id"]
