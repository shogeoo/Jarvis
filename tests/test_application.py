import os
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from jarvis.application import JarvisApplication
from jarvis.infrastructure.config import Config
from jarvis.infrastructure.model_capabilities import ModelCapabilities


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
            self.assertEqual(
                app.modules.loaded_names(),
                {"agents", "module_manager", "speech_input", "speech_output", "system"},
            )
        finally:
            app.stop()

        client.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
