"""In-memory activity tracker for background tasks."""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class ActivityTask:
    task_id: str
    label: str
    done: bool = False
    task_type: str = "task"   # "cover", "rehash", "verify", "task"
    total: int = 0             # batch size; 0 = individual task
    completed: int = 0
    started_at: datetime = field(default_factory=datetime.utcnow)
    finished_at: Optional[datetime] = None

    @property
    def percent(self) -> int:
        if self.total <= 0:
            return 0
        return min(100, int(self.completed / self.total * 100))


_tasks: dict[str, ActivityTask] = {}
_DONE_TTL = 5  # seconds to keep finished tasks visible


def start(task_id: str, label: str, task_type: str = "task") -> None:
    _tasks[task_id] = ActivityTask(task_id=task_id, label=label, task_type=task_type)


def start_batch(task_id: str, label: str, total: int, task_type: str = "task") -> None:
    _tasks[task_id] = ActivityTask(
        task_id=task_id, label=label, total=total, task_type=task_type
    )


def increment(task_id: str) -> None:
    if task_id in _tasks:
        _tasks[task_id].completed += 1


def finish(task_id: str) -> None:
    if task_id in _tasks:
        _tasks[task_id].done = True
        _tasks[task_id].finished_at = datetime.utcnow()


def get_active() -> list[ActivityTask]:
    now = datetime.utcnow()
    # Auto-finish batch tasks where all items are done
    for task in _tasks.values():
        if not task.done and task.total > 0 and task.completed >= task.total:
            task.done = True
            task.finished_at = now
    # Prune expired done tasks
    expired = [
        k for k, v in _tasks.items()
        if v.done and v.finished_at and (now - v.finished_at).total_seconds() > _DONE_TTL
    ]
    for k in expired:
        del _tasks[k]
    return list(_tasks.values())


def get_card_states() -> dict:
    """Return per-card activity state keyed by card identifier for collection overlays."""
    tasks = get_active()
    card_states: dict[str, str] = {}
    batch_types: list[str] = []

    for task in tasks:
        if task.done:
            continue
        tid = task.task_id
        if tid.startswith("cover-lib-"):
            card_states[f"lib-{tid[len('cover-lib-'):]}"] = "cover"
        elif tid.startswith("cover-") and "-batch" not in tid:
            card_states[f"wanted-{tid[len('cover-'):]}"] = "cover"
        elif task.task_type in ("rehash", "verify") and task.total > 0:
            if task.task_type not in batch_types:
                batch_types.append(task.task_type)

    return {"states": card_states, "batch_types": batch_types}
