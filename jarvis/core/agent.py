"""Неизменяемое event-thinking-action ядро Jarvis."""

from __future__ import annotations

import json
import queue
import re
import threading
from typing import TYPE_CHECKING, Any

from ..capabilities.api import ActionDefinition, EventDefinition

if TYPE_CHECKING:
    from .agents import AgentManager
from .core_protocol import _capability_identity
from .descriptions import apply_descriptions
from .generation import AgentGeneration
from .prompts import agent_system_prompt
from .protocol import (
    ActionRequest,
    CallResult,
    Event,
    canonical_event_value,
)


class Agent(AgentGeneration):
    """Самостоятельный агент со своим контекстом, пачкой и потоком."""

    def __init__(
        self,
        agent_id: str,
        preset: str,
        manager: "AgentManager",
        *,
        parent_id: str | None = None,
        primary: bool = False,
        enabled_modules: set[str] | None = None,
        enabled_actions: set[str] | None = None,
        enabled_handlers: set[str] | None = None,
        restored_messages: list[dict[str, Any]] | None = None,
        additional_instructions: str = "",
        model: str | None = None,
        capabilities_add: list[str] | None = None,
        description: str | None = None,
        pending_turn: bool = False,
    ):
        self.agent_id = agent_id
        self.preset = preset
        self.parent_id = parent_id
        self.manager = manager
        from ..infrastructure.semantic_memory import SemanticMemory
        memory_root = manager.memory.instances if manager.memory is not None else manager.presets.root.parent / 'memory/instances'
        self._semantic_memory = SemanticMemory(memory_root / agent_id / 'semantic.json')
        self.primary = primary
        if description is not None and not isinstance(description, str):
            raise ValueError("description must be a string or null")
        self.description = description
        selected = manager.presets.load(preset)
        self.additional_instructions = additional_instructions
        self.model_override = model
        supplied = [
            value
            for values in (enabled_modules, enabled_actions, enabled_handlers)
            for value in (values or [])
        ]
        self._additional_capabilities = (
            list(capabilities_add)
            if capabilities_add is not None
            else [value for value in supplied if value not in selected.capabilities]
        )
        self._assignment_order = {
            "modules": list(enabled_modules or ()),
            "actions": list(enabled_actions or ()),
            "handlers": list(enabled_handlers or ()),
        }
        self._enabled_modules = set(enabled_modules or ())
        self._enabled_actions = set(enabled_actions or ())
        self._enabled_handlers = set(enabled_handlers or ())
        self._capabilities_lock = threading.RLock()
        self.history: list[dict[str, Any]] = [
            {"role": "system", "content": ""},
            *[
                dict(message)
                for message in (restored_messages or ())
                if message.get("role") != "system"
            ],
        ]
        for message in self.history[1:]:
            if message.get("role") != "user":
                continue
            content = message.get("content")
            text = (
                content
                if isinstance(content, str)
                else content[0].get("text")
                if isinstance(content, list)
                and content
                and isinstance(content[0], dict)
                else None
            )
            try:
                value = json.loads(text)
            except (ValueError, TypeError):
                continue
            if not isinstance(value, dict):
                continue
            current = canonical_event_value(
                value, self.manager.capabilities.event_id_for_handler
            )
            if current != value:
                if isinstance(content, str):
                    message["content"] = json.dumps(
                        current, ensure_ascii=False, separators=(",", ":")
                    )
                else:
                    message["content"] = [
                        {
                            **content[0],
                            "text": json.dumps(
                                current, ensure_ascii=False, separators=(",", ":")
                            ),
                        },
                        *content[1:],
                    ]
        self._events: "queue.Queue[Event | CallResult | None]" = queue.Queue()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._state = "created"
        self._state_lock = threading.Lock()
        self._generation = 0
        self._generation_lock = threading.RLock()
        self._active_stream = None
        self._needs_generation = pending_turn
        self._accepted_inputs = set()
        self._used_call_ids = self._scan_call_ids(self.history)

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
                    if isinstance(action, dict) and isinstance(
                        action.get("call_id"), str
                    ):
                        used.add(action["call_id"])
        return used

    @property
    def semantic_memory(self):
        return self._semantic_memory

    @property
    def person_prompt(self):
        return self.manager.presets.load(self.preset).person_prompt

    def effective_model(self):
        preset = self.manager.presets.load(self.preset)
        return self.model_override or preset.model or self.manager.default_model()

    def _refresh_configuration(self):
        preset = self.manager.presets.load(self.preset)
        ordered = list(
            dict.fromkeys([*self.intrinsic_capabilities(), *preset.capabilities, *self._additional_capabilities])
        )
        partition = self.manager.capabilities.partition(ordered)
        if self.preset != "capability_manager":
            partition["actions"] = [value for value in partition["actions"] if value not in {"list_capabilities", "toggle_capability"}]
        previous = self.capabilities_snapshot()
        current = {key: set(values) for key, values in partition.items()}
        runnable = self.manager.capabilities.runnable_snapshot(current)
        self.manager.capabilities.load_snapshot(runnable, start_handlers=False)
        with self._capabilities_lock:
            self._assignment_order = partition
            self._enabled_modules = current["modules"]
            self._enabled_actions = current["actions"]
            self._enabled_handlers = current["handlers"]
        if not self.manager.restoring_shutdown:
            self.manager.capabilities.start_snapshot(runnable)
        removed = {key: previous[key] - current[key] for key in previous}
        if any(removed.values()):
            self.manager.capabilities.release_snapshot(
                removed, agents=self.manager.agents_snapshot()
            )

    def intrinsic_capabilities(self):
        from .system_actions import system_capabilities

        return system_capabilities(primary=self.primary, preset=self.preset)

    @property
    def state(self) -> str:
        with self._state_lock:
            return self._state

    def assigned_capability_ids(self):
        """Represent each indivisible module once, including paused assignments."""
        actions, _ = self._contract()
        return list(dict.fromkeys([
            *self._assignment_order["modules"],
            *self._assignment_order["handlers"],
            *self._assignment_order["actions"],
            *[name for name in actions if "." not in name],
        ]))

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
        return self.capabilities_snapshot()

    def capabilities_snapshot_lists(self):
        return {
            key: [value for value in self._assignment_order[key] if value in values]
            for key, values in self.capabilities_snapshot().items()
        }

    def is_enabled_action(self, action_id: str) -> bool:
        if action_id in {"no_action", "speech"}:
            return True
        snapshot = self.capabilities_snapshot()
        return action_id in snapshot["actions"] or any(
            action_id in self.manager.capabilities.module_action_ids(module_id)
            for module_id in snapshot["modules"]
        )

    def enable_module(self, module_id: str) -> None:
        if (
            module_id not in self.manager.presets.load(self.preset).capabilities
            and module_id not in self.intrinsic_capabilities()
            and module_id not in self._additional_capabilities
        ):
            self._additional_capabilities.append(module_id)
        self._refresh_configuration()
        self._persist_context()

    def disable_module(self, module_id: str) -> None:
        if self.manager.capabilities.is_system(module_id):
            raise ValueError("Cannot disable a system capability")
        if module_id in self.manager.presets.load(self.preset).capabilities:
            raise ValueError("Cannot remove a mandatory preset capability")
        self._additional_capabilities = [
            value for value in self._additional_capabilities if value != module_id
        ]
        self._refresh_configuration()
        self._persist_context()

    def enable_action(self, action_id: str) -> None:
        if (
            action_id not in self.manager.presets.load(self.preset).capabilities
            and action_id not in self.intrinsic_capabilities()
            and action_id not in self._additional_capabilities
        ):
            self._additional_capabilities.append(action_id)
        self._refresh_configuration()
        self._persist_context()

    def disable_action(self, action_id: str) -> None:
        if self.manager.capabilities.is_system(action_id):
            raise ValueError("Cannot disable a system capability")
        if action_id in self.manager.presets.load(self.preset).capabilities:
            raise ValueError("Cannot remove a mandatory preset capability")
        self._additional_capabilities = [
            value for value in self._additional_capabilities if value != action_id
        ]
        self._refresh_configuration()
        self._persist_context()

    def enable_handler(self, handler_id: str) -> None:
        if (
            handler_id not in self.manager.presets.load(self.preset).capabilities
            and handler_id not in self.intrinsic_capabilities()
            and handler_id not in self._additional_capabilities
        ):
            self._additional_capabilities.append(handler_id)
        self._refresh_configuration()
        self._persist_context()

    def disable_handler(self, handler_id: str) -> None:
        if self.manager.capabilities.is_system(handler_id):
            raise ValueError("Cannot disable a system capability")
        if handler_id in self.manager.presets.load(self.preset).capabilities:
            raise ValueError("Cannot remove a mandatory preset capability")
        self._additional_capabilities = [
            value for value in self._additional_capabilities if value != handler_id
        ]
        self._refresh_configuration()
        self._persist_context()

    def accepts_event(self, event: Event) -> bool:
        if event.handler_id == "core:speech":
            return self.primary or "speech_detected" in self.standalone_handlers()
        if (
            event.handler_id is not None
            and self.manager.capabilities.is_globally_paused(
                "handler", event.handler_id
            )
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
        return {
            "agent_id": self.agent_id,
            "preset_id": self.preset,
            "parent_id": self.parent_id,
            "description": self.description,
            "model": self.model_override,
            "additional_instructions": self.additional_instructions,
            "capabilities_add": list(self._additional_capabilities),
            "messages": list(self.history[1:]),
            "pending_turn": self._needs_generation,
        }

    def _persist_context(self) -> None:
        self.manager.persist_agent(self)

    def _contract(
        self,
        snapshot: dict[str, set[str]] | None = None,
        *,
        model_id: str | None = None,
    ) -> tuple[dict[str, ActionDefinition], dict[str, EventDefinition]]:
        self._refresh_configuration()
        assigned = self.capabilities_snapshot()
        paused = self.manager.capabilities.global_paused()
        enabled = {key: assigned[key] - paused[key] for key in assigned}
        snapshot = (
            enabled
            if snapshot is None
            else {key: set(snapshot[key]) & enabled[key] for key in enabled}
        )
        missing = self.manager.capabilities.missing(
            self.manager.capabilities.runnable_snapshot(self.capabilities_snapshot())
        )
        if missing:
            raise ValueError(
                f"Пресет {self.preset} ссылается на незагруженные capabilities: "
                f"{sorted(missing)}"
            )
        actions = self.manager.actions.for_capabilities(
            modules=snapshot["modules"],
            actions=snapshot["actions"],
            primary=self.primary,
        )
        actions = {
            name: spec
            for name, spec in actions.items()
            if name not in paused["actions"]
        }
        assigned_actions = [
            name for name in self._assignment_order["actions"] if name in actions
        ]
        actions = {
            **{
                name: spec
                for name, spec in actions.items()
                if name not in assigned_actions
            },
            **{name: actions[name] for name in assigned_actions},
        }
        events = self.manager.events.for_capabilities(
            modules=snapshot["modules"],
            handlers=snapshot["handlers"],
            primary=self.primary,
        )
        assigned_events = [
            self.manager.capabilities.event_id_for_handler(name)
            for name in self._assignment_order["handlers"]
            if name in snapshot["handlers"]
        ]
        events = {
            **{
                name: spec
                for name, spec in events.items()
                if name not in assigned_events
            },
            **{name: events[name] for name in assigned_events},
        }
        actions, events = apply_descriptions(actions, events)
        self.history[0] = {
            "role": "system",
            "content": agent_system_prompt(
                self.person_prompt.replace(
                    "{storage_root}",
                    str(self.manager.capabilities.storage_root.resolve()),
                )
                if self.preset == "capability_manager"
                else self.person_prompt,
                self.manager.environment,
                actions,
                events,
                self.manager.capabilities.catalog(
                    {
                        key: [
                            name
                            for name in self._assignment_order[key]
                            if name in snapshot[key]
                            and not (
                                key == "actions"
                                and self.manager.actions.get(name)
                                and self.manager.actions.get(name).owner.startswith(
                                    "core"
                                )
                            )
                        ]
                        for key in snapshot
                    },
                    describe_unloaded=True,
                ),
                agent_id=self.agent_id,
                preset_id=self.preset,
                additional_instructions=self.additional_instructions,
                model_capabilities=self.manager.capabilities_for_model(
                    model_id or self.effective_model()
                ),
                semantic_memory=self.semantic_memory.snapshot(),
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
        return self.enqueue_batch([event])

    def enqueue_result(self, result: CallResult) -> bool:
        return self.enqueue_batch([result])

    @staticmethod
    def _input_identity(item):
        return ('result', item.call_id) if isinstance(item, CallResult) else ('event', item.id)

    def enqueue_batch(self, items) -> bool:
        with self.manager.input_lock:
            if self._stop.is_set() or self.manager.frozen.is_set():
                return False
            with self._generation_lock:
                old_length = len(self.history)
                old_pending = self._needs_generation
                try:
                    fresh = [item for item in items if self._input_identity(item) not in self._accepted_inputs]
                    self.history.extend(item.model_message() for item in fresh)
                    self._needs_generation = True
                    self._persist_context()
                    self._accepted_inputs.update(self._input_identity(item) for item in fresh)
                except Exception:
                    del self.history[old_length:]
                    self._needs_generation = old_pending
                    raise
            for item in items:
                if isinstance(item, CallResult):
                    self.manager.debug.result(item, agent_id=self.agent_id)
                else:
                    self.manager.debug.input(item, agent_id=self.agent_id)
            with self._events.mutex:
                self._events.queue.extend(items)
                self._events.unfinished_tasks += len(items)
                self._events.not_empty.notify_all()
            return True

    def resume(self):
        # Empty batch requests inference over an already durable pending input.
        self._events.put(_RESUME)

    def interrupt(self) -> None:
        with self._generation_lock:
            self._generation += 1
            generation = self._generation
            stream = self._active_stream
            self._active_stream = None
            self._thread = threading.Thread(
                target=self._run,
                args=(generation,),
                name=f"jarvis-agent-{self.agent_id}-{generation}",
                daemon=True,
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
            batch = [] if first is _RESUME else [first]
            while True:
                try:
                    event = self._events.get_nowait()
                except queue.Empty:
                    break
                if event is None:
                    self._stop.set()
                    break
                if event is not _RESUME:
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

        missing = [item for item in batch if self._input_identity(item) not in self._accepted_inputs]
        if missing:
            with self.manager.input_lock, self._generation_lock:
                if self._stop.is_set() or self.manager.frozen.is_set():
                    return
                self.history.extend(item.model_message() for item in missing)
                self._needs_generation = True
                self._persist_context()
                self._accepted_inputs.update(self._input_identity(item) for item in missing)
        try:
            snapshot = self.known_snapshot()
        except Exception as exc:  # noqa: BLE001
            self._model_failure(exc)
            return
        self._run_turn(batch, snapshot, generation)

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
                with self.manager.input_lock:
                    if self.manager.frozen.is_set():
                        break
                    self.manager.capabilities.dispatch(action=action, spec=spec, agent=self)
            except Exception as exc:  # noqa: BLE001
                kind, capability_id = _capability_identity(action.action_id)
                self.manager.report_capability_error(
                    kind,
                    capability_id,
                    exc,
                    agent_id=self.agent_id,
                    call_id=action.call_id,
                )

_RESUME = object()
