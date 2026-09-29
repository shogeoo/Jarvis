import json
import io
import os
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from jarvis.application import JarvisApplication
from jarvis.core.protocol import ActionRequest
from jarvis.infrastructure.config import Config
from jarvis.infrastructure.context import MemoryStore
from jarvis.infrastructure.model_capabilities import ModelCapabilities

import fixtures
from test_runtime import _Response


class ApplicationTests(unittest.TestCase):
    @patch("jarvis.application.speech_service")
    @patch("jarvis.application.discover_model_capabilities")
    @patch("jarvis.application.OpenAI")
    def test_default_layout_starts_and_stops_without_hardware(
        self, openai, discover, speech
    ):
        client = Mock()
        openai.return_value = client
        response_number = iter(range(1000))

        def model_response(**kwargs):
            call_id = f"application-noop-{next(response_number)}"
            content = json.dumps({
                "actions": [{"action_id": "no_action", "call_id": call_id, "data": {}}]
            })
            return _Response(content)

        client.chat.completions.create.side_effect = model_response
        discover.return_value = ModelCapabilities("test", ("text",))
        with tempfile.TemporaryDirectory() as project_dir:
            project = Path(project_dir)
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
                output = io.StringIO()
                app = JarvisApplication(config, memory=memory, stream=output).start()
                try:
                    self.assertEqual(
                        json.loads((root / "automations.json").read_text(encoding="utf-8")),
                        [],
                    )
                    self.assertEqual(app.main_agent.name, "Jarvis")
                    self.assertEqual(app.main_agent.preset, "main")
                    system_prompt = app.main_agent.history[0]["content"]
                    ordered_blocks = ["ENVIRONMENT:\n", "PERSON:\n", "MEMORY:\n", "MODEL INFO:\n", "CAPABILITIES:\n"]
                    positions = [system_prompt.index(block) for block in ordered_blocks]
                    self.assertEqual(positions, sorted(positions))
                    self.assertIn("Model ID: test", system_prompt)
                    self.assertIn('"entries": []', system_prompt)
                    self.assertIn("среде Jarvis", system_prompt)
                    self.assertIn("Текущая модель поддерживает следующие модальности: text", system_prompt)
                    self.assertIn('"action_id": "say"', system_prompt)
                    self.assertIn('"event_id": "tick.event"', system_prompt)
                    self.assertIn('"event_id": "structure_error"', system_prompt)
                    self.assertIn(
                        '"event_id": "capability_error"', system_prompt
                    )
                    self.assertIn("call_result", system_prompt)
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
                    paused = app.capabilities.toggle_global_state("action", "say")
                    self.assertEqual(paused["state"], "paused")
                    self.assertEqual(
                        set(paused["affected_agent_ids"]),
                        {app.main_agent.agent_id, other_agent.agent_id},
                    )
                    self.assertIn("say", app.main_agent.standalone_actions())
                    self.assertNotIn("say", app.capabilities.loaded_actions())
                    running = app.capabilities.toggle_global_state("action", "say")
                    self.assertEqual(running["state"], "running")
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
                            and '"call_result"' in message["content"]
                        ]
                        if results:
                            break
                        time.sleep(0.01)
                    self.assertEqual(
                        results[0],
                        {
                            "event_id": "call_result",
                            "call_id": "say-1",
                            "data": {"spoken": True, "text": "test"},
                        },
                    )
                    started = [
                        json.loads(message["content"])
                        for message in app.main_agent.history
                        if message["role"] == "user"
                        and '"system_started"' in message["content"]
                    ]
                    self.assertEqual(len(started), 1)
                    timestamp = datetime.fromisoformat(started[0]["data"]["datetime"])
                    self.assertIsNotNone(timestamp.tzinfo)
                    self.assertTrue(output.getvalue().startswith("Инициализация системы Jarvis....\nСистема инициализирована.\n\n{"))
                    self.assertNotIn("\n\n\n", output.getvalue())
                finally:
                    app.stop()

        client.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
