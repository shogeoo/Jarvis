import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from jarvis.capabilities.manager import CapabilityManager
from jarvis.core.protocol import ActionRequest
from jarvis.core.registry import ActionRegistry, EventRegistry
from jarvis.core.runtime import ActionResultTracker, EventBus
from jarvis.infrastructure.debug import Debugger


ACTION_CODE = """
from jarvis.capabilities import action_definition
from jarvis.core.protocol import object_schema


def run(data, context):
    return {"value": data["value"]}


def create_action():
    return action_definition(
        "run",
        object_schema({"value": {"type": "string"}}),
        object_schema({"value": {"type": "string"}}),
        run,
    )
"""

HANDLER_CODE = """
from jarvis.capabilities import event_definition, handler_definition
from jarvis.core.protocol import object_schema


IDLE = event_definition(
    "isolated.idle",
    "idle",
    object_schema({"value": {"type": "string"}}),
)


def start(context):
    context.stop_event.wait()


def create_handler():
    return handler_definition("idle handler", IDLE, start)
"""

MODULE_CODE = """
from jarvis.capabilities import module_definition

from .actions.run.action import create_action as run
from .handlers.idle.handler import create_handler as idle


def create_module():
    return module_definition(
        "isolated test",
        (run(),),
        (idle(),),
    )
"""

SIGNAL_ACTION = """
from pathlib import Path

from jarvis.capabilities import action_definition
from jarvis.core.protocol import object_schema


def run(data, context):
    return {"value": data["value"]}


def on_signal(name, data):
    Path(__file__).resolve().parent.joinpath("signals.txt").write_text(
        f"{name}:{data['value']}", encoding="utf-8"
    )


def create_action():
    return action_definition(
        "signal",
        object_schema({"value": {"type": "string"}}),
        object_schema({"value": {"type": "string"}}),
        run,
    )
"""


class _Agent:
    agent_id = "agent-001"
    name = "test"
    preset = "test"

    def __init__(self):
        self.results = []

    def enqueue_result(self, result):
        self.results.append(result)


class _Manager:
    def __init__(self, agent):
        self.agent = agent
        self.debug = Debugger(enabled=False)
        self.results = ActionResultTracker(debug=self.debug)

    def deliver_result(self, result):
        self.agent.enqueue_result(result)
        return True

    def report_capability_error(self, capability, error):
        self.debug.log("capability_error", capability=capability, error=str(error))


class UnitHostTests(unittest.TestCase):
    def _root(self, temporary: str) -> Path:
        root = Path(temporary)
        module = root / "modules" / "isolated"
        (module / "actions" / "run").mkdir(parents=True)
        (module / "handlers" / "idle").mkdir(parents=True)
        (module / "actions" / "run" / "action.py").write_text(
            ACTION_CODE, encoding="utf-8"
        )
        (module / "handlers" / "idle" / "handler.py").write_text(
            HANDLER_CODE, encoding="utf-8"
        )
        (module / "module.py").write_text(MODULE_CODE, encoding="utf-8")
        python = module / ".venv" / "bin" / "python"
        python.parent.mkdir(parents=True)
        os.symlink(sys.executable, python)
        return root

    def _manager(self, root: Path):
        actions, events = ActionRegistry(), EventRegistry()
        bus = EventBus(events, debug=Debugger(enabled=False))
        config = SimpleNamespace(project_root=Path.cwd(), jarvis_dir=root)
        manager = CapabilityManager(
            bus,
            actions,
            events,
            root=root,
            config=config,
            debug=Debugger(enabled=False),
        )
        fake = _Manager(_Agent())
        bus.manager = fake
        return manager, actions, fake

    def test_module_worker_returns_action_result(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = self._root(temporary)
            manager, actions, fake = self._manager(root)
            try:
                manager.load_module("isolated", start_handlers=True)
                manager.dispatch(
                    action=ActionRequest("isolated.run", {"value": "ok"}, "run-1"),
                    spec=actions.require("isolated.run"),
                    agent=fake.agent,
                )
                deadline = time.time() + 5
                while not fake.agent.results and time.time() < deadline:
                    time.sleep(0.01)
                self.assertEqual(
                    fake.agent.results[0].model_value(),
                    {
                        "type": "action_result",
                        "action_id": "run-1",
                        "data": {"value": "ok"},
                    },
                )
            finally:
                manager.shutdown()

    def test_standalone_action_unit_returns_result(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = self._root(temporary)
            unit = root / "actions" / "echo"
            unit.mkdir(parents=True)
            (unit / "action.py").write_text(ACTION_CODE, encoding="utf-8")
            python = unit / ".venv" / "bin" / "python"
            python.parent.mkdir(parents=True)
            os.symlink(sys.executable, python)
            manager, actions, fake = self._manager(root)
            try:
                manager.load_action("echo", start_handlers=True)
                manager.dispatch(
                    action=ActionRequest("echo", {"value": "hi"}, "echo-1"),
                    spec=actions.require("echo"),
                    agent=fake.agent,
                )
                deadline = time.time() + 5
                while not fake.agent.results and time.time() < deadline:
                    time.sleep(0.01)
                self.assertEqual(
                    fake.agent.results[0].model_value()["data"], {"value": "hi"}
                )
            finally:
                manager.shutdown()

    def test_signal_reaches_running_unit(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = self._root(temporary)
            unit = root / "actions" / "beacon"
            unit.mkdir(parents=True)
            (unit / "action.py").write_text(SIGNAL_ACTION, encoding="utf-8")
            python = unit / ".venv" / "bin" / "python"
            python.parent.mkdir(parents=True)
            os.symlink(sys.executable, python)
            manager, actions, fake = self._manager(root)
            try:
                manager.load_action("beacon", start_handlers=True)
                self.assertTrue(
                    manager.signal("action", "beacon", "ping", {"value": "42"})
                )
                deadline = time.time() + 5
                marker = unit / "signals.txt"
                while not marker.exists() and time.time() < deadline:
                    time.sleep(0.01)
                self.assertEqual(marker.read_text(encoding="utf-8"), "ping:42")
            finally:
                manager.shutdown()


if __name__ == "__main__":
    unittest.main()
