import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from jarvis.application import JarvisApplication
from jarvis.core.protocol import ActionRequest
from jarvis.infrastructure.config import Config
from jarvis.infrastructure.context import MemoryStore
from jarvis.infrastructure.model_capabilities import ModelCapabilities

import fixtures


class ApplicationTests(unittest.TestCase):
    @patch("jarvis.application.speech_service")
    @patch("jarvis.application.discover_model_capabilities")
    @patch("jarvis.application.OpenAI")
    def test_default_layout_starts_and_stops_without_hardware(
        self, openai, discover, speech
    ):
        client = Mock()
        openai.return_value = client
        discover.return_value = ModelCapabilities("test", ("text",))
        with tempfile.TemporaryDirectory() as project_dir:
            project = Path(project_dir)
            fixtures.write_master_prompt(project)
            root = fixtures.write_jarvis_root(project / ".jarvis")
            config = Config(
                model="test",
                base_url="http://localhost/v1",
                api_key="test",
                project_root=project,
                jarvis_dir=root,
            )
            with tempfile.TemporaryDirectory() as temporary:
                memory = MemoryStore(Path(temporary))
                app = JarvisApplication(config, memory=memory).start()
                try:
                    self.assertEqual(app.main_agent.name, "Jarvis")
                    self.assertEqual(app.main_agent.preset, "main")
                    system_prompt = app.main_agent.history[0]["content"]
                    self.assertIn('"type": "say"', system_prompt)
                    self.assertIn('"type": "tick.event"', system_prompt)
                    self.assertIn('"type": "structure_error"', system_prompt)
                    self.assertIn(
                        '"type": "capability_error"', system_prompt
                    )
                    self.assertIn("action_result", system_prompt)
                    self.assertIn("environment", system_prompt)
                    self.assertEqual(
                        app.capabilities.loaded_snapshot(),
                        {
                            "modules": set(),
                            "actions": {"say"},
                            "handlers": {"tick"},
                        },
                    )
                    other = app.agents.spawn(
                        parent_id=app.main_agent.agent_id,
                        name="other",
                        preset="worker",
                    )
                    other_agent = app.agents.require_agent(other["agent_id"])
                    disabled = app.capabilities.disable_for_edit(
                        kind="action", capability_id="say"
                    )
                    self.assertEqual(
                        set(disabled["disabled_for"]),
                        {app.main_agent.agent_id, other_agent.agent_id},
                    )
                    self.assertNotIn("say", app.main_agent.standalone_actions())
                    restored = app.capabilities.enable_after_edit(
                        kind="action", capability_id="say"
                    )
                    self.assertEqual(
                        set(restored["restored_for"]),
                        {app.main_agent.agent_id, other_agent.agent_id},
                    )
                    self.assertIn("say", app.main_agent.standalone_actions())

                    app.capabilities.dispatch(
                        action=ActionRequest(
                            "say", {"text": "test"}, "say-1"
                        ),
                        spec=app.actions.require("say"),
                        agent=app.main_agent,
                    )
                    deadline = time.time() + 2
                    results = []
                    while time.time() < deadline:
                        results = [
                            json.loads(message["content"])
                            for message in app.main_agent.history
                            if message["role"] == "user"
                            and '"action_result"' in message["content"]
                        ]
                        if results:
                            break
                        time.sleep(0.01)
                    self.assertEqual(
                        results[0],
                        {
                            "type": "action_result",
                            "action_id": "say-1",
                            "data": {"spoken": True, "text": "test"},
                        },
                    )
                finally:
                    app.stop()

        client.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
