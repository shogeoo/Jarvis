import json
import tempfile
import unittest
from pathlib import Path

from jarvis.infrastructure.debug import Debugger
from jarvis.modules.manager import ModuleManager
from jarvis.core.registry import ActionRegistry, EventRegistry
from jarvis.core.runtime import EventBus
from jarvis.presets import PresetStore


MODULE_CODE = """
from jarvis.modules import Module, action
from .actions.run import run

def create_module():
    return Module(
        module_id="example",
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
                    "module_id": "example",
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

    def test_default_presets_only_reference_valid_modules(self):
        event_registry = EventRegistry()
        action_registry = ActionRegistry()
        bus = EventBus(event_registry, debug=Debugger(enabled=False))
        manager = ModuleManager(
            bus,
            action_registry,
            event_registry,
            modules_dir=Path(".jarvis/modules"),
            debug=Debugger(enabled=False),
        )
        try:
            existing = manager.existing_names()
            self.assertLessEqual(
                {"agents", "module_manager", "module_control", "speech_input", "speech_output"},
                existing,
            )
            for module_id in existing:
                self.assertEqual(manager.validate(module_id)["module"], module_id)
            for preset in PresetStore(Path(".jarvis/presets")).list():
                self.assertLessEqual(set(preset.modules), existing)
            manager.load("agents", start_handlers=False)
            catalog = manager.catalog({"agents"})
            self.assertEqual(catalog[0]["module_id"], "agents")
            self.assertTrue(
                all(
                    item["type"].startswith("agents.")
                    for item in catalog[0]["actions"] + catalog[0]["events"]
                )
            )
        finally:
            manager.shutdown()

    def test_module_is_global_and_presets_control_access_by_module_id(self):
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
            self.assertIn("example.run", action_registry.for_modules({"example"}))
            self.assertNotIn("example.run", action_registry.for_modules(set()))
            manager.unload("example")

    def test_module_environment_rejects_unpinned_dependencies(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._create_module(root)
            manifest_path = root / "example" / "module.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest.update(
                {"execution": "isolated", "requirements": "requirements.txt"}
            )
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            (root / "example" / "requirements.txt").write_text(
                "requests>=2\n", encoding="utf-8"
            )
            events = EventRegistry()
            manager = ModuleManager(
                EventBus(events, debug=Debugger(enabled=False)),
                ActionRegistry(),
                events,
                modules_dir=root,
                debug=Debugger(enabled=False),
            )
            with self.assertRaisesRegex(ValueError, "закреплены"):
                manager.create_environment("example")
            self.assertFalse((root / "example" / ".venv").exists())


if __name__ == "__main__":
    unittest.main()
