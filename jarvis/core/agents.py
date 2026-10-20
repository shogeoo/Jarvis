"""Неизменяемое event-thinking-action ядро Jarvis."""

from __future__ import annotations

import re
import threading
import traceback
from pathlib import Path
from typing import Any

from ..infrastructure.console import logger
from ..infrastructure.context import MemoryStore
from ..infrastructure.debug import Debugger
from ..infrastructure.model_capabilities import ModelCapabilities
from ..infrastructure.semantic_memory import SemanticMemory
from ..presets import PresetStore

from .agent import Agent
from .calls import CallResultTracker
from .core_protocol import _capability_identity
from .events import EventBus
from .instance_configuration import InstanceConfiguration
from .lifecycle import ProcessManager
from .protocol import (
    CallResult,
    Event,
)
from .registry import ActionRegistry, EventRegistry


class AgentManager(InstanceConfiguration):
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
        self.presets.resolve_kind = capabilities.resolve_kind
        self.presets.is_system = capabilities.is_system
        self.environment = environment
        self.config = config
        self.services = services if services is not None else {}
        self.model_capabilities = model_capabilities
        self._model_capability_cache = {}
        self._result_delivery = threading.local()
        self.debug = debug or Debugger(enabled=False)
        self.memory = memory
        self.semantic_memory = SemanticMemory(
            (memory.instances if memory is not None else (Path(config.jarvis_dir) if config is not None else presets.root.parent) / 'memory/instances')
            / 'main/semantic.json'
        )
        self.input_lock = threading.RLock()
        self.frozen = threading.Event()
        self.completed = threading.Event()
        self.final_lock = threading.Lock()
        self.final_stream = None
        self.core_stops = {}
        from ..memory.reranker import Reranker
        from ..memory.reader import ArchiveReader
        from .shutdown import SessionShutdown
        root = Path(config.jarvis_dir) if config is not None else presets.root.parent
        self.reranker = Reranker(root)
        self.archive_reader = ArchiveReader(memory.root if memory is not None else root / 'memory', self.reranker)
        self.session_shutdown = SessionShutdown(self)
        self.restoring_shutdown = False
        self.results = CallResultTracker(debug=self.debug)
        self.stopping = threading.Event()
        self.processes = ProcessManager()
        self.services.setdefault("process_manager", self.processes)
        self.agents: dict[str, Agent] = {}
        self.primary_agent_id: str | None = None
        self._lock = threading.RLock()
        self._lifecycle_lock = threading.RLock()
        bus.manager = self  # type: ignore[attr-defined]

    def _new_id(self) -> str:
        if self.memory is not None:
            return self.memory.allocate_agent_id()
        with self._lock:
            ids = set(self.agents)
            if self.memory is not None:
                ids.update(record["agent_id"] for record in self.memory.load_all())
            number = (
                max(
                    (
                        int(value[6:])
                        for value in ids
                        if re.fullmatch(r"agent-[0-9]+", value)
                    ),
                    default=0,
                )
                + 1
            )
            return f"agent-{number:05d}"

    def deliver_result(self, result: CallResult) -> bool:
        """Доставить результат только инициировавшему агенту."""

        with self._lock:
            agent = self.agents.get(result.agent_id)
            self.core_stops.pop((result.agent_id, result.call_id), None)
        if agent is None:
            self.debug.log(
                "call_result_dropped",
                call_id=result.call_id,
                reason="unknown_agent",
            )
            return False
        batch = getattr(self._result_delivery, "batch", None)
        if batch is not None:
            batch.append((agent, result))
        else:
            return agent.enqueue_result(result)
        return True

    def deliver_speech_with_interruptions(self, event: Event, completions) -> bool:
        """Finalize speech calls and enqueue the whole input atomically."""
        self.events.validate(event.event_id, event.data)
        agent = self.require_agent(event.target)
        collected = []
        self._result_delivery.batch = collected
        try:
            for complete in completions:
                complete({"status": "interrupted"})
        finally:
            del self._result_delivery.batch
            for recipient in dict.fromkeys(
                item[0] for item in collected if item[0] is not agent
            ):
                recipient.enqueue_batch(
                    [result for owner, result in collected if owner is recipient]
                )
        return agent.enqueue_batch(
            [result for owner, result in collected if owner is agent] + [event]
        )

    def spawn_root(self, *, preset: str) -> Agent:
        """Создать корневой экземпляр с постоянным agent_id main."""

        return self._spawn(preset=preset, parent_id=None, primary=True)

    def restore(self, *, preset: str) -> Agent | None:
        """Поднять экземпляры из памяти или создать новый корневой агент."""

        records = self.memory.load_all() if self.memory is not None else []
        if records and not self.restoring_shutdown:
            from .shutdown import unfinished_calls, TERMINATED
            for record in records:
                for call in unfinished_calls(record['messages']):
                    if call['action_id'] == 'shutdown':
                        continue
                    record['messages'].append(CallResult(call['call_id'], {'status': 'interrupted', 'info': TERMINATED}, record['agent_id']).model_message())
                    record['pending_turn'] = True
                self.memory.save(record)
        if not records and self.memory is not None and self.memory.has_existing_state():
            logger.error(
                "Jarvis restore failed: сохранённая память есть, но не содержит целого состояния; данные сохранены"
            )
            return None
        if not records:
            return self.spawn_root(preset=preset)
        return self._restore(records)

    def _restore(self, records: list[dict[str, Any]]) -> Agent | None:
        primary = next(
            (
                record
                for record in records
                if record["agent_id"] == "main" and record["parent_id"] is None
            ),
            None,
        )
        if primary is None:
            self.debug.log(
                "memory_restore_failed", error="Нет сохранённого корневого агента"
            )
            logger.error(
                "Jarvis restore failed: нет целого корневого агента; сохранённые данные оставлены"
            )
            return None
        try:
            main_agent = self._spawn_record(primary, primary=True)
        except Exception as exc:  # noqa: BLE001
            self.debug.log(
                "memory_restore_failed",
                agent_id=primary.get("agent_id"),
                error=str(exc),
                traceback=traceback.format_exc(),
            )
            logger.error(
                f"Jarvis restore failed for {primary.get('agent_id')}: {traceback.format_exc()}"
            )
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
                        logger.error(
                            f"Jarvis restore failed for {record['agent_id']}: {traceback.format_exc()}"
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
            logger.error(
                f"Jarvis restore skipped {record['agent_id']}: parent {record['parent_id']} отсутствует"
            )
        if not self.restoring_shutdown:
            for agent in self.agents_snapshot():
                if getattr(agent, '_needs_generation', False) and not agent.primary:
                    agent.resume()
        return main_agent

    def _spawn_record(self, record: dict[str, Any], *, primary: bool) -> Agent:
        preset_id = record.get("preset_id", record.get("preset"))
        selected = self.presets.load(preset_id)
        additions = record.get("capabilities_add", [])
        instructions = record.get("additional_instructions", "")
        legacy = "_legacy_capabilities" in record
        if legacy:
            additions = [
                value
                for value in record["_legacy_capabilities"]
                if value not in selected.capabilities
            ]
            old_person = record.get("_legacy_person")
            if old_person and old_person != selected.person_prompt:
                instructions = old_person
        return self._spawn(
            preset=preset_id,
            parent_id=record["parent_id"],
            primary=primary,
            agent_id=record["agent_id"],
            restored_messages=record["messages"],
            additional_instructions=instructions,
            model=record.get("model"),
            capabilities_add=additions,
            description=record.get("description"),
            pending_turn=record.get("pending_turn", False),
            persist_initial=legacy
            or bool(
                record.get("restore_from_preset")
                or record.get("restore_missing_context")
            ),
        )

    def spawn(self, *, parent_id: str, preset_id: str, description: str | None = None) -> dict[str, Any]:
        if parent_id not in self.agents:
            raise ValueError(f"Родительский агент не найден: {parent_id}")
        self.presets.load(preset_id)
        if description is not None and not isinstance(description, str):
            raise ValueError("description must be a string or null")
        agent = self._spawn(preset=preset_id, parent_id=parent_id, description=description)
        return self.describe(agent)

    def _spawn(self, **kwargs) -> Agent:
        with self._lifecycle_lock:
            return self._spawn_locked(**kwargs)

    def _spawn_locked(
        self,
        *,
        preset: str,
        parent_id: str | None,
        primary: bool = False,
        agent_id: str | None = None,
        restored_messages: list[dict[str, Any]] | None = None,
        persist_initial: bool = True,
        additional_instructions: str = "",
        model: str | None = None,
        capabilities_add: list[str] | None = None,
        description: str | None = None,
        pending_turn: bool = False,
    ) -> Agent:
        if self.stopping.is_set() or (self.frozen.is_set() and not self.restoring_shutdown):
            raise RuntimeError("runtime_stopping")
        selected = self.presets.load(preset)
        initial = {
            "modules": set(selected.modules),
            "actions": set(selected.actions),
            "handlers": set(selected.handlers),
        }
        if capabilities_add:
            additions = self.capabilities.partition(capabilities_add)
            for key in initial:
                initial[key].update(additions[key])
        system_ids = {name for name, spec in self.actions.all().items() if spec.owner.startswith("core")}
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
                preset,
                self,
                parent_id=parent_id,
                primary=primary,
                enabled_modules=list(initial["modules"]),
                enabled_actions=list(initial["actions"]),
                enabled_handlers=list(initial["handlers"]),
                restored_messages=restored_messages,
                additional_instructions=additional_instructions,
                model=model,
                capabilities_add=capabilities_add,
                description=description,
                pending_turn=pending_turn,
            )
            self.agents[resolved_id] = agent
            self.bus.bind(agent)
            if primary and self.primary_agent_id is None:
                self.primary_agent_id = resolved_id
        try:
            if not self.restoring_shutdown:
                agent.start()
            else:
                agent._contract()
            if not self.restoring_shutdown:
                self.capabilities.start_snapshot(self.capabilities.runnable_snapshot(initial))
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
        with agent._generation_lock, self._lock:
            # Удалённый агент мог завершить ход уже после memory.delete;
            # поздний persist не должен воскрешать его запись.
            if self.agents.get(agent.agent_id) is not agent or agent._stop.is_set():
                return
            try:
                stored = self.memory.save(agent.memory_record())
                agent.history[1:] = stored
            except Exception as exc:  # noqa: BLE001
                self.debug.log(
                    "memory_save_error", agent_id=agent.agent_id, error=str(exc)
                )
                logger.error(f"Jarvis persistence failed for {agent.agent_id}: {traceback.format_exc()}")
                raise

    def require_agent(self, agent_id: str) -> Agent:
        agent = self.agents.get(agent_id)
        if agent is None:
            raise ValueError(f"Агент не найден: {agent_id}")
        return agent

    def interrupt(
        self, *, agent_id: str, requester_id: str | None = None
    ) -> dict[str, Any]:
        agent = self.require_agent(agent_id)
        agent.interrupt()
        return {"agent_id": agent_id, "state": "waiting"}

    def delete(self, *, agent_id: str) -> dict[str, Any]:
        with self._lifecycle_lock:
            return self._delete_locked(agent_id=agent_id)

    def _delete_locked(self, *, agent_id: str) -> dict[str, Any]:
        self.require_agent(agent_id)
        records = self.memory.load_all() if self.memory is not None else []
        ordered = []
        visited = set()

        def walk(current: str) -> None:
            if current in visited:
                return
            visited.add(current)
            for child in self.agents_snapshot():
                if child.parent_id == current:
                    walk(child.agent_id)
            for record in records:
                if record["parent_id"] == current:
                    walk(record["agent_id"])
            ordered.append(current)

        walk(agent_id)
        with self._lock:
            removed = [
                self.agents.pop(candidate)
                for candidate in ordered
                if candidate in self.agents
            ]
            if self.primary_agent_id in visited:
                self.primary_agent_id = None
        errors = []
        for agent in removed:
            candidate = agent.agent_id
            snapshot = agent.capabilities_snapshot()
            agent.stop(wait=False)
            self.bus.unbind(candidate)
            for pending in self.results.for_agent(candidate):
                if self.memory is not None:
                    from .shutdown import TERMINATED
                    self.session_shutdown._append_result(candidate, pending.call_id, TERMINATED)
                self.capabilities.cancel_call(candidate, pending.call_id)
            self.results.discard_agent(candidate)
            try:
                if self.memory is not None:
                    record = self.memory.load(agent.preset, candidate)
                    if record is not None:
                        record['messages'].append(Event('instance_deleted', {'agent_id': candidate, 'reason': 'Instance and descendants deleted.'}).model_message())
                        self.memory.save(record)
                    self.memory.delete(agent.preset, candidate)
            except Exception as exc:
                errors.append(str(exc))
            try:
                self.capabilities.release_snapshot(
                    snapshot, agents=self.agents_snapshot()
                )
            except Exception as exc:
                errors.append(str(exc))
            with agent._events.mutex:
                agent._events.queue.clear()
                agent._events.queue.append(None)
                agent._events.not_empty.notify_all()
            with agent._generation_lock:
                agent.history.clear()
                agent._used_call_ids.clear()
            with agent._capabilities_lock:
                agent._enabled_modules.clear()
                agent._enabled_actions.clear()
                agent._enabled_handlers.clear()
        if self.memory is not None:
            for record in records:
                if record["agent_id"] in visited:
                    try:
                        self.memory.delete(record["preset_id"], record["agent_id"])
                    except Exception as exc:
                        errors.append(str(exc))
        if errors:
            raise RuntimeError("Agent cleanup failed: " + "; ".join(errors))
        return {"agent_id": agent_id, "deleted": True}

    def remove_preset(self, preset_id: str) -> None:
        with self._lifecycle_lock:
            records = self.memory.load_all() if self.memory is not None else []
            tree = {
                item["agent_id"] for item in records if item["preset_id"] == preset_id
            }
            tree.update(
                agent.agent_id
                for agent in self.agents_snapshot()
                if agent.preset == preset_id
            )
            changed = True
            while changed:
                before = len(tree)
                tree.update(
                    item["agent_id"] for item in records if item["parent_id"] in tree
                )
                tree.update(
                    agent.agent_id
                    for agent in self.agents_snapshot()
                    if agent.parent_id in tree
                )
                changed = len(tree) != before
            for agent_id in list(tree):
                if agent_id in self.agents:
                    self.delete(agent_id=agent_id)
            if self.memory is not None:
                for record in records:
                    if record["agent_id"] in tree:
                        self.memory.delete(
                            record.get("preset_id", record.get("preset")),
                            record["agent_id"],
                        )
                self.memory.delete_preset(preset_id)
            self.presets.delete(preset_id)

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

    def interrupt_execution(self, agent_id, call_id, info):
        pending = self.results.discard(agent_id, call_id)
        if pending is not None:
            self.deliver_result(CallResult(call_id, {'status': 'interrupted', 'info': info}, agent_id))

    def fail_call(self, agent_id: str, call_id: str, error: Exception | str) -> None:
        with self._lock:
            self.core_stops.pop((agent_id, call_id), None)
        pending = self.results.discard(agent_id, call_id)
        if pending is not None:
            kind, capability_id = _capability_identity(pending.action_id)
            self.report_capability_error(
                kind, capability_id, error, agent_id=agent_id, call_id=call_id
            )

    def disable_call(self, agent_id: str, call_id: str) -> None:
        self._finish_stopped_call(
            agent_id,
            call_id,
            status="disabled",
            info="Action was disabled before completion.",
        )

    def pause_call(self, agent_id: str, call_id: str) -> None:
        self._finish_stopped_call(
            agent_id,
            call_id,
            status="paused",
            info="Capability is globally paused.",
        )

    def _finish_stopped_call(
        self, agent_id: str, call_id: str, *, status: str, info: str
    ) -> None:
        pending = self.results.discard(agent_id, call_id)
        if pending is not None:
            self.capabilities.cancel_execution(agent_id, call_id, pending.action_id)
            self.deliver_result(
                CallResult(
                    call_id=call_id,
                    agent_id=agent_id,
                    data={"status": status, "info": info},
                )
            )

    def report_capability_error(
        self,
        kind: str,
        capability_id: str,
        error: Exception | str,
        *,
        agent_id: str | None = None,
        call_id: str | None = None,
    ) -> None:
        target = agent_id
        event_id = "capability_error"
        data = {
            "capability_id": capability_id,
            "error": str(error),
            "call_id": call_id,
        }
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
        if self.stopping.is_set():
            return
        self.stopping.set()
        with self.input_lock:
            self.frozen.set()
        self.session_shutdown.stop()
        agents = self.agents_snapshot()
        for agent in agents:
            agent.stop(wait=False)
        from .shutdown import TERMINATED
        for agent in agents:
            for pending in self.results.for_agent(agent.agent_id):
                if pending.action_id == 'shutdown':
                    continue
                self.results.discard(agent.agent_id, pending.call_id)
                self.capabilities.cancel_execution(agent.agent_id, pending.call_id, pending.action_id)
                if self.memory is not None:
                    self.session_shutdown._append_result(agent.agent_id, pending.call_id, TERMINATED)
        self.results.clear()

    def shutdown(self) -> None:
        self.begin_shutdown()
        self.processes.stop()
        self.capabilities.shutdown()
        self.reranker.close()
        for agent in list(self.agents.values()):
            if agent._thread is not None:
                agent._thread.join(timeout=2)
