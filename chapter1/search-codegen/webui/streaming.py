"""Bounded, redacted, replayable observation of an independently running job."""

import asyncio
from collections import deque
from copy import deepcopy
import json
import threading
import time
from .serialization import redact
from config import Config


def encode(seq, kind, data):
    return f"id: {seq}\nevent: {kind}\ndata: {json.dumps(data,ensure_ascii=False)}\n\n"


class StreamLog:
    def __init__(self, job_id, owner_id, limit=512, budget=2 * 1024 * 1024):
        self.lock = threading.RLock()
        self.job_id, self.owner_id = job_id, owner_id
        self.limit, self.budget = limit, budget
        self.events = deque()
        self.size = 0
        self.seq = 0
        self.cut_keys = set()
        self.blocks = {}
        self.tools = {}
        self.raw = {}
        self.last = {}
        self.status = "queued"
        self.error = None
        self.truncated = False
        self.progress = {}
        self.subscribers = 0
        self.context = {}
        self.tail = (
            max(
                [
                    len(getattr(Config, k, ""))
                    for k in (
                        "OPENAI_API_KEY",
                        "OPENROUTER_API_KEY",
                        "DASHSCOPE_API_KEY",
                    )
                ]
                + [1]
            )
            - 1
        )

    def emit(self, kind, **data):
        with self.lock:
            self.seq += 1
            payload = redact(
                dict(
                    job_id=self.job_id,
                    owner_id=self.owner_id,
                    timestamp=time.time(),
                    **data,
                )
            )
            frame = encode(self.seq, kind, payload)
            length = len(frame.encode())
            if length > 128 * 1024:
                self.truncated = True
                payload = {
                    "job_id": self.job_id,
                    "owner_id": self.owner_id,
                    "preview_truncated": True,
                }
                frame = encode(self.seq, kind, payload)
                length = len(frame.encode())
            self.events.append((self.seq, frame, length))
            self.size += length
            while len(self.events) > self.limit or self.size > self.budget:
                self.size -= self.events.popleft()[2]

    def snapshot(self):
        with self.lock:
            return deepcopy(
                dict(
                    job_id=self.job_id,
                    owner_id=self.owner_id,
                    status=self.status,
                    error=self.error,
                    blocks=list(self.blocks.values()),
                    tools=list(self.tools.values()),
                    progress=self.progress,
                    through_seq=self.seq,
                    result_ref=self.owner_id,
                    preview_truncated=self.truncated,
                )
            )

    def publish_text(self, key, context, force=False):
        raw = self.raw[key]
        # Withhold a key-length tail before redaction; fragments never disclose a split key.
        safe = redact(raw)
        if key in self.cut_keys:
            force = False
        if not force and self.tail:
            safe = safe[: -self.tail] if len(safe) > self.tail else ""
        clipped = safe.encode()[:48000].decode("utf-8", errors="ignore")
        block = dict(
            item_key=key,
            text=clipped,
            truncated=key in self.cut_keys or len(clipped) < len(safe),
            revision=self.seq + 1,
            **context,
        )
        self.blocks[key] = block
        self.emit("answer.snapshot", **block)

    def observe(self, event, **context):
        with self.lock:
            typ = event.get("type", "")
            self.context.update(context)
            stage = f"{context.get('backend','')}:{context.get('stage','turn')}"
            key = (
                f"{stage}:{event.get('output_index',0)}:{event.get('content_index',0)}"
            )
            if typ in ("response.output_text.delta", "response.output_text.done"):
                if key not in self.raw and len(self.raw) >= 16:
                    self.truncated = True
                    return
                text = (
                    event.get("delta", "")
                    if typ.endswith("delta")
                    else event.get("text", "")
                )
                if not isinstance(text, str):
                    return
                if typ.endswith("delta") and key in self.cut_keys:
                    return
                raw = (self.raw.get(key, "") + text) if typ.endswith("delta") else text
                self.raw[key] = raw[:12000]
                if len(raw) > 12000:
                    self.cut_keys.add(key)
                    self.truncated = True
                if (
                    typ.endswith("done")
                    or time.monotonic() - self.last.get(key, 0) >= 0.1
                ):
                    self.publish_text(key, context, force=typ.endswith("done"))
                    self.last[key] = time.monotonic()
            item = event.get("item") or {}
            if isinstance(item, dict) and item.get("type") in (
                "web_search_call",
                "code_interpreter_call",
            ):
                call_key = f"{stage}:{item.get('id',event.get('output_index',0))}"
                if call_key not in self.tools and len(self.tools) >= 128:
                    self.truncated = True
                    return
                tool = dict(
                    call_id=call_key,
                    type=item["type"],
                    status=item.get("status", "added"),
                    **context,
                )
                self.tools[call_key] = tool
                self.emit("tool.state", **tool)
            # Some providers send dedicated status events without an item object.
            if typ.startswith(
                ("response.web_search_call.", "response.code_interpreter_call.")
            ):
                call_key = f"{stage}:{event.get('item_id',event.get('output_index',0))}"
                if call_key not in self.tools and len(self.tools) >= 128:
                    self.truncated = True
                    return
                tool = dict(
                    call_id=call_key,
                    type=typ.split(".")[1],
                    status=typ.rsplit(".", 1)[-1],
                    **context,
                )
                self.tools[call_key] = tool
                self.emit("tool.state", **tool)
            if typ in (
                "response.completed",
                "response.failed",
                "response.incomplete",
                "error",
            ):
                for k in list(self.raw):
                    if k.startswith(stage + ":"):
                        self.publish_text(k, context, force=True)

    def done(self, status, error=None):
        with self.lock:
            for key in list(self.raw):
                context = {
                    k: v
                    for k, v in self.blocks.get(key, {}).items()
                    if k in ("backend", "stage", "turn_id")
                }
                self.publish_text(key, context, force=True)
            self.raw.clear()
            self.status = status
            self.error = redact(error)
            self.emit(
                "job.done", status=status, error=self.error, result_ref=self.owner_id
            )

    def read(self, cursor):
        with self.lock:
            reset = (
                cursor is None
                or cursor > self.seq
                or (self.events and cursor < self.events[0][0] - 1)
            )
            if reset:
                return [
                    (self.seq, encode(self.seq, "stream.reset", self.snapshot()))
                ], self.status in ("completed", "error")
            return [
                (seq, frame) for seq, frame, _ in self.events if seq > cursor
            ], self.status in ("completed", "error")


async def event_frames(log, cursor, request, exists, heartbeat=15):
    last = time.monotonic()
    try:
        while exists():
            if await request.is_disconnected():
                break
            frames, done = log.read(cursor)
            for seq, frame in frames:
                cursor = seq
                last = time.monotonic()
                yield frame
            if done:
                break
            if time.monotonic() - last >= heartbeat:
                yield ": heartbeat\n\n"
                last = time.monotonic()
            await asyncio.sleep(0.05)
    finally:
        with log.lock:
            log.subscribers -= 1
