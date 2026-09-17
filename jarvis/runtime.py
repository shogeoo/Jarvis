"""Асинхронная шина событий и жизненный цикл агентов."""

from __future__ import annotations

import json
import queue
import threading
import time
import traceback
from concurrent.futures import Future, wait, FIRST_COMPLETED
from dataclasses import dataclass
from typing import Any, Callable

from .debug import Debugger
from .lifecycle import ActionPool, ProcessManager
from .config import ROOT
from .module_api import ActionContext
from .model_capabilities import ModelCapabilities
from .prompts import (
    agent_system_prompt, MAIN_AGENT_INSTRUCTIONS,
    MODULE_BUILDER_INSTRUCTIONS, SUBAGENT_INSTRUCTIONS,
)
from .protocol import (
    ActionRequest,
    Event,
    action_result_event,
    actions_response_schema,
    json_text,
    make_id,
    parse_actions,
    response_format,
    validate_json,
)
from .registry import ActionRegistry, EventRegistry


PRESETS_PATH = ROOT / ".jarvis" / "agents" / "presets.json"


class EventBus:
    """Доставляет каждое событие ровно одному целевому агенту."""

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
            self.debug.log(
                "event_rejected",
                event=event.debug_value(),
                error=str(exc),
            )
            return False
        target = event.target or "main"
        with self._lock:
            agent = self._agents.get(target)
        if agent is None:
            self.debug.log(
                "event_dropped",
                reason="target_agent_not_found",
                event=event.debug_value(),
            )
            return False
        agent.enqueue(event)
        return True


class Agent:
    """Один последовательный модельный цикл с параллельными действиями."""

    def __init__(
        self,
        agent_id: str,
        name: str,
        manager: "AgentManager",
        *,
        model: str,
        client: Any,
        base_prompt: str,
        audience: str,
        allowed_actions: set[str] | None = None,
        parent_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        capabilities: ModelCapabilities | None = None,
    ):
        self.agent_id = agent_id
        self.name = name
        self.manager = manager
        self.model = model
        self.client = client
        self.base_prompt = base_prompt
        self.audience = audience
        self.allowed_actions = allowed_actions
        self.parent_id = parent_id
        self.metadata = metadata or {}
        self.capabilities = capabilities
        initial_specs = self.available_actions()
        if audience == "main":
            role_instructions = (
                "Справка о роли разработчика. Следующий блок описывает другого агента.\n"
                "<module_builder_reference>\n" + MODULE_BUILDER_INSTRUCTIONS
                + "\n</module_builder_reference>\n\n" + MAIN_AGENT_INSTRUCTIONS
            )
        else:
            role_instructions = SUBAGENT_INSTRUCTIONS if audience != "module_builder" else ""
        self.history: list[dict[str, Any]] = [
            {
                "role": "system",
                "content": agent_system_prompt(
                    self.base_prompt,
                    initial_specs,
                    extra=role_instructions,
                    capabilities=self.capabilities,
                ),
            }
        ]
        self._events: "queue.Queue[Event | None]" = queue.Queue()
        self._stop = threading.Event()
        self._close_after_actions = threading.Event()
        self._hold_until_event = threading.Event()
        self._hold_released = threading.Event()
        self._hold_released.set()
        self._hold_event_type: str | None = None
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

    def available_actions(self):
        return self.manager.actions.for_agent(
            self.audience,
            self.allowed_actions,
        )

    def start(self) -> "Agent":
        if self._thread is not None:
            return self
        self._set_state("waiting")
        self._thread = threading.Thread(
            target=self._run,
            name=f"jarvis-agent-{self.agent_id}",
            daemon=True,
        )
        self._thread.start()
        return self

    def enqueue(self, event: Event) -> None:
        if self._stop.is_set():
            self.manager.debug.log(
                "event_ignored",
                agent_id=self.agent_id,
                reason="agent_stopped",
                event=event.debug_value(),
            )
            return
        if self._hold_until_event.is_set() and event.type == self._hold_event_type:
            self._hold_until_event.clear()
            self._hold_released.set()
        self._events.put(event)

    def hold_until(self, event_type: str) -> None:
        self._hold_event_type = event_type
        self._hold_released.clear()
        self._hold_until_event.set()

    def request_close(self) -> None:
        self._close_after_actions.set()

    def stop(self, *, wait: bool = True, timeout: float = 10.0) -> None:
        self._stop.set()
        self._hold_released.set()
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
            while self._hold_until_event.is_set() and not self._stop.is_set():
                if first.type == self._hold_event_type:
                    self._hold_until_event.clear()
                    break
                self._events.put(first)
                self._hold_released.wait(timeout=0.1)
                if self._stop.is_set():
                    return
                first = self._events.get()
            if first is None or self._stop.is_set():
                return
            batch = [first]
            while True:
                try:
                    next_event = self._events.get_nowait()
                except queue.Empty:
                    break
                if next_event is None:
                    self._stop.set()
                    break
                batch.append(next_event)
            self._turn(batch)

    def _turn(self, events: list[Event]) -> None:
        if self._stop.is_set():
            return
        self._set_state("thinking", event_count=len(events))
        for event in events:
            message = event.model_message(self.capabilities)
            self.history.append(message)
            self.manager.debug.input(event, self.capabilities)
        specs = self.available_actions()
        schema = {name: spec.data_schema for name, spec in specs.items()}
        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=self.history,
                response_format=response_format(schema),
            )
            message = response.choices[0].message
            refusal = getattr(message, "refusal", None)
            content = message.content or ""
            self.history.append({"role": "assistant", "content": content})
            self.manager.debug.model(self.agent_id, content)
            if refusal:
                raise RuntimeError(f"Модель отказалась выполнить запрос: {refusal}")
            value = json.loads(content)
            raw_actions = value.get("actions") if isinstance(value, dict) else None
            if isinstance(raw_actions, list) and len(raw_actions) > 1:
                # Some local structured-output backends append no_action to a
                # useful action. It is redundant in a non-empty response.
                value["actions"] = [
                    item for item in raw_actions
                    if item.get("type") != "no_action"
                ]
            validate_json(
                value,
                actions_response_schema(schema),
                where=f"ответ агента {self.agent_id}",
            )
            actions = parse_actions(value)
            for action in actions:
                if action.type not in specs:
                    raise ValueError(f"Действие недоступно этому агенту: {action.type}")
        except Exception as exc:  # noqa: BLE001
            self._model_failure(exc)
            return

        if len(actions) == 1 and actions[0].type == "no_action":
            self._set_state("waiting")
            return
        self._set_state("acting", action_count=len(actions))
        self._execute_actions(actions)
        if self._close_after_actions.is_set():
            self.stop(wait=False)
        elif not self._stop.is_set():
            self._set_state("waiting")

    def _model_failure(self, exc: Exception) -> None:
        self.manager.debug.log(
            "model_error",
            agent_id=self.agent_id,
            error=str(exc),
            traceback=traceback.format_exc(),
        )
        if self.parent_id:
            self.manager.bus.publish(
                Event(
                    type="agent.failed",
                    data={
                        "agent_id": self.agent_id,
                        "error": str(exc),
                    },
                    source=f"agent:{self.agent_id}",
                    target=self.parent_id,
                )
            )
        self._set_state("waiting")

    def _execute_actions(self, actions: list[ActionRequest]) -> None:
        futures: list[Future] = []
        for action in actions:
            if self._stop.is_set() or self.manager.stopping.is_set():
                break
            try:
                future = self.manager.action_pool.submit(self._execute_action, action)
            except RuntimeError:
                if self.manager.stopping.is_set():
                    break
                raise
            futures.append(future)
        with self._future_lock:
            self._active_futures.update(futures)
        try:
            pending = set(futures)
            while pending and not self._stop.is_set():
                done, pending = wait(pending, timeout=0.1, return_when=FIRST_COMPLETED)
                for future in done:
                    if not future.cancelled() and not self._stop.is_set():
                        try:
                            result = future.result()
                        except Exception as exc:
                            self._model_failure(exc)
                        else:
                            self.manager.bus.publish(result)
        finally:
            with self._future_lock:
                self._active_futures.difference_update(futures)

    def _execute_action(self, action: ActionRequest) -> Event:
        if self._stop.is_set() or self.manager.stopping.is_set():
            return action_result_event(action, status="error", error="runtime_stopping",
                                       target=self.agent_id)
        self.manager.debug.action("start", self.agent_id, action)
        spec = self.manager.actions.get(action.type)
        if spec is None:
            result = action_result_event(
                action,
                status="error",
                error=f"Действие больше не зарегистрировано: {action.type}",
                target=self.agent_id,
            )
            self.manager.debug.action("finish", self.agent_id, action, result=result.debug_value())
            return result
        try:
            self.manager.actions.validate(action.type, action.data)
            context = ActionContext(
                agent_id=self.agent_id,
                action_id=action.id,
                event_bus=self.manager.bus,
                agent_manager=self.manager,
                module_manager=self.manager.module_manager,
                config=self.manager.config,
                metadata=self.metadata,
                stop_event=self._stop,
            )
            value = spec.handler(action.data, context)
            json.dumps(value, ensure_ascii=False)
            result = action_result_event(
                action,
                status="success",
                result=value,
                target=self.agent_id,
            )
        except Exception as exc:  # noqa: BLE001
            result = action_result_event(
                action,
                status="error",
                error=str(exc),
                target=self.agent_id,
            )
        self.manager.debug.action("finish", self.agent_id, action, result=result.debug_value())
        return result


@dataclass(frozen=True, slots=True)
class AgentPreset:
    name: str
    description: str
    system_prompt: str
    audience: str = "subagent"
    allowed_actions: frozenset[str] | None = None


class AgentManager:
    """Создаёт агентов и предоставляет встроенные операции управления ими."""

    def __init__(
        self,
        *,
        model: str,
        client: Any,
        actions: Any,
        events: EventRegistry,
        bus: EventBus,
        module_manager: Any = None,
        config: Any = None,
        debug: Debugger | None = None,
        max_workers: int = 16,
        capabilities: ModelCapabilities | None = None,
    ):
        self.model = model
        self.client = client
        self.actions: ActionRegistry = actions
        self.events = events
        self.bus = bus
        self.module_manager = module_manager
        self.config = config
        self.capabilities = capabilities
        self.debug = debug or Debugger(enabled=False)
        self.stopping = threading.Event()
        self.processes = ProcessManager()
        self.action_pool = ActionPool(max_workers)
        self.agents: dict[str, Agent] = {}
        self.presets: dict[str, AgentPreset] = {}
        self._lock = threading.RLock()
        self._counter = 0
        self._load_presets()
        bus.manager = self  # type: ignore[attr-defined]

    def add_preset(self, preset: AgentPreset, *, persist: bool = True) -> None:
        self.presets[preset.name] = preset
        if persist:
            self._save_presets()

    def _load_presets(self) -> None:
        try:
            raw_presets = json.loads(PRESETS_PATH.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return
        if not isinstance(raw_presets, list):
            return
        for raw in raw_presets:
            if not isinstance(raw, dict):
                continue
            try:
                if raw["name"] == "module_builder":
                    continue
                self.presets[raw["name"]] = AgentPreset(
                    name=raw["name"],
                    description=raw.get("description", ""),
                    system_prompt=raw["system_prompt"],
                    audience=raw.get("agent_type", raw["name"]),
                    allowed_actions=frozenset(raw.get("actions", [])) or None,
                )
            except (KeyError, TypeError):
                continue

    def _save_presets(self) -> None:
        PRESETS_PATH.parent.mkdir(parents=True, exist_ok=True)
        data = [
            {
                "name": preset.name,
                "description": preset.description,
                "system_prompt": preset.system_prompt,
                "agent_type": preset.audience,
                "actions": sorted(preset.allowed_actions or []),
            }
            for preset in sorted(self.presets.values(), key=lambda item: item.name)
            if preset.name != "module_builder"
        ]
        PRESETS_PATH.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def create_preset(
        self,
        *,
        name: str,
        description: str,
        system_prompt: str,
        allowed_actions: list[str],
    ) -> dict[str, Any]:
        if not name or name == "module_builder":
            raise ValueError("Недопустимое имя пресета")
        unknown = sorted(name for name in allowed_actions if self.actions.get(name) is None)
        if unknown:
            raise ValueError(f"Неизвестные действия пресета: {unknown}")
        self.add_preset(
            AgentPreset(
                name=name,
                description=description,
                system_prompt=system_prompt,
                audience=name,
                allowed_actions=frozenset(allowed_actions) or None,
            )
        )
        return self.describe_preset(name)

    def delete_preset(self, name: str) -> dict[str, Any]:
        if name == "module_builder":
            raise ValueError("Встроенный пресет module_builder нельзя удалить")
        if self.presets.pop(name, None) is None:
            raise ValueError(f"Пресет не найден: {name}")
        self._save_presets()
        return {"name": name, "deleted": True}

    def list_presets(self) -> list[dict[str, Any]]:
        return [self.describe_preset(name) for name in sorted(self.presets)]

    def describe_preset(self, name: str) -> dict[str, Any]:
        preset = self.presets.get(name)
        if preset is None:
            raise ValueError(f"Пресет не найден: {name}")
        return {
            "name": preset.name,
            "description": preset.description,
            "actions": sorted(preset.allowed_actions or []),
        }

    def create_main(self, system_prompt: str) -> Agent:
        with self._lock:
            if "main" in self.agents:
                return self.agents["main"]
            agent = Agent(
                "main",
                "main",
                self,
                model=self.model,
                client=self.client,
                base_prompt=system_prompt,
                audience="main",
                capabilities=self.capabilities,
            )
            self.agents[agent.agent_id] = agent
            self.bus.bind(agent)
            return agent.start()

    def spawn(
        self,
        *,
        parent_id: str,
        preset: str,
        task: str,
        name: str | None = None,
        system_prompt: str | None = None,
        allowed_actions: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if self.stopping.is_set():
            raise RuntimeError("runtime_stopping")
        if parent_id not in self.agents:
            raise ValueError(f"Родительский агент не найден: {parent_id}")
        selected = self.presets.get(preset)
        if selected is not None:
            if preset == "module_builder" and allowed_actions:
                raise ValueError("Пресет module_builder нельзя расширить действиями")
            base_prompt = selected.system_prompt
            audience = selected.audience
            allowed = set(selected.allowed_actions) if selected.allowed_actions else None
        else:
            if preset != "custom" or not system_prompt:
                raise ValueError(f"Пресет агента не найден: {preset}")
            base_prompt = system_prompt
            audience = "custom"
            allowed = set(allowed_actions or []) or None
        if allowed_actions:
            allowed = set(allowed_actions)
        if allowed is not None:
            unknown = sorted(name for name in allowed if self.actions.get(name) is None)
            if unknown:
                raise ValueError(f"Неизвестные действия субагента: {unknown}")
        agent_metadata = dict(metadata or {})
        agent_metadata.setdefault("preset", preset)
        agent_metadata.setdefault("agent_type", audience)
        if self.module_manager is not None:
            self.module_manager.load_scope(audience, start_handlers=True)
        with self._lock:
            if self.stopping.is_set():
                raise RuntimeError("runtime_stopping")
            self._counter += 1
            agent_id = f"agent-{self._counter:04d}"
            agent = Agent(
                agent_id,
                name or agent_id,
                self,
                model=self.model,
                client=self.client,
                base_prompt=base_prompt,
                audience=audience,
                allowed_actions=allowed,
                parent_id=parent_id,
                metadata=agent_metadata,
                capabilities=self.capabilities,
            )
            self.agents[agent_id] = agent
            self.bus.bind(agent)
            agent.start()
        self.bus.publish(
            Event(
                type="agent.task",
                data={
                    "agent_id": agent_id,
                    "parent_id": parent_id,
                    "preset": preset,
                    "name": agent.name,
                    "agent_type": audience,
                    "task": task,
                },
                source=f"agent:{parent_id}",
                target=agent_id,
            )
        )
        self.debug.log(
            "agent_spawned",
            agent_id=agent_id,
            parent_id=parent_id,
            preset=preset,
            task=task,
            allowed_actions=sorted(agent.available_actions()),
        )
        return {
            "agent_id": agent_id,
            "name": agent.name,
            "agent_type": audience,
            "parent_id": parent_id,
            "preset": preset,
            "state": agent.state,
        }

    def send_message(self, *, sender_id: str, agent_id: str, text: str) -> dict[str, Any]:
        sender = self.agents.get(sender_id)
        target = self.agents.get(agent_id)
        if sender is None or target is None:
            raise ValueError("Отправитель или получатель не найден")
        if sender_id != "main" and agent_id != sender.parent_id:
            raise ValueError("Субагент может отправлять сообщения только родителю")
        self.bus.publish(
            Event(
                type="agent.message",
                data={"from_agent": sender_id, "text": text},
                source=f"agent:{sender_id}",
                target=agent_id,
            )
        )
        return {"delivered": True, "agent_id": agent_id}

    def interrupt(self, *, requester_id: str, agent_id: str, reason: str) -> dict[str, Any]:
        if agent_id == "main":
            raise ValueError("Главный агент нельзя прервать этим действием")
        agent = self.agents.get(agent_id)
        if agent is None:
            raise ValueError(f"Агент не найден: {agent_id}")
        agent.stop(wait=False)
        self._notify_parent(
            agent,
            "agent.interrupted",
            {"agent_id": agent_id, "reason": reason},
        )
        return {"agent_id": agent_id, "state": "stopped", "reason": reason}

    def hold(self, *, agent_id: str, until_event: str) -> None:
        agent = self.agents.get(agent_id)
        if agent is None:
            raise ValueError(f"Агент не найден: {agent_id}")
        agent.hold_until(until_event)

    def delete(self, *, requester_id: str, agent_id: str, reason: str) -> dict[str, Any]:
        if agent_id == "main":
            raise ValueError("Главный агент нельзя удалить этим действием")
        with self._lock:
            agent = self.agents.pop(agent_id, None)
        if agent is None:
            raise ValueError(f"Агент не найден: {agent_id}")
        agent.stop(wait=False)
        self.bus.unbind(agent_id)
        self._notify_parent(
            agent,
            "agent.deleted",
            {"agent_id": agent_id, "reason": reason},
        )
        return {"agent_id": agent_id, "deleted": True}

    def complete(
        self,
        *,
        agent_id: str,
        summary: str,
        event_type: str = "agent.completed",
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        agent = self.agents.get(agent_id)
        if agent is None:
            raise ValueError(f"Агент не найден: {agent_id}")
        agent.request_close()
        data = {"agent_id": agent_id, "summary": summary}
        if extra:
            data.update(extra)
        self._notify_parent(agent, event_type, data)
        return {"agent_id": agent_id, "accepted": True}

    def _notify_parent(self, agent: Agent, event_type: str, data: dict[str, Any]) -> None:
        if agent.parent_id:
            self.bus.publish(
                Event(
                    type=event_type,
                    data=data,
                    source=f"agent:{agent.agent_id}",
                    target=agent.parent_id,
                )
            )

    def list_agents(self) -> list[dict[str, Any]]:
        with self._lock:
            return [
                {
                    "agent_id": agent.agent_id,
                    "name": agent.name,
                    "parent_id": agent.parent_id,
                    "state": agent.state,
                    "preset": agent.metadata.get("preset"),
                    "agent_type": agent.metadata.get("agent_type", agent.audience),
                }
                for agent in sorted(self.agents.values(), key=lambda item: item.agent_id)
            ]

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
