"""Clean-install contracts independent of user storage and external hardware."""

import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from jarvis.application import JarvisApplication
from jarvis.core.protocol import ActionRequest
from jarvis.core.prompts import read_master_prompt
from jarvis.infrastructure.config import Config, load_config
from jarvis.infrastructure.model_capabilities import ModelCapabilities
from jarvis.infrastructure.runtime_layout import ensure_runtime_layout
from jarvis.presets import PresetStore
from jarvis.speech.service import SpeechService
from test_runtime import _Client, _wait


class DistributionTests(unittest.TestCase):
    def test_config_paths_are_relative_to_environment_file_not_package(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            env = root / "custom.env"
            env.write_text(
                "LLM_MODEL=test\nJARVIS_DIR=storage\nLLM_REASONING_EFFORT=\n",
                encoding="utf-8",
            )
            with patch.dict(os.environ, {}, clear=True):
                config = load_config(env)
            self.assertEqual(config.project_root, root)
            self.assertEqual(config.jarvis_dir, root / "storage")
            self.assertIsNone(config.reasoning_effort)

    def test_baseline_resources_and_presets_do_not_need_installed_extensions(self):
        self.assertIn("action_definition", read_master_prompt())
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ensure_runtime_layout(root)
            store = PresetStore(root / "presets")
            self.assertTrue(store.load("main").protected)
            self.assertIn("Module Manager", store.load("module_manager").person_prompt)
            self.assertEqual(list((root / "actions").iterdir()), [])
            original = root / "presets" / "main" / "personprompt.txt"
            original.write_text("Personal prompt", encoding="utf-8")
            ensure_runtime_layout(root)
            self.assertEqual(original.read_text(), "Personal prompt")

    def test_packaged_environment_example_matches_repository_template(self):
        from importlib.resources import files

        self.assertEqual(
            files("jarvis").joinpath("assets", "env_example.txt").read_text(),
            (Path(__file__).resolve().parents[1] / ".env.example").read_text(),
        )

    @patch("jarvis.speech.service.STT_ENABLED", True)
    @patch("jarvis.speech.service.TTS_ENABLED", True)
    @patch("jarvis.speech.service.pipeline")
    @patch("jarvis.speech.service.Speaker")
    @patch("jarvis.speech.service.shutil.which", return_value=None)
    def test_missing_s2_keeps_stt_independent(self, which, speaker, pipeline):
        service = SpeechService()
        with tempfile.TemporaryDirectory() as temporary:
            service.start(Path(temporary), emit=Mock())
            self.assertFalse(service.can_speak)
            speaker.assert_not_called()
            pipeline.return_value.ensure_running.assert_called_once()
            self.assertTrue(service.available)
            service.shutdown()

    @patch("jarvis.application.speech_service")
    @patch("jarvis.application.shutil.which", return_value=None)
    @patch(
        "jarvis.application.discover_model_capabilities",
        return_value=ModelCapabilities("test", ("text",)),
    )
    @patch("jarvis.application.OpenAI")
    def test_fresh_start_without_s2_omits_speech_and_can_spawn_module_manager(
        self, openai, discovery, which, speech
    ):
        client = _Client([])
        client.close = Mock()
        openai.return_value = client
        speech.can_speak = False
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            app = JarvisApplication(
                Config("test", "http://invalid", "test", root, root / "storage")
            ).start()
            try:
                self.assertIsNotNone(app.main_agent)
                specs, _ = app.main_agent._contract()
                self.assertNotIn("speech", specs)
                self.assertIn("reply", specs)
                self.assertIn("speech_detected", app.events.all())
                self.assertFalse(app.capabilities._hosts)
                result = app.agents.spawn(
                    parent_id="main", name="builder", preset="module_manager"
                )
                developer = app.agents.require_agent(result["agent_id"])
                dev_specs, _ = developer._contract()
                for name in (
                    "read_file",
                    "write_file",
                    "edit_file",
                    "execute_command",
                    "capability_info",
                ):
                    self.assertIn(name, dev_specs)
                self.assertFalse(app.capabilities._hosts)
                marker = root / "command.started"
                command = f"printf ready > {marker}; sleep 100"
                app.capabilities.dispatch(
                    action=ActionRequest(
                        "execute_command",
                        {"command": command, "cwd": str(root)},
                        "long-command",
                    ),
                    spec=dev_specs["execute_command"],
                    agent=developer,
                )
                self.assertTrue(_wait(marker.exists))
                app.agents.delete(agent_id=developer.agent_id)
                self.assertFalse(
                    app.agents.processes._processes
                    and any(
                        proc.poll() is None for proc in app.agents.processes._processes
                    )
                )
                self.assertNotIn(developer.agent_id, app.agents.agents)
            finally:
                app.stop()

    def test_present_s2_omits_reply_even_when_tts_is_unavailable(self):
        for can_speak in (True, False):
            with self.subTest(can_speak=can_speak), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                client = _Client([])
                client.close = Mock()
                with patch("jarvis.application.OpenAI", return_value=client), patch(
                    "jarvis.application.discover_model_capabilities",
                    return_value=ModelCapabilities("test", ("text",)),
                ), patch("jarvis.application.shutil.which", return_value="/usr/bin/s2"), patch(
                    "jarvis.application.speech_service"
                ) as speech:
                    speech.can_speak = can_speak
                    app = JarvisApplication(Config("test", "http://invalid", "test", root, root / "storage")).start()
                    try:
                        specs, _ = app.main_agent._contract()
                        self.assertNotIn("reply", specs)
                        self.assertNotIn("reply", app.actions.all())
                        self.assertEqual("speech" in specs, can_speak)
                    finally:
                        app.stop()
