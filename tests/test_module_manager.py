import tempfile
import unittest
import importlib.util
from pathlib import Path
from types import SimpleNamespace

from jarvis.capabilities.manager import CapabilityManager
from jarvis.core.registry import ActionRegistry, EventRegistry
from jarvis.core.runtime import EventBus
from jarvis.infrastructure.debug import Debugger
from jarvis.presets import PresetStore


ROOT = Path(__file__).resolve().parents[1]
MODULE_MANAGER_ACTIONS = {
    "send_message_to_agent",
    "read_file",
    "write_file",
    "edit_file",
    "execute_command",
    "list_capabilities",
    "capability_info",
    "toggle_capability",
}
RUNTIME_PRESENT = (
    (ROOT / ".jarvis" / "presets" / "module_manager" / "personprompt.txt").is_file()
    and (ROOT / ".jarvis" / "presets" / "main" / "personprompt.txt").is_file()
    and all(
        (ROOT / ".jarvis" / "actions" / action_id / "action.py").is_file()
        for action_id in MODULE_MANAGER_ACTIONS
    )
)


class ModuleManagerContractTests(unittest.TestCase):
    @unittest.skipUnless(RUNTIME_PRESENT, "External .jarvis runtime is not installed")
    def test_module_manager_prompt_contains_self_sufficient_sdk_contract(self):
        prompt = PresetStore(ROOT / ".jarvis" / "presets").load("module_manager").person_prompt
        for marker in (
            "НЕ читай исходники ядра Jarvis",
            "action_definition",
            "handler_definition",
            "module_definition",
            "context.emit",
            "object_schema",
            "create_module",
            "toggle_capability",
            "ДОЖДИСЬ call_result",
        ):
            self.assertIn(marker, prompt)

    def _load_action(self, action_id):
        path = ROOT / ".jarvis" / "actions" / action_id / "action.py"
        spec = importlib.util.spec_from_file_location(f"test_{action_id}_action", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    @unittest.skipUnless(RUNTIME_PRESENT, "External .jarvis runtime is not installed")
    def test_file_actions_accept_paths_outside_jarvis(self):
        reader = self._load_action("read_file")
        writer = self._load_action("write_file")
        editor = self._load_action("edit_file")
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "nested" / "outside.txt"
            context = SimpleNamespace()
            self.assertTrue(writer.run({"path": str(target), "content": "one\ntwo\nthree\n"}, context)["written"])
            self.assertEqual(
                reader.run({"path": str(target), "start_line": 2, "end_line": 3}, context)["content"],
                "2: two\n3: three",
            )
            self.assertTrue(editor.run({"path": str(target), "start_line": 2, "end_line": 2, "content": "changed"}, context)["edited"])
            self.assertEqual(target.read_text(encoding="utf-8"), "one\nchanged\nthree\n")

    @unittest.skipUnless(RUNTIME_PRESENT, "External .jarvis runtime is not installed")
    def test_execute_command_runs_and_captures_both_streams(self):
        module = self._load_action("execute_command")
        with tempfile.TemporaryDirectory() as temporary:
            context = SimpleNamespace(config=SimpleNamespace(project_root=Path(temporary)))
            result = module.run(
                {
                    "command": "printf 'out'; printf 'err' >&2; exit 7",
                    "cwd": None,
                },
                context,
            )
            self.assertEqual(result, {"exit_code": 7, "stdout": "out", "stderr": "err"})

    @unittest.skipUnless(RUNTIME_PRESENT, "External .jarvis runtime is not installed")
    def test_execute_command_accepts_cwd_outside_project(self):
        module = self._load_action("execute_command")
        with tempfile.TemporaryDirectory() as temporary:
            context = SimpleNamespace(config=SimpleNamespace(project_root=ROOT))
            result = module.run({"command": "pwd", "cwd": temporary}, context)
            self.assertEqual(result["exit_code"], 0)
            self.assertEqual(result["stdout"].strip(), temporary)

    @unittest.skipUnless(RUNTIME_PRESENT, "External .jarvis runtime is not installed")
    def test_module_manager_has_only_its_management_actions_by_default(self):
        presets = PresetStore(ROOT / ".jarvis" / "presets")
        module_manager = presets.load("module_manager")
        main = presets.load("main")
        self.assertEqual(set(module_manager.actions), MODULE_MANAGER_ACTIONS)
        self.assertTrue(
            (MODULE_MANAGER_ACTIONS - {"send_message_to_agent"}).isdisjoint(main.actions)
        )

    @unittest.skipUnless(RUNTIME_PRESENT, "External .jarvis runtime is not installed")
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
                self.assertIn("globally_running", item)
                self.assertIn("globally_paused", item)
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
