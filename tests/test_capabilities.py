import json
import tempfile
import unittest
from pathlib import Path

from jarvis.capabilities.manager import CapabilityManager
from jarvis.core.registry import ActionRegistry, EventRegistry
from jarvis.core.runtime import EventBus
from jarvis.infrastructure.debug import Debugger
from jarvis.presets import PresetStore

import fixtures


class CapabilityTests(unittest.TestCase):
    def manager(self, root):
        events = EventRegistry()
        actions = ActionRegistry()
        bus = EventBus(events, debug=Debugger(enabled=False))
        manager = CapabilityManager(
            bus,
            actions,
            events,
            root=root,
            debug=Debugger(enabled=False),
        )
        return manager, actions, events

    def test_standalone_action_loads_runs_and_unloads(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = fixtures.write_jarvis_root(Path(temporary))
            manager, actions, events = self.manager(root)
            try:
                summary = manager.load_action("say", start_handlers=False)
                self.assertEqual(summary["id"], "say")
                self.assertEqual(
                    summary["result_schema"]["properties"]["spoken"],
                    {"type": "boolean"},
                )
                spec = actions.require("say")
                self.assertEqual(
                    spec.run({"text": "ok"}, None),
                    {"spoken": True, "text": "ok"},
                )
                self.assertIn(
                    "say", actions.for_capabilities(modules=set(), actions={"say"})
                )
                self.assertNotIn(
                    "say", actions.for_capabilities(modules=set(), actions=set())
                )
            finally:
                manager.shutdown()

    def test_module_container_loads_prefixed_units(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = fixtures.write_jarvis_root(Path(temporary))
            fixtures.write_echo_module(root)
            manager, actions, events = self.manager(root)
            try:
                summary = manager.load_module("echo", start_handlers=False)
                self.assertEqual(summary["module_id"], "echo")
                self.assertEqual(
                    [action["id"] for action in summary["actions"]],
                    ["echo.repeat"],
                )
                spec = actions.require("echo.repeat")
                self.assertEqual(
                    spec.run({"value": "ok"}, None), {"value": "ok"}
                )
                self.assertIn(
                    "echo.echoed",
                    events.for_capabilities(modules={"echo"}, handlers=set()),
                )
            finally:
                manager.shutdown()

    def test_part_of_module_cannot_be_enabled_separately(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = fixtures.write_jarvis_root(Path(temporary))
            fixtures.write_echo_module(root)
            manager, actions, events = self.manager(root)
            try:
                with self.assertRaisesRegex(ValueError, "отдельно"):
                    manager.load_action("echo.repeat")
                with self.assertRaisesRegex(ValueError, "отдельно"):
                    manager.load_handler("echo.monitor")
            finally:
                manager.shutdown()

    def test_fixture_presets_only_reference_existing_capabilities(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = fixtures.write_jarvis_root(Path(temporary))
            fixtures.write_echo_module(root)
            manager, actions, events = self.manager(root)
            try:
                existing = {
                    "modules": manager.existing_modules(),
                    "actions": manager.existing_actions(),
                    "handlers": manager.existing_handlers(),
                }
                self.assertLessEqual(
                    {"echo"}, existing["modules"]
                )
                self.assertLessEqual({"say"}, existing["actions"])
                self.assertLessEqual({"tick"}, existing["handlers"])
                for preset in PresetStore(root / "presets").list():
                    self.assertLessEqual(set(preset.modules), existing["modules"])
                    self.assertLessEqual(set(preset.actions), existing["actions"])
                    self.assertLessEqual(set(preset.handlers), existing["handlers"])
                manager.load_module("echo", start_handlers=False)
                catalog = manager.catalog(
                    {"modules": {"echo"}, "actions": set(), "handlers": set()}
                )
                self.assertEqual(catalog["modules"][0]["module_id"], "echo")
                self.assertTrue(
                    all(
                        item["type"].startswith("echo.")
                        for item in catalog["modules"][0]["actions"]
                    )
                )
            finally:
                manager.shutdown()

    def test_module_environment_rejects_unpinned_dependencies(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = fixtures.write_jarvis_root(Path(temporary))
            module = fixtures.write_echo_module(root)
            manifest_path = module / "module.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest.update(
                {"execution": "isolated", "requirements": "requirements.txt"}
            )
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            (module / "requirements.txt").write_text("requests>=2\n", encoding="utf-8")
            manager, actions, events = self.manager(root)
            with self.assertRaisesRegex(ValueError, "закреплены"):
                manager.create_environment("echo")
            self.assertFalse((module / ".venv").exists())


if __name__ == "__main__":
    unittest.main()
