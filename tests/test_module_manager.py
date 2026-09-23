import tempfile
import unittest
from pathlib import Path

from jarvis.capabilities.manager import CapabilityManager
from jarvis.core.registry import ActionRegistry, EventRegistry
from jarvis.core.runtime import EventBus
from jarvis.infrastructure.debug import Debugger
from jarvis.presets import PresetStore


ROOT = Path(__file__).resolve().parents[1]


class ModuleManagerContractTests(unittest.TestCase):
    def test_module_manager_has_only_its_management_actions_by_default(self):
        presets = PresetStore(ROOT / ".jarvis" / "presets")
        module_manager = presets.load("module_manager")
        main = presets.load("main")
        expected = {
            "send_message_to_agent",
            "read_file",
            "write_file",
            "edit_file",
            "execute_command",
            "list_capabilities",
            "capability_info",
            "set_capability_enabled",
        }
        self.assertEqual(set(module_manager.actions), expected)
        self.assertTrue((expected - {"send_message_to_agent"}).isdisjoint(main.actions))

    def test_capability_metadata_is_complete_without_loading_runtime(self):
        manager = CapabilityManager(
            EventBus(EventRegistry(), debug=Debugger(enabled=False)),
            ActionRegistry(),
            EventRegistry(),
            root=ROOT / ".jarvis",
            debug=Debugger(enabled=False),
        )
        try:
            existing = manager.list_existing()
            for item in existing["actions"]:
                self.assertTrue(item["description"])
                self.assertIn("globally_enabled", item)
                self.assertIn("globally_disabled", item)
        finally:
            manager.shutdown()

    def test_capability_logs_are_nested_under_runtime_logs(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "actions" / "sample").mkdir(parents=True)
            (root / "actions" / "sample" / "action.py").write_text(
                "from jarvis.capabilities import action_definition\n"
                "from jarvis.core.protocol import object_schema\n"
                "def run(data, context): return {}\n"
                "def create_action(): return action_definition('sample', object_schema({}), object_schema({}), run)\n",
                encoding="utf-8",
            )
            manager = CapabilityManager(
                EventBus(EventRegistry(), debug=Debugger(enabled=False)),
                ActionRegistry(),
                EventRegistry(),
                root=root,
                debug=Debugger(enabled=False),
            )
            try:
                manager.load_action("sample", start_handlers=True)
                self.assertTrue(manager._actions["sample"].host.stderr_stream.name.endswith("runtime/logs/action-sample.log"))
            finally:
                manager.shutdown()


if __name__ == "__main__":
    unittest.main()
