import tempfile
import time
import unittest
from pathlib import Path

from jarvis.capabilities.manager import CapabilityManager
from jarvis.core.protocol import ActionRequest
from jarvis.core.registry import ActionRegistry, EventRegistry
from jarvis.core.runtime import ActionResultTracker, EventBus
from jarvis.infrastructure.debug import Debugger
from jarvis.presets import PresetStore

import fixtures


class _Agent:
    agent_id = "agent-001"
    name = "test"
    preset = "test"

    def __init__(self):
        self.results = []

    def enqueue_result(self, result):
        self.results.append(result)

    def is_enabled_action(self, action_id):
        return True


class _Manager:
    def __init__(self, agent):
        self.agent = agent
        self.debug = Debugger(enabled=False)
        self.results = ActionResultTracker(debug=self.debug)

    def deliver_result(self, result):
        self.agent.enqueue_result(result)
        return True

    def report_capability_error(self, capability, error):
        self.debug.log("capability_error", capability=capability, error=str(error))


class CapabilityTests(unittest.TestCase):
    def manager(self, root):
        events = EventRegistry()
        actions = ActionRegistry()
        bus = EventBus(events, debug=Debugger(enabled=False))
        manager = CapabilityManager(
            bus,
            actions,
            events,
            root=root,
            debug=Debugger(enabled=False),
        )
        return manager, actions, events

    def dispatch(self, manager, action_id, data, action_key="run-1"):
        agent = _Agent()
        fake = _Manager(agent)
        manager.event_bus.manager = fake
        manager.dispatch(
            action=ActionRequest(action_id, data, action_key),
            spec=manager.actions.require(action_id),
            agent=agent,
        )
        deadline = time.time() + 5
        while not agent.results and time.time() < deadline:
            time.sleep(0.01)
        return agent.results

    def test_standalone_action_loads_runs_and_unloads(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = fixtures.write_jarvis_root(Path(temporary))
            manager, actions, events = self.manager(root)
            try:
                summary = manager.load_action("say", start_handlers=True)
                self.assertEqual(summary["id"], "say")
                self.assertEqual(
                    summary["result_schema"]["properties"]["spoken"],
                    {"type": "boolean"},
                )
                self.assertIn(
                    "say", actions.for_capabilities(modules=set(), actions={"say"})
                )
                self.assertNotIn(
                    "say", actions.for_capabilities(modules=set(), actions=set())
                )
                results = self.dispatch(manager, "say", {"text": "ok"})
                self.assertEqual(
                    results[0].model_value(),
                    {
                        "type": "action_result",
                        "call_id": "run-1",
                        "data": {"spoken": True, "text": "ok"},
                    },
                )
            finally:
                manager.shutdown()

    def test_module_container_loads_prefixed_units(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = fixtures.write_jarvis_root(Path(temporary))
            fixtures.write_echo_module(root)
            manager, actions, events = self.manager(root)
            try:
                summary = manager.load_module("echo", start_handlers=True)
                self.assertEqual(summary["module_id"], "echo")
                self.assertEqual(
                    [action["id"] for action in summary["actions"]],
                    ["echo.repeat"],
                )
                self.assertIn(
                    "echo.echoed",
                    events.for_capabilities(modules={"echo"}, handlers=set()),
                )
                results = self.dispatch(
                    manager, "echo.repeat", {"value": "ok"}
                )
                self.assertEqual(
                    results[0].model_value()["data"], {"value": "ok"}
                )
            finally:
                manager.shutdown()

    def test_part_of_module_cannot_be_enabled_separately(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = fixtures.write_jarvis_root(Path(temporary))
            fixtures.write_echo_module(root)
            manager, actions, events = self.manager(root)
            try:
                with self.assertRaisesRegex(ValueError, "отдельно"):
                    manager.load_action("echo.repeat")
                with self.assertRaisesRegex(ValueError, "отдельно"):
                    manager.load_handler("echo.monitor")
            finally:
                manager.shutdown()

    def test_hardcoded_ids_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = fixtures.write_jarvis_root(Path(temporary))
            fixtures.write_action(
                root / "actions",
                "bad",
                "from dataclasses import replace\n"
                "from jarvis.capabilities import action_definition\n"
                "from jarvis.core.protocol import object_schema\n"
                "def run(data, context):\n"
                "    return {}\n"
                "def create_action():\n"
                "    return replace(action_definition('t', {}, {}, run), id='bad')\n",
            )
            manager, actions, events = self.manager(root)
            try:
                with self.assertRaisesRegex(ValueError, "захардкожен"):
                    manager.load_action("bad", start_handlers=False)
            finally:
                manager.shutdown()

    def test_fixture_presets_only_reference_existing_capabilities(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = fixtures.write_jarvis_root(Path(temporary))
            fixtures.write_echo_module(root)
            manager, actions, events = self.manager(root)
            try:
                existing = {
                    "modules": manager.existing_modules(),
                    "actions": manager.existing_actions(),
                    "handlers": manager.existing_handlers(),
                }
                self.assertLessEqual({"echo"}, existing["modules"])
                self.assertLessEqual({"say"}, existing["actions"])
                self.assertLessEqual({"tick"}, existing["handlers"])
                for preset in PresetStore(root / "presets").list():
                    self.assertLessEqual(set(preset.modules), existing["modules"])
                    self.assertLessEqual(set(preset.actions), existing["actions"])
                    self.assertLessEqual(set(preset.handlers), existing["handlers"])
                manager.load_module("echo", start_handlers=False)
                catalog = manager.catalog(
                    {"modules": {"echo"}, "actions": set(), "handlers": set()}
                )
                self.assertEqual(catalog["modules"][0]["module_id"], "echo")
                self.assertTrue(
                    all(
                        item["type"].startswith("echo.")
                        for item in catalog["modules"][0]["actions"]
                    )
                )
            finally:
                manager.shutdown()

    def test_module_environment_rejects_unpinned_dependencies(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = fixtures.write_jarvis_root(Path(temporary))
            module = fixtures.write_echo_module(root)
            (module / "requirements.txt").write_text("requests>=2\n", encoding="utf-8")
            manager, actions, events = self.manager(root)
            with self.assertRaisesRegex(ValueError, "закреплены"):
                manager.create_environment("module", "echo")


if __name__ == "__main__":
    unittest.main()
