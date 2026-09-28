"""Subscription transport contracts without starting a real Codex session."""

import json
import queue
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from jarvis.core.protocol import actions_response_schema, object_schema
from jarvis.core.runtime import AgentManager
from jarvis.application import JarvisApplication
from jarvis.infrastructure.config import load_config
from jarvis.infrastructure.model_capabilities import ModelCapabilities
from jarvis.infrastructure.config import Config
from jarvis.infrastructure.context import MemoryStore
from jarvis.infrastructure.subscription import (
    SUBSCRIPTION_MODALITIES,
    SubscriptionClient,
    SubscriptionQuotaExceeded,
    SubscriptionStream,
    _action_schemas,
    _history_items,
    _normalize_response,
    _quota_error,
    _strict_schema,
    _user_parts,
)


class SubscriptionTests(unittest.TestCase):
    def test_application_authenticates_before_startup_event(self):
        import fixtures
        from test_runtime import _Response, _wait

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            storage = fixtures.write_jarvis_root(root / "storage")
            backend = Mock()
            backend.chat.completions.create.side_effect = lambda **kwargs: _Response(
                '{"actions":[{"action_id":"no_action","call_id":"ready-1","data":{}}]}'
            )
            backend.close = Mock()
            order = []
            backend.ensure_chatgpt_login.side_effect = lambda: order.append("login")
            with (
                patch("jarvis.application.OpenAI"),
                patch(
                    "jarvis.application.discover_model_capabilities",
                    return_value=ModelCapabilities("api", ("text", "image", "file")),
                ),
                patch(
                    "jarvis.infrastructure.subscription.SubscriptionClient",
                    return_value=backend,
                ),
                patch("jarvis.application.speech_service") as speech,
                patch("jarvis.application.shutil.which", return_value=None),
            ):
                speech.can_speak = False
                app = JarvisApplication(
                    Config(
                        "api",
                        "http://invalid",
                        "secret",
                        root,
                        storage,
                        subscription_enabled=True,
                        subscription_model="sub",
                    ),
                    memory=MemoryStore(root / "memory"),
                )
                try:
                    self.assertEqual(order, ["login"])
                    app.start()
                    self.assertEqual(
                        app.agents.model_capabilities.input_modalities,
                        ("text", "image"),
                    )
                    backend.ensure_chatgpt_login.assert_called_once()
                    self.assertIsNotNone(app.main_agent)
                    self.assertTrue(
                        _wait(
                            lambda: any(
                                '"event_id":"system_started"'
                                in str(message.get("content", ""))
                                for message in app.main_agent.history
                            )
                        )
                    )
                finally:
                    app.stop()

    def test_configuration_has_separate_models_and_legacy_api_alias(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / ".env"
            path.write_text(
                "CHATGPT_SUBSCRIPTION_ENABLED=true\nLLM_API_MODEL=api-model\n"
                "LLM_SUBSCRIPTION_MODEL=subscription-model\nLLM_MODEL=old-model\n"
            )
            with patch.dict("os.environ", {}, clear=True):
                config = load_config(path)
            self.assertTrue(config.subscription_enabled)
            self.assertEqual(config.model, "api-model")
            self.assertEqual(config.subscription_model, "subscription-model")
            self.assertEqual(
                SUBSCRIPTION_MODALITIES.input_modalities, ("text", "image")
            )
            path.write_text("CHATGPT_SUBSCRIPTION_ENABLED=no\n")
            with (
                patch.dict("os.environ", {}, clear=True),
                self.assertRaisesRegex(ValueError, "true or false"),
            ):
                load_config(path)

    def test_strict_translation_and_protocol_restoration(self):
        data = object_schema(
            {
                "path": {"type": "string"},
                "start_line": {"type": "integer", "minimum": 1, "default": None},
            },
            required=["path"],
        )
        action_schema = actions_response_schema(
            {"read_file": data, "no_action": object_schema({})}
        )
        strict = _strict_schema(action_schema)
        self.assertFalse(strict["additionalProperties"])
        translated_data = _action_schemas(strict)["read_file"]
        self.assertEqual(translated_data["required"], ["path", "start_line"])
        self.assertEqual(
            translated_data["properties"]["start_line"]["type"], ["integer", "null"]
        )
        self.assertNotIn("minimum", translated_data["properties"]["start_line"])
        answer = '{"actions":[{"action_id":"read_file","call_id":"a-1","data":{"path":"/tmp/file","start_line":null}}]}'
        result = json.loads(_normalize_response(answer, action_schema))
        self.assertEqual(result["actions"][0]["data"], {"path": "/tmp/file"})

    def test_free_form_automation_data_uses_json_string_on_wire(self):
        schema = actions_response_schema(
            {"create_automation": AgentManager.automation_data_schema(None)}
        )
        translated = _action_schemas(_strict_schema(schema))["create_automation"]
        self.assertEqual(
            translated["properties"]["event"]["properties"]["data"]["type"], "string"
        )
        answer = json.dumps(
            {
                "actions": [
                    {
                        "action_id": "create_automation",
                        "call_id": "a-2",
                        "data": {
                            "event": {
                                "event_id": "speech_detected",
                                "data": '{"text":"go"}',
                            },
                            "call_result": None,
                            "actions": [
                                {"action_id": "speech", "data": '{"text":"hello"}'}
                            ],
                        },
                    }
                ]
            }
        )
        restored = json.loads(_normalize_response(answer, schema))["actions"][0]["data"]
        self.assertEqual(restored["event"]["data"], {"text": "go"})
        self.assertEqual(restored["actions"][0]["data"], {"text": "hello"})
        self.assertIsNone(restored["call_result"])

    def test_history_preserves_roles_images_and_unsupported_attachment_names(self):
        content = [
            {"type": "text", "text": '{"event_id":"example","data":{}}'},
            {
                "type": "image_url",
                "image_url": {"url": "data:image/png;base64,YQ==", "name": "cat.png"},
            },
            {
                "type": "file",
                "file": {
                    "filename": "report.pdf",
                    "file_data": "data:application/pdf;base64,YQ==",
                },
            },
        ]
        raw, user_input = _user_parts(content)
        self.assertEqual(
            [part["type"] for part in raw],
            ["input_text", "input_text", "input_image", "input_text"],
        )
        self.assertIn("cat.png", raw[1]["text"])
        self.assertIn("report.pdf", raw[3]["text"])
        self.assertEqual(user_input[2].url, "data:image/png;base64,YQ==")
        messages = [
            {"role": "user", "content": content},
            {"role": "assistant", "content": '{"actions":[]}'},
        ]
        history = _history_items(messages)
        self.assertEqual([item["role"] for item in history], ["user", "assistant"])
        self.assertEqual(history[1]["content"][0]["type"], "output_text")

    def test_login_opens_browser_only_when_not_using_chatgpt(self):
        sdk = Mock()
        sdk.account.return_value.model_dump.return_value = {
            "account": {"type": "chatgpt"}
        }
        api = Mock()
        router = SubscriptionClient(
            api_client=api,
            api_model="api",
            subscription_model="codex",
            api_capabilities=SUBSCRIPTION_MODALITIES,
            project_root=Path("/tmp"),
            codex=sdk,
        )
        with patch("jarvis.infrastructure.subscription.webbrowser.open") as browser:
            router.ensure_chatgpt_login()
        browser.assert_not_called()
        sdk.account.return_value.model_dump.side_effect = [
            {"account": None},
            {"account": {"type": "chatgpt"}},
        ]
        sdk.login_chatgpt.return_value.auth_url = "https://example.invalid/login"
        sdk.login_chatgpt.return_value.wait.return_value.success = True
        with patch(
            "jarvis.infrastructure.subscription.webbrowser.open", return_value=True
        ) as browser:
            router.ensure_chatgpt_login()
        browser.assert_called_once_with("https://example.invalid/login")
        sdk.login_chatgpt.return_value.wait.assert_called_once()

    def test_quota_switch_reuses_unchanged_messages_and_schema(self):
        sdk = Mock()
        api = Mock()
        api.chat.completions.create.return_value = iter(
            [
                SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            delta=SimpleNamespace(content="api reply", refusal=None)
                        )
                    ]
                )
            ]
        )
        changed = []
        router = SubscriptionClient(
            api_client=api,
            api_model="api-model",
            subscription_model="codex-model",
            api_capabilities=ModelCapabilities("api", ("text", "image", "file")),
            project_root=Path("/tmp"),
            on_fallback=changed.append,
            codex=sdk,
        )
        with patch.object(
            router, "_generate", side_effect=SubscriptionQuotaExceeded("limit")
        ):
            message = {"role": "user", "content": "hello"}
            system = {
                "role": "system",
                "content": SUBSCRIPTION_MODALITIES.prompt_block(),
            }
            options = {
                "messages": [system, message],
                "model": "api-model",
                "response_format": {"json_schema": {"schema": {}}},
                "stream": True,
            }
            stream = router.create(**options)
            self.assertEqual(next(stream).choices[0].delta.content, "api reply")
            with self.assertRaises(StopIteration):
                next(stream)
            self.assertEqual(len(changed), 1)
            self.assertEqual(options["messages"][1], message)
            sent = api.chat.completions.create.call_args.kwargs
            self.assertEqual(sent["model"], "api-model")
            self.assertEqual(sent["messages"][1], message)
            self.assertEqual(sent["response_format"], options["response_format"])
            self.assertIn("file", sent["messages"][0]["content"])
            stream.close()
            # Subsequent turns stay on the API path for this process.
            api.chat.completions.create.return_value = iter([])
            router.create(**options)
            self.assertEqual(len(changed), 1)

    def test_api_fallback_receives_original_file_and_updated_modalities(self):
        api = Mock()
        api.chat.completions.create.return_value = iter([])
        router = SubscriptionClient(
            api_client=api, api_model="api-model", subscription_model="codex-model",
            api_capabilities=ModelCapabilities("api", ("text", "image", "file")),
            project_root=Path("/tmp"), codex=Mock(),
        )
        system = {"role": "system", "content": "Personal.\n\n" + SUBSCRIPTION_MODALITIES.prompt_block() + "\n\n{}"}
        content = [
            {"type": "text", "text": '{"event_id":"uploaded","data":{}}'},
            {"type": "file", "file": {"filename": "report.pdf", "file_data": "data:application/pdf;base64,YQ=="}},
            {"type": "input_audio", "input_audio": {"format": "ogg", "data": "YQ=="}},
        ]
        options = {"messages": [system, {"role": "user", "content": content}], "model": "ignored", "stream": True}
        router._api_request(options)
        sent = api.chat.completions.create.call_args.kwargs
        self.assertEqual(sent["model"], "api-model")
        self.assertIn("text, image, file", sent["messages"][0]["content"])
        self.assertEqual([part["type"] for part in sent["messages"][1]["content"]], ["text", "file"])
        self.assertEqual(sent["messages"][1]["content"][1]["file"]["filename"], "report.pdf")
        self.assertEqual(options["messages"][1]["content"], content)

    def test_cancel_interrupts_the_active_turn_once(self):
        stream = SubscriptionStream.__new__(SubscriptionStream)
        stream._closed = __import__("threading").Event()
        stream._lock = __import__("threading").RLock()
        stream._queue = queue.Queue()
        stream._turn = Mock()
        stream._turn_finished = False
        stream._api_stream = None
        stream.close()
        self.assertTrue(stream._closed.is_set())
        stream._turn.interrupt.assert_called_once()

    def test_quota_codes_are_specific(self):
        for code in ("usageLimitExceeded", "rateLimitExceeded"):
            self.assertTrue(
                _quota_error(
                    SimpleNamespace(codex_error_info=SimpleNamespace(root=code))
                )
            )
            self.assertTrue(
                _quota_error(
                    SimpleNamespace(codex_error_info=SimpleNamespace(root={code: {}}))
                )
            )
        self.assertFalse(
            _quota_error(
                SimpleNamespace(codex_error_info=SimpleNamespace(root="badRequest"))
            )
        )
        self.assertTrue(
            _quota_error(
                RuntimeError("You’ve hit your usage limit. Try again tomorrow.")
            )
        )


if __name__ == "__main__":
    unittest.main()
