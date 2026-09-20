import json
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
        "isolated.run",
        "run",
        object_schema({"value": {"type": "string"}}),
        object_schema({"value": {"type": "string"}}),
        run,
    )
"""

HANDLER_CODE = """
from jarvis.capabilities import handler_definition


def start(context):
    context.stop_event.wait()


def create_handler():
    return handler_definition("isolated.idle", "idle", (), start)
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


class IsolatedModuleTests(unittest.TestCase):
    def test_isolated_worker_returns_action_result(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            module = root / "modules" / "isolated"
            (module / "actions").mkdir(parents=True)
            (module / "handlers").mkdir()
            (module / "actions" / "run.py").write_text(ACTION_CODE, encoding="utf-8")
            (module / "handlers" / "idle.py").write_text(HANDLER_CODE, encoding="utf-8")
            (module / "module.json").write_text(
                json.dumps(
                    {
                        "module_id": "isolated",
                        "description": "isolated test",
                        "execution": "isolated",
                    }
                ),
                encoding="utf-8",
            )
            python = module / ".venv" / "bin" / "python"
            python.parent.mkdir(parents=True)
            os.symlink(sys.executable, python)

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
            agent = fake.agent
            agent.results.clear()
            try:
                manager.load_module("isolated", start_handlers=True)
                spec = actions.require("isolated.run")
                manager.dispatch(
                    action=ActionRequest(
                        "isolated.run", {"value": "ok"}, "run-1"
                    ),
                    spec=spec,
                    agent=agent,
                )
                deadline = time.time() + 5
                while not agent.results and time.time() < deadline:
                    time.sleep(0.01)
                self.assertEqual(
                    agent.results[0].model_value(),
                    {
                        "type": "action_result",
                        "action_id": "run-1",
                        "data": {"value": "ok"},
                    },
                )
            finally:
                manager.shutdown()


if __name__ == "__main__":
    unittest.main()
