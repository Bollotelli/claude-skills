"""To-do list stored in ``~/.jarvis/aufgaben.json``."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from .storage import JsonStore


class TaskList:
    def __init__(self, path: Path):
        self.store = JsonStore(path)

    def add(self, title: str, due: str = "") -> dict:
        with self.store.lock:
            tasks = self.store.load()
            task = {
                "id": JsonStore.next_id(tasks),
                "title": title.strip(),
                "due": due.strip(),
                "done": False,
                "created": datetime.now().isoformat(timespec="seconds"),
            }
            tasks.append(task)
            self.store.save(tasks)
            return task

    def list(self, include_done: bool = False) -> list[dict]:
        tasks = self.store.load()
        return tasks if include_done else [t for t in tasks if not t.get("done")]

    def complete(self, task_id: int) -> dict | None:
        with self.store.lock:
            tasks = self.store.load()
            for task in tasks:
                if task.get("id") == task_id:
                    task["done"] = True
                    task["done_at"] = datetime.now().isoformat(timespec="seconds")
                    self.store.save(tasks)
                    return task
            return None
