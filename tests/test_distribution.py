"""Personal configuration contracts isolated from live storage and hardware."""

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
from jarvis.infrastructure.config import Config, load_config, ROOT, DEFAULT_JARVIS_DIR
from jarvis.infrastructure.model_capabilities import ModelCapabilities
from jarvis.infrastructure.runtime_layout import ensure_runtime_layout
from jarvis.presets import PresetStore
from jarvis.speech.service import SpeechService
from test_runtime import _Client, _wait


class DistributionTests(unittest.TestCase):
    def test_reasoning_effort_is_optional_and_configurable(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.assertIsNone(Config("test", None, None, root, root).reasoning_effort)
            for value, expected in ((None, None), ("", None), ("high", "high")):
                with self.subTest(value=value):
                    env = root / ".env"
                    env.write_text("LLM_MODEL=test\n" + (f"LLM_REASONING_EFFORT={value}\n" if value is not None else ""))
                    with patch.dict(os.environ, {}, clear=True):
                        self.assertEqual(load_config(env).reasoning_effort, expected)

    def test_config_storage_is_fixed_at_project_root(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            env = root / "custom.env"
            env.write_text(
                "LLM_MODEL=test\nJARVIS_DIR=storage\nLLM_REASONING_EFFORT=\n",
                encoding="utf-8",
            )
            with patch.dict(os.environ, {}, clear=True):
                config = load_config(env)
            self.assertEqual(config.project_root, ROOT)
            self.assertEqual(config.jarvis_dir, DEFAULT_JARVIS_DIR)
            self.assertIsNone(config.reasoning_effort)

    def test_baseline_resources_and_presets_do_not_need_installed_extensions(self):
        self.assertNotIn("action_definition", read_master_prompt())
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
            from importlib.resources import files
            self.assertEqual(original.read_text(), files("jarvis").joinpath("assets", "main.txt").read_text())
            manager = root / "presets" / "module_manager" / "personprompt.txt"
            manager.write_text("Changed", encoding="utf-8")
            ensure_runtime_layout(root)
            self.assertEqual(manager.read_text(), files("jarvis").joinpath("assets", "module_manager.txt").read_text())
            custom = root / "presets" / "custom"
            custom.mkdir()
            (custom / "personprompt.txt").write_text("Keep this personality")
            ensure_runtime_layout(root)
            self.assertEqual((custom / "personprompt.txt").read_text(), "Keep this personality")

    def test_environment_example_contains_only_model_settings(self):
        template = (ROOT / ".env.example").read_text()
        self.assertEqual({line.split("=", 1)[0] for line in template.splitlines()},
                         {"LLM_API_KEY", "LLM_BASE_URL", "LLM_API_MODEL", "LLM_SUBSCRIPTION_MODEL", "CHATGPT_SUBSCRIPTION_ENABLED", "LLM_REASONING_EFFORT"})

    def test_speech_constants_ignore_old_environment_settings(self):
        import importlib
        from jarvis.speech import config
        with patch.dict(os.environ, {"STT_DEVICE": "cpu", "STT_COMPUTE_TYPE": "int8", "TTS_SERVER_ARGS": "[]"}):
            importlib.reload(config)
            self.assertEqual(config.STT_DEVICE, "cuda")
            self.assertEqual(config.STT_COMPUTE_TYPE, "float16")
            self.assertEqual(config.TTS_SERVER_ARGS, ["--cuda", "0", "-ngl", "-1"])
            self.assertEqual(config.build_config(Path("/temporary/storage")).runtime_dir, Path("/temporary/storage/runtime"))
        importlib.reload(config)

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
                self.assertIn(str(app.config.jarvis_dir.resolve()), developer.history[0]["content"])
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
