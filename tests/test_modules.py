import json
import tempfile
import unittest
from pathlib import Path

from jarvis.debug import Debugger
from jarvis.module_manager import ModuleManager
from jarvis.protocol import Event
from jarvis.registry import ActionRegistry, EventRegistry
from jarvis.runtime import EventBus


MODULE_CODE = """
from jarvis.module_api import Module, action
from .actions.run import run

def create_module():
    return Module(
        name="example",
        description="test",
        actions=(action("example.run", "run", {
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
            "additionalProperties": False,
        }, run),),
    )
"""


class ModuleTests(unittest.TestCase):
    def _create_module(self, root: Path) -> None:
        module_path = root / "example"
        (module_path / "actions").mkdir(parents=True)
        (module_path / "handlers").mkdir()
        (module_path / "actions" / "__init__.py").write_text("", encoding="utf-8")
        (module_path / "handlers" / "__init__.py").write_text("", encoding="utf-8")
        (module_path / "actions" / "run.py").write_text(
            "def run(data, ctx):\n    return {'value': data['value']}\n",
            encoding="utf-8",
        )
        (module_path / "module.json").write_text(
            json.dumps(
                {
                    "name": "example",
                    "description": "test",
                    "entrypoint": "module.py",
                    "factory": "create_module",
                }
            ),
            encoding="utf-8",
        )
        (module_path / "module.py").write_text(MODULE_CODE, encoding="utf-8")

    def test_module_may_contain_only_actions(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._create_module(root)
            event_registry = EventRegistry()
            action_registry = ActionRegistry()
            bus = EventBus(event_registry, debug=Debugger(enabled=False))
            manager = ModuleManager(
                bus,
                action_registry,
                event_registry,
                modules_dir=root,
                debug=Debugger(enabled=False),
            )
            summary = manager.load("example", start_handlers=False)
            self.assertEqual(summary["actions"][0]["type"], "example.run")
            self.assertEqual(summary["events"], [])
            result = action_registry.require("example.run").handler(
                {"value": "ok"}, None
            )
            self.assertEqual(result, {"value": "ok"})
            manager.unload("example")

    def test_module_is_global_and_agent_lists_control_access(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._create_module(root)
            event_registry = EventRegistry()
            action_registry = ActionRegistry()
            bus = EventBus(event_registry, debug=Debugger(enabled=False))
            manager = ModuleManager(
                bus,
                action_registry,
                event_registry,
                modules_dir=root,
                debug=Debugger(enabled=False),
            )
            manager.load("example", start_handlers=False)
            self.assertIn("example.run", action_registry.for_agent())
            self.assertIn("example.run", action_registry.for_agent({"example.run"}))
            self.assertNotIn("example.run", action_registry.for_agent(set()))
            manager.unload("example")


if __name__ == "__main__":
    unittest.main()
