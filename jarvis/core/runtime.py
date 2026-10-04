"""Неизменяемое event-thinking-action ядро Jarvis."""

from __future__ import annotations

from ..infrastructure.console import logger

import json
import queue
import re
import secrets
import threading
import time
import traceback
from dataclasses import replace
from pathlib import Path
from typing import Any

from ..infrastructure.context import MemoryStore
from ..infrastructure.automations import AutomationStore
from ..infrastructure.debug import Debugger
from ..infrastructure.model_capabilities import ModelCapabilities
from ..infrastructure.semantic_memory import SemanticMemory
from ..capabilities.api import ActionDefinition, EventDefinition
from ..presets import PresetStore


PRIMARY_AGENT_NAME = "Jarvis"
from .lifecycle import ProcessManager
from .prompts import agent_system_prompt
from .descriptions import apply_descriptions
from .protocol import (
    ActionRequest,
    CallResult,
    Event,
    actions_response_schema,
    empty_object_schema,
    object_schema,
    parse_actions,
    response_format,
    validate_json,
    validate_result,
    canonical_event_value,
)
from .registry import ActionRegistry, EventRegistry


def register_core_protocol(actions: ActionRegistry, events: EventRegistry) -> None:
    """Зарегистрировать зарезервированные элементы протокола."""
    from .system_actions import register_system_actions
    register_system_actions(actions)

    actions.register(
        ActionDefinition(
            id="no_action",
            description=(
                'Finish the current cycle and wait for new events. Must be the only action in the response.'
            ),
            data_schema=empty_object_schema(),
            result_schema=empty_object_schema(),
            run=lambda data, context: {},
            owner="core",
        )
    )
    events.register(
        EventDefinition(
            event_id="structure_error",
            description=(
                'The previous response violated the JSON protocol or available input schema. Return a corrected response with fresh call identifiers.'
            ),
            data_schema=object_schema(
                {
                    "code": {"type": "string"},
                    "message": {"type": "string"},
                    "response": {"type": "string"},
                }
            ),
        ),
        owner="core",
    )
    events.register(
        EventDefinition(
            event_id="message_from_agent",
            description=(
                'A direct message from another agent. from_agent_id and from_name identify the sender; text contains their message or task.'
            ),
            data_schema=object_schema(
                {
                    "from_agent_id": {"type": "string"},
                    "from_name": {"type": "string"},
                    "text": {"type": "string"},
                }
            ),
        ),
        owner="core",
    )
    events.register(
        EventDefinition(
            event_id="system_started",
            description=(
                'The system has started. This event gives you an opportunity to take initiative: consider your personality, available capabilities and current context, then independently choose a useful action or begin an interaction. Initiative is welcome but no specific action is mandatory. datetime is the local startup time with timezone.'
            ),
            data_schema=object_schema({
                "datetime": {
                    "type": "string",
                    "description": "Local startup time in ISO 8601 with timezone offset.",
                }
            }),
        ),
        owner="core:primary",
    )
    events.register(
        EventDefinition(
            event_id="capability_error",
            description=(
                'An unhandled capability failure. kind and id identify its source; error contains details and call_id identifies an affected invocation if any.'
            ),
            data_schema=object_schema(
                {
                    "kind": {
                        "type": "string",
                        "enum": ["module", "action", "handler"],
                    },
                    "id": {"type": "string"},
                    "error": {"type": "string"},
                    "call_id": {"type": ["string", "null"]},
                }
            ),
        ),
        owner="core",
    )
    actions.register(
        ActionDefinition(
            id="speech",
            description=(
                'Speak text aloud. Returns successful after playback or interrupted if new user speech interrupted it.'
            ),
            data_schema=object_schema({"text": {"type": "string"}}),
            result_schema=object_schema(
                {
                    "status": {
                        "type": "string",
                        "enum": ["successful", "interrupted"],
                    }
                }
            ),
            run=_speech_action_run,
            owner="core:speech",
        )
    )
    events.register(
        EventDefinition(
            event_id="speech_detected",
            description=(
                'User speech recognized from the microphone. text contains the recognized utterance.'
            ),
            data_schema=object_schema({"text": {"type": "string"}}),
        ),
        owner="core:speech",
    )


def _speech_action_run(data, context):
    from ..speech import service

    return service.speak_result(data["text"])


def _capability_identity(action_id: str) -> tuple[str, str]:
    """Capability, отвечающая за упавший вызов.

    Вызов действия внутри модуля сообщает весь модуль: по определению,
    capability — это целый модуль, автономное действие или handler, а
    единица модуля сама по себе capability не является.
    """

    if "." in action_id:
        return "module", action_id.split(".", 1)[0]
    return "action", action_id


class EventBus:
    """Адресно доставляет события actions и рассылает события handlers."""

    def __init__(self, events: EventRegistry, *, debug: Debugger | None = None):
        self.events = events
        self.debug = debug or Debugger(enabled=False)
        self._agents: dict[str, Agent] = {}
        self._lock = threading.RLock()

    def bind(self, agent: "Agent") -> None:
        with self._lock:
            self._agents[agent.agent_id] = agent

    def unbind(self, agent_id: str) -> None:
        with self._lock:
            self._agents.pop(agent_id, None)

    def publish(self, event: Event) -> bool:
        try:
            self.events.validate(event.event_id, event.data)
        except ValueError as exc:
            self.debug.log("event_rejected", event=event.debug_value(), error=str(exc))
            return False

        with self._lock:
            if event.target is not None:
                recipients = [self._agents.get(event.target)]
            elif event.handler_id is not None:
                recipients = [
                    agent
                    for agent in self._agents.values()
                    if agent.accepts_event(event)
                ]
            else:
                recipients = []
            delivered = False
            for agent in recipients:
                if agent is not None and (event.handler_id is None or agent.accepts_event(event)):
                    accepted = agent.enqueue(event)
                    delivered = (accepted is not False) or delivered
        if not delivered:
            self.debug.log(
                "event_dropped",
                reason="no_recipient",
                event=event.debug_value(),
            )
        return delivered


class PendingAction:
    """Незавершённое действие, ожидающее ровно один авторский результат."""

    def __init__(
        self,
        *,
        agent_id: str,
        call_id: str,
        action_id: str,
        result_schema: dict[str, Any],
    ):
        self.agent_id = agent_id
        self.call_id = call_id
        self.action_id = action_id
        self.result_schema = result_schema


class CallResultTracker:
    """Связывает авторские результаты с действиями по call_id."""

    def __init__(self, *, debug: Debugger | None = None):
        self.debug = debug or Debugger(enabled=False)
        self._lock = threading.RLock()
        self._pending: dict[tuple[str, str], PendingAction] = {}

    def has_pending(self, agent_id: str, call_id: str) -> bool:
        with self._lock:
            return (agent_id, call_id) in self._pending

    def begin(
        self,
        *,
        agent_id: str,
        call_id: str,
        action_id: str,
        result_schema: dict[str, Any],
    ) -> None:
        with self._lock:
            if (agent_id, call_id) in self._pending:
                raise ValueError(
                    f"call_id повторяется в ответе: {call_id!r}"
                )
            self._pending[(agent_id, call_id)] = PendingAction(
                agent_id=agent_id,
                call_id=call_id,
                action_id=action_id,
                result_schema=result_schema,
            )

    def complete(
        self,
        manager: "AgentManager",
        *,
        agent_id: str,
        call_id: str,
        data: dict[str, Any],
        parts: tuple = (),
    ) -> bool:
        with self._lock:
            pending = self._pending.get((agent_id, call_id))
        if pending is None:
            self.debug.log(
                "call_result_rejected",
                agent_id=agent_id,
                call_id=call_id,
                reason="unknown_or_completed",
            )
            return False
        try:
            validate_result(
                data,
                pending.result_schema,
                where=f"результат {pending.action_id}",
            )
        except ValueError as exc:
            self.debug.log(
                "call_result_rejected",
                agent_id=agent_id,
                call_id=call_id,
                reason="invalid_schema",
                error=str(exc),
            )
            manager.fail_call(agent_id, call_id, f"Некорректный результат {pending.action_id}: {exc}")
            return False
        with self._lock:
            if self._pending.pop((agent_id, call_id), None) is not pending:
                return False
        result = CallResult(
            call_id=call_id, data=data, agent_id=agent_id, parts=tuple(parts)
        )
        return manager.deliver_result(result)

    def discard(self, agent_id: str, call_id: str) -> PendingAction | None:
        with self._lock:
            return self._pending.pop((agent_id, call_id), None)

    def for_agent(self, agent_id: str) -> list[PendingAction]:
        with self._lock:
            return [item for item in self._pending.values() if item.agent_id == agent_id]

    def for_capability(self, action_ids: set[str], agent_ids: set[str] | None = None) -> list[PendingAction]:
        with self._lock:
            return [item for item in self._pending.values() if item.action_id in action_ids and (agent_ids is None or item.agent_id in agent_ids)]

    def discard_agent(self, agent_id: str) -> None:
        with self._lock:
            for key in [key for key in self._pending if key[0] == agent_id]:
                self._pending.pop(key, None)

    def clear(self) -> None:
        with self._lock:
            self._pending.clear()


class Agent:
    """Самостоятельный агент со своим контекстом, пачкой и потоком."""

    def __init__(
        self,
        agent_id: str,
        name: str,
        preset: str,
        manager: "AgentManager",
        *,
        parent_id: str | None = None,
        primary: bool = False,
        enabled_modules: set[str] | None = None,
        enabled_actions: set[str] | None = None,
        enabled_handlers: set[str] | None = None,
        disabled_modules: set[str] | None = None,
        disabled_actions: set[str] | None = None,
        disabled_handlers: set[str] | None = None,
        restored_messages: list[dict[str, Any]] | None = None,
        person_prompt: str | None = None,
        protected: bool | None = None,
    ):
        self.agent_id = agent_id
        self.name = name
        self.preset = preset
        self.parent_id = parent_id
        self.manager = manager
        self.primary = primary
        selected = manager.presets.load(preset)
        self.person_prompt = selected.person_prompt if person_prompt is None or preset in {"main", "module_manager"} else person_prompt
        self.protected = selected.protected if protected is None else protected
        self._assignment_order = {
            "modules": list(enabled_modules or ()),
            "actions": list(enabled_actions or ()),
            "handlers": list(enabled_handlers or ()),
        }
        self._enabled_modules = set(enabled_modules or ())
        self._enabled_actions = set(enabled_actions or ())
        self._enabled_handlers = set(enabled_handlers or ())
        self._disabled_modules = set(disabled_modules or ())
        self._disabled_actions = set(disabled_actions or ())
        self._disabled_handlers = set(disabled_handlers or ())
        self._capabilities_lock = threading.RLock()
        self.history: list[dict[str, Any]] = [
            {"role": "system", "content": ""},
            *[dict(message) for message in (restored_messages or ()) if message.get("role") != "system"],
        ]
        for message in self.history[1:]:
            if message.get("role") != "user":
                continue
            content = message.get("content")
            text = content if isinstance(content, str) else content[0].get("text") if isinstance(content, list) and content and isinstance(content[0], dict) else None
            try:
                value = json.loads(text)
            except (ValueError, TypeError):
                continue
            if not isinstance(value, dict):
                continue
            current = canonical_event_value(value, self.manager.capabilities.event_id_for_handler)
            if current != value:
                if isinstance(content, str):
                    message["content"] = json.dumps(current, ensure_ascii=False, separators=(",", ":"))
                else:
                    message["content"] = [{**content[0], "text": json.dumps(current, ensure_ascii=False, separators=(",", ":"))}, *content[1:]]
        self._events: "queue.Queue[Event | CallResult | None]" = queue.Queue()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._state = "created"
        self._state_lock = threading.Lock()
        self._generation = 0
        self._generation_lock = threading.RLock()
        self._active_stream = None
        self._used_call_ids = self._scan_call_ids(self.history)

    def _is_automated_result(self, item: Event | CallResult) -> bool:
        return (isinstance(item, CallResult)
                and bool(re.fullmatch(r"auto-[0-9]{5,}", item.call_id))
                and item.call_id in self._used_call_ids)

    @staticmethod
    def _scan_call_ids(messages: list[dict[str, Any]]) -> set[str]:
        used = set()
        for message in messages:
            if message.get("role") != "assistant":
                continue
            try:
                value = json.loads(message.get("content") or "")
            except (ValueError, TypeError):
                content = message.get("content")
                if isinstance(content, str):
                    for match in re.finditer(r'"call_id"\s*:\s*"([^"\\]+)"', content):
                        used.add(match.group(1))
                continue
            if isinstance(value, dict) and isinstance(value.get("actions"), list):
                for action in value["actions"]:
                    if isinstance(action, dict) and isinstance(action.get("call_id"), str):
                        used.add(action["call_id"])
        return used

    @property
    def state(self) -> str:
        with self._state_lock:
            return self._state

    def _set_state(self, state: str, **extra: Any) -> None:
        if self._stop.is_set() and state != "stopped":
            return
        with self._state_lock:
            self._state = state
        self.manager.debug.state(self.agent_id, state, **extra)

    def modules(self) -> set[str]:
        with self._capabilities_lock:
            return set(self._enabled_modules)

    def standalone_actions(self) -> set[str]:
        with self._capabilities_lock:
            return set(self._enabled_actions)

    def standalone_handlers(self) -> set[str]:
        with self._capabilities_lock:
            return set(self._enabled_handlers)

    def capabilities_snapshot(self) -> dict[str, set[str]]:
        with self._capabilities_lock:
            return {
                "modules": set(self._enabled_modules),
                "actions": set(self._enabled_actions),
                "handlers": set(self._enabled_handlers),
            }

    def known_snapshot(self) -> dict[str, set[str]]:
        snapshot = self.capabilities_snapshot()
        with self._capabilities_lock:
            snapshot["modules"].update(self._disabled_modules)
            snapshot["actions"].update(self._disabled_actions)
            snapshot["handlers"].update(self._disabled_handlers)
        return snapshot

    def disabled_snapshot(self) -> dict[str, set[str]]:
        with self._capabilities_lock:
            return {"modules": set(self._disabled_modules), "actions": set(self._disabled_actions), "handlers": set(self._disabled_handlers)}

    def is_enabled_action(self, action_id: str) -> bool:
        if action_id in {"no_action", "speech"}:
            return True
        snapshot = self.capabilities_snapshot()
        return action_id in snapshot["actions"] or any(
            action_id in self.manager.capabilities.module_action_ids(module_id)
            for module_id in snapshot["modules"]
        )

    def enable_module(self, module_id: str) -> None:
        with self._capabilities_lock:
            if module_id not in self._enabled_modules:
                self._assignment_order["modules"] = [name for name in self._assignment_order["modules"] if name != module_id] + [module_id]
            self._enabled_modules.add(module_id)
            self._disabled_modules.discard(module_id)
        self._persist_context()

    def disable_module(self, module_id: str) -> None:
        with self._capabilities_lock:
            self._enabled_modules.discard(module_id)
            self._disabled_modules.add(module_id)
        self._persist_context()

    def enable_action(self, action_id: str) -> None:
        with self._capabilities_lock:
            if action_id not in self._enabled_actions:
                self._assignment_order["actions"] = [name for name in self._assignment_order["actions"] if name != action_id] + [action_id]
            self._enabled_actions.add(action_id)
            self._disabled_actions.discard(action_id)
        self._persist_context()

    def disable_action(self, action_id: str) -> None:
        with self._capabilities_lock:
            self._enabled_actions.discard(action_id)
            self._disabled_actions.add(action_id)
        self._persist_context()

    def enable_handler(self, handler_id: str) -> None:
        with self._capabilities_lock:
            if handler_id not in self._enabled_handlers:
                self._assignment_order["handlers"] = [name for name in self._assignment_order["handlers"] if name != handler_id] + [handler_id]
            self._enabled_handlers.add(handler_id)
            self._disabled_handlers.discard(handler_id)
        self._persist_context()

    def disable_handler(self, handler_id: str) -> None:
        with self._capabilities_lock:
            self._enabled_handlers.discard(handler_id)
            self._disabled_handlers.add(handler_id)
        self._persist_context()

    def accepts_event(self, event: Event) -> bool:
        if event.handler_id == "core:speech":
            return self.primary
        if event.handler_id is not None and self.manager.capabilities.is_globally_paused(
            "handler", event.handler_id
        ):
            return False
        if event.module_id is not None and self.manager.capabilities.is_globally_paused(
            "module", event.module_id
        ):
            return False
        snapshot = self.capabilities_snapshot()
        if event.handler_id is not None and event.handler_id in snapshot["handlers"]:
            return True
        if event.module_id is not None and event.module_id in snapshot["modules"]:
            return True
        return False

    def memory_record(self) -> dict[str, Any]:
        """Снимок экземпляра для долговременной памяти."""

        with self._capabilities_lock:
            snapshot = self.capabilities_snapshot()
            disabled = self.disabled_snapshot()
            assignments = {key: [name for name in self._assignment_order[key] if name in snapshot[key]] for key in ("modules", "actions", "handlers")}
        return {
            "agent_id": self.agent_id,
            "name": self.name,
            "preset": self.preset,
            "person_prompt": self.person_prompt,
            "protected": self.protected,
            "parent_id": self.parent_id,
            **assignments,
            "disabled_modules": sorted(disabled["modules"]),
            "disabled_actions": sorted(disabled["actions"]),
            "disabled_handlers": sorted(disabled["handlers"]),
            "messages": list(self.history[1:]),
        }

    def _persist_context(self) -> None:
        self.manager.persist_agent(self)

    def _contract(
        self,
        snapshot: dict[str, set[str]] | None = None,
    ) -> tuple[dict[str, ActionDefinition], dict[str, EventDefinition]]:
        assigned = self.capabilities_snapshot()
        paused = self.manager.capabilities.global_paused()
        enabled = {key: assigned[key] - paused[key] for key in assigned}
        snapshot = enabled if snapshot is None else {key: set(snapshot[key]) & enabled[key] for key in enabled}
        missing = self.manager.capabilities.missing(
            self.manager.capabilities.runnable_snapshot(self.capabilities_snapshot())
        )
        if missing:
            raise ValueError(
                f"Пресет {self.preset} ссылается на незагруженные capabilities: "
                f"{sorted(missing)}"
            )
        actions = self.manager.actions.for_capabilities(
            modules=snapshot["modules"], actions=snapshot["actions"],
            primary=self.primary,
            developer=self.preset == "module_manager",
            protected=self.protected,
        )
        actions = {name: spec for name, spec in actions.items() if name not in self._disabled_actions and name not in paused["actions"]}
        assigned_actions = [name for name in self._assignment_order["actions"] if name in actions]
        actions = {**{name: spec for name, spec in actions.items() if name not in assigned_actions},
                   **{name: actions[name] for name in assigned_actions}}
        events = self.manager.events.for_capabilities(
            modules=snapshot["modules"], handlers=snapshot["handlers"],
            primary=self.primary,
        )
        assigned_events = [self.manager.capabilities.event_id_for_handler(name) for name in self._assignment_order["handlers"] if name in snapshot["handlers"]]
        events = {**{name: spec for name, spec in events.items() if name not in assigned_events},
                  **{name: events[name] for name in assigned_events}}
        if self.primary:
            for name in ("create_automation", "edit_automation"):
                if name in actions:
                    schema = self.manager.automation_data_schema()
                    if name == "edit_automation":
                        schema = object_schema({"automation_id": {"type": "string"}, "automation": schema})
                    actions[name] = replace(actions[name], data_schema=schema)
        actions, events = apply_descriptions(actions, events)
        self.history[0] = {
            "role": "system",
            "content": agent_system_prompt(
                self.person_prompt.replace("{storage_root}", str(self.manager.capabilities.root.resolve())) if self.preset == "module_manager" else self.person_prompt,
                self.manager.environment,
                actions,
                events,
                self.manager.capabilities.catalog(
                    {key: [name for name in self._assignment_order[key] if name in snapshot[key] and not (key == "actions" and self.manager.actions.get(name) and self.manager.actions.get(name).owner.startswith("core"))] for key in snapshot},
                    describe_unloaded=True,
                ),
                agent_id=self.agent_id,
                agent_name=self.name,
                model_capabilities=self.manager.model_capabilities or ModelCapabilities(
                    self.manager.model, ("text", "image", "audio", "video", "file")
                ),
                semantic_memory=self.manager.semantic_memory.snapshot() if self.primary else None,
            ),
        }
        return actions, events

    def start(self) -> "Agent":
        if self._thread is not None:
            return self
        self._contract()
        self._set_state("waiting")
        self._thread = threading.Thread(
            target=self._run,
            args=(self._generation,),
            name=f"jarvis-agent-{self.agent_id}",
            daemon=True,
        )
        self._thread.start()
        return self

    def enqueue(self, event: Event) -> bool:
        if not self._stop.is_set():
            self._events.put(event)
            return True
        return False

    def enqueue_result(self, result: CallResult) -> None:
        if not self._stop.is_set():
            self._events.put(result)

    def interrupt(self) -> None:
        with self._generation_lock:
            self._generation += 1
            generation = self._generation
            stream = self._active_stream
            self._active_stream = None
            self._thread = threading.Thread(
                target=self._run, args=(generation,),
                name=f"jarvis-agent-{self.agent_id}-{generation}", daemon=True,
            )
            self._thread.start()
            self._set_state("waiting")
        if stream is not None:
            try:
                stream.close()
            except Exception as exc:
                self.manager.debug.error(str(exc))

    def stop(self, *, wait: bool = True, timeout: float = 10.0) -> None:
        self._stop.set()
        with self._generation_lock:
            self._generation += 1
            stream = self._active_stream
            self._active_stream = None
        if stream is not None:
            try:
                stream.close()
            except Exception as exc:
                self.manager.debug.error(str(exc))
        self._events.put(None)
        if wait and self._thread is not None:
            self._thread.join(timeout=timeout)
        self._set_state("stopped")

    def _run(self, generation: int) -> None:
        while not self._stop.is_set() and generation == self._generation:
            try:
                first = self._events.get(timeout=0.1)
            except queue.Empty:
                continue
            if first is None:
                return
            if generation != self._generation:
                self._requeue_front([first])
                return
            batch = [first]
            while True:
                try:
                    event = self._events.get_nowait()
                except queue.Empty:
                    break
                if event is None:
                    self._stop.set()
                    break
                batch.append(event)
            if generation != self._generation:
                self._requeue_front(batch)
                return
            if not self._stop.is_set():
                self._turn(batch, generation)

    def _requeue_front(self, items: list[Event | CallResult]) -> None:
        with self._events.mutex:
            for item in reversed(items):
                self._events.queue.appendleft(item)
            self._events.not_empty.notify_all()

    def _turn(self, batch: list[Event | CallResult], generation: int) -> None:
        """Обработать пачку, удерживая новые события до корректного ответа."""

        try:
            snapshot = self.known_snapshot()
        except Exception as exc:  # noqa: BLE001
            self._model_failure(exc)
            return
        self._run_turn(batch, snapshot, generation)

    def _event_history_message(self, item: Event) -> dict[str, Any]:
        # Keep original attachments in Jarvis memory even when the subscription
        # model cannot receive their modality. API fallback gets the same
        # persisted context and can use its own model capabilities.
        if self.manager.config is not None and self.manager.config.subscription_enabled:
            return item.model_message()
        return item.model_message(self.manager.model_capabilities)

    def _run_turn(
        self, batch: list[Event | CallResult], snapshot: dict[str, set[str]], generation: int
    ) -> None:
        """Выполнить один цикл на неизменяемом снимке capabilities."""

        if generation != self._generation:
            self._requeue_front(batch)
            return
        self._set_state("thinking", event_count=len(batch))
        try:
            specs, _ = self._contract(snapshot)
        except Exception as exc:  # noqa: BLE001
            with self._generation_lock:
                if self._stop.is_set():
                    return
                if generation != self._generation:
                    self._requeue_front(batch)
                    return
                for item in batch:
                    if isinstance(item, CallResult):
                        self.history.append(item.model_message())
                        self.manager.debug.result(item, agent_id=self.agent_id)
                    else:
                        self.history.append(self._event_history_message(item))
                        self.manager.debug.input(
                            item, self.manager.model_capabilities, agent_id=self.agent_id
                        )
                self._persist_context()
                self._model_failure(exc)
                return

        requires_model = False
        for index, item in enumerate(batch):
            with self._generation_lock:
                if self._stop.is_set():
                    return
                if generation != self._generation:
                    self._requeue_front(batch[index:])
                    return
                if isinstance(item, CallResult):
                    self.history.append(item.model_message())
                    self.manager.debug.result(item, agent_id=self.agent_id)
                    model_value = item.model_value()
                else:
                    self.history.append(self._event_history_message(item))
                    self.manager.debug.input(
                        item, self.manager.model_capabilities, agent_id=self.agent_id
                    )
                    model_value = item.model_value()

                automation = self.manager.matching_automation(model_value)
                if not automation:
                    if not self._is_automated_result(item):
                        requires_model = True
                    continue
                automated_actions = self.manager.automation_requests(
                    automation, specs=specs, agent=self
                )
                if automated_actions is None:
                    if not self._is_automated_result(item):
                        requires_model = True
                    continue
                assistant_content = json.dumps(
                    {"actions": [action.model_value() for action in automated_actions]},
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                self.history.append({"role": "assistant", "content": assistant_content})
                self._used_call_ids.update(self._scan_call_ids([self.history[-1]]))
                self.manager.debug.model(self.agent_id, assistant_content)
                self._persist_context()
                self._set_state("acting", action_count=len(automated_actions), automated=True)
                self._execute_actions(automated_actions, specs)

        self._persist_context()
        if not requires_model:
            self._set_state("waiting")
            return

        schemas = {name: spec.data_schema for name, spec in specs.items()}

        failures = 0
        while not self._stop.is_set() and generation == self._generation:
            try:
                stream = self.manager.client.chat.completions.create(
                    model=self.manager.model,
                    messages=list(self.history),
                    response_format=response_format(schemas),
                    **({"reasoning_effort": self.manager.config.reasoning_effort}
                       if self.manager.config is not None and self.manager.config.reasoning_effort is not None else {}),
                    stream=True,
                )
                with self._generation_lock:
                    if generation != self._generation or self._stop.is_set():
                        stream.close()
                        return
                    self._active_stream = stream
                content = ""
                refusal = ""
                try:
                    for chunk in stream:
                        if generation != self._generation or self._stop.is_set():
                            return
                        if chunk.choices:
                            delta = chunk.choices[0].delta
                            content += delta.content or ""
                            refusal += getattr(delta, "refusal", None) or ""
                finally:
                    stream.close()
                    with self._generation_lock:
                        if self._active_stream is stream:
                            self._active_stream = None
            except Exception as exc:  # noqa: BLE001
                if generation != self._generation:
                    return
                failures += 1
                self.manager.debug.error(str(exc))
                if failures >= 5:
                    self._model_failure(exc)
                    return
                delay = min(0.25 * 2 ** (failures - 1), 4.0)
                deadline = time.monotonic() + delay
                while time.monotonic() < deadline and generation == self._generation and not self._stop.is_set():
                    time.sleep(min(0.05, deadline - time.monotonic()))
                continue

            with self._generation_lock:
                if generation != self._generation or self._stop.is_set():
                    return
                appeared = self._scan_call_ids([{"role": "assistant", "content": content}])
                repeated = appeared & self._used_call_ids
                self._used_call_ids.update(appeared)
                self.history.append({"role": "assistant", "content": content})
                self._persist_context()
            self.manager.debug.model(self.agent_id, content)
            if refusal:
                self._append_structure_error(
                    "refusal",
                    f"Модель отказалась выполнить запрос: {refusal}",
                    content,
                    generation=generation,
                )
                continue

            try:
                if repeated:
                    raise ValueError(f"call_id уже использован в context: {sorted(repeated)!r}")
                if any(re.fullmatch(r"auto-[0-9]{5,}", identifier) for identifier in appeared):
                    raise ValueError("auto-xxxxx call_id зарезервирован для автоматизаций")
                value = json.loads(content)
                validate_json(
                    value,
                    actions_response_schema(schemas),
                    where=f"ответ агента {self.agent_id}",
                )
                actions = parse_actions(value)
                global_changes: set[tuple[str, str]] = set()
                for action in actions:
                    if action.action_id == "toggle_capability":
                        target = (action.data["kind"], action.data["id"])
                        if target in global_changes:
                            raise ValueError(
                                "Global disable and enable for one capability must be called in separate responses; wait for call_result"
                            )
                        global_changes.add(target)
                    if action.action_id not in specs:
                        raise ValueError(
                            f"Действие недоступно этому агенту: {action.action_id}"
                        )
                    if self.manager.results.has_pending(
                        self.agent_id, action.call_id
                    ):
                        raise ValueError(
                            f"call_id повторяется в ответе: {action.call_id!r}"
                        )
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                code = "invalid_json" if isinstance(exc, json.JSONDecodeError) else "invalid_structure"
                self._append_structure_error(code, str(exc), content, generation=generation)
                continue

            if len(actions) == 1 and actions[0].action_id == "no_action":
                self._set_state("waiting")
                return
            self._set_state("acting", action_count=len(actions))
            with self._generation_lock:
                if generation != self._generation or self._stop.is_set():
                    return
                self._execute_actions(actions, specs)
            if not self._stop.is_set():
                self._set_state("waiting")
            return

    def _append_structure_error(self, code: str, message: str, response: str, *, generation: int | None = None) -> None:
        with self._generation_lock:
            if self._stop.is_set() or (generation is not None and generation != self._generation):
                return
            self._append_structure_error_locked(code, message, response)

    def _append_structure_error_locked(self, code: str, message: str, response: str) -> None:
        event = Event(
            event_id="structure_error",
            data={"code": code, "message": message, "response": response},
            source="core",
            target=self.agent_id,
        )
        self.history.append(
            event.model_message(self.manager.model_capabilities)
        )
        self.manager.debug.input(
            event, self.manager.model_capabilities, agent_id=self.agent_id
        )
        self._persist_context()

    def _model_failure(self, exc: Exception) -> None:
        self.manager.debug.log(
            "model_request_error",
            agent_id=self.agent_id,
            error=str(exc),
            traceback=traceback.format_exc(),
        )
        self._set_state("waiting")

    def _execute_actions(
        self,
        actions: list[ActionRequest],
        specs: dict[str, ActionDefinition],
    ) -> None:
        for action in actions:
            if self._stop.is_set() or self.manager.stopping.is_set():
                break
            spec = specs[action.action_id]
            try:
                self.manager.capabilities.dispatch(
                    action=action,
                    spec=spec,
                    agent=self,
                )
            except Exception as exc:  # noqa: BLE001
                kind, capability_id = _capability_identity(action.action_id)
                self.manager.report_capability_error(kind, capability_id, exc, agent_id=self.agent_id, call_id=action.call_id)

class AgentManager:
    """Управляет равноправными экземплярами агентов."""

    def __init__(
        self,
        *,
        model: str,
        client: Any,
        actions: ActionRegistry,
        events: EventRegistry,
        bus: EventBus,
        capabilities: Any,
        presets: PresetStore,
        environment: str,
        config: Any = None,
        services: dict[str, Any] | None = None,
        debug: Debugger | None = None,
        model_capabilities: ModelCapabilities | None = None,
        memory: MemoryStore | None = None,
    ):
        self.model = model
        self.client = client
        self.actions = actions
        self.events = events
        self.bus = bus
        self.capabilities = capabilities
        self.presets = presets
        self.environment = environment
        self.config = config
        self.services = services if services is not None else {}
        self.model_capabilities = model_capabilities
        self.debug = debug or Debugger(enabled=False)
        self.memory = memory
        self.semantic_memory = SemanticMemory(
            (Path(config.jarvis_dir) if config is not None else presets.root.parent) / "memory" / "main" / "semantic.json"
        )
        self.results = CallResultTracker(debug=self.debug)
        automation_path = (
            Path(config.jarvis_dir) / "automations.json"
            if config is not None
            else presets.root.parent / "automations.json"
        )
        self.automations = AutomationStore(automation_path, resolve_handler=self.capabilities.event_id_for_handler)
        self.stopping = threading.Event()
        self.processes = ProcessManager()
        self.services.setdefault("process_manager", self.processes)
        self.agents: dict[str, Agent] = {}
        self.primary_agent_id: str | None = None
        self._lock = threading.RLock()
        self._lifecycle_lock = threading.RLock()
        bus.manager = self  # type: ignore[attr-defined]

    def matching_automation(self, model_value: dict[str, Any]) -> list[dict[str, Any]]:
        try:
            return self.automations.matching(model_value)
        except Exception as exc:  # noqa: BLE001
            self.debug.log("automation_read_error", error=str(exc))
            return []

    def automation_requests(
        self,
        automations: list[dict[str, Any]],
        *,
        specs: dict[str, ActionDefinition],
        agent: Agent,
    ) -> list[ActionRequest] | None:
        requests = []
        reserved = set(agent._used_call_ids)
        for automation in automations:
            for item in automation["actions"]:
                spec = specs.get(item["action_id"])
                if spec is None:
                    self.debug.log(
                        "automation_action_unavailable",
                        agent_id=agent.agent_id,
                        action_id=item["action_id"],
                    )
                    return None
                try:
                    validate_json(
                        item["data"], spec.data_schema,
                        where=f"automation action {item['action_id']}",
                    )
                except ValueError as exc:
                    self.debug.log(
                        "automation_action_invalid",
                        agent_id=agent.agent_id,
                        action_id=item["action_id"],
                        error=str(exc),
                    )
                    return None
                call_number = max((int(value[5:]) for value in reserved
                                   if re.fullmatch(r"auto-[0-9]{5,}", value)), default=0) + 1
                call_id = f"auto-{call_number:05d}"
                reserved.add(call_id)
                requests.append(
                    ActionRequest(
                        action_id=item["action_id"],
                        call_id=call_id,
                        data=item["data"],
                    )
                )
        agent._used_call_ids.update(action.call_id for action in requests)
        return requests

    def automation_data_schema(self) -> dict[str, Any]:
        """Describe automation structure once, without copying capability catalogs."""
        data = object_schema({}, additional_properties=True)
        action = object_schema({
            "action_id": {"type": "string", "description": "An available action identifier."},
            "data": {**data, "description": "Arguments matching that action's data_schema."},
        })
        actions = {"type": "array", "minItems": 1, "items": action, "description": "Actions to execute when the trigger matches, without call identifiers."}
        event = object_schema({
            "event_id": {"type": "string", "description": "The exact event identifier."},
            "data": {**data, "description": "The complete event data to match exactly."},
        })
        result = object_schema({
            "event_id": {"type": "string", "enum": ["call_result"]},
            "call_id": {"type": "string", "description": "Example invocation identifier; its value is ignored when matching."},
            "data": {**data, "description": "The complete result data to match exactly."},
        })
        return object_schema({
            "event": {**event, "type": ["object", "null"], "description": "Complete event trigger, or null when using a call_result trigger.", "default": None},
            "call_result": {**result, "type": ["object", "null"], "description": "Complete result trigger, or null when using an event trigger.", "default": None},
            "actions": actions,
        }, required=["actions"])

    def _new_id(self) -> str:
        with self._lock:
            if len(self.agents) >= 1000:
                raise RuntimeError("Исчерпаны идентификаторы agent-000..agent-999")
            for _ in range(2000):
                candidate = f"agent-{secrets.randbelow(1000):03d}"
                if candidate not in self.agents:
                    return candidate
        raise RuntimeError("Не удалось подобрать свободный идентификатор агента")

    def deliver_result(self, result: CallResult) -> bool:
        """Доставить результат только инициировавшему агенту."""

        with self._lock:
            agent = self.agents.get(result.agent_id)
        if agent is None:
            self.debug.log(
                "call_result_dropped",
                call_id=result.call_id,
                reason="unknown_agent",
            )
            return False
        agent.enqueue_result(result)
        return True

    def spawn_root(self, *, name: str, preset: str) -> Agent:
        """Создать первый экземпляр без родителя. Имя main — константа."""

        return self._spawn(
            name=PRIMARY_AGENT_NAME, preset=preset, parent_id=None, primary=True
        )

    def restore(self, *, name: str, preset: str) -> Agent | None:
        """Поднять экземпляры из памяти или создать новый корневой агент."""

        records = self.memory.load_all() if self.memory is not None else []
        if not records and self.memory is not None and self.memory.has_existing_state():
            logger.error("Jarvis restore failed: сохранённая память есть, но не содержит целого состояния; данные сохранены")
            return None
        if not records:
            return self.spawn_root(name=name, preset=preset)
        return self._restore(records, default_name=name, default_preset=preset)

    def _restore(
        self, records: list[dict[str, Any]], *, default_name: str, default_preset: str
    ) -> Agent | None:
        primary = next(
            (
                record
                for record in records
                if record["agent_id"] == "main" and record["parent_id"] is None
            ),
            None,
        )
        if primary is None:
            primary = next(
                (record for record in records if record["parent_id"] is None),
                None,
            )
        if primary is None:
            self.debug.log("memory_restore_failed", error="Нет сохранённого корневого агента")
            logger.error("Jarvis restore failed: нет целого корневого агента; сохранённые данные оставлены")
            return None
        primary = {**primary, "name": PRIMARY_AGENT_NAME}
        try:
            main_agent = self._spawn_record(primary, primary=True)
        except Exception as exc:  # noqa: BLE001
            self.debug.log(
                "memory_restore_failed",
                agent_id=primary.get("agent_id"),
                error=str(exc),
                traceback=traceback.format_exc(),
            )
            logger.error(f"Jarvis restore failed for {primary.get('agent_id')}: {traceback.format_exc()}")
            return None

        spawned = {main_agent.agent_id}
        remaining = [
            record for record in records if record["agent_id"] != main_agent.agent_id
        ]
        progress = True
        while remaining and progress:
            progress = False
            for record in list(remaining):
                if record["parent_id"] in spawned:
                    try:
                        self._spawn_record(record, primary=False)
                    except Exception as exc:  # noqa: BLE001
                        self.debug.log(
                            "memory_restore_failed",
                            agent_id=record["agent_id"],
                            error=str(exc),
                            traceback=traceback.format_exc(),
                        )
                        logger.error(f"Jarvis restore failed for {record['agent_id']}: {traceback.format_exc()}")
                    else:
                        spawned.add(record["agent_id"])
                    remaining.remove(record)
                    progress = True
        for record in remaining:
            self.debug.log(
                "memory_restore_skipped",
                agent_id=record["agent_id"],
                reason="missing_parent",
            )
            logger.error(f"Jarvis restore skipped {record['agent_id']}: parent {record['parent_id']} отсутствует")
        return main_agent

    def _spawn_record(self, record: dict[str, Any], *, primary: bool) -> Agent:
        snapshot = {
            "modules": list(record.get("modules") or ()),
            "actions": list(record.get("actions") or ()),
            "handlers": list(record.get("handlers") or ()),
        }
        return self._spawn(
            name=record["name"],
            preset=record["preset"],
            parent_id=record["parent_id"],
            primary=primary,
            agent_id=record["agent_id"],
            capabilities_override=None if record.get("restore_from_preset") else snapshot,
            disabled_override=None if record.get("restore_from_preset") else {
                "modules": set(record.get("disabled_modules") or ()),
                "actions": set(record.get("disabled_actions") or ()),
                "handlers": set(record.get("disabled_handlers") or ()),
            },
            restored_messages=record["messages"],
            person_prompt=None if record.get("restore_from_preset") else record.get("person_prompt"),
            protected=None if record.get("restore_from_preset") else record.get("protected"),
            persist_initial=bool(record.get("restore_from_preset") or record.get("restore_missing_context")),
        )

    def spawn(self, *, parent_id: str, name: str, preset: str) -> dict[str, Any]:
        if parent_id not in self.agents:
            raise ValueError(f"Родительский агент не найден: {parent_id}")
        selected = self.presets.load(preset)
        agent = self._spawn(name=name, preset=preset, parent_id=parent_id)
        return self.describe(agent)

    def _spawn(self, **kwargs) -> Agent:
        with self._lifecycle_lock:
            return self._spawn_locked(**kwargs)

    def _spawn_locked(
        self,
        *,
        name: str,
        preset: str,
        parent_id: str | None,
        primary: bool = False,
        agent_id: str | None = None,
        capabilities_override: dict[str, list[str] | set[str]] | None = None,
        disabled_override: dict[str, set[str]] | None = None,
        restored_messages: list[dict[str, Any]] | None = None,
        persist_initial: bool = True,
        person_prompt: str | None = None,
        protected: bool | None = None,
    ) -> Agent:
        if self.stopping.is_set():
            raise RuntimeError("runtime_stopping")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("Имя агента не должно быть пустым")
        selected = self.presets.load(preset)
        with self._lock:
            if selected.protected and any(agent.preset == preset for agent in self.agents.values()):
                raise ValueError(f"Пресет {preset} защищён: его экземпляр уже существует")
        if capabilities_override is not None:
            initial = {
                "modules": set(capabilities_override.get("modules", ())),
                "actions": set(capabilities_override.get("actions", ())),
                "handlers": set(capabilities_override.get("handlers", ())),
            }
        else:
            initial = {
                "modules": set(selected.modules) - set(selected.disabled_modules),
                "actions": set(selected.actions) - set(selected.disabled_actions),
                "handlers": set(selected.handlers) - set(selected.disabled_handlers),
            }
            disabled_override = {
                "modules": set(selected.disabled_modules),
                "actions": set(selected.disabled_actions),
                "handlers": set(selected.disabled_handlers),
            }
        disabled_override = disabled_override or {"modules": set(), "actions": set(), "handlers": set()}
        from .system_actions import SYSTEM_MAIN, SYSTEM_DEVELOPER, SYSTEM_ALL, SYSTEM_PROTECTED
        system_ids = SYSTEM_ALL | (SYSTEM_MAIN | {"speech"} if primary else SYSTEM_DEVELOPER if preset == "module_manager" else set())
        if selected.protected:
            system_ids |= SYSTEM_PROTECTED
        initial["actions"].difference_update(system_ids)
        self.capabilities.load_snapshot(
            self.capabilities.runnable_snapshot(initial), start_handlers=False
        )
        with self._lock:
            if primary:
                resolved_id = "main"
            elif agent_id is not None:
                resolved_id = agent_id
            else:
                resolved_id = self._new_id()
            if resolved_id in self.agents:
                raise ValueError(f"Экземпляр {resolved_id} уже существует")
            agent = Agent(
                resolved_id,
                name.strip(),
                preset,
                self,
                parent_id=parent_id,
                primary=primary,
                enabled_modules=[name for name in (capabilities_override or {"modules": selected.modules})["modules"] if name in initial["modules"]],
                enabled_actions=[name for name in (capabilities_override or {"actions": selected.actions})["actions"] if name in initial["actions"]],
                enabled_handlers=[name for name in (capabilities_override or {"handlers": selected.handlers})["handlers"] if name in initial["handlers"]],
                disabled_modules=(disabled_override or {}).get("modules"),
                disabled_actions=(disabled_override or {}).get("actions"),
                disabled_handlers=(disabled_override or {}).get("handlers"),
                restored_messages=restored_messages,
                person_prompt=person_prompt,
                protected=protected,
            )
            self.agents[resolved_id] = agent
            self.bus.bind(agent)
            if primary and self.primary_agent_id is None:
                self.primary_agent_id = resolved_id
        try:
            agent.start()
            self.capabilities.start_snapshot(
                self.capabilities.runnable_snapshot(initial)
            )
        except Exception:
            with self._lock:
                self.agents.pop(resolved_id, None)
                if self.primary_agent_id == resolved_id:
                    self.primary_agent_id = None
            self.bus.unbind(resolved_id)
            self.capabilities.release_snapshot(initial, agents=self.agents_snapshot())
            raise
        if persist_initial:
            self.persist_agent(agent)
        return agent

    def agents_snapshot(self) -> list[Agent]:
        with self._lock:
            return list(self.agents.values())

    def persist_agent(self, agent: Agent) -> None:
        """Сохранить метаданные и контекст экземпляра в память."""

        if self.memory is None:
            return
        with self._lock:
            # Удалённый агент мог завершить ход уже после memory.delete;
            # поздний persist не должен воскрешать его запись.
            if self.agents.get(agent.agent_id) is not agent or agent._stop.is_set():
                return
            try:
                self.memory.save(agent.memory_record())
            except Exception as exc:  # noqa: BLE001
                self.debug.log("memory_save_error", agent_id=agent.agent_id, error=str(exc))
                logger.error(f"Jarvis persistence failed for {agent.agent_id}: {traceback.format_exc()}")

    def require_agent(self, agent_id: str) -> Agent:
        agent = self.agents.get(agent_id)
        if agent is None:
            raise ValueError(f"Агент не найден: {agent_id}")
        return agent

    def interrupt(self, *, agent_id: str, requester_id: str | None = None) -> dict[str, Any]:
        agent = self.require_agent(agent_id)
        if agent.protected and requester_id != agent_id:
            raise ValueError(f"Защищённый агент {agent_id} не может быть прерван извне")
        agent.interrupt()
        return {"agent_id": agent_id, "state": "waiting"}

    def delete(self, *, agent_id: str) -> dict[str, Any]:
        with self._lifecycle_lock:
            return self._delete_locked(agent_id=agent_id)

    def _delete_locked(self, *, agent_id: str) -> dict[str, Any]:
        self.require_agent(agent_id)
        ordered = []
        def walk(current: str) -> None:
            for child in self.agents_snapshot():
                if child.parent_id == current:
                    walk(child.agent_id)
            ordered.append(current)
        walk(agent_id)
        for candidate in ordered:
            if self.require_agent(candidate).protected:
                raise ValueError(f"Защищённый агент {candidate} не может быть удалён")
        with self._lock:
            removed = [self.agents.pop(candidate) for candidate in ordered]
        errors = []
        for agent in removed:
            candidate = agent.agent_id
            snapshot = agent.capabilities_snapshot()
            agent.stop(wait=False)
            self.bus.unbind(candidate)
            for pending in self.results.for_agent(candidate):
                self.capabilities.cancel_call(candidate, pending.call_id)
            self.results.discard_agent(candidate)
            try:
                if self.memory is not None:
                    self.memory.delete(agent.preset, candidate)
            except Exception as exc:
                errors.append(str(exc))
            try:
                self.capabilities.release_snapshot(snapshot, agents=self.agents_snapshot())
            except Exception as exc:
                errors.append(str(exc))
            with agent._events.mutex:
                agent._events.queue.clear()
                agent._events.queue.append(None)
                agent._events.not_empty.notify_all()
            with agent._generation_lock:
                agent.history.clear()
                agent._used_call_ids.clear()
                agent.person_prompt = ""
            with agent._capabilities_lock:
                agent._enabled_modules.clear()
                agent._enabled_actions.clear()
                agent._enabled_handlers.clear()
                agent._disabled_modules.clear()
                agent._disabled_actions.clear()
                agent._disabled_handlers.clear()
        if errors:
            raise RuntimeError("Agent cleanup failed: " + "; ".join(errors))
        return {"agent_id": agent_id, "deleted": True}

    def remove_preset(self, preset_id: str) -> None:
        with self._lifecycle_lock:
            if self.presets.load(preset_id).protected:
                raise ValueError("Protected preset cannot be removed")
            records = self.memory.load_all() if self.memory is not None else []
            tree = {item["agent_id"] for item in records if item["preset"] == preset_id}
            tree.update(agent.agent_id for agent in self.agents_snapshot() if agent.preset == preset_id)
            changed = True
            while changed:
                before = len(tree)
                tree.update(item["agent_id"] for item in records if item["parent_id"] in tree)
                tree.update(agent.agent_id for agent in self.agents_snapshot() if agent.parent_id in tree)
                changed = len(tree) != before
            for agent in self.agents_snapshot():
                if agent.agent_id in tree and agent.protected:
                    raise ValueError("Protected descendant cannot be removed")
            for record in records:
                if record["agent_id"] in tree and record.get("protected"):
                    raise ValueError("Protected saved descendant cannot be removed")
            for agent_id in list(tree):
                if agent_id in self.agents:
                    self.delete(agent_id=agent_id)
            if self.memory is not None:
                for record in records:
                    if record["agent_id"] in tree:
                        self.memory.delete(record["preset"], record["agent_id"])
                self.memory.delete_preset(preset_id)
            self.presets.delete(preset_id)

    @staticmethod
    def describe(agent: Agent) -> dict[str, Any]:
        snapshot = agent.capabilities_snapshot()
        return {
            "agent_id": agent.agent_id,
            "name": agent.name,
            "parent_id": agent.parent_id,
            "state": agent.state,
            "preset": agent.preset,
            "modules": sorted(snapshot["modules"]),
            "actions": sorted(snapshot["actions"]),
            "handlers": sorted(snapshot["handlers"]),
        }

    def list_agents(self) -> list[dict[str, Any]]:
        with self._lock:
            return [
                self.describe(agent)
                for agent in sorted(self.agents.values(), key=lambda item: item.agent_id)
            ]

    def agents_with_module(self, module_id: str) -> list[str]:
        with self._lock:
            return sorted(
                agent.agent_id
                for agent in self.agents.values()
                if module_id in agent.modules()
            )

    def agents_with_action(self, action_id: str) -> list[str]:
        with self._lock:
            return sorted(
                agent.agent_id
                for agent in self.agents.values()
                if action_id in agent.standalone_actions()
            )

    def agents_with_handler(self, handler_id: str) -> list[str]:
        with self._lock:
            return sorted(
                agent.agent_id
                for agent in self.agents.values()
                if handler_id in agent.standalone_handlers()
            )

    def enable_module(self, agent_id: str, module_id: str) -> dict[str, Any]:
        agent = self.require_agent(agent_id)
        if self.capabilities.is_globally_paused("module", module_id):
            agent.enable_module(module_id)
            return {"agent_id": agent_id, "module_id": module_id, "enabled": True, "globally_paused": True}
        self.capabilities.load_module(module_id, start_handlers=False)
        agent.enable_module(module_id)
        try:
            self.capabilities.start_module(module_id)
        except Exception:
            agent.disable_module(module_id)
            if not self.agents_with_module(module_id):
                self.capabilities.unload_module(module_id)
            raise
        return {"agent_id": agent_id, "module_id": module_id, "enabled": True}

    def disable_module(self, agent_id: str, module_id: str) -> dict[str, Any]:
        agent = self.require_agent(agent_id)
        for pending in self.results.for_capability(self.capabilities.module_action_ids(module_id), {agent_id}):
            self.disable_call(agent_id, pending.call_id)
        agent.disable_module(module_id)
        if not self.agents_with_module(module_id):
            self.capabilities.unload_module(module_id)
        return {"agent_id": agent_id, "module_id": module_id, "enabled": False}

    def enable_action(self, agent_id: str, action_id: str) -> dict[str, Any]:
        agent = self.require_agent(agent_id)
        spec = self.actions.get(action_id)
        if spec is not None and spec.owner.startswith("core"):
            agent.enable_action(action_id)
            return {"agent_id": agent_id, "action_id": action_id, "enabled": True}
        if self.capabilities.is_globally_paused("action", action_id):
            agent.enable_action(action_id)
            return {"agent_id": agent_id, "action_id": action_id, "enabled": True, "globally_paused": True}
        self.capabilities.load_action(action_id, start_handlers=False)
        agent.enable_action(action_id)
        try:
            self.capabilities.start_action(action_id)
        except Exception:
            agent.disable_action(action_id)
            if not self.agents_with_action(action_id):
                self.capabilities.unload_action(action_id)
            raise
        return {"agent_id": agent_id, "action_id": action_id, "enabled": True}

    def disable_action(self, agent_id: str, action_id: str) -> dict[str, Any]:
        agent = self.require_agent(agent_id)
        for pending in self.results.for_capability({action_id}, {agent_id}):
            self.disable_call(agent_id, pending.call_id)
        agent.disable_action(action_id)
        if not self.agents_with_action(action_id):
            self.capabilities.unload_action(action_id)
        return {"agent_id": agent_id, "action_id": action_id, "enabled": False}

    def enable_handler(self, agent_id: str, handler_id: str) -> dict[str, Any]:
        agent = self.require_agent(agent_id)
        if self.capabilities.is_globally_paused("handler", handler_id):
            agent.enable_handler(handler_id)
            return {"agent_id": agent_id, "handler_id": handler_id, "enabled": True, "globally_paused": True}
        self.capabilities.load_handler(handler_id, start_handlers=False)
        agent.enable_handler(handler_id)
        try:
            self.capabilities.start_handler(handler_id)
        except Exception:
            agent.disable_handler(handler_id)
            if not self.agents_with_handler(handler_id):
                self.capabilities.unload_handler(handler_id)
            raise
        return {"agent_id": agent_id, "handler_id": handler_id, "enabled": True}

    def disable_handler(self, agent_id: str, handler_id: str) -> dict[str, Any]:
        agent = self.require_agent(agent_id)
        agent.disable_handler(handler_id)
        if not self.agents_with_handler(handler_id):
            self.capabilities.unload_handler(handler_id)
        return {"agent_id": agent_id, "handler_id": handler_id, "enabled": False}

    def agents_assigned_and_enabled(self, kind: str, capability_id: str) -> list[Agent]:
        result = []
        for agent in self.agents_snapshot():
            snapshot = agent.capabilities_snapshot()
            if capability_id in snapshot.get({"module": "modules", "action": "actions", "handler": "handlers"}[kind], set()):
                result.append(agent)
        return result

    def toggle_capability(self, kind: str, capability_id: str) -> dict[str, Any]:
        return self.capabilities.toggle_global_state(kind=kind, capability_id=capability_id)

    def fail_call(self, agent_id: str, call_id: str, error: Exception | str) -> None:
        pending = self.results.discard(agent_id, call_id)
        if pending is not None:
            kind, capability_id = _capability_identity(pending.action_id)
            self.report_capability_error(kind, capability_id, error, agent_id=agent_id, call_id=call_id)

    def disable_call(self, agent_id: str, call_id: str) -> None:
        self._finish_stopped_call(
            agent_id, call_id, status="disabled",
            info="Action was disabled before completion.",
        )

    def pause_call(self, agent_id: str, call_id: str) -> None:
        self._finish_stopped_call(
            agent_id, call_id, status="paused",
            info="Capability is globally paused.",
        )

    def _finish_stopped_call(self, agent_id: str, call_id: str, *, status: str, info: str) -> None:
        pending = self.results.discard(agent_id, call_id)
        if pending is not None:
            self.capabilities.cancel_execution(agent_id, call_id, pending.action_id)
            self.deliver_result(CallResult(
                call_id=call_id, agent_id=agent_id,
                data={"status": status, "info": info},
            ))

    def report_capability_error(
        self, kind: str, capability_id: str, error: Exception | str,
        *, agent_id: str | None = None, call_id: str | None = None,
    ) -> None:
        target = agent_id
        event_id = "capability_error"
        data = {"kind": kind, "id": capability_id, "error": str(error), "call_id": call_id}
        if target is None:
            self.debug.log(event_id, **data)
            return
        self.bus.publish(
            Event(
                event_id=event_id,
                data=data,
                source="core",
                target=target,
            )
        )

    def begin_shutdown(self) -> None:
        self.stopping.set()
        self.results.clear()
        with self._lock:
            agents = list(self.agents.values())
        for agent in agents:
            agent.stop(wait=False)

    def shutdown(self) -> None:
        self.begin_shutdown()
        self.processes.stop()
        self.capabilities.shutdown()
        for agent in list(self.agents.values()):
            if agent._thread is not None:
                agent._thread.join(timeout=2)
