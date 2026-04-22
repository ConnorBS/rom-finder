"""In-memory activity tracker for background tasks (cover fetches, etc.).

Tasks are stored in a module-level dict so all coroutines share the same state.
Finished tasks are kept for 5 seconds before being cleaned up so the tray can
show a brief "done" confirmation before disappearing.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class ActivityTask:
    task_id: str
    label: str
    done: bool = False
    started_at: datetime = field(default_factory=datetime.utcnow)
    finished_at: Optional[datetime] = None


_tasks: dict[str, ActivityTask] = {}

_DONE_TTL = 5  # seconds to keep finished tasks visible


def start(task_id: str, label: str) -> None:
    _tasks[task_id] = ActivityTask(task_id=task_id, label=label)


def finish(task_id: str) -> None:
    if task_id in _tasks:
        _tasks[task_id].done = True
        _tasks[task_id].finished_at = datetime.utcnow()


def get_active() -> list[ActivityTask]:
    """Return current tasks, pruning any that finished more than TTL seconds ago."""
    now = datetime.utcnow()
    expired = [
        k for k, v in _tasks.items()
        if v.done and v.finished_at and (now - v.finished_at).total_seconds() > _DONE_TTL
    ]
    for k in expired:
        del _tasks[k]
    return list(_tasks.values())
