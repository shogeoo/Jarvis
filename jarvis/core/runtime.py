"""Неизменяемое event-thinking-action ядро Jarvis."""

from __future__ import annotations

import json
import queue
import secrets
import threading
import time
import traceback
from concurrent.futures import Future
from typing import Any

from ..infrastructure.debug import Debugger
from ..infrastructure.model_capabilities import ModelCapabilities
from ..modules.api import ActionContext, ActionSpec, EventDefinition
from ..presets import PresetStore
from .lifecycle import ActionPool, ProcessManager
from .prompts import agent_system_prompt
from .protocol import (
    ActionRequest,
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
    """Зарегистрировать два зарезервированных элемента протокола."""

    actions.register(
        ActionSpec(
            type="no_action",
            description=(
                "Завершить текущий цикл и ждать новые события. "
                "Допустимо только как единственное действие ответа."
            ),
            data_schema=empty_object_schema(),
            handler=lambda data, context: None,
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
            elif event.module_id is not None:
                recipients = [
                    agent
                    for agent in self._agents.values()
                    if agent.accepts_module(event.module_id)
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
    ):
        self.agent_id = agent_id
        self.name = name
        self.preset = preset
        self.parent_id = parent_id
        self.manager = manager
        self.history: list[dict[str, Any]] = [
            {"role": "system", "content": ""}
        ]
        self._events: "queue.Queue[Event | None]" = queue.Queue()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._future_lock = threading.Lock()
        self._active_futures: set[Future] = set()
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
        return set(self.manager.presets.load(self.preset).modules)

    def accepts_module(self, module_id: str) -> bool:
        try:
            return module_id in self.modules()
        except ValueError:
            return False

    def _contract(
        self,
        module_ids: set[str] | None = None,
    ) -> tuple[dict[str, ActionSpec], dict[str, EventDefinition]]:
        preset = self.manager.presets.load(self.preset)
        module_ids = set(preset.modules) if module_ids is None else module_ids
        missing = module_ids - self.manager.modules.loaded_names()
        if missing:
            raise ValueError(
                f"Пресет {self.preset} ссылается на незагруженные модули: "
                f"{sorted(missing)}"
            )
        actions = self.manager.actions.for_modules(module_ids)
        events = self.manager.events.for_modules(module_ids)
        self.history[0] = {
            "role": "system",
            "content": agent_system_prompt(
                preset.person_prompt,
                self.manager.environment_prompt,
                actions,
                events,
                self.manager.modules.catalog(module_ids),
                capabilities=self.manager.capabilities,
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

    def stop(self, *, wait: bool = True, timeout: float = 10.0) -> None:
        self._stop.set()
        with self._future_lock:
            for future in self._active_futures:
                future.cancel()
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

    def _turn(self, events: list[Event]) -> None:
        """Обработать пачку, удерживая новые события до корректного ответа."""

        try:
            module_ids = self.modules()
        except Exception as exc:  # noqa: BLE001
            self._model_failure(exc)
            return
        self.manager.begin_agent_turn(self.agent_id, module_ids)
        try:
            self._run_turn(events, module_ids)
        finally:
            self.manager.end_agent_turn(self.agent_id)

    def _run_turn(self, events: list[Event], module_ids: set[str]) -> None:
        """Выполнить один цикл на неизменяемом снимке модулей."""

        self._set_state("thinking", event_count=len(events))
        for event in events:
            self.history.append(event.model_message(self.manager.capabilities))
            self.manager.debug.input(event, self.manager.capabilities)

        try:
            specs, _ = self._contract(module_ids)
        except Exception as exc:  # noqa: BLE001
            self._model_failure(exc)
            return
        schemas = {name: spec.data_schema for name, spec in specs.items()}

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
        self.history.append(event.model_message(self.manager.capabilities))
        self.manager.debug.input(event, self.manager.capabilities)

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
        specs: dict[str, ActionSpec],
    ) -> None:
        for action in actions:
            if self._stop.is_set() or self.manager.stopping.is_set():
                break
            spec = specs[action.type]
            try:
                future = self.manager.action_pool.submit(
                    self._execute_action, action, spec
                )
            except Exception as exc:  # noqa: BLE001
                self.manager.debug.log(
                    "action_error",
                    agent_id=self.agent_id,
                    action=action.model_value(),
                    error=str(exc),
                )
                continue
            with self._future_lock:
                self._active_futures.add(future)
            try:
                future.result()
            except Exception as exc:  # noqa: BLE001
                self.manager.debug.log(
                    "action_error",
                    agent_id=self.agent_id,
                    action=action.model_value(),
                    error=str(exc),
                )
            finally:
                with self._future_lock:
                    self._active_futures.discard(future)

    def _execute_action(self, action: ActionRequest, spec: ActionSpec) -> None:
        if self._stop.is_set() or self.manager.stopping.is_set():
            return
        validate_json(
            action.data,
            spec.data_schema,
            where=f"аргументы {action.type}",
        )
        module_id = (
            spec.owner.removeprefix("module:")
            if spec.owner.startswith("module:")
            else "core"
        )
        self.manager.begin_module_action(module_id)
        try:
            context = ActionContext(
                agent_id=self.agent_id,
                action_id=action.id,
                event_bus=self.manager.bus,
                agent_manager=self.manager,
                modules=self.manager.modules,
                module_id=module_id,
                config=self.manager.config,
                services=self.manager.services,
                metadata={"preset": self.preset, "agent_name": self.name},
                stop_event=self._stop,
            )
            spec.handler(action.data, context)
        finally:
            self.manager.end_module_action(module_id)


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
        modules: Any,
        presets: PresetStore,
        environment_prompt: str,
        config: Any = None,
        services: dict[str, Any] | None = None,
        debug: Debugger | None = None,
        max_workers: int = 16,
        capabilities: ModelCapabilities | None = None,
    ):
        self.model = model
        self.client = client
        self.actions = actions
        self.events = events
        self.bus = bus
        self.modules = modules
        self.presets = presets
        self.environment_prompt = environment_prompt
        self.config = config
        self.services = services if services is not None else {}
        self.capabilities = capabilities
        self.debug = debug or Debugger(enabled=False)
        self.stopping = threading.Event()
        self.processes = ProcessManager()
        self.services.setdefault("process_manager", self.processes)
        self.action_pool = ActionPool(max_workers)
        self.agents: dict[str, Agent] = {}
        self._lock = threading.RLock()
        self._action_condition = threading.Condition()
        self._active_module_actions: dict[str, int] = {}
        self._active_agent_turns: dict[str, set[str]] = {}
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

    def spawn_root(self, *, name: str, preset: str) -> Agent:
        """Создать первый экземпляр без родителя."""

        return self._spawn(name=name, preset=preset, parent_id=None)

    def spawn(self, *, parent_id: str, name: str, preset: str) -> dict[str, Any]:
        if parent_id not in self.agents:
            raise ValueError(f"Родительский агент не найден: {parent_id}")
        agent = self._spawn(name=name, preset=preset, parent_id=parent_id)
        return self.describe(agent)

    def _spawn(self, *, name: str, preset: str, parent_id: str | None) -> Agent:
        if self.stopping.is_set():
            raise RuntimeError("runtime_stopping")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("Имя агента не должно быть пустым")
        selected = self.presets.load(preset)
        missing = set(selected.modules) - self.modules.loaded_names()
        if missing:
            raise ValueError(f"Не загружены модули пресета {preset}: {sorted(missing)}")
        with self._lock:
            agent_id = self._new_id()
            agent = Agent(
                agent_id,
                name.strip(),
                preset,
                self,
                parent_id=parent_id,
            )
            self.agents[agent_id] = agent
            self.bus.bind(agent)
        try:
            agent.start()
        except Exception:
            with self._lock:
                self.agents.pop(agent_id, None)
            self.bus.unbind(agent_id)
            raise
        return agent

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
        agent.stop(wait=False)
        self.bus.unbind(agent_id)
        return {"agent_id": agent_id, "deleted": True, "reason": reason}

    @staticmethod
    def describe(agent: Agent) -> dict[str, Any]:
        return {
            "agent_id": agent.agent_id,
            "name": agent.name,
            "parent_id": agent.parent_id,
            "state": agent.state,
            "preset": agent.preset,
            "modules": sorted(agent.modules()),
        }

    def list_agents(self) -> list[dict[str, Any]]:
        with self._lock:
            return [
                self.describe(agent)
                for agent in sorted(self.agents.values(), key=lambda item: item.agent_id)
            ]

    def agents_using_preset(self, preset: str) -> list[str]:
        with self._lock:
            return sorted(
                agent.agent_id
                for agent in self.agents.values()
                if agent.preset == preset
            )

    def begin_module_action(self, module_id: str) -> None:
        with self._action_condition:
            self._active_module_actions[module_id] = (
                self._active_module_actions.get(module_id, 0) + 1
            )

    def end_module_action(self, module_id: str) -> None:
        with self._action_condition:
            remaining = self._active_module_actions.get(module_id, 1) - 1
            if remaining > 0:
                self._active_module_actions[module_id] = remaining
            else:
                self._active_module_actions.pop(module_id, None)
            self._action_condition.notify_all()

    def begin_agent_turn(self, agent_id: str, module_ids: set[str]) -> None:
        with self._action_condition:
            self._active_agent_turns[agent_id] = set(module_ids)

    def end_agent_turn(self, agent_id: str) -> None:
        with self._action_condition:
            self._active_agent_turns.pop(agent_id, None)
            self._action_condition.notify_all()

    def agent_turn_uses(self, agent_id: str, module_id: str) -> bool:
        with self._action_condition:
            return module_id in self._active_agent_turns.get(agent_id, set())

    def wait_module_idle(self, module_id: str, timeout: float = 30.0) -> None:
        deadline = time.monotonic() + timeout
        with self._action_condition:
            while self._active_module_actions.get(module_id, 0) or any(
                module_id in modules
                for modules in self._active_agent_turns.values()
            ):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(
                        f"Модуль {module_id} не освободился для обновления"
                    )
                self._action_condition.wait(remaining)

    def begin_shutdown(self) -> None:
        self.stopping.set()
        with self._lock:
            agents = list(self.agents.values())
        for agent in agents:
            agent.stop(wait=False)

    def shutdown(self) -> None:
        self.begin_shutdown()
        self.processes.stop()
        self.action_pool.shutdown(timeout=2)
        deadline = time.monotonic() + 2
        for agent in list(self.agents.values()):
            if agent._thread is not None:
                agent._thread.join(max(0, deadline - time.monotonic()))
