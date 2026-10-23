"""Standalone native TaskGraph restore checks; never run an agent or provider.

Execute from the repository root with:
PYTHONPATH=matterloop-core/src/python python3 matterloop-agents/tests/native_taskgraph_restore_unittest.py

Use private package paths only in this standalone process to avoid unrelated
optional agents/runtime imports from matterloop_agents package __init__.
"""

from pathlib import Path
import sys
import types
import unittest

root = Path(__file__).resolve().parents[1] / "src/python/matterloop_agents"
for name, directory in (
    ("matterloop_agents", root),
    ("matterloop_agents.collaboration", root / "collaboration"),
):
    package = types.ModuleType(name)
    package.__path__ = [str(directory)]
    sys.modules[name] = package

from matterloop_agents.collaboration.errors import InvalidTaskGraphError
from matterloop_agents.collaboration.models import TaskSpec, TaskState, TaskStatus
from matterloop_agents.collaboration.task_graph import TaskGraph


class RestoredSnapshotTests(unittest.TestCase):
    def test_duplicate_state_entries_are_rejected(self):
        task = TaskSpec("a", "task", "python")
        states = (TaskState(task), TaskState(task))
        with self.assertRaisesRegex(InvalidTaskGraphError, "duplicate"):
            TaskGraph((task,), states=states)

    def test_conflicting_duplicate_entries_are_rejected(self):
        task = TaskSpec("a", "task", "python")
        states = (TaskState(task, status=TaskStatus.PENDING),
                  TaskState(task, status=TaskStatus.READY))
        with self.assertRaisesRegex(InvalidTaskGraphError, "duplicate"):
            TaskGraph((task,), states=states)

    def test_exact_once_snapshot_restores(self):
        task = TaskSpec("a", "task", "python")
        restored = TaskGraph((task,), states=TaskGraph((task,)).states())
        self.assertEqual(tuple(s.spec.task_id for s in restored.states()), ("a",))

    def test_missing_snapshot_entry_fails(self):
        task = TaskSpec("a", "task", "python")
        with self.assertRaisesRegex(InvalidTaskGraphError, "identifiers"):
            TaskGraph((task,), states=())


if __name__ == "__main__":
    unittest.main()
