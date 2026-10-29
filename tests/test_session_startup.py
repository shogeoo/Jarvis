"""Logical startup identity must survive Python/runtime restarts, not end a session."""

import io
import itertools
import json
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

import fixtures
from test_runtime import _Client, _Response, _wait
from jarvis.application import JarvisApplication
from jarvis.core.protocol import Event
from jarvis.core.registry import ActionRegistry, EventRegistry
from jarvis.core.core_protocol import register_core_protocol
from jarvis.infrastructure.config import Config
from jarvis.infrastructure.context import MemoryStore
from jarvis.infrastructure.model_capabilities import ModelCapabilities


class SessionStartupTests(unittest.TestCase):
    def test_start_then_process_restart_only_notifies_main(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = fixtures.write_jarvis_root(Path(temporary) / ".jarvis")
            config = Config("test", "http://invalid", "test", Path(temporary), root)
            counter = itertools.count()
            client = _Client([])
            client.close = lambda: None
            client.chat.completions.create = lambda **kw: _Response(
                json.dumps(
                    {
                        "actions": [
                            {
                                "action_id": "no_action",
                                "call_id": f"start-{next(counter)}",
                                "data": {},
                            }
                        ]
                    }
                )
            )
            with (
                patch("jarvis.application.OpenAI", return_value=client),
                patch("jarvis.application.speech_service") as speech,
                patch("jarvis.application.shutil.which", return_value=None),
                patch(
                    "jarvis.application.discover_model_capabilities",
                    return_value=ModelCapabilities("test", ("text",)),
                ),
            ):
                speech.can_speak = False
                first = JarvisApplication(config, stream=io.StringIO()).start()
                try:
                    self.assertTrue(_wait(lambda: len(first.main_agent.history) >= 3))
                    child_id = first.agents.spawn(parent_id="main", preset_id="worker")[
                        "agent_id"
                    ]
                    self.assertEqual(
                        len(first.agents.require_agent(child_id).history), 1
                    )
                    self.assertTrue(first.memory.state()["started"])
                finally:
                    first.stop()
                second = JarvisApplication(config, stream=io.StringIO()).start()
                try:
                    self.assertTrue(
                        _wait(
                            lambda: "system_restarted" in str(second.main_agent.history)
                        )
                    )
                    events = [
                        json.loads(m["content"])["event_id"]
                        for m in second.main_agent.history
                        if m["role"] == "user"
                    ]
                    self.assertEqual(events.count("system_started"), 1)
                    self.assertEqual(events.count("system_restarted"), 1)
                    child = second.agents.require_agent(child_id)
                    self.assertNotIn("system_started", str(child.history[1:]))
                    self.assertNotIn("system_restarted", str(child.history[1:]))
                    specs = child._contract()[1]
                    self.assertNotIn("system_started", specs)
                    self.assertNotIn("system_restarted", specs)
                    for name in ["system_started", "system_restarted"]:
                        self.assertFalse(
                            second.bus.publish(
                                Event(
                                    name,
                                    {"datetime": "2026-10-21T12:00:00+03:00"},
                                    target=child_id,
                                )
                            )
                        )
                finally:
                    second.stop()

    def test_durable_marker_does_not_depend_on_runtime_or_empty_context(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = MemoryStore(Path(temporary) / "memory")
            self.assertEqual(store.claim_start_event(), "system_started")
            self.assertEqual(
                MemoryStore(store.root).claim_start_event(), "system_restarted"
            )
            store.update_state(
                session_id="session_000002", next_session=3, started=False
            )
            self.assertEqual(
                MemoryStore(store.root).claim_start_event(), "system_started"
            )

    def test_old_session_without_marker_uses_preserved_history(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = MemoryStore(Path(temporary) / "memory")
            state = store.state()
            state.pop("started")
            store._write_json(store.root / "state.json", state)
            store._write_json(
                store.instances / "main/context.json",
                [{"role": "user", "content": "Previous activity"}],
            )
            self.assertEqual(store.claim_start_event(), "system_restarted")

    def test_environment_and_personalities_do_not_contain_concrete_contracts(self):
        actions, events = ActionRegistry(), EventRegistry()
        register_core_protocol(actions, events)
        identifiers = set(actions.all()) | set(events.all())
        identifiers.update(
            spec.owner.removeprefix("core:module:")
            for spec in actions.all().values()
            if spec.owner.startswith("core:module:")
        )
        assets = Path(__file__).resolve().parents[1] / "jarvis/assets"
        for path in [assets / "environment.txt", *assets.glob("*_person.txt")]:
            text = path.read_text()
            for identifier in identifiers:
                self.assertIsNone(
                    re.search(
                        r"(?<![a-z0-9_])" + re.escape(identifier) + r"(?![a-z0-9_])",
                        text,
                    ),
                    f"{path.name} contains {identifier}",
                )
            self.assertNotIn("action_definition", text)
        from jarvis.core.descriptions import read_descriptions

        descriptions = read_descriptions()
        self.assertIn(
            "create_action",
            descriptions["modules"]["capability_development"]["description"],
        )
        self.assertIn("skip_first", descriptions["modules"]["memory"]["description"])
        self.assertIn(
            "unfinished", descriptions["handlers"]["system_restarted"]["description"]
        )
