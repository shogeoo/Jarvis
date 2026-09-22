"""Неизменяемое event-thinking-action ядро Jarvis."""

from __future__ import annotations

import json
import queue
import secrets
import threading
import traceback
from typing import Any

from ..infrastructure.context import MemoryStore
from ..infrastructure.debug import Debugger
from ..infrastructure.model_capabilities import ModelCapabilities
from ..capabilities.api import ActionDefinition, EventDefinition
from ..presets import PresetStore


PRIMARY_AGENT_NAME = "Jarvis"
from .lifecycle import ProcessManager
from .prompts import agent_system_prompt
from .protocol import (
    ActionRequest,
    ActionResult,
    Event,
    actions_response_schema,
    empty_object_schema,
    object_schema,
    parse_actions,
    response_format,
    validate_json,
)
from .registry import ActionRegistry, EventRegistry


def register_core_protocol(actions: ActionRegistry, events: EventRegistry) -> None:
    """Зарегистрировать зарезервированные элементы протокола."""

    actions.register(
        ActionDefinition(
            id="no_action",
            description=(
                "Завершить текущий цикл и ждать новые события. "
                "Допустимо только как единственное действие ответа."
            ),
            args_schema=empty_object_schema(),
            result_schema=empty_object_schema(),
            run=lambda data, context: {},
            owner="core",
        )
    )
    events.register(
        EventDefinition(
            type="structure_error",
            description=(
                "Предыдущий ответ модели нарушил JSON или доступную схему. "
                "Нужно немедленно вернуть исправленный ответ."
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
            type="message_from_agent",
            description=(
                "Адресное сообщение от другого агента. from_agent_id и from_name "
                "являются метаданными отправителя; text содержит прямую речь "
                "или задачу."
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
            type="capability_error",
            description=(
                "Необработанная ошибка в коде capability: действия, handler "
                "или модуля. capability содержит идентификатор источника."
            ),
            data_schema=object_schema(
                {
                    "capability": {"type": "string"},
                    "error": {"type": "string"},
                }
            ),
        ),
        owner="core",
    )
    actions.register(
        ActionDefinition(
            id="speech",
            description=(
                "Озвучить text и вернуть статус озвучки. status равен "
                "successful после воспроизведения или interrupted, если "
                "реплика была прервана новой речью пользователя."
            ),
            args_schema=object_schema({"text": {"type": "string"}}),
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
            type="speech_detected",
            description=(
                "Речь пользователя с микрофона: text содержит распознанную "
                "реплику."
            ),
            data_schema=object_schema({"text": {"type": "string"}}),
        ),
        owner="core:speech",
    )


def _speech_action_run(data, context):
    from ..speech import service

    return service.speak_result(data["text"])


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
            self.events.validate(event.type, event.data)
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
                if agent is not None:
                    agent.enqueue(event)
                    delivered = True
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
        action_id: str,
        action_type: str,
        result_schema: dict[str, Any],
    ):
        self.agent_id = agent_id
        self.action_id = action_id
        self.action_type = action_type
        self.result_schema = result_schema


class ActionResultTracker:
    """Связывает авторские результаты с действиями по action_id."""

    def __init__(self, *, debug: Debugger | None = None):
        self.debug = debug or Debugger(enabled=False)
        self._lock = threading.RLock()
        self._pending: dict[tuple[str, str], PendingAction] = {}

    def has_pending(self, agent_id: str, action_id: str) -> bool:
        with self._lock:
            return (agent_id, action_id) in self._pending

    def begin(
        self,
        *,
        agent_id: str,
        action_id: str,
        action_type: str,
        result_schema: dict[str, Any],
    ) -> None:
        with self._lock:
            if (agent_id, action_id) in self._pending:
                raise ValueError(
                    f"action_id повторяется в ответе: {action_id!r}"
                )
            self._pending[(agent_id, action_id)] = PendingAction(
                agent_id=agent_id,
                action_id=action_id,
                action_type=action_type,
                result_schema=result_schema,
            )

    def complete(
        self,
        manager: "AgentManager",
        *,
        agent_id: str,
        action_id: str,
        data: dict[str, Any],
        parts: tuple = (),
    ) -> bool:
        with self._lock:
            pending = self._pending.pop((agent_id, action_id), None)
        if pending is None:
            self.debug.log(
                "action_result_rejected",
                agent_id=agent_id,
                action_id=action_id,
                reason="unknown_or_completed",
            )
            return False
        try:
            validate_json(
                data,
                pending.result_schema,
                where=f"результат {pending.action_type}",
            )
        except ValueError as exc:
            self.debug.log(
                "action_result_rejected",
                agent_id=agent_id,
                action_id=action_id,
                reason="invalid_schema",
                error=str(exc),
            )
            return False
        result = ActionResult(
            action_id=action_id, data=data, agent_id=agent_id, parts=tuple(parts)
        )
        return manager.deliver_result(result)

    def discard(self, agent_id: str, action_id: str) -> None:
        with self._lock:
            self._pending.pop((agent_id, action_id), None)

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
        restored_messages: list[dict[str, Any]] | None = None,
    ):
        self.agent_id = agent_id
        self.name = name
        self.preset = preset
        self.parent_id = parent_id
        self.manager = manager
        self.primary = primary
        self._enabled_modules = set(enabled_modules or ())
        self._enabled_actions = set(enabled_actions or ())
        self._enabled_handlers = set(enabled_handlers or ())
        self._capabilities_lock = threading.RLock()
        self.history: list[dict[str, Any]] = [
            {"role": "system", "content": ""},
            *[dict(message) for message in (restored_messages or ())],
        ]
        self._events: "queue.Queue[Event | ActionResult | None]" = queue.Queue()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._state = "created"
        self._state_lock = threading.Lock()

    @property
    def state(self) -> str:
        with self._state_lock:
            return self._state

    def _set_state(self, state: str, **extra: Any) -> None:
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

    def enable_module(self, module_id: str) -> None:
        with self._capabilities_lock:
            self._enabled_modules.add(module_id)
        self._persist_context()

    def disable_module(self, module_id: str) -> None:
        with self._capabilities_lock:
            self._enabled_modules.discard(module_id)
        self._persist_context()

    def enable_action(self, action_id: str) -> None:
        with self._capabilities_lock:
            self._enabled_actions.add(action_id)
        self._persist_context()

    def disable_action(self, action_id: str) -> None:
        with self._capabilities_lock:
            self._enabled_actions.discard(action_id)
        self._persist_context()

    def enable_handler(self, handler_id: str) -> None:
        with self._capabilities_lock:
            self._enabled_handlers.add(handler_id)
        self._persist_context()

    def disable_handler(self, handler_id: str) -> None:
        with self._capabilities_lock:
            self._enabled_handlers.discard(handler_id)
        self._persist_context()

    def accepts_event(self, event: Event) -> bool:
        if event.handler_id == "core:speech":
            return self.primary
        snapshot = self.capabilities_snapshot()
        if event.handler_id is not None and event.handler_id in snapshot["handlers"]:
            return True
        if event.module_id is not None and event.module_id in snapshot["modules"]:
            return True
        return False

    def memory_record(self) -> dict[str, Any]:
        """Снимок экземпляра для долговременной памяти."""

        snapshot = self.capabilities_snapshot()
        return {
            "agent_id": self.agent_id,
            "name": self.name,
            "preset": self.preset,
            "parent_id": self.parent_id,
            "modules": sorted(snapshot["modules"]),
            "actions": sorted(snapshot["actions"]),
            "handlers": sorted(snapshot["handlers"]),
            "messages": list(self.history[1:]),
        }

    def _persist_context(self) -> None:
        self.manager.persist_agent(self)

    def _contract(
        self,
        snapshot: dict[str, set[str]] | None = None,
    ) -> tuple[dict[str, ActionDefinition], dict[str, EventDefinition]]:
        preset = self.manager.presets.load(self.preset)
        if snapshot is None:
            snapshot = {
                "modules": set(preset.modules),
                "actions": set(preset.actions),
                "handlers": set(preset.handlers),
            }
        missing = self.manager.capabilities.missing(snapshot)
        if missing:
            raise ValueError(
                f"Пресет {self.preset} ссылается на незагруженные capabilities: "
                f"{sorted(missing)}"
            )
        actions = self.manager.actions.for_capabilities(
            modules=snapshot["modules"], actions=snapshot["actions"],
            primary=self.primary,
        )
        events = self.manager.events.for_capabilities(
            modules=snapshot["modules"], handlers=snapshot["handlers"],
            primary=self.primary,
        )
        self.history[0] = {
            "role": "system",
            "content": agent_system_prompt(
                preset.person_prompt,
                self.manager.master_prompt,
                actions,
                events,
                self.manager.capabilities.catalog(snapshot),
                model_capabilities=self.manager.model_capabilities,
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
            name=f"jarvis-agent-{self.agent_id}",
            daemon=True,
        )
        self._thread.start()
        return self

    def enqueue(self, event: Event) -> None:
        if not self._stop.is_set():
            self._events.put(event)

    def enqueue_result(self, result: ActionResult) -> None:
        if not self._stop.is_set():
            self._events.put(result)

    def stop(self, *, wait: bool = True, timeout: float = 10.0) -> None:
        self._stop.set()
        self._events.put(None)
        if wait and self._thread is not None:
            self._thread.join(timeout=timeout)
        self._set_state("stopped")

    def _run(self) -> None:
        while not self._stop.is_set():
            first = self._events.get()
            if first is None:
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
            if not self._stop.is_set():
                self._turn(batch)

    def _turn(self, batch: list[Event | ActionResult]) -> None:
        """Обработать пачку, удерживая новые события до корректного ответа."""

        try:
            snapshot = self.capabilities_snapshot()
        except Exception as exc:  # noqa: BLE001
            self._model_failure(exc)
            return
        self._run_turn(batch, snapshot)

    def _run_turn(
        self, batch: list[Event | ActionResult], snapshot: dict[str, set[str]]
    ) -> None:
        """Выполнить один цикл на неизменяемом снимке capabilities."""

        self._set_state("thinking", event_count=len(batch))
        for item in batch:
            if isinstance(item, ActionResult):
                self.history.append(item.model_message())
                self.manager.debug.result(item, agent_id=self.agent_id)
            else:
                self.history.append(
                    item.model_message(self.manager.model_capabilities)
                )
                self.manager.debug.input(
                    item, self.manager.model_capabilities, agent_id=self.agent_id
                )
        self._persist_context()

        try:
            specs, _ = self._contract(snapshot)
        except Exception as exc:  # noqa: BLE001
            self._model_failure(exc)
            return
        schemas = {name: spec.args_schema for name, spec in specs.items()}

        while not self._stop.is_set():
            try:
                response = self.manager.client.chat.completions.create(
                    model=self.manager.model,
                    messages=self.history,
                    response_format=response_format(schemas),
                )
                message = response.choices[0].message
                refusal = getattr(message, "refusal", None)
                content = message.content or ""
            except Exception as exc:  # noqa: BLE001
                self._model_failure(exc)
                return

            self.history.append({"role": "assistant", "content": content})
            self._persist_context()
            self.manager.debug.model(self.agent_id, content)
            if refusal:
                self._append_structure_error(
                    "refusal",
                    f"Модель отказалась выполнить запрос: {refusal}",
                    content,
                )
                continue

            try:
                value = json.loads(content)
                validate_json(
                    value,
                    actions_response_schema(schemas),
                    where=f"ответ агента {self.agent_id}",
                )
                actions = parse_actions(value)
                for action in actions:
                    if action.type not in specs:
                        raise ValueError(
                            f"Действие недоступно этому агенту: {action.type}"
                        )
                    if self.manager.results.has_pending(
                        self.agent_id, action.action_id
                    ):
                        raise ValueError(
                            f"action_id повторяется в ответе: {action.action_id!r}"
                        )
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                code = "invalid_json" if isinstance(exc, json.JSONDecodeError) else "invalid_structure"
                self._append_structure_error(code, str(exc), content)
                continue

            if len(actions) == 1 and actions[0].type == "no_action":
                self._set_state("waiting")
                return
            self._set_state("acting", action_count=len(actions))
            self._execute_actions(actions, specs)
            if not self._stop.is_set():
                self._set_state("waiting")
            return

    def _append_structure_error(self, code: str, message: str, response: str) -> None:
        event = Event(
            type="structure_error",
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
            spec = specs[action.type]
            try:
                self.manager.capabilities.dispatch(
                    action=action,
                    spec=spec,
                    agent=self,
                )
            except Exception as exc:  # noqa: BLE001
                capability = spec.owner.split("|")[0]
                self.manager.report_capability_error(capability, exc)

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
        master_prompt: str,
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
        self.master_prompt = master_prompt
        self.config = config
        self.services = services if services is not None else {}
        self.model_capabilities = model_capabilities
        self.debug = debug or Debugger(enabled=False)
        self.memory = memory
        self.results = ActionResultTracker(debug=self.debug)
        self.stopping = threading.Event()
        self.processes = ProcessManager()
        self.services.setdefault("process_manager", self.processes)
        self.agents: dict[str, Agent] = {}
        self.primary_agent_id: str | None = None
        self._lock = threading.RLock()
        bus.manager = self  # type: ignore[attr-defined]

    def _new_id(self) -> str:
        with self._lock:
            if len(self.agents) >= 1000:
                raise RuntimeError("Исчерпаны идентификаторы agent-000..agent-999")
            for _ in range(2000):
                candidate = f"agent-{secrets.randbelow(1000):03d}"
                if candidate not in self.agents:
                    return candidate
        raise RuntimeError("Не удалось подобрать свободный идентификатор агента")

    def deliver_result(self, result: ActionResult) -> bool:
        """Доставить результат только инициировавшему агенту."""

        with self._lock:
            agent = self.agents.get(result.agent_id)
        if agent is None:
            self.debug.log(
                "action_result_dropped",
                action_id=result.action_id,
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

    def restore(self, *, name: str, preset: str) -> Agent:
        """Поднять экземпляры из памяти или создать новый корневой агент."""

        records = self.memory.load_all() if self.memory is not None else []
        if not records:
            return self.spawn_root(name=name, preset=preset)
        return self._restore(records, default_name=name, default_preset=preset)

    def _restore(
        self, records: list[dict[str, Any]], *, default_name: str, default_preset: str
    ) -> Agent:
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
                {
                    "agent_id": "main",
                    "name": PRIMARY_AGENT_NAME,
                    "preset": default_preset,
                    "parent_id": None,
                    "modules": [],
                    "actions": [],
                    "handlers": [],
                    "messages": [],
                },
            )
        primary = {**primary, "name": PRIMARY_AGENT_NAME}
        try:
            main_agent = self._spawn_record(primary, primary=True)
        except Exception as exc:  # noqa: BLE001
            self.debug.log(
                "memory_restore_failed",
                agent_id=primary.get("agent_id"),
                error=str(exc),
            )
            if self.memory is not None:
                self.memory.delete(
                    primary.get("preset", "main"), primary.get("agent_id", "main")
                )
            return self.spawn_root(name=default_name, preset=default_preset)

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
                        )
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
            if self.memory is not None:
                self.memory.delete(record["preset"], record["agent_id"])
        return main_agent

    def _spawn_record(self, record: dict[str, Any], *, primary: bool) -> Agent:
        snapshot = {
            "modules": set(record.get("modules") or ()),
            "actions": set(record.get("actions") or ()),
            "handlers": set(record.get("handlers") or ()),
        }
        if any(snapshot.values()):
            try:
                self.capabilities.load_snapshot(snapshot, start_handlers=False)
            except Exception as exc:  # noqa: BLE001
                self.debug.log(
                    "memory_capabilities_fallback",
                    agent_id=record["agent_id"],
                    error=str(exc),
                )
                snapshot = {}
        return self._spawn(
            name=record["name"],
            preset=record["preset"],
            parent_id=record["parent_id"],
            primary=primary,
            agent_id=record["agent_id"],
            capabilities_override=snapshot or None,
            restored_messages=record["messages"],
        )

    def spawn(self, *, parent_id: str, name: str, preset: str) -> dict[str, Any]:
        if parent_id not in self.agents:
            raise ValueError(f"Родительский агент не найден: {parent_id}")
        selected = self.presets.load(preset)
        if selected.protected:
            raise ValueError(
                f"Пресет {preset} защищён: у него может существовать только "
                "корневой экземпляр"
            )
        agent = self._spawn(name=name, preset=preset, parent_id=parent_id)
        return self.describe(agent)

    def _spawn(
        self,
        *,
        name: str,
        preset: str,
        parent_id: str | None,
        primary: bool = False,
        agent_id: str | None = None,
        capabilities_override: dict[str, set[str]] | None = None,
        restored_messages: list[dict[str, Any]] | None = None,
    ) -> Agent:
        if self.stopping.is_set():
            raise RuntimeError("runtime_stopping")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("Имя агента не должно быть пустым")
        selected = self.presets.load(preset)
        if capabilities_override is not None:
            initial = {
                "modules": set(capabilities_override.get("modules", ())),
                "actions": set(capabilities_override.get("actions", ())),
                "handlers": set(capabilities_override.get("handlers", ())),
            }
        else:
            initial = {
                "modules": set(selected.modules),
                "actions": set(selected.actions),
                "handlers": set(selected.handlers),
            }
        self.capabilities.load_snapshot(initial, start_handlers=False)
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
                enabled_modules=initial["modules"],
                enabled_actions=initial["actions"],
                enabled_handlers=initial["handlers"],
                restored_messages=restored_messages,
            )
            self.agents[resolved_id] = agent
            self.bus.bind(agent)
            if primary and self.primary_agent_id is None:
                self.primary_agent_id = resolved_id
        try:
            agent.start()
            self.capabilities.start_snapshot(initial)
        except Exception:
            with self._lock:
                self.agents.pop(resolved_id, None)
                if self.primary_agent_id == resolved_id:
                    self.primary_agent_id = None
            self.bus.unbind(resolved_id)
            self.capabilities.release_snapshot(initial, agents=self.agents_snapshot())
            raise
        self.persist_agent(agent)
        return agent

    def agents_snapshot(self) -> list[Agent]:
        with self._lock:
            return list(self.agents.values())

    def persist_agent(self, agent: Agent) -> None:
        """Сохранить метаданные и контекст экземпляра в память."""

        if self.memory is None:
            return
        try:
            self.memory.save(agent.memory_record())
        except Exception as exc:  # noqa: BLE001
            self.debug.log(
                "memory_save_error", agent_id=agent.agent_id, error=str(exc)
            )

    def require_agent(self, agent_id: str) -> Agent:
        agent = self.agents.get(agent_id)
        if agent is None:
            raise ValueError(f"Агент не найден: {agent_id}")
        return agent

    def interrupt(self, *, agent_id: str, reason: str) -> dict[str, Any]:
        agent = self.require_agent(agent_id)
        agent.stop(wait=False)
        return {"agent_id": agent_id, "state": "stopped", "reason": reason}

    def delete(self, *, agent_id: str, reason: str) -> dict[str, Any]:
        with self._lock:
            agent = self.agents.pop(agent_id, None)
        if agent is None:
            raise ValueError(f"Агент не найден: {agent_id}")
        if agent_id == self.primary_agent_id:
            self.primary_agent_id = None
        snapshot = agent.capabilities_snapshot()
        agent.stop(wait=False)
        self.bus.unbind(agent_id)
        self.results.discard_agent(agent_id)
        self.capabilities.release_snapshot(
            snapshot, agents=self.agents_snapshot()
        )
        if self.memory is not None:
            self.memory.delete(agent.preset, agent_id)
        return {"agent_id": agent_id, "deleted": True, "reason": reason}

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
        agent.disable_module(module_id)
        if not self.agents_with_module(module_id):
            self.capabilities.unload_module(module_id)
        return {"agent_id": agent_id, "module_id": module_id, "enabled": False}

    def enable_action(self, agent_id: str, action_id: str) -> dict[str, Any]:
        agent = self.require_agent(agent_id)
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
        agent.disable_action(action_id)
        if not self.agents_with_action(action_id):
            self.capabilities.unload_action(action_id)
        return {"agent_id": agent_id, "action_id": action_id, "enabled": False}

    def enable_handler(self, agent_id: str, handler_id: str) -> dict[str, Any]:
        agent = self.require_agent(agent_id)
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

    def disable_capability_everywhere(
        self, *, kind: str, capability_id: str
    ) -> list[str]:
        if kind == "module":
            targets = self.agents_with_module(capability_id)
            disable = lambda agent_id: self.require_agent(agent_id).disable_module(
                capability_id
            )
            unload = lambda: self.capabilities.unload_module(capability_id)
        elif kind == "action":
            targets = self.agents_with_action(capability_id)
            disable = lambda agent_id: self.require_agent(agent_id).disable_action(
                capability_id
            )
            unload = lambda: self.capabilities.unload_action(capability_id)
        elif kind == "handler":
            targets = self.agents_with_handler(capability_id)
            disable = lambda agent_id: self.require_agent(agent_id).disable_handler(
                capability_id
            )
            unload = lambda: self.capabilities.unload_handler(capability_id)
        else:
            raise ValueError(f"Неизвестный вид capability: {kind!r}")
        for agent_id in targets:
            disable(agent_id)
        unload()
        return targets

    def restore_capability(
        self, *, kind: str, capability_id: str, agent_ids: list[str]
    ) -> list[str]:
        if kind == "module":
            self.capabilities.load_module(capability_id, start_handlers=False)
            enable = lambda agent: agent.enable_module(capability_id)
            start = lambda: self.capabilities.start_module(capability_id)
            rollback = lambda agent_id: self.require_agent(agent_id).disable_module(
                capability_id
            )
            unused = lambda: not self.agents_with_module(capability_id)
            unload = lambda: self.capabilities.unload_module(capability_id)
        elif kind == "action":
            self.capabilities.load_action(capability_id, start_handlers=False)
            enable = lambda agent: agent.enable_action(capability_id)
            start = lambda: self.capabilities.start_action(capability_id)
            rollback = lambda agent_id: self.require_agent(agent_id).disable_action(
                capability_id
            )
            unused = lambda: not self.agents_with_action(capability_id)
            unload = lambda: self.capabilities.unload_action(capability_id)
        elif kind == "handler":
            self.capabilities.load_handler(capability_id, start_handlers=False)
            enable = lambda agent: agent.enable_handler(capability_id)
            start = lambda: self.capabilities.start_handler(capability_id)
            rollback = lambda agent_id: self.require_agent(agent_id).disable_handler(
                capability_id
            )
            unused = lambda: not self.agents_with_handler(capability_id)
            unload = lambda: self.capabilities.unload_handler(capability_id)
        else:
            raise ValueError(f"Неизвестный вид capability: {kind!r}")
        restored = []
        for agent_id in agent_ids:
            agent = self.agents.get(agent_id)
            if agent is not None:
                enable(agent)
                restored.append(agent_id)
        if restored:
            try:
                start()
            except Exception:
                for agent_id in restored:
                    rollback(agent_id)
                if unused():
                    unload()
                raise
        return restored

    def report_capability_error(
        self, capability: str, error: Exception | str
    ) -> None:
        target = self.primary_agent_id
        event_type = "capability_error"
        data = {"capability": capability, "error": str(error)}
        if target is None:
            self.debug.log(event_type, **data)
            return
        self.bus.publish(
            Event(
                type=event_type,
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
