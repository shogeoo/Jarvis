import json
import tempfile
import unittest
from pathlib import Path

from jarvis.debug import Debugger
from jarvis.module_manager import ModuleManager
from jarvis.protocol import Event
from jarvis.registry import ActionRegistry, EventRegistry
from jarvis.runtime import EventBus


class ModuleTests(unittest.TestCase):
    def test_module_may_contain_only_actions(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            module_path = root / "example"
            module_path.mkdir()
            (module_path / "module.json").write_text(
                json.dumps(
                    {
                        "name": "example",
                        "version": "1.0.0",
                        "description": "test",
                        "entrypoint": "module.py",
                        "factory": "create_module",
                    }
                ),
                encoding="utf-8",
            )
            (module_path / "module.py").write_text(
                """
from jarvis.module_api import Module, action

def run(data, ctx):
    return {"value": data["value"]}

def create_module():
    return Module(
        name="example",
        version="1.0.0",
        description="test",
        actions=(action("example.run", "run", {
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
            "additionalProperties": False,
        }, run),),
    )
""",
                encoding="utf-8",
            )
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


if __name__ == "__main__":
    unittest.main()
