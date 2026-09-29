"""Direct ChatGPT subscription transport and Jarvis-owned OAuth tests."""

import base64
import json
import os
import stat
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
from urllib.request import urlopen
from unittest.mock import Mock, patch

import fixtures
from jarvis.application import JarvisApplication
from jarvis.core.protocol import actions_response_schema, object_schema
from jarvis.core.runtime import AgentManager
from jarvis.infrastructure.config import Config, load_config
from jarvis.infrastructure.context import MemoryStore
from jarvis.infrastructure.model_capabilities import ModelCapabilities
from jarvis.infrastructure.subscription import (
    SUBSCRIPTION_MODALITIES,
    SubscriptionClient,
    SubscriptionQuotaExceeded,
    SubscriptionStream,
    _history_items,
    _normalize_response,
    _quota_error,
    _strict_schema,
)
from jarvis.infrastructure.subscription_auth import SubscriptionAuth, _account_id
from test_runtime import _Response, _wait


def _jwt(account_id="account-test"):
    payload = json.dumps({"chatgpt_account_id": account_id}).encode()
    return "a." + base64.urlsafe_b64encode(payload).decode().rstrip("=") + ".b"


class _ResponseStream:
    def __init__(self, events=(), *, status=200, text=""):
        self.status_code = status
        self.text = text
        self.events = events
        self.closed = False

    def iter_lines(self):
        for event in self.events:
            yield ("data: " + json.dumps(event)).encode()

    def close(self):
        self.closed = True


def _client(auth=None, session=None, *, api_capabilities=None, fallback=None):
    api = Mock()
    router = SubscriptionClient(
        api_client=api,
        api_model="api-model",
        subscription_model="gpt-6-luna",
        api_capabilities=api_capabilities
        or ModelCapabilities("api", ("text", "image", "file")),
        auth=auth or Mock(),
        on_fallback=fallback,
        session=session or Mock(),
    )
    return router, api


class SubscriptionTests(unittest.TestCase):
    def test_config_uses_separate_models_and_subscription_modalities(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / ".env"
            path.write_text(
                "CHATGPT_SUBSCRIPTION_ENABLED=true\nLLM_API_MODEL=api-model\nLLM_SUBSCRIPTION_MODEL=gpt-6-luna\n"
            )
            with patch.dict(os.environ, {}, clear=True):
                config = load_config(path)
            self.assertTrue(config.subscription_enabled)
            self.assertEqual(config.model, "api-model")
            self.assertEqual(config.subscription_model, "gpt-6-luna")
            self.assertEqual(
                SUBSCRIPTION_MODALITIES.input_modalities, ("text", "image")
            )

    def test_strict_schema_restores_omitted_optional_fields(self):
        data = object_schema(
            {
                "path": {"type": "string"},
                "start_line": {"type": "integer", "default": None},
            },
            required=["path"],
        )
        schema = actions_response_schema(
            {"read_file": data, "no_action": object_schema({})}
        )
        strict = _strict_schema(schema)
        self.assertFalse(strict["additionalProperties"])
        answer = '{"actions":[{"action_id":"read_file","call_id":"a-1","data":{"path":"/tmp/file","start_line":null}}]}'
        self.assertEqual(
            json.loads(_normalize_response(answer, schema))["actions"][0]["data"],
            {"path": "/tmp/file"},
        )

    def test_open_automation_data_round_trips_as_json(self):
        schema = actions_response_schema(
            {"create_automation": AgentManager.automation_data_schema(None)}
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
        result = json.loads(_normalize_response(answer, schema))["actions"][0]["data"]
        self.assertEqual(result["event"]["data"], {"text": "go"})
        self.assertEqual(result["actions"][0]["data"], {"text": "hello"})

    def test_direct_context_keeps_system_role_history_and_image(self):
        messages = [
            {"role": "system", "content": "Jarvis system prompt"},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": '{"event_id":"example","data":{}}'},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": "data:image/png;base64,YQ==",
                            "name": "cat.png",
                        },
                    },
                    {
                        "type": "file",
                        "file": {
                            "filename": "report.pdf",
                            "file_data": "data:application/pdf;base64,YQ==",
                        },
                    },
                ],
            },
            {"role": "assistant", "content": '{"actions":[]}'},
        ]
        result = _history_items(messages)
        self.assertEqual(
            [message["role"] for message in result], ["system", "user", "assistant"]
        )
        self.assertEqual(result[0]["content"][0]["text"], "Jarvis system prompt")
        self.assertEqual(
            [part["type"] for part in result[1]["content"]],
            ["input_text", "input_text", "input_image", "input_text"],
        )
        self.assertIn("cat.png", result[1]["content"][1]["text"])
        self.assertIn("report.pdf", result[1]["content"][3]["text"])
        self.assertEqual(result[2]["content"][0]["type"], "output_text")

    def test_direct_request_contains_only_jarvis_messages_and_no_codex_tools(self):
        schema = actions_response_schema({"no_action": object_schema({})})
        response = _ResponseStream(
            [
                {
                    "type": "response.output_text.delta",
                    "delta": '{"actions":[{"action_id":"no_action",',
                },
                {
                    "type": "response.output_text.delta",
                    "delta": '"call_id":"act-1","data":{}}]}',
                },
                {"type": "response.completed"},
            ]
        )
        auth = Mock()
        auth.credentials.return_value = {
            "access_token": "secret",
            "account_id": "account-test",
        }
        session = Mock()
        session.post.return_value = response
        router, _ = _client(auth, session)
        messages = [
            {"role": "system", "content": "Only Jarvis rules"},
            {"role": "user", "content": '{"event_id":"start","data":{}}'},
        ]
        options = {
            "messages": messages,
            "response_format": {"json_schema": {"schema": schema}},
            "stream": True,
            "reasoning_effort": "high",
        }
        stream = router.create(**options)
        content = "".join(chunk.choices[0].delta.content for chunk in stream)
        self.assertIn('"call_id":"act-1"', content)
        sent = session.post.call_args.kwargs
        body = sent["json"]
        self.assertEqual(body["input"], _history_items(messages))
        self.assertNotIn("tools", body)
        self.assertNotIn("developer_instructions", body)
        self.assertNotIn("instructions", body)
        self.assertEqual(body["reasoning"], {"effort": "high"})
        self.assertEqual(body["text"]["format"]["type"], "json_schema")
        self.assertEqual(sent["headers"]["ChatGPT-Account-Id"], "account-test")
        self.assertTrue(response.closed)
        stream.close()

    def test_usage_limit_retries_identical_request_through_api(self):
        auth = Mock()
        auth.credentials.return_value = {
            "access_token": "secret",
            "account_id": "account-test",
        }
        session = Mock()
        session.post.return_value = _ResponseStream(
            status=429, text="You’ve hit your usage limit"
        )
        changed = []
        router, api = _client(auth, session, fallback=changed.append)
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
        messages = [
            {"role": "system", "content": SUBSCRIPTION_MODALITIES.prompt_block()},
            {"role": "user", "content": "hello"},
        ]
        options = {
            "messages": messages,
            "response_format": {"json_schema": {"schema": {}}},
            "stream": True,
        }
        stream = router.create(**options)
        self.assertEqual(next(stream).choices[0].delta.content, "api reply")
        with self.assertRaises(StopIteration):
            next(stream)
        self.assertEqual(len(changed), 1)
        self.assertEqual(
            api.chat.completions.create.call_args.kwargs["messages"][1], messages[1]
        )
        self.assertEqual(
            api.chat.completions.create.call_args.kwargs["model"], "api-model"
        )
        stream.close()

    def test_api_fallback_preserves_original_file(self):
        router, api = _client()
        api.chat.completions.create.return_value = iter([])
        messages = [
            {"role": "system", "content": SUBSCRIPTION_MODALITIES.prompt_block()},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "read"},
                    {
                        "type": "file",
                        "file": {
                            "filename": "report.pdf",
                            "file_data": "data:application/pdf;base64,YQ==",
                        },
                    },
                    {
                        "type": "input_audio",
                        "input_audio": {"format": "ogg", "data": "YQ=="},
                    },
                ],
            },
        ]
        router._api_request({"messages": messages, "stream": True})
        content = api.chat.completions.create.call_args.kwargs["messages"][1]["content"]
        self.assertEqual([part["type"] for part in content], ["text", "file"])
        self.assertEqual(content[1]["file"]["filename"], "report.pdf")
        self.assertEqual(len(messages[1]["content"]), 3)

    def test_api_fallback_updates_model_info_id(self):
        router, api = _client()
        api.chat.completions.create.return_value = iter([])
        router._api_request(
            {
                "messages": [
                    {
                        "role": "system",
                        "content": "MODEL INFO:\nModel ID: gpt-6-luna\n"
                        + ModelCapabilities(
                            "gpt-6-luna", ("text", "image")
                        ).prompt_block(),
                    },
                    {"role": "user", "content": "hello"},
                ],
                "stream": True,
            }
        )
        system = api.chat.completions.create.call_args.kwargs["messages"][0]["content"]
        self.assertIn("Model ID: api-model", system)
        self.assertNotIn("Model ID: gpt-6-luna", system)
        self.assertIn("text, image, file", system)

    def test_close_cancels_direct_http_stream(self):
        stream = SubscriptionStream.__new__(SubscriptionStream)
        stream._closed = threading.Event()
        stream._lock = threading.RLock()
        stream._queue = __import__("queue").Queue()
        stream._response = Mock()
        stream._api_stream = None
        stream.close()
        stream._response.close.assert_called_once()

    def test_quota_classification_does_not_hide_other_errors(self):
        self.assertTrue(_quota_error({"error": {"code": "usage_limit_exceeded"}}))
        self.assertTrue(_quota_error({"code": "rate_limit_exceeded"}))
        self.assertFalse(_quota_error({"error": {"code": "invalid_json_schema"}}))
        self.assertTrue(_quota_error(RuntimeError("You’ve hit your usage limit")))

    def test_auth_file_is_private_and_separate_from_codex(self):
        with tempfile.TemporaryDirectory() as temporary:
            auth = SubscriptionAuth(Path(temporary), session=Mock())
            self.assertEqual(auth.path, Path(temporary) / "runtime/auth/chatgpt.json")
            self.assertEqual(_account_id(_jwt()), "account-test")
            auth._save(
                {"access_token": _jwt(), "refresh_token": "refresh", "expires_in": 3600}
            )
            self.assertEqual(auth.credentials()["account_id"], "account-test")
            self.assertEqual(stat.S_IMODE(auth.path.stat().st_mode), 0o600)

    def test_saved_auth_skips_browser_and_refreshes_when_expired(self):
        with tempfile.TemporaryDirectory() as temporary:
            session = Mock()
            auth = SubscriptionAuth(Path(temporary), session=session)
            auth._save(
                {"access_token": _jwt(), "refresh_token": "refresh", "expires_in": 3600}
            )
            with patch(
                "jarvis.infrastructure.subscription_auth.webbrowser.open"
            ) as browser:
                auth.ensure_login(Mock())
            browser.assert_not_called()
            auth._state["expires_at"] = time.time() - 1
            session.post.return_value.json.return_value = {
                "access_token": "new-access",
                "refresh_token": "new-refresh",
                "expires_in": 3600,
            }
            refreshed = auth.credentials()
            self.assertEqual(refreshed["account_id"], "account-test")
            self.assertEqual(refreshed["access_token"], "new-access")

    def test_first_login_opens_browser_and_saves_independent_session(self):
        with tempfile.TemporaryDirectory() as temporary:
            session = Mock()
            session.post.return_value.json.return_value = {
                "access_token": _jwt(),
                "refresh_token": "refresh",
                "expires_in": 3600,
            }
            auth = SubscriptionAuth(Path(temporary), session=session)
            notices = []
            workers = []

            def visit(url):
                query = parse_qs(urlsplit(url).query)
                self.assertEqual(query["code_challenge_method"], ["S256"])
                self.assertEqual(
                    query["redirect_uri"], ["http://localhost:1455/auth/callback"]
                )

                def callback():
                    with urlopen(
                        "http://localhost:1455/auth/callback?code=login-code&state="
                        + query["state"][0],
                        timeout=3,
                    ) as response:
                        self.assertEqual(response.status, 200)

                worker = threading.Thread(target=callback)
                workers.append(worker)
                worker.start()
                return True

            with patch(
                "jarvis.infrastructure.subscription_auth.webbrowser.open",
                side_effect=visit,
            ):
                auth.ensure_login(notices.append)
            for worker in workers:
                worker.join(timeout=3)
            self.assertTrue(auth.path.exists())
            self.assertEqual(auth.credentials()["account_id"], "account-test")
            self.assertIn("Войдите", notices[0])
            self.assertEqual(
                session.post.call_args.kwargs["data"]["code"], "login-code"
            )

    def test_application_authenticates_before_startup_event(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            storage = fixtures.write_jarvis_root(root / "storage")
            backend = Mock()
            backend.chat.completions.create.side_effect = lambda **kwargs: _Response(
                '{"actions":[{"action_id":"no_action","call_id":"ready-1","data":{}}]}'
            )
            order = []
            backend.ensure_chatgpt_login.side_effect = lambda announce: (
                order.append("login"),
                announce("Войдите в аккаунт ChatGPT."),
            )
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
                output = __import__("io").StringIO()
                app = JarvisApplication(
                    Config(
                        "api",
                        "http://invalid",
                        "secret",
                        root,
                        storage,
                        subscription_enabled=True,
                        subscription_model="gpt-6-luna",
                    ),
                    memory=MemoryStore(root / "memory"),
                    stream=output,
                )
                try:
                    self.assertEqual(order, ["login"])
                    self.assertIn("Войдите в аккаунт ChatGPT.", output.getvalue())
                    self.assertNotIn("system_started", output.getvalue())
                    app.start()
                    self.assertIn(
                        "MODEL INFO:\nModel ID: gpt-6-luna",
                        app.main_agent.history[0]["content"],
                    )
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


if __name__ == "__main__":
    unittest.main()
