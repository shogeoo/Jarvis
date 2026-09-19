import os
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from jarvis.application import JarvisApplication
from jarvis.core.protocol import ActionRequest
from jarvis.infrastructure.config import Config
from jarvis.infrastructure.model_capabilities import ModelCapabilities


class _Receiver:
    agent_id = "agent-999"
    name = "receiver"
    preset = "main"

    def __init__(self):
        self.events = []

    def accepts_module(self, module_id):
        return module_id == "speech_output"

    def enqueue(self, event):
        self.events.append(event)


class ApplicationTests(unittest.TestCase):
    @patch.dict(os.environ, {"STT_ENABLED": "false", "TTS_ENABLED": "false"})
    @patch("jarvis.application.discover_model_capabilities")
    @patch("jarvis.application.OpenAI")
    def test_default_layout_starts_and_stops_without_hardware(
        self, openai, discover
    ):
        client = Mock()
        openai.return_value = client
        discover.return_value = ModelCapabilities("test", ("text",))
        config = Config(
            model="test",
            base_url="http://localhost/v1",
            api_key="test",
            project_root=Path.cwd(),
            jarvis_dir=Path.cwd() / ".jarvis",
        )

        app = JarvisApplication(config).start()
        try:
            self.assertEqual(app.main_agent.name, "main")
            self.assertEqual(app.main_agent.preset, "main")
            system_prompt = app.main_agent.history[0]["content"]
            self.assertIn('"module_id": "speech_output"', system_prompt)
            self.assertIn('"type": "speech_output.speak"', system_prompt)
            self.assertIn('"type": "structure_error"', system_prompt)
            self.assertIn('"type": "module_error"', system_prompt)
            self.assertNotIn('"module_id": "module_manager"', system_prompt)
            self.assertEqual(
                app.modules.loaded_names(),
                {"agents", "module_control", "speech_input", "speech_output"},
            )
            self.assertNotIn("module_manager", app.modules.loaded_names())
            other = app.agents.spawn(
                parent_id=app.main_agent.agent_id,
                name="other",
                preset="main",
            )
            other_agent = app.agents.require_agent(other["agent_id"])
            disabled = app.modules.disable_for_edit("speech_output")
            self.assertEqual(
                set(disabled["disabled_for"]),
                {app.main_agent.agent_id, other_agent.agent_id},
            )
            self.assertNotIn("speech_output", app.main_agent.modules())
            self.assertNotIn("speech_output", other_agent.modules())
            self.assertNotIn("speech_output", app.modules.loaded_names())
            restored = app.modules.enable_after_edit("speech_output")
            self.assertEqual(
                set(restored["restored_for"]),
                {app.main_agent.agent_id, other_agent.agent_id},
            )
            self.assertIn("speech_output", app.main_agent.modules())
            self.assertIn("speech_output", other_agent.modules())

            receiver = _Receiver()
            app.bus.bind(receiver)
            app.modules.dispatch(
                action=ActionRequest(
                    "speech_output.speak", {"text": "test"}
                ),
                spec=app.actions.require("speech_output.speak"),
                agent=receiver,
            )
            deadline = time.time() + 2
            while not receiver.events and time.time() < deadline:
                time.sleep(0.01)
            self.assertEqual(receiver.events[0].type, "speech_output.result")
            self.assertEqual(receiver.events[0].data["status"], "error")
        finally:
            app.stop()

        client.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
