"""Cross-component contracts of live presets, tools, access and model overrides."""

import base64
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import fixtures
import test_system_state
from test_runtime import _Response
from jarvis.capabilities.api import PENDING
from jarvis.capabilities.file_tools import TOOL_NAMES, file_action
from jarvis.core.agent_actions import run as agent_action
from jarvis.core.protocol import Event
from jarvis.infrastructure.runtime_layout import ensure_runtime_layout
from jarvis.presets import PresetStore


class TargetArchitectureTests(unittest.TestCase):
    setUp = test_system_state.SystemStateTests.setUp
    invoke = test_system_state.SystemStateTests.invoke
    def test_override_fallback_does_not_change_saved_override_or_duplicate_input(self):
        self.manager.presets.edit("worker", changes={"model": "preset_model"})
        child_id = self.manager.spawn(parent_id="main", preset_id="worker")["agent_id"]
        child = self.manager.require_agent(child_id)
        self.manager.edit_agent(child_id, {"model": "missing_model"})
        requests = []

        def request(**options):
            requests.append(options)
            if options["model"] == "missing_model":
                error = RuntimeError("model not found")
                error.status_code = 404
                raise error
            return _Response('{"actions":[{"action_id":"no_action","call_id":"fallback_ok","data":{}}]}')

        self.manager.client.chat.completions.create = request
        child._turn([Event("message_from_agent", {"from_agent_id": "main", "text": "Task"})], child._generation)
        self.assertEqual([request["model"] for request in requests], ["missing_model", "preset_model"])
        self.assertIn("Текущая модель: preset_model", requests[-1]["messages"][0]["content"])
        self.assertEqual(child.model_override, "missing_model")
        self.assertEqual(self.memory.load("worker", child_id)["model"], "missing_model")
        self.assertEqual(len([message for message in child.history if message["role"] == "user"]), 1)

    def test_live_base_and_individual_addition_sources_remain_independent(self):
        child_id = self.manager.spawn(parent_id="main", preset_id="worker")["agent_id"]
        self.manager.edit_agent(child_id, {"capabilities_add": ["say"], "additional_instructions": "Individual rule."})
        with self.assertRaisesRegex(ValueError, "mandatory"):
            self.manager.edit_agent(child_id, {"capabilities_remove": ["say"]})
        self.manager.presets.edit("worker", changes={"person": "ChangedPerson", "capabilities_remove": ["say"], "model": "new_model"})
        self.manager.refresh_presets("worker")
        result = self.manager.get_agent(child_id)
        self.assertNotIn("say", result["preset"]["capabilities"])
        self.assertIn("say", result["additions"]["capabilities"])
        self.assertIn("say", result["effective"]["capabilities"])
        self.assertEqual(result["effective"]["model"], "new_model")
        self.assertEqual(result["effective"]["additional_instructions"], "Individual rule.")
        self.manager.edit_agent(child_id, {"capabilities_remove": ["say"], "model": "override"})
        self.assertNotIn("say", self.manager.get_agent(child_id)["effective"]["capabilities"])
        self.manager.edit_agent(child_id, {"model": None})
        self.assertEqual(self.manager.get_agent(child_id)["effective"]["model"], "new_model")

    def test_unavailable_override_and_preset_fall_back_to_env_default(self):
        self.manager.presets.edit("worker", changes={"model": "missing_preset"})
        child_id = self.manager.spawn(parent_id="main", preset_id="worker")["agent_id"]
        self.manager.edit_agent(child_id, {"model": "missing_override"})
        child = self.manager.require_agent(child_id)
        models = []

        def request(**options):
            models.append(options["model"])
            if options["model"] != "test":
                error = RuntimeError("model unavailable")
                error.status_code = 404
                raise error
            return _Response('{"actions":[{"action_id":"no_action","call_id":"env_ok","data":{}}]}')

        self.manager.client.chat.completions.create = request
        child._turn([Event("message_from_agent", {"from_agent_id": "main", "text": "Task"})], child._generation)
        self.assertEqual(models, ["missing_override", "missing_preset", "test"])
        self.assertEqual(child.model_override, "missing_override")

    def test_main_deletion_removes_semantic_memory_and_all_descendants(self):
        child_id = self.manager.spawn(parent_id="main", preset_id="worker")["agent_id"]
        self.manager.semantic_memory.write("A confirmed fact.")
        main = self.main
        self.manager.delete(agent_id="main")
        self.assertEqual(self.manager.agents, {})
        self.assertFalse((self.root / "memory/main").exists())
        self.assertFalse(self.memory.agent_dir("worker", child_id).exists())
        self.manager.persist_agent(main)
        self.assertFalse((self.root / "memory/main").exists())

    def test_management_action_assignment_never_grants_subagent_authority(self):
        child_id = self.manager.spawn(parent_id="main", preset_id="worker")["agent_id"]
        self.manager.enable_action(child_id, "delete_agent")
        child = self.manager.require_agent(child_id)
        self.assertIn("delete_agent", child._contract()[0])
        context = SimpleNamespace(agent_id=child_id, action_id="delete_agent", agent_manager=self.capabilities._agent_api)
        with self.assertRaisesRegex(ValueError, "main"):
            agent_action({"agent_id": "main"}, context)
        self.assertIn("main", self.manager.agents)

    def test_messages_are_available_to_agents_with_no_assigned_capabilities(self):
        self.manager.presets.create("empty", "EmptyAgent", [])
        child_id = self.manager.spawn(parent_id="main", preset_id="empty")["agent_id"]
        child = self.manager.require_agent(child_id)
        actions, events = child._contract()
        self.assertIn("send_message_to_agent", actions)
        self.assertIn("message_from_agent", events)
        self.assertNotIn("spawn_agent", actions)
        self.assertEqual(child.capabilities_snapshot(), {"actions": set(), "handlers": set(), "modules": set()})

    def test_id_collision_between_categories_is_rejected_without_host(self):
        fixtures.write_handler(self.root / "capabilities/handlers", "say", fixtures.TICK_HANDLER)
        with self.assertRaisesRegex(ValueError, "exactly one"):
            self.capabilities.resolve_kind("say")

    def test_builtin_module_and_actions_cannot_be_globally_paused(self):
        for kind, identifier in (("module", "capability_development"), ("action", "send_message_to_agent")):
            with self.assertRaisesRegex(ValueError, "Built-in"):
                self.capabilities.toggle_global_state(kind, identifier)
            self.assertFalse(self.capabilities.is_globally_paused(kind, identifier))


class SharedToolTests(unittest.TestCase):
    def test_schemas_and_behavior_match_between_module_and_standalone(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            context = SimpleNamespace()
            for name in TOOL_NAMES:
                standalone = file_action(name, root)
                module = file_action("capability_development." + name, root)
                self.assertEqual(standalone.data_schema, module.data_schema)
                self.assertEqual(standalone.result_schema, module.result_schema)
                self.assertEqual(standalone.description, module.description)
            file_action("write_file", root).run({"path": "example", "content": "one\ntwo\n"}, context)
            standalone = file_action("read_file", root).run({"path": "example"}, context)
            module = file_action("capability_development.read_file", root).run({"path": "example"}, context)
            self.assertEqual(standalone, module)

    def test_every_command_uses_home_with_no_inherited_shell_state(self):
        command = file_action("execute_command", Path.home())
        context = SimpleNamespace()
        command.run({"command": "cd /tmp; export JARVIS_TOOL_TEST_VAR=previous"}, context)
        result = command.run({"command": 'pwd; printf "%s" "${JARVIS_TOOL_TEST_VAR-unset}"'}, context)
        self.assertEqual(result["stdout"], str(Path.home()) + "\nunset")

    def test_all_binary_modalities_use_exactly_one_completion(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            samples = {"image.png": (b"\x89PNG\r\n\x1a\n", "image"), "audio.wav": (b"RIFF\0WAVE", "audio"),
                       "video.mp4": (b"\0\0ftyp", "video"), "document.pdf": (b"%PDF-1.7", "file"), "data.bin": (b"\0\xff", "file")}
            for filename, (raw, modality) in samples.items():
                (root / filename).write_bytes(raw)
                for prefix in ("", "capability_development."):
                    completions = []
                    context = SimpleNamespace(complete=lambda data, parts: completions.append((data, parts)))
                    result = file_action(prefix + "read_file", root).run({"path": filename}, context)
                    self.assertIs(result, PENDING)
                    self.assertEqual(len(completions), 1)
                    self.assertEqual(completions[0][1][0].type, modality)
                    self.assertEqual(base64.b64decode(completions[0][1][0].data), raw)

    def test_fallback_assets_never_overwrite_existing_person(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ensure_runtime_layout(root)
            person = root / "presets/main/person.txt"
            person.write_text("UserPerson")
            ensure_runtime_layout(root)
            self.assertEqual(person.read_text(), "UserPerson")
            person.unlink()
            ensure_runtime_layout(root)
            self.assertNotEqual(person.read_text(), "UserPerson")
            self.assertEqual(set(json.loads((root / "presets/main/preset.json").read_text())), {"model", "capabilities"})
            self.assertNotIn("protected", PresetStore(root / "presets").load("main").__slots__)
