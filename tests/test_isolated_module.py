import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from jarvis.core.protocol import ActionRequest
from jarvis.core.registry import ActionRegistry, EventRegistry
from jarvis.core.runtime import EventBus
from jarvis.infrastructure.debug import Debugger
from jarvis.modules.manager import ModuleManager


MODULE_CODE = '''
from jarvis.core.protocol import object_schema
from jarvis.modules import ActionQueue, Module, action, event, event_handler

tasks = ActionQueue()

def run(ctx):
    while not ctx.stop_event.is_set():
        task = tasks.get()
        if task is not None:
            ctx.emit("isolated.done", {"value": task.data["value"]}, target=task.agent_id)

def stop(ctx):
    tasks.clear()
    tasks.close()

def create_module():
    done = event("isolated.done", "done", object_schema({"value": {"type": "string"}}))
    handler = event_handler("isolated.worker", "worker", (done,), run, stop=stop)
    return Module(
        module_id="isolated",
        description="isolated test",
        actions=(action("isolated.run", "run", object_schema({"value": {"type": "string"}}), tasks.submit),),
        handlers=(handler,),
    )
'''


class _Agent:
    agent_id = "agent-001"
    name = "test"
    preset = "test"

    def __init__(self):
        self.events = []

    def accepts_module(self, module_id):
        return module_id == "isolated"

    def enqueue(self, event):
        self.events.append(event)


class IsolatedModuleTests(unittest.TestCase):
    def test_local_venv_worker_returns_handler_event(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            module = root / "isolated"
            (module / "actions").mkdir(parents=True)
            (module / "handlers").mkdir()
            (module / "actions" / "__init__.py").write_text("", encoding="utf-8")
            (module / "handlers" / "__init__.py").write_text("", encoding="utf-8")
            (module / "module.py").write_text(MODULE_CODE, encoding="utf-8")
            (module / "module.json").write_text(
                json.dumps(
                    {
                        "module_id": "isolated",
                        "description": "isolated test",
                        "entrypoint": "module.py",
                        "factory": "create_module",
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
            manager = ModuleManager(
                bus,
                actions,
                events,
                modules_dir=root,
                config=config,
                debug=Debugger(enabled=False),
            )
            agent = _Agent()
            bus.bind(agent)
            try:
                manager.load("isolated", start_handlers=True)
                spec = actions.require("isolated.run")
                manager.dispatch(
                    action=ActionRequest("isolated.run", {"value": "ok"}),
                    spec=spec,
                    agent=agent,
                )
                deadline = time.time() + 3
                while not agent.events and time.time() < deadline:
                    time.sleep(0.01)
                self.assertEqual(agent.events[0].model_value(), {"type": "isolated.done", "data": {"value": "ok"}})
            finally:
                manager.shutdown()


if __name__ == "__main__":
    unittest.main()
