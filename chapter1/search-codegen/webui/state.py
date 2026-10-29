"""Single-process records and atomic single-flight execution ownership."""

from copy import deepcopy
from datetime import datetime, timezone
import threading
import time
from uuid import uuid4
from .serialization import byte_size, redact


class StateError(Exception):
    def __init__(self, code, message, status=400, details=None):
        self.code, self.message, self.status, self.details = (
            code,
            message,
            status,
            details,
        )
        super().__init__(message)


def now():
    return datetime.now(timezone.utc).isoformat()


def new_id(prefix):
    return prefix + "_" + uuid4().hex


class Store:
    def __init__(
        self,
        max_records=50,
        owner_budget=16 * 1024 * 1024,
        total_budget=128 * 1024 * 1024,
    ):
        self.lock = threading.RLock()
        self.owners, self.jobs = {}, {}
        self.current_job_id = None
        self.max_records, self.owner_budget, self.total_budget = (
            max_records,
            owner_budget,
            total_budget,
        )

    def get(self, owner_id):
        if owner_id not in self.owners:
            raise StateError("not_found", "记录不存在，可能已重启或被回收", 404)
        return self.owners[owner_id]

    def add(self, kind, config=None, agent=None):
        with self.lock:
            owner_id = new_id("session" if kind == "conversation" else "experiment")
            owner = {
                "id": owner_id,
                "kind": kind,
                "title": "新会话" if kind == "conversation" else "实验 1-3",
                "created_at": now(),
                "updated_at": now(),
                "status": "empty",
                "config": config,
                "turns": [],
                "active_job_id": None,
                "_agent": agent,
            }
            self.owners[owner_id] = owner
            self._prune(protected=owner_id)
            return owner

    def detail(self, owner_id):
        with self.lock:
            return deepcopy(redact(self.get(owner_id)))

    def history(self):
        with self.lock:
            return [
                {
                    "id": o["id"],
                    "kind": o["kind"],
                    "title": o["title"],
                    "status": o["status"],
                    "updated_at": o["updated_at"],
                    "active_job_id": o["active_job_id"],
                    "empty": o["status"] == "empty",
                }
                for o in sorted(
                    self.owners.values(), key=lambda o: o["updated_at"], reverse=True
                )
            ]

    def reserve(self, kind, owner_id, total=0):
        with self.lock:
            if self.current_job_id:
                raise StateError(
                    "busy",
                    "已有任务执行中，请等待完成",
                    409,
                    {"job_id": self.current_job_id},
                )
            owner = self.get(owner_id)
            if owner.get("evidence_limited"):
                raise StateError(
                    "evidence_limit_exceeded", "证据容量超限，请新建会话", 409
                )
            job_id = new_id("job")
            job = {
                "id": job_id,
                "kind": kind,
                "owner_id": owner_id,
                "status": "queued",
                "created_at": now(),
                "started_at": None,
                "finished_at": None,
                "error": None,
                "progress": {"done": 0, "total": total, "current_backend": None},
                "result_ref": owner_id,
                "_start": time.monotonic(),
            }
            self.jobs[job_id] = job
            self.current_job_id = owner["active_job_id"] = job_id
            owner["status"] = "queued"
            return job_id

    def start(self, job_id):
        with self.lock:
            job = self.jobs[job_id]
            job.update(status="running", started_at=now())
            self.get(job["owner_id"])["status"] = "running"

    def progress(self, job_id, done, current):
        with self.lock:
            self.jobs[job_id]["progress"].update(done=done, current_backend=current)

    def snapshot(self, job_id):
        with self.lock:
            if job_id not in self.jobs:
                raise StateError("not_found", "任务不存在，可能已重启或被回收", 404)
            job = self.jobs[job_id]
            snap = deepcopy(redact(job))
            config = self.get(job["owner_id"]).get("config") or {}
            snap["backend"] = config.get("backend")
            snap["model"] = config.get("requested_model")
            snap["elapsed_seconds"] = job.get(
                "_elapsed", time.monotonic() - job["_start"]
            )
            return snap

    def finish(self, job_id, error=None):
        with self.lock:
            job = self.jobs[job_id]
            owner = self.get(job["owner_id"])
            try:
                if byte_size(owner) > self.owner_budget:
                    error = {
                        "code": "evidence_limit_exceeded",
                        "message": "证据未保留，无法完整导出；请新建会话",
                    }
                    for key in ("turns", "runs", "acceptance", "_agent"):
                        owner.pop(key, None)
                    owner["evidence_limited"] = True
                job.update(
                    status="error" if error else "completed",
                    error=redact(error),
                    finished_at=now(),
                    _elapsed=time.monotonic() - job["_start"],
                )
                owner.update(
                    status=job["status"], updated_at=now(), error=redact(error)
                )
            finally:
                owner["active_job_id"] = None
                if self.current_job_id == job_id:
                    self.current_job_id = None
            self._prune(protected=owner["id"])

    def delete(self, owner_id):
        with self.lock:
            owner = self.get(owner_id)
            if owner["active_job_id"]:
                raise StateError("busy", "运行中记录不能删除", 409)
            self.owners.pop(owner_id)
            for jid in [j for j, v in self.jobs.items() if v["owner_id"] == owner_id]:
                self.jobs.pop(jid)

    def clear(self):
        with self.lock:
            retained = [o["id"] for o in self.owners.values() if o["active_job_id"]]
            ids = [i for i in self.owners if i not in retained]
            for owner_id in ids:
                self.delete(owner_id)
            return {"deleted": len(ids), "retained_active_ids": retained}

    def _prune(self, protected=None):
        ended = sorted(
            [o for o in self.owners.values() if not o["active_job_id"]],
            key=lambda o: o["updated_at"],
        )
        while (
            len(ended) > self.max_records
            or sum(byte_size(o) for o in self.owners.values()) > self.total_budget
        ):
            victim = next((o for o in ended if o["id"] != protected), None)
            if victim is None:
                break
            self.delete(victim["id"])
            ended.remove(victim)
