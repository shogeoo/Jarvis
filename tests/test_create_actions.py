"""Контракт файловых действий create_automation и create_preset."""

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from jarvis.capabilities.manager import CapabilityManager
from jarvis.core.registry import ActionRegistry, EventRegistry
from jarvis.core.runtime import EventBus
from jarvis.infrastructure.automations import AutomationStore
from jarvis.infrastructure.debug import Debugger

import fixtures


ROOT = Path(__file__).resolve().parents[1]


def _action_source(action_id):
    source = ROOT / ".jarvis" / "actions" / action_id / "action.py"
    if source.is_file():
        return source.read_text(encoding="utf-8")
    return fixtures.CONTROL_ACTIONS[action_id]


class CreateActionsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = fixtures.write_jarvis_root(Path(temporary.name))
        self.manager = CapabilityManager(
            EventBus(EventRegistry(), debug=Debugger(enabled=False)),
            ActionRegistry(),
            EventRegistry(),
            root=self.root,
            debug=Debugger(enabled=False),
        )
        self.addCleanup(self.manager.shutdown)

    def load_action(self, action_id):
        path = self.root / "actions" / action_id / "action.py"
        fixtures.write_action(self.root / "actions", action_id, _action_source(action_id))
        spec = importlib.util.spec_from_file_location(
            f"create_actions_{action_id}", path
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def automation_context(self, agent_id):
        return SimpleNamespace(
            agent_id=agent_id,
            config=SimpleNamespace(jarvis_dir=self.root),
        )

    def preset_context(self, agent_id):
        return SimpleNamespace(
            agent_id=agent_id,
            capabilities=self.manager,
            config=SimpleNamespace(jarvis_dir=self.root),
        )

    def test_create_automation_refuses_other_agents(self):
        module = self.load_action("create_automation")
        with self.assertRaisesRegex(ValueError, "main"):
            module.run(
                {
                    "event": {"handler_id": "tick", "data": {"text": "go"}},
                    "actions": [{"action_id": "say", "data": {"text": "ok"}}],
                },
                self.automation_context("agent-001"),
            )
        self.assertFalse((self.root / "automations.json").exists())

    def test_create_automation_appends_validated_rule_for_main(self):
        module = self.load_action("create_automation")
        rule = {
            "event": {"handler_id": "tick", "data": {"text": "go"}},
            "actions": [{"action_id": "say", "data": {"text": "ok"}}],
        }
        result = module.run(rule, self.automation_context("main"))
        self.assertEqual(result, {"status": "created"})
        self.assertEqual(AutomationStore(self.root / "automations.json").list(), [rule])

    def test_create_automation_rejects_malformed_rule(self):
        module = self.load_action("create_automation")
        with self.assertRaises(ValueError):
            module.run(
                {
                    "event": {"handler_id": "tick", "data": {"text": "go"}},
                    "actions": [{"action_id": "say", "call_id": "smuggled", "data": {}}],
                },
                self.automation_context("main"),
            )
        self.assertEqual(AutomationStore(self.root / "automations.json").list(), [])

    def test_create_preset_refuses_other_agents(self):
        module = self.load_action("create_preset")
        result = module.run(
            {
                "preset_id": "intruder",
                "person_prompt": "Not allowed.",
                "actions": [],
                "handlers": [],
                "modules": [],
            },
            self.preset_context("agent-001"),
        )
        self.assertEqual(result["status"], "not_created")
        self.assertIn("main", result["error"])
        self.assertFalse((self.root / "presets" / "intruder").exists())

    def test_create_preset_rejects_unknown_capability_ids(self):
        module = self.load_action("create_preset")
        result = module.run(
            {
                "preset_id": "broken",
                "person_prompt": "References nothing real.",
                "actions": ["missing-action"],
                "handlers": ["missing-handler"],
                "modules": [],
            },
            self.preset_context("main"),
        )
        self.assertEqual(result["status"], "not_created")
        self.assertIn("missing-action", result["error"])
        self.assertFalse((self.root / "presets" / "broken").exists())

    def test_create_preset_accepts_existing_capability_ids(self):
        module = self.load_action("create_preset")
        result = module.run(
            {
                "preset_id": "listener",
                "person_prompt": "Listen to ticks.",
                "actions": ["say"],
                "handlers": ["tick"],
                "modules": [],
            },
            self.preset_context("main"),
        )
        self.assertEqual(
            result,
            {"status": "created", "preset_id": "listener", "error": None},
        )
        preset_dir = self.root / "presets" / "listener"
        self.assertTrue(preset_dir.is_dir())
        capabilities = json.loads(
            (preset_dir / "capabilities.json").read_text(encoding="utf-8")
        )
        self.assertEqual(capabilities["actions"], ["say"])
        self.assertEqual(capabilities["handlers"], ["tick"])

    def test_create_preset_refuses_overwrite(self):
        module = self.load_action("create_preset")
        context = self.preset_context("main")
        payload = {
            "preset_id": "listener",
            "person_prompt": "Original prompt.",
            "actions": ["say"],
            "handlers": [],
            "modules": [],
        }
        self.assertEqual(module.run(payload, context)["status"], "created")
        payload["person_prompt"] = "Replacement prompt."
        result = module.run(payload, context)
        self.assertEqual(result["status"], "not_created")
        self.assertIn("существует", result["error"])
        self.assertEqual(
            (self.root / "presets" / "listener" / "personprompt.txt").read_text(
                encoding="utf-8"
            ),
            "Original prompt.\n",
        )


if __name__ == "__main__":
    unittest.main()
