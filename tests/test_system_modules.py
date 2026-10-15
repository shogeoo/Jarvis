"""Public contracts for system modules, descriptions and indivisible assignments."""

import json
from types import SimpleNamespace
from unittest.mock import patch
import unittest

import fixtures
import test_system_state
from test_runtime import _wait
from jarvis.core.descriptions import apply_descriptions
from jarvis.core.protocol import ActionRequest, validate_json


class SystemModuleTests(unittest.TestCase):
    setUp = test_system_state.SystemStateTests.setUp

    def invoke(self, identifier, data=None, *, caller="main"):
        context = SimpleNamespace(
            agent_id=caller,
            action_id=identifier,
            agent_manager=self.capabilities._agent_api,
            capabilities=self.capabilities,
            module_id=identifier.split(".", 1)[0] if "." in identifier else None,
        )
        return self.actions.require(identifier).run(data or {}, context)

    def catalog(self, agent):
        agent._contract()
        return json.loads(
            agent.history[0]["content"]
            .split("<CAPABILITIES>\n", 1)[1]
            .split("\n</CAPABILITIES>", 1)[0]
        )

    def capability_manager(self):
        self.manager.presets.create(
            "capability_manager",
            "Capability developer.",
            [],
            description="Develops capabilities.",
        )
        return self.manager.require_agent(
            self.manager.spawn(parent_id="main", preset_id="capability_manager")[
                "agent_id"
            ]
        )

    def test_preset_list_is_compact_and_get_contains_complete_preset(self):
        self.manager.presets.edit(
            "worker", changes={"description": "Reusable specialist."}
        )
        listed = self.invoke("agents.list_presets")["presets"]
        self.assertEqual({item["id"] for item in listed}, {"main", "worker"})
        self.assertTrue(all(set(item) == {"id", "description"} for item in listed))
        worker = self.invoke("agents.get_preset", {"preset_id": "worker"})
        self.assertEqual(
            set(worker), {"id", "description", "person", "model", "capabilities"}
        )
        self.assertEqual(worker["description"], "Reusable specialist.")
        self.assertEqual(worker["capabilities"], ["say", "tick"])

    def test_instance_description_is_optional_independent_and_persisted(self):
        child_id = self.manager.spawn(parent_id="main", preset_id="worker")["agent_id"]
        child = self.manager.require_agent(child_id)
        self.assertIsNone(child.description)
        self.manager.edit_agent(
            child_id,
            {
                "description": "Particular instance purpose.",
                "additional_instructions": "Individual instructions.",
            },
        )
        self.manager.presets.edit(
            "worker", changes={"description": "Different preset description."}
        )
        record = self.memory.load("worker", child_id)
        self.assertEqual(record["description"], "Particular instance purpose.")
        result = self.invoke("agents.get", {"agent_id": child_id})
        self.assertEqual(result["description"], "Particular instance purpose.")
        self.assertEqual(result["additional_instructions"], "Individual instructions.")
        self.assertNotIn("effective", result)
        self.assertNotIn("additions", result)
        self.assertNotIn("preset", result)
        self.assertIsInstance(result["capabilities"], list)
        self.manager.delete(agent_id=child_id)
        restored = self.manager._spawn_record(record, primary=False)
        self.assertEqual(restored.description, "Particular instance purpose.")
        self.manager.edit_agent(child_id, {"description": None})
        self.assertIsNone(self.memory.load("worker", child_id)["description"])

    def test_spawn_description_has_valid_schema_and_reaches_instance(self):
        specs, _ = self.main._contract()
        schema = specs["agents.spawn"].data_schema
        self.assertEqual(
            schema["properties"]["description"]["type"], ["string", "null"]
        )
        validate_json(
            {"preset_id": "worker", "description": "Specific purpose."}, schema
        )
        validate_json({"preset_id": "worker"}, schema)
        result = self.invoke(
            "agents.spawn", {"preset_id": "worker", "description": "Specific purpose."}
        )
        self.assertEqual(
            self.manager.require_agent(result["agent_id"]).description,
            "Specific purpose.",
        )

    def test_main_system_modules_are_nested_and_not_flattened(self):
        catalog = self.catalog(self.main)
        modules = {item["module_id"]: item for item in catalog["modules"]}
        self.assertEqual(set(modules), {"agents", "semantic_memory"})
        self.assertEqual(
            {item["action_id"] for item in modules["semantic_memory"]["actions"]},
            {"semantic_memory.write", "semantic_memory.edit", "semantic_memory.delete"},
        )
        self.assertEqual(len(modules["agents"]["actions"]), 11)
        standalone = {item["action_id"] for item in catalog["actions"]}
        self.assertTrue(
            all(
                not name.startswith(("agents.", "semantic_memory."))
                for name in standalone
            )
        )
        self.assertNotIn("toggle_capability", standalone)
        self.assertNotIn("list_capabilities", standalone)
        self.assertNotIn("list_active_capabilities", self.actions.all())
        for old in ("spawn_agent", "create_preset", "memory_write"):
            self.assertNotIn(old, self.actions.all())

    def test_assigned_list_includes_system_and_paused_without_module_members(self):
        fixtures.write_echo_module(self.root)
        self.manager.enable_module("main", "echo")
        self.manager.toggle_capability("module", "echo")
        items = self.invoke("list_assigned_capabilities")["capabilities"]
        identifiers = [item["id"] for item in items]
        self.assertEqual(len(identifiers), len(set(identifiers)))
        self.assertTrue(
            {
                "no_action",
                "send_message_to_agent",
                "agents",
                "semantic_memory",
                "speech_detected",
                "echo",
            }
            <= set(identifiers)
        )
        self.assertTrue(all("." not in name for name in identifiers))
        self.assertTrue(
            all("kind" not in item and item["description"] for item in items)
        )
        self.assertEqual(
            set(identifiers), set(self.manager.get_agent("main")["capabilities"])
        )

    def test_development_tools_are_intrinsic_and_inventory_is_manager_only(self):
        controller = self.capability_manager()
        self.assertEqual(
            self.manager.presets.load("capability_manager").capabilities, ()
        )
        specs, _ = controller._contract()
        self.assertNotIn("list_assigned_capabilities", specs)
        self.assertNotIn("list_available_capabilities", specs)
        self.assertIn("toggle_capability", specs)
        self.assertIn("capability_development.read_file", specs)
        result = self.invoke("list_capabilities", caller=controller.agent_id)
        self.assertEqual(set(result), {"capabilities"})
        self.assertTrue(
            {
                "agents",
                "semantic_memory",
                "capability_development",
                "no_action",
                "speech_detected",
                "say",
                "tick",
            }
            <= {item["id"] for item in result["capabilities"]}
        )
        self.assertTrue(all("kind" not in item for item in result["capabilities"]))
        with self.assertRaisesRegex(ValueError, "capability_manager"):
            self.invoke("list_capabilities")
        with self.assertRaisesRegex(ValueError, "capability_manager"):
            self.manager.enable_action("main", "toggle_capability")

    def test_system_capabilities_cannot_be_disabled_paused_or_unloaded(self):
        for identifier in (
            "agents",
            "semantic_memory",
            "no_action",
            "send_message_to_agent",
            "speech_detected",
        ):
            with self.subTest(identifier=identifier):
                with self.assertRaisesRegex(ValueError, "system"):
                    self.invoke("disable_capability", {"capability_id": identifier})
                with self.assertRaisesRegex(ValueError, "system"):
                    self.manager.edit_agent(
                        "main", {"capabilities_remove": [identifier]}
                    )
                kind = self.capabilities.resolve_kind(identifier)
                with self.assertRaisesRegex(ValueError, "system"):
                    self.capabilities.toggle_global_state(kind, identifier)
                with self.assertRaisesRegex(ValueError, "system"):
                    getattr(self.capabilities, "unload_" + kind)(identifier)

    def test_enabling_intrinsic_capabilities_is_idempotent_without_saved_additions(
        self,
    ):
        for identifier in ("agents", "semantic_memory", "no_action", "speech_detected"):
            result = self.invoke("enable_capability", {"capability_id": identifier})
            self.assertTrue(result["enabled"])
        self.assertEqual(self.main.memory_record()["capabilities_add"], [])

    def test_assigned_management_module_grants_full_delegation_not_individual_members(
        self,
    ):
        child_id = self.manager.spawn(parent_id="main", preset_id="worker")["agent_id"]
        with self.assertRaisesRegex(ValueError, "agents"):
            self.invoke("agents.list_presets", caller=child_id)
        self.manager.enable_module(child_id, "agents")
        result = self.invoke(
            "agents.create_preset",
            {
                "preset_id": "specialist",
                "description": "A reusable specialist.",
                "person": "Independent specialist.",
                "capabilities": [],
            },
            caller=child_id,
        )
        self.assertEqual(result["status"], "created")
        self.assertTrue(self.invoke("agents.list_presets", caller=child_id)["presets"])
        with self.assertRaises(ValueError):
            self.capabilities.partition(["agents.spawn"])
        with self.assertRaises(ValueError):
            self.manager.enable_action(child_id, "agents.spawn")
        self.assertNotIn(
            "agents.spawn",
            self.manager.require_agent(child_id)._additional_capabilities,
        )
        with self.assertRaisesRegex(ValueError, "system"):
            self.manager.disable_module(child_id, "agents")

    def test_system_module_cannot_be_removed_through_preset(self):
        self.manager.presets.edit("worker", changes={"capabilities_add": ["agents"]})
        with self.assertRaisesRegex(ValueError, "system"):
            self.manager.presets.edit("worker", changes={"capabilities": []})

    def test_stale_pause_entries_cannot_hide_system_capabilities(self):
        self.capabilities._global_paused["modules"].add("agents")
        self.capabilities._global_paused["actions"].add("no_action")
        self.capabilities._global_paused["handlers"].add("speech_detected")
        actions, events = self.main._contract()
        self.assertIn("agents.spawn", actions)
        self.assertIn("no_action", actions)
        self.assertIn("speech_detected", events)

    def test_available_inventory_excludes_assigned_and_system_capabilities(self):
        fixtures.write_action(
            self.root / "capabilities/actions", "new_ability", fixtures.ECHO_ACTION
        )
        self.assertEqual(
            self.invoke("list_available_capabilities"),
            {"capabilities": [{"id": "new_ability", "description": "Return a value."}]},
        )

    def test_description_only_preset_edit_preserves_person_bytes(self):
        path = self.manager.presets.path("worker") / "person.txt"
        original = "  Unchanged personality.  \n\n"
        path.write_text(original, encoding="utf-8")
        self.manager.presets.edit("worker", changes={"description": "New overview."})
        self.assertEqual(path.read_text(encoding="utf-8"), original)

    def test_rpc_delegation_uses_module_assignment_instead_of_main_identity(self):
        child_id = self.manager.spawn(parent_id="main", preset_id="worker")["agent_id"]
        request = {
            "rpc_id": "rpc-agents-1",
            "target": "agent_manager",
            "method": "presets_list",
            "caller_agent_id": child_id,
        }
        with patch.object(self.capabilities, "_send_host") as send:
            self.capabilities._handle_rpc(SimpleNamespace(), request)
            self.assertFalse(send.call_args.args[1]["ok"])
            self.manager.enable_module(child_id, "agents")
            self.capabilities._handle_rpc(SimpleNamespace(), request)
            response = send.call_args.args[1]
        self.assertTrue(response["ok"])
        self.assertTrue(
            all(set(item) == {"id", "description"} for item in response["value"])
        )

    def test_capability_info_matches_model_contract_for_all_shapes(self):
        catalog = self.catalog(self.main)
        expected = [*catalog["actions"], *catalog["modules"]]
        for contract in expected:
            identifier = contract.get("module_id", contract.get("action_id"))
            with self.subTest(identifier=identifier):
                result = self.invoke("capability_info", {"capability_id": identifier})
                self.assertEqual(result["info"], contract)
        event = next(
            item for item in catalog["events"] if item["event_id"] == "speech_detected"
        )
        self.assertEqual(
            self.invoke("capability_info", {"capability_id": "speech_detected"})[
                "info"
            ],
            event,
        )

    def test_system_module_runs_through_normal_dispatch_and_finalizes_once(self):
        spec = self.main._contract()[0]["semantic_memory.write"]
        with patch.object(
            self.main, "enqueue_result", wraps=self.main.enqueue_result
        ) as deliver:
            self.capabilities.dispatch(
                action=ActionRequest(
                    "semantic_memory.write",
                    {"content": "A confirmed stable fact."},
                    "memory-contract-1",
                ),
                spec=spec,
                agent=self.main,
            )
            self.assertTrue(_wait(lambda: deliver.call_count == 1))
            self.assertEqual(deliver.call_args.args[0].data, {"id": "mem_000001"})
        self.main._contract()
        self.assertIn("A confirmed stable fact.", self.main.history[0]["content"])

    def test_description_named_properties_are_not_schema_metadata(self):
        specs, events = self.main._contract()
        described, _ = apply_descriptions(specs, events)
        for identifier in ("agents.spawn", "agents.create_preset"):
            field = described[identifier].data_schema["properties"]["description"]
            self.assertIn("type", field)
            self.assertIsInstance(field["description"], str)
