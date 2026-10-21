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


def _client(
    auth=None, session=None, *, api_capabilities=None, fallback=None, loader=None
):
    api = Mock()
    router = SubscriptionClient(
        api_client=api,
        api_model="api-model",
        subscription_model="gpt-6-luna",
        api_capabilities=api_capabilities
        or ModelCapabilities("api", ("text", "image", "file")),
        auth=auth or Mock(),
        on_fallback=fallback,
        api_capabilities_loader=loader,
        session=session or Mock(),
    )
    return router, api


class MemorySubscriptionTests(unittest.TestCase):
    def router(self, responses):
        auth = Mock()
        auth.credentials.return_value = {
            "access_token": "test-token",
            "account_id": "test-account",
        }
        session = Mock()
        session.post.side_effect = responses
        router, api = _client(
            auth, session, api_capabilities=ModelCapabilities("agent-api", ("text",))
        )
        return router, api, session

    @staticmethod
    def response(text="# Session\nЗавершено."):
        return _ResponseStream(
            [
                {"type": "response.output_text.delta", "delta": text},
                {"type": "response.completed"},
            ]
        )

    def messages(self):
        return [
            {"role": "system", "content": "Summarize your own history as Markdown."},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "An event"},
                    {
                        "type": "image_url",
                        "image_url": {"url": "data:image/png;base64,YQ=="},
                    },
                ],
            },
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "<thinking_start>Reasoning<thinking_end>"},
                    {"type": "text", "text": '{"actions":[]}'},
                ],
            },
        ]

    def test_memory_tries_its_configured_model_and_returns_plain_markdown(self):
        router, api, session = self.router([self.response()])
        messages = self.messages()
        result = router.create_memory_completion(
            model="memory-text-image",
            api_model="provider/api-images",
            messages=messages,
            modalities={"text", "image"},
        )
        self.assertEqual(result.choices[0].message.content, "# Session\nЗавершено.")
        body = session.post.call_args.kwargs["json"]
        self.assertEqual(body["model"], "memory-text-image")
        self.assertNotIn("text", body)  # No agent action JSON schema.
        self.assertEqual(body["input"][1]["content"][1]["type"], "input_image")
        self.assertEqual(
            body["input"][2]["content"],
            [
                {
                    "type": "output_text",
                    "text": "<thinking_start>Reasoning<thinking_end>",
                },
                {"type": "output_text", "text": '{"actions":[]}'},
            ],
        )
        api.chat.completions.create.assert_not_called()
        self.assertFalse(router._memory_streams)

    def test_unavailable_model_falls_back_without_changing_model_or_dropping_media(
        self,
    ):
        router, api, session = self.router(
            [_ResponseStream(status=400, text="This model is not supported")]
        )
        messages = self.messages()
        for _ in range(2):
            router.create_memory_completion(
                model="memory-unavailable",
                api_model="provider/api-fallback",
                messages=messages,
                modalities={"text", "image"},
            )
        self.assertEqual(session.post.call_count, 1)
        self.assertEqual(api.chat.completions.create.call_count, 2)
        self.assertEqual(
            api.chat.completions.create.call_args.kwargs,
            {
                "model": "provider/api-fallback",
                "messages": messages,
                "stream": False,
            },
        )
        self.assertFalse(
            router._api_only
        )  # Other agent/subscription models stay enabled.

    def test_foreign_provider_model_is_not_replaced_with_the_agent_model(self):
        router, api, session = self.router(
            [_ResponseStream(status=404, text="Model not found")]
        )
        router.create_memory_completion(
            model="subscription-memory",
            api_model="provider/memory-model",
            messages=self.messages(),
            modalities={"text", "image"},
        )
        self.assertEqual(
            session.post.call_args.kwargs["json"]["model"], "subscription-memory"
        )
        self.assertEqual(
            api.chat.completions.create.call_args.kwargs["model"],
            "provider/memory-model",
        )

    def test_subscription_quota_falls_back_and_is_not_retried_for_each_profile(self):
        router, api, session = self.router(
            [_ResponseStream(status=429, text="Usage limit reached")]
        )
        for model in ["openai/memory-a", "openai/memory-b"]:
            router.create_memory_completion(
                model="subscription-memory",
                api_model=model,
                messages=self.messages(),
                modalities={"text", "image"},
            )
        self.assertEqual(session.post.call_count, 1)
        self.assertEqual(
            [c.kwargs["model"] for c in api.chat.completions.create.call_args_list],
            ["openai/memory-a", "openai/memory-b"],
        )

    def test_audio_and_video_use_api_with_original_parts_even_for_text_only_agent(self):
        for kind, value, modality in [
            ("input_audio", {"data": "YQ==", "format": "wav"}, "audio"),
            ("video_url", {"url": "data:video/mp4;base64,YQ=="}, "video"),
        ]:
            with self.subTest(kind=kind):
                router, api, session = self.router([])
                messages = [
                    {"role": "system", "content": "Interpret"},
                    {"role": "user", "content": [{"type": kind, kind: value}]},
                ]
                router.create_memory_completion(
                    model="subscription-memory",
                    api_model="provider/full-media",
                    messages=messages,
                    modalities={"text", modality},
                )
                session.post.assert_not_called()
                self.assertEqual(
                    api.chat.completions.create.call_args.kwargs["messages"], messages
                )

    def test_transient_subscription_failure_does_not_permanently_cache_model(self):
        router, api, session = self.router(
            [
                _ResponseStream(status=503, text="Temporarily unavailable"),
                self.response("Recovered"),
            ]
        )
        for _ in range(2):
            result = router.create_memory_completion(
                model="subscription-memory",
                api_model="provider/api-memory",
                messages=self.messages(),
                modalities={"text", "image"},
            )
        self.assertEqual(result.choices[0].message.content, "Recovered")
        self.assertEqual(session.post.call_count, 2)
        self.assertEqual(api.chat.completions.create.call_count, 1)

    def test_cancelled_memory_request_never_starts_api_fallback(self):
        router, api, session = self.router([])
        with self.assertRaises(InterruptedError):
            router.create_memory_completion(
                model="subscription-memory",
                api_model="provider/api-memory",
                messages=self.messages(),
                modalities={"text", "image"},
                cancelled=lambda: True,
            )
        session.post.assert_not_called()
        api.chat.completions.create.assert_not_called()

    def test_archiver_routes_memory_requests_through_subscription_client(self):
        from jarvis.memory.archiver import Archiver

        router, api, session = self.router([self.response()])
        config = SimpleNamespace(
            subscription_enabled=True,
            memory_subscription_model="subscription-image",
            memory_model_text_image="provider/api-image",
        )
        archiver = Archiver(Mock(), config, router, "Environment")
        self.assertEqual(
            archiver.request(self.messages(), {"text", "image"}),
            "# Session\nЗавершено.",
        )
        self.assertEqual(
            session.post.call_args.kwargs["json"]["model"], "subscription-image"
        )
        api.chat.completions.create.assert_not_called()

    def test_subscription_memory_succeeds_without_an_api_model_setting(self):
        from jarvis.memory.archiver import Archiver

        router, api, session = self.router([self.response()])
        archiver = Archiver(
            Mock(),
            SimpleNamespace(
                subscription_enabled=True, memory_subscription_model="memory-only"
            ),
            router,
            "Environment",
        )
        self.assertIn("Завершено", archiver.request(self.messages(), {"text", "image"}))
        self.assertEqual(session.post.call_args.kwargs["json"]["model"], "memory-only")
        api.chat.completions.create.assert_not_called()

    def test_missing_subscription_setting_uses_only_api_and_preserves_media(self):
        from jarvis.memory.archiver import Archiver

        router, api, session = self.router([])
        config = SimpleNamespace(
            subscription_enabled=True,
            memory_subscription_model=None,
            memory_model_text_image="provider/api-image",
        )
        api.chat.completions.create.return_value = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="API Markdown"))]
        )
        result = Archiver(Mock(), config, router, "Environment").request(
            self.messages(), {"text", "image"}
        )
        self.assertEqual(result, "API Markdown")
        session.post.assert_not_called()
        self.assertEqual(
            api.chat.completions.create.call_args.kwargs["model"], "provider/api-image"
        )
        self.assertEqual(
            api.chat.completions.create.call_args.kwargs["messages"], self.messages()
        )


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

    def test_api_model_discovery_is_deferred_until_subscription_fallback(self):
        loader = Mock(return_value=ModelCapabilities("api-model", ("text", "image")))
        router, _ = _client(loader=loader)
        loader.assert_not_called()
        router._switch_to_api(SubscriptionQuotaExceeded("limit"))
        loader.assert_called_once()
        self.assertEqual(router.api_capabilities.input_modalities, ("text", "image"))

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
                        "content": "<MODEL_INFO>\n"
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
        self.assertIn("Текущая модель: api-model", system)
        self.assertNotIn("Текущая модель: gpt-6-luna", system)
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
                ) as discover,
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
                    self.assertIs(app.services["memory_client"], backend)
                    discover.assert_not_called()
                    self.assertIn("Войдите в аккаунт ChatGPT.", output.getvalue())
                    self.assertNotIn("system_started", output.getvalue())
                    app.start()
                    discover.assert_not_called()
                    speech.begin_background.assert_called_once()
                    self.assertIn(
                        "<MODEL_INFO>\nТекущая модель: gpt-6-luna",
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
