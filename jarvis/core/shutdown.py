"""Logical session shutdown, separate from stopping a Python process."""

from __future__ import annotations
import json
import threading
from copy import deepcopy
from ..capabilities.api import PENDING, ActionContext
from ..infrastructure.context import read_json
from ..memory.archiver import Archiver
from .media import render_request
from .protocol import CallResult, parse_actions, response_format

INTERRUPTED = "Action interrupted by session shutdown."
TERMINATED = "Action interrupted because its execution process was terminated."


class SessionShutdown:
    def __init__(self, manager):
        self.manager = manager
        self.thread = None
        self.cancelled = threading.Event()
        self.lock = threading.Lock()
        if manager.memory is not None:
            saved = manager.memory.state()["shutdown"]
            if saved is not None and saved["stage"] == "finished":
                manager.memory.update_state(shutdown=None)

    @property
    def pending(self):
        return (
            self.manager.memory is not None
            and self.manager.memory.state()["shutdown"] is not None
        )

    def start(self, call_id=None):
        with self.lock:
            if self.thread is not None and self.thread.is_alive():
                raise ValueError("Session shutdown already in progress")
            manager, store = self.manager, self.manager.memory
            if store is None:
                raise RuntimeError("Persistent memory is required for shutdown")
            # The global gate orders the freeze against all accepted inputs and dispatch.
            with manager.input_lock:
                if not self.pending:
                    state = store.state()
                    store.update_state(
                        shutdown={
                            "session_id": state["session_id"],
                            "call_id": call_id,
                            "stage": "freeze",
                            "error": None,
                        }
                    )
                manager.frozen.set()
            self.thread = threading.Thread(
                target=self._run, name="jarvis-session-shutdown", daemon=True
            )
            self.thread.start()
        return PENDING

    def _stage(self, name):
        store = self.manager.memory
        state = store.state()["shutdown"]
        store.update_state(shutdown={**state, "stage": name, "error": None})

    def freeze(self):
        manager = self.manager
        manager.frozen.set()
        # Stop generation first; inputs have already been durably accepted.
        for agent in manager.agents_snapshot():
            agent.stop(wait=False)
        callback = manager.services.get("freeze_session_input")
        if callback:
            callback()
        shutdown_id = manager.memory.state()["shutdown"]["call_id"]
        for agent in manager.agents_snapshot():
            for pending in manager.results.for_agent(agent.agent_id):
                if pending.action_id == "shutdown" and pending.call_id == shutdown_id:
                    continue
                manager.results.discard(agent.agent_id, pending.call_id)
                manager.capabilities.cancel_execution(
                    agent.agent_id, pending.call_id, pending.action_id
                )
                self._append_result(agent.agent_id, pending.call_id, INTERRUPTED)
        # Also cover accepted actions not dispatched before the freeze, and pending
        # invocations lost when the previous Python process died.
        for record in manager.memory.load_all():
            for call in unfinished_calls(record["messages"]):
                if call["action_id"] != "shutdown":
                    self._append_result(
                        record["agent_id"], call["call_id"], INTERRUPTED
                    )
        manager.processes.stop()
        # Canceled input preparation must release files before archive conversion.
        for agent in manager.agents_snapshot():
            with agent._media_lock:
                pass

    def _append_result(self, agent_id, call_id, info):
        store = self.manager.memory
        record = next(
            r for r in store.records(include_deleted=True) if r["agent_id"] == agent_id
        )
        directory = store.instances / agent_id
        context = read_json(directory / "context.json", [])
        if any(_result_id(m) == call_id for m in context):
            return
        result = CallResult(call_id, {"status": "interrupted", "info": info}, agent_id)
        context.append(result.model_message())
        store._write_json(directory / "context.json", context)
        agent = self.manager.agents.get(agent_id)
        if agent is not None:
            with agent._generation_lock:
                agent.history = [agent.history[0], *deepcopy(context)]
        self.manager.debug.result(result, agent_id=agent_id)

    def _run(self):
        manager, store = self.manager, self.manager.memory
        archiver = Archiver(
            store,
            manager.config,
            manager.services.get(
                "memory_client",
                manager.services.get("memory_api_client", manager.client),
            ),
            manager.environment,
            stopped=self.cancelled.is_set,
        )
        operations = [
            ("freeze", self.freeze),
            ("interpret", archiver.interpret),
            ("instances", archiver.summarize_instances),
            ("summary", archiver.summarize_session),
            ("commit", archiver.commit),
            ("cleanup", self.cleanup),
            ("final", self.final),
        ]
        try:
            stage = store.state()["shutdown"]["stage"]
            if stage == "finished":
                manager.completed.set()
                return
            begin = [name for name, _ in operations].index(stage)
            for i in range(begin, len(operations)):
                name, operation = operations[i]
                archiver.check()
                operation()
                if name != "final":
                    self._stage(operations[i + 1][0])
        except Exception as exc:
            state = store.state()["shutdown"]
            store.update_state(shutdown={**state, "error": str(exc)})
            manager.debug.notice(
                "Ошибка завершения сессии на этапе "
                + state["stage"]
                + ": "
                + str(exc)
                + "\nДанные сохранены. После перезапуска завершение продолжится с этого этапа."
            )

    def cleanup(self):
        store = self.manager.memory
        store.clear_after_archive()
        for agent in self.manager.agents_snapshot():
            with agent._generation_lock:
                agent.history = [agent.history[0]]
                agent._used_call_ids.clear()
        state = store.state()
        # Advance exactly once even if the process stopped during cleanup.
        if state["session_id"] == state["shutdown"]["session_id"]:
            store.update_state(
                session_id=f"session_{state['next_session']:06d}",
                next_session=state["next_session"] + 1,
                started=False,
            )

    def final(self):
        manager, store = self.manager, self.manager.memory
        state = store.state()["shutdown"]
        main = manager.require_agent("main")
        prepare = manager.services.get("prepare_final_output")
        if prepare:
            prepare()
        action_id = "speech" if manager.actions.get("speech") is not None else "reply"
        spec = manager.actions.require(action_id)
        directory = store.sessions / state["session_id"] / "instances/main"
        # Service phase uses a private copy of the completed Main context.
        history = read_json(directory / "context.json")
        restart = manager.services.pop("session_restart_event", None)
        if restart is not None:
            history.append(restart.model_message())
            manager.debug.input(restart, agent_id="main")
        event = CallResult(state["call_id"], {"status": "completed"}, "main")
        manager.debug.result(event, agent_id="main")
        system = main.history[0]["content"] + (
            "\nThe logical session has been archived successfully. "
            "You have exactly one final action: "
            + action_id
            + ". Give the user your concluding words. "
            "No other actions or follow-up turns are permitted."
        )
        messages = [
            {"role": "system", "content": system},
            *history,
            event.model_message(),
        ]
        # No retries of final generation/action across restarts: at most one farewell.
        self._stage("finished")
        try:
            from ..infrastructure.subscription import SubscriptionClient
            def prepare(model, caps):
                from ..infrastructure.model_capabilities import ModelCapabilities
                if "/" in model and model != manager.model:
                    caps = manager.capabilities_for_model(model)
                caps = ModelCapabilities(model, caps.input_modalities)
                return render_request(messages, caps, directory, "main", state["session_id"])
            routing = isinstance(manager.client, SubscriptionClient)
            stream = manager.client.chat.completions.create(
                model=main.effective_model(),
                messages=messages if routing else prepare(main.effective_model(), manager.capabilities_for_model(main.effective_model())),
                **({"_jarvis_messages": prepare} if routing else {}),
                response_format=response_format({action_id: spec.data_schema}),
                stream=True,
            )
            with manager.final_lock:
                manager.final_stream = stream
            content = ""
            try:
                for chunk in stream:
                    if self.cancelled.is_set():
                        return
                    if chunk.choices:
                        content += chunk.choices[0].delta.content or ""
            finally:
                stream.close()
                with manager.final_lock:
                    manager.final_stream = None
            actions = parse_actions(json.loads(content))
            if len(actions) != 1 or actions[0].action_id != action_id:
                return
            from .protocol import validate_json

            validate_json(actions[0].data, spec.data_schema)
            manager.debug.model("main", content)
            context = ActionContext(
                agent_id="main",
                call_id=actions[0].call_id,
                action_id=action_id,
                capability_id=action_id,
                module_id=None,
                complete=lambda *a, **k: False,
                config=manager.config,
                agent_manager=manager.capabilities._agent_api,
                capabilities=manager.capabilities,
                services=manager.services,
            )
            spec.run(actions[0].data, context)
        finally:
            manager.completed.set()

    def stop(self):
        self.cancelled.set()
        with self.manager.final_lock:
            stream = self.manager.final_stream
        if stream is not None:
            stream.close()
        if self.thread is not None and self.thread is not threading.current_thread():
            self.thread.join(timeout=2)


def _result_id(message):
    content = message.get("content")
    if isinstance(content, list):
        content = next(
            (p.get("text") for p in content if p.get("type") == "text"), None
        )
    try:
        value = json.loads(content)
    except (TypeError, ValueError):
        return None
    return (
        value.get("call_id")
        if isinstance(value, dict) and value.get("event_id") == "call_result"
        else None
    )


def unfinished_calls(messages):
    completed = {v for m in messages if (v := _result_id(m))}
    # capability_error is also a terminal outcome for an invocation.
    for m in messages:
        try:
            event = json.loads(m.get("content"))
            if event.get("event_id") == "capability_error":
                completed.add(event.get("data", {}).get("call_id"))
        except (ValueError, TypeError, AttributeError):
            pass
    calls = {}
    for m in messages:
        if m.get("role") != "assistant":
            continue
        try:
            for call in parse_actions(json.loads(m.get("content"))):
                if call.action_id != "no_action" and call.call_id not in completed:
                    calls[call.call_id] = call.model_value()
        except (ValueError, TypeError):
            pass
    return list(calls.values())
