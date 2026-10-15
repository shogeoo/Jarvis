"""Контракт системных действий create_preset."""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from jarvis.capabilities.manager import CapabilityManager
from jarvis.core.registry import ActionRegistry, EventRegistry
from jarvis.core.runtime import EventBus
from jarvis.core.runtime import register_core_protocol
from jarvis.presets import PresetStore
from jarvis.infrastructure.debug import Debugger

import fixtures


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
        self.actions = ActionRegistry()
        register_core_protocol(self.actions, EventRegistry())
        self.state_manager = SimpleNamespace(
            actions=self.actions,
            presets=PresetStore(self.root / "presets"),
            capabilities=self.manager,
            refresh_presets=lambda preset_id: None,
            require_agent=lambda agent_id: SimpleNamespace(modules=lambda: {"agents"} if agent_id == "main" else set()),
        )

    def load_action(self, action_id):
        def run(data, context):
            data = {"description": "Test specialization.", **data}
            context.action_id = action_id
            context.agent_manager = SimpleNamespace(_manager=self.state_manager)
            context.capabilities = self.manager
            context.metadata = {"preset": "main"}
            return self.actions.require(action_id).run(data, context)
        return SimpleNamespace(run=run)

    def preset_context(self, agent_id):
        return SimpleNamespace(
            agent_id=agent_id,
            capabilities=self.manager,
            config=SimpleNamespace(jarvis_dir=self.root),
        )

    def test_create_preset_refuses_other_agents(self):
        module = self.load_action("agents.create_preset")
        with self.assertRaisesRegex(ValueError, "agents"):
            module.run(
            {
                "preset_id": "intruder",
                "person": "Not allowed.",
                "capabilities": [],
            },
            self.preset_context("agent-001"),
        )
        self.assertFalse((self.root / "presets" / "intruder").exists())

    def test_create_preset_rejects_unknown_capability_ids(self):
        module = self.load_action("agents.create_preset")
        result = module.run(
            {
                "preset_id": "broken",
                "person": "References nothing real.",
                "capabilities": ["missing_action", "missing_handler"],
            },
            self.preset_context("main"),
        )
        self.assertEqual(result["status"], "not_created")
        self.assertIn("missing_action", result["error"])
        self.assertFalse((self.root / "presets" / "broken").exists())

    def test_create_preset_accepts_existing_capability_ids(self):
        module = self.load_action("agents.create_preset")
        result = module.run(
            {
                "preset_id": "listener",
                "person": "Listen to ticks.",
                "capabilities": ["say", "tick"],
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
            (preset_dir / "preset.json").read_text(encoding="utf-8")
        )
        self.assertEqual(capabilities["capabilities"], ["say", "tick"])

    def test_create_preset_refuses_overwrite(self):
        module = self.load_action("agents.create_preset")
        context = self.preset_context("main")
        payload = {
            "preset_id": "listener",
            "person": "Original prompt.",
            "capabilities": ["say"],
        }
        self.assertEqual(module.run(payload, context)["status"], "created")
        payload["person_prompt"] = "Replacement prompt."
        result = module.run(payload, context)
        self.assertEqual(result["status"], "not_created")
        self.assertIn("существует", result["error"])
        self.assertEqual(
            (self.root / "presets" / "listener" / "person.txt").read_text(
                encoding="utf-8"
            ),
            "Original prompt.\n",
        )


if __name__ == "__main__":
    unittest.main()
