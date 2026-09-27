"""The dynamic model catalog and JSON declaration contracts."""

import json
import re
import tempfile
import unittest
from pathlib import Path

import fixtures
import test_runtime as runtime_tests
from jarvis.capabilities import action_definition, handler_definition, HandlerContext
from jarvis.capabilities.worker import _load_leaf, _load_module
from jarvis.core.prompts import read_master_prompt
from jarvis.core.protocol import (
    ActionRequest,
    CallResult,
    Event,
    arguments_with_defaults,
    object_schema,
    response_format,
    validate_data_schema,
    validate_result,
)
from jarvis.infrastructure.automations import AutomationStore
from jarvis.infrastructure.context import MemoryStore


def catalog(agent):
    agent._contract()
    return json.loads(agent.history[0]["content"].split("\n\nCAPABILITY:\n", 1)[1])


class CatalogRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.fixture = runtime_tests.RuntimeTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.addCleanup(self.fixture.tearDown)
        self.root = self.fixture.root
        self.manager = self.fixture.manager(runtime_tests._Client([]))
        self.agent = self.manager.spawn_root(name="main", preset="main")

    def ids(self, kind):
        field = {"actions": "action_id", "events": "event_id", "modules": "module_id"}[
            kind
        ]
        return [item[field] for item in catalog(self.agent)[kind]]

    def test_catalog_has_three_arrays_and_no_handler_or_runtime_metadata(self):
        fixtures.write_echo_module(self.root)
        self.manager.enable_module("main", "echo")
        value = catalog(self.agent)
        self.assertEqual(list(value), ["actions", "events", "modules"])
        module = value["modules"][0]
        self.assertEqual(set(module), {"module_id", "description", "actions", "events"})
        self.assertEqual(module["actions"][0]["action_id"], "echo.repeat")
        self.assertEqual(module["events"][0]["event_id"], "echo.echoed")
        self.assertNotIn("echo.repeat", self.ids("actions"))
        self.assertNotIn("echo.echoed", self.ids("events"))
        serialized = json.dumps(value, ensure_ascii=False)
        self.assertIsNone(re.search(r"[А-Яа-яЁё]", serialized))
        self.assertNotIn('"handler_id"', serialized)
        self.assertNotIn('"path"', json.dumps(module))
        self.assertNotIn("standalone", value)
        self.assertNotIn("core_protocol", value)

    def test_disabled_and_paused_capabilities_are_not_described(self):
        fixtures.write_echo_module(self.root)
        self.manager.enable_module("main", "echo")
        self.manager.disable_handler("main", "tick")
        self.assertNotIn("tick.event", self.ids("events"))
        self.manager.disable_action("main", "say")
        self.assertNotIn("say", self.ids("actions"))
        self.manager.toggle_capability("module", "echo")
        self.assertNotIn("echo", self.ids("modules"))
        self.assertIn("echo", self.agent.known_snapshot()["modules"])

    def test_new_actions_append_in_enable_order_even_if_preloaded(self):
        for name in ("aaa", "zzz"):
            fixtures.write_action(self.root / "actions", name, fixtures.SAY_ACTION)
            self.manager.capabilities.load_action(name)
        before = self.ids("actions")
        self.manager.enable_action("main", "zzz")
        self.manager.enable_action("main", "aaa")
        self.assertEqual(self.ids("actions"), before + ["zzz", "aaa"])
        self.manager.disable_action("main", "zzz")
        self.manager.enable_action("main", "zzz")
        self.assertEqual(self.ids("actions"), before + ["aaa", "zzz"])

    def test_new_events_append_in_enable_order_even_if_preloaded(self):
        for name in ("aaa", "zzz"):
            fixtures.write_handler(
                self.root / "handlers",
                name,
                fixtures.TICK_HANDLER.replace("tick.event", name),
            )
            self.manager.capabilities.load_handler(name)
        before = self.ids("events")
        self.manager.enable_handler("main", "zzz")
        self.manager.enable_handler("main", "aaa")
        self.assertEqual(self.ids("events"), before + ["zzz", "aaa"])
        self.manager.disable_handler("main", "zzz")
        self.manager.enable_handler("main", "zzz")
        self.assertEqual(self.ids("events"), before + ["aaa", "zzz"])

    def test_modules_and_new_members_append_without_reordering_old_members(self):
        fixtures.write_echo_module(self.root)
        self.manager.enable_module("main", "echo")
        before = catalog(self.agent)["modules"][0]["actions"]
        self.manager.toggle_capability("module", "echo")
        fixtures.write_action(
            self.root / "modules/echo/actions", "aaa", fixtures.ECHO_ACTION
        )
        path = self.root / "modules/echo/module.py"
        source = (
            path.read_text()
            .replace(
                "from .actions.repeat.action",
                "from .actions.aaa.action import create_action as aaa\nfrom .actions.repeat.action",
            )
            .replace("(repeat(),)", "(aaa(), repeat())")
        )
        path.write_text(source)
        self.manager.toggle_capability("module", "echo")
        actual = catalog(self.agent)["modules"][0]["actions"]
        self.assertEqual(actual[:-1], before)
        self.assertEqual(actual[-1]["action_id"], "echo.aaa")

    def test_global_pause_and_resume_append_to_catalog_end(self):
        for name in ("aaa", "zzz"):
            fixtures.write_action(self.root / "actions", name, fixtures.SAY_ACTION)
            self.manager.enable_action("main", name)
        before = self.ids("actions")
        self.manager.toggle_capability("action", "aaa")
        self.manager.toggle_capability("action", "aaa")
        self.assertEqual(
            self.ids("actions"), [name for name in before if name != "aaa"] + ["aaa"]
        )

    def test_catalog_order_survives_restore(self):
        memory = MemoryStore(self.root / "memory")
        self.manager.memory = memory
        for name in ("zzz", "aaa"):
            fixtures.write_action(self.root / "actions", name, fixtures.SAY_ACTION)
            self.manager.enable_action("main", name)
        before = catalog(self.agent)
        self.manager.persist_agent(self.agent)
        self.manager.shutdown()
        restored_manager = self.fixture.manager(
            runtime_tests._Client([]), memory=memory
        )
        restored = restored_manager.restore(name="main", preset="main")
        self.assertEqual(catalog(restored), before)

    def test_only_assigned_system_development_tool_is_visible_to_main(self):
        self.assertNotIn("execute_command", self.ids("actions"))
        self.manager.enable_action("main", "execute_command")
        self.assertEqual(self.ids("actions")[-1], "execute_command")

    def test_automation_description_does_not_copy_unassigned_capabilities(self):
        fixtures.write_action(
            self.root / "actions", "unassigned_secret", fixtures.SAY_ACTION
        )
        self.assertNotIn("unassigned_secret", self.agent._contract()[0])
        self.assertNotIn("unassigned_secret", self.agent.history[0]["content"])
        self.assertLess(len(json.dumps(self.manager.automation_data_schema())), 6000)

    def test_startup_event_is_described_only_to_main(self):
        info = self.manager.spawn(parent_id="main", name="worker", preset="worker")
        worker = self.manager.require_agent(info["agent_id"])
        self.assertIn("system_started", self.ids("events"))
        self.assertNotIn(
            "system_started", [event["event_id"] for event in catalog(worker)["events"]]
        )

    def test_optional_default_reaches_normal_dispatch_and_partial_result_is_accepted(
        self,
    ):
        args = object_schema(
            {
                "text": {"type": "string"},
                "prefix": {"type": "string", "default": "default:"},
            },
            required=["text"],
        )
        self.fixture.write_action(
            "optional",
            args,
            {
                "properties": {
                    "text": {"description": "Echoed text."},
                    "error": {"description": "Failure details."},
                }
            },
            "    return {'text': data['prefix'] + data['text']}\n",
        )
        self.manager.enable_action("main", "optional")
        self.manager.capabilities.dispatch(
            action=ActionRequest("optional", {"text": "hello"}, "optional-1"),
            spec=self.manager.actions.require("optional"),
            agent=self.agent,
        )
        self.assertTrue(
            runtime_tests._wait(lambda: self.fixture.call_results(self.agent))
        )
        self.assertEqual(
            self.fixture.call_results(self.agent)[0]["data"], {"text": "default:hello"}
        )

    def test_saved_legacy_events_are_normalized_without_deleting_history(self):
        record = self.agent.memory_record()
        record["messages"] = [
            {"role": "user", "content": '{"handler_id":"tick","data":{"text":"old"}}'},
            {
                "role": "user",
                "content": '{"type":"call_result","call_id":"old-call","data":{}}',
            },
        ]
        self.manager.shutdown()
        manager = self.fixture.manager(runtime_tests._Client([]))
        restored = manager._spawn_record(record, primary=True)
        self.assertEqual(
            json.loads(restored.history[1]["content"]),
            {"event_id": "tick.event", "data": {"text": "old"}},
        )
        self.assertEqual(
            json.loads(restored.history[2]["content"])["event_id"], "call_result"
        )
        self.assertIn("handler_id", record["messages"][0]["content"])


class DeclarationTests(unittest.TestCase):
    def description(self):
        return {
            "action_id": "echo",
            "description": "Echo text.",
            "data_schema": {
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": "Text to echo.",
                        "default": None,
                    },
                    "prefix": {
                        "type": "string",
                        "description": "Optional prefix.",
                        "default": "",
                    },
                },
                "required": ["text"],
            },
            "result_schema": {
                "properties": {
                    "text": {"description": "Echoed text."},
                    "error": {"description": "Failure details."},
                }
            },
        }

    def test_developer_json_description_and_optional_parameters(self):
        definition = action_definition(self.description(), run=lambda data, context: {})
        self.assertEqual(definition.id, "echo")
        self.assertEqual(
            arguments_with_defaults({"text": "hello"}, definition.data_schema),
            {"text": "hello", "prefix": ""},
        )
        self.assertNotIn("additionalProperties", definition.data_schema)
        self.assertEqual(definition.result_schema, self.description()["result_schema"])
        self.assertFalse(
            response_format({"echo": definition.data_schema})["json_schema"]["strict"]
        )

    def test_required_missing_and_unknown_parameters_are_rejected(self):
        schema = self.description()["data_schema"]
        for value in ({}, {"text": "hello", "extra": True}):
            with self.assertRaises(ValueError):
                arguments_with_defaults(value, schema)

    def test_result_fields_can_be_omitted_or_null_without_type_constraints(self):
        schema = self.description()["result_schema"]
        for result in ({}, {"text": "hi"}, {"error": None}, {"text": 123}):
            validate_result(result, schema)
        for result in ([], None, {"undeclared": 1}):
            with self.assertRaises(ValueError):
                validate_result(result, schema)

    def test_invalid_json_descriptions_are_rejected(self):
        for field, value in (("type", "string"), ("default", None), ("required", True)):
            document = self.description()
            document["result_schema"]["properties"]["text"][field] = value
            with self.assertRaises(ValueError):
                action_definition(document, run=lambda data, context: {})
        document = self.description()
        del document["data_schema"]["properties"]["text"]["default"]
        with self.assertRaises(ValueError):
            action_definition(document, run=lambda data, context: {})

    def test_non_english_description_is_rejected(self):
        document = self.description()
        document["description"] = "Описание"
        with self.assertRaisesRegex(ValueError, "English"):
            action_definition(document, run=lambda data, context: {})

    def test_nullable_object_and_nested_array_defaults(self):
        schema = object_schema(
            {
                "options": {
                    "type": ["object", "null"],
                    "properties": {"flag": {"type": "boolean", "default": True}},
                    "required": [],
                    "default": None,
                }
            },
            required=[],
        )
        validate_data_schema(schema)
        self.assertEqual(arguments_with_defaults({}, schema), {"options": None})
        self.assertEqual(
            arguments_with_defaults({"options": {}}, schema),
            {"options": {"flag": True}},
        )
        rows = object_schema(
            {
                "rows": {
                    "type": "array",
                    "items": object_schema(
                        {"flag": {"type": "boolean", "default": True}}, required=[]
                    ),
                }
            }
        )
        self.assertEqual(
            arguments_with_defaults({"rows": [{}]}, rows), {"rows": [{"flag": True}]}
        )

    def test_handler_declares_one_event_and_cannot_emit_another(self):
        description = {
            "handler_id": "watch",
            "description": "Observe notifications.",
            "event": {
                "event_id": "notice",
                "description": "A notification arrived.",
                "data_schema": object_schema({"text": {"type": "string"}}),
            },
        }
        definition = handler_definition(description, start=lambda context: None)
        self.assertEqual(definition.event.event_id, "notice")
        emitted = []
        context = HandlerContext("watch", Path("."), emitted.append, event_id="notice")
        context.emit("notice", {"text": "hello"})
        self.assertEqual(
            emitted[0].model_value(), {"event_id": "notice", "data": {"text": "hello"}}
        )
        with self.assertRaises(ValueError):
            context.emit("other", {})

    def test_system_and_handler_events_have_identical_envelopes(self):
        for event in (
            Event("system_started", {}),
            Event("notice", {}, handler_id="watch"),
        ):
            self.assertEqual(set(event.model_value()), {"event_id", "data"})
        self.assertEqual(
            CallResult("one", {}).model_value(),
            {"event_id": "call_result", "call_id": "one", "data": {}},
        )

    def test_module_event_requires_namespace(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixtures.write_echo_module(root)
            path = root / "modules/echo/handlers/monitor/handler.py"
            path.write_text(path.read_text().replace("echo.echoed", "unqualified"))
            with self.assertRaisesRegex(ValueError, "namespace"):
                _load_module(root, Path("modules/echo"), "echo")

    def test_json_action_identifier_is_checked_against_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            code = (
                "from jarvis.capabilities import action_definition\ndef create_action():\n    return action_definition("
                + repr(self.description())
                + ", run=lambda data, context: {})\n"
            )
            fixtures.write_action(root / "actions", "echo", code)
            self.assertIn(
                "echo", _load_leaf(root, Path("actions/echo"), "action", "echo").actions
            )
            with self.assertRaisesRegex(ValueError, "Declared ID"):
                _load_leaf(root, Path("actions/echo"), "action", "different")

    def test_masterprompt_is_common_and_contains_no_development_sdk(self):
        prompt = read_master_prompt()
        self.assertNotIn("action_definition", prompt)
        self.assertNotIn("handler_definition", prompt)
        self.assertLess(len(prompt.splitlines()), 60)
        self.assertIn('"event_id":"call_result"', prompt)

    def test_legacy_automation_is_read_without_rewriting_saved_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "automations.json"
            old = '[{"event":{"handler_id":"watch","data":{}},"actions":[{"action_id":"echo","data":{"text":"ok"}}]}]'
            path.write_text(old)
            store = AutomationStore(path, resolve_handler=lambda identifier: "notice")
            self.assertEqual(
                store.list()[0]["event"], {"event_id": "notice", "data": {}}
            )
            self.assertEqual(len(store.matching({"event_id": "notice", "data": {}})), 1)
            self.assertEqual(path.read_text(), old)


if __name__ == "__main__":
    unittest.main()
