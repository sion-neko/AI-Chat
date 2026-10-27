"""調査中のターンと、その進み具合のイベント。

調査は画面とは切り離して、サーバーのスレッドで最後まで走らせる。
進み具合はここに溜めておき、画面には /api/events の1本のストリームで配る。
画面をつなぎ直したとき（リロード・別の会話から戻ったとき）は、溜めた分から流し直す。

イベントは {"turn_id", "type", ...}。調査を始めると "job"（調査の中身の説明）が来て、
最後は "done" / "cancelled" / "error" のどれかで終わる（終わったら、ここからは消える）。
"""

import queue
import threading
import time
import uuid
from dataclasses import dataclass, field


@dataclass
class Job:
    turn_id: str
    conversation_id: str
    # 画面に出す調査の説明（質問、質問のターンの位置、続報か、など）
    info: dict
    cancel: threading.Event = field(default_factory=threading.Event)
    events: list[dict] = field(default_factory=list)


_lock = threading.Lock()
_jobs: dict[str, Job] = {}
_listeners: set[queue.Queue] = set()

FINISHED = {"done", "cancelled", "error"}


class Busy(Exception):
    """その会話はまだ調査中。"""


def start(conversation_id: str, info: dict) -> Job:
    """調査を登録して、始まったことを配る。同じ会話で調査中なら Busy。"""
    with _lock:
        if any(j.conversation_id == conversation_id for j in _jobs.values()):
            raise Busy
        job = Job(uuid.uuid4().hex, conversation_id, {**info, "started_at": time.time()})
        _jobs[job.turn_id] = job
    publish(job, {"type": "job", "conversation_id": conversation_id, **job.info})
    return job


def publish(job: Job, event: dict):
    """イベントを溜めて、つないでいる画面すべてに配る。終わりのイベントなら調査を外す。"""
    event = {"turn_id": job.turn_id, **event}
    with _lock:
        if event["type"] in FINISHED:
            _jobs.pop(job.turn_id, None)
        else:
            job.events.append(event)
        for q in _listeners:
            q.put(event)


def get(turn_id: str) -> Job | None:
    with _lock:
        return _jobs.get(turn_id)


def running() -> list[Job]:
    with _lock:
        return list(_jobs.values())


def subscribe() -> queue.Queue:
    """配られるイベントを受け取るキュー。今調査中のものは、溜めた分を先に入れておく。

    溜めた分のあとに {"type": "ready"} を入れる（画面はここで、つなぎ直す前の調査のうち終わったものを知る）。
    """
    q: queue.Queue = queue.Queue()
    with _lock:
        for job in _jobs.values():
            for event in job.events:
                q.put(event)
        q.put({"type": "ready"})
        _listeners.add(q)
    return q


def unsubscribe(q: queue.Queue):
    with _lock:
        _listeners.discard(q)
