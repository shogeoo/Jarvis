"""Контракт capability-дерева: всё грузится, ID из файлов, один handler — один event."""

import unittest
from pathlib import Path

from jarvis.capabilities.manager import CapabilityManager
from jarvis.core.registry import ActionRegistry, EventRegistry
from jarvis.core.runtime import EventBus
from jarvis.infrastructure.debug import Debugger
from jarvis.presets import PresetStore

ROOT = Path(__file__).resolve().parents[1]


def _manager():
    actions, events = ActionRegistry(), EventRegistry()
    bus = EventBus(events, debug=Debugger(enabled=False))
    return CapabilityManager(
        bus, actions, events, root=ROOT, debug=Debugger(enabled=False)
    )


class CapabilityTreeTests(unittest.TestCase):
    def test_everything_loads_and_presets_are_valid(self):
        manager = _manager()
        try:
            snapshot = {
                "modules": manager.existing_modules(),
                "actions": manager.existing_actions(),
                "handlers": manager.existing_handlers(),
            }
            self.assertTrue(snapshot["actions"], "нет корневых действий")
            manager.load_snapshot(snapshot, start_handlers=False)
            self.assertEqual(manager.missing(snapshot), set())
            catalog = manager.catalog(
                {
                    "modules": manager.loaded_modules(),
                    "actions": manager.loaded_actions(),
                    "handlers": manager.loaded_handlers(),
                }
            )
            module_handlers = {
                handler["id"]
                for module in catalog["modules"]
                for handler in module["handlers"]
            }
            for preset in PresetStore(ROOT / "presets").list():
                self.assertLessEqual(set(preset.modules), snapshot["modules"])
                self.assertLessEqual(set(preset.actions), snapshot["actions"])
                self.assertLessEqual(
                    set(preset.handlers),
                    snapshot["handlers"] | module_handlers,
                )
        finally:
            manager.shutdown()

    def test_one_handler_one_event(self):
        manager = _manager()
        try:
            for handler_id in sorted(manager.existing_handlers()):
                summary = manager.load_handler(handler_id, start_handlers=False)
                self.assertEqual(len(summary["events"]), 1)
            for module_id in sorted(manager.existing_modules()):
                summary = manager.load_module(module_id, start_handlers=False)
                for handler in summary["handlers"]:
                    loaded = manager._handlers[handler["id"]]
                    self.assertIsNotNone(loaded.definition.event.type)
        finally:
            manager.shutdown()


if __name__ == "__main__":
    unittest.main()
