"""Неизменяемое event-thinking-action ядро Jarvis."""

from __future__ import annotations

import json
import time
import traceback
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    pass
from .protocol import (
    CallResult,
    Event,
    actions_response_schema,
    parse_actions,
    response_format,
    validate_json,
)


class AgentGeneration:
    def _event_history_message(self, item: Event) -> dict[str, Any]:
        return item.model_message()

    def _run_turn(
        self,
        batch: list[Event | CallResult],
        snapshot: dict[str, set[str]],
        generation: int,
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
                self._model_failure(exc)
                return

        if not batch and not self._needs_generation:
            self._set_state("waiting")
            return

        schemas = {name: spec.data_schema for name, spec in specs.items()}

        candidates = list(
            dict.fromkeys(
                value
                for value in (
                    self.model_override,
                    self.manager.presets.load(self.preset).model,
                    self.manager.default_model(),
                )
                if value
            )
        )
        candidate_index = 0
        active_model = candidates[0]
        failures = 0
        while not self._stop.is_set() and generation == self._generation:
            try:
                specs, _ = self._contract(model_id=active_model)
                schemas = {name: spec.data_schema for name, spec in specs.items()}
                from ..infrastructure.subscription import SubscriptionClient
                routing = isinstance(self.manager.client, SubscriptionClient)
                with self._generation_lock:
                    from copy import deepcopy
                    original_history = deepcopy(self.history)
                request_messages = (original_history if routing else self._model_messages(active_model, history=original_history))
                request_started = time.monotonic()
                self.manager.debug.log("model_request_started", agent_id=self.agent_id,
                                       model=active_model, messages=len(request_messages),
                                       system_characters=len(request_messages[0]["content"]))
                stream = self.manager.client.chat.completions.create(
                    model=active_model,
                    messages=request_messages,
                    response_format=response_format(schemas),
                    **(
                        {"reasoning_effort": self.manager.config.reasoning_effort}
                        if self.manager.config is not None
                        and self.manager.config.reasoning_effort is not None
                        else {}
                    ),
                    stream=True,
                    **({"_jarvis_messages": lambda model, caps, history=original_history: self._model_messages(model, capabilities=caps, history=history)} if routing else {}),
                )
                with self._generation_lock:
                    if generation != self._generation or self._stop.is_set():
                        stream.close()
                        return
                    self._active_stream = stream
                first_chunk = True
                content = ""
                refusal = ""
                thinking = ""
                reasoning_details = []
                try:
                    for chunk in stream:
                        if generation != self._generation or self._stop.is_set():
                            return
                        if first_chunk:
                            self.manager.debug.log("model_first_chunk", agent_id=self.agent_id,
                                                   model=active_model,
                                                   seconds=round(time.monotonic() - request_started, 3))
                            first_chunk = False
                        if chunk.choices:
                            delta = chunk.choices[0].delta
                            content += delta.content or ""
                            refusal += getattr(delta, "refusal", None) or ""
                            thought = getattr(delta, 'reasoning_content', None) or getattr(delta, 'reasoning', None)
                            if isinstance(thought, str):
                                thinking += thought
                            details = getattr(delta, 'reasoning_details', None)
                            if isinstance(details, list):
                                reasoning_details.extend(details)
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
                self.manager.debug.notice(f"Model request failed [{self.agent_id}, {active_model}]: {exc}")
                status = getattr(exc, "status_code", None)
                insufficient_balance = status == 402 or any(
                    phrase in str(exc).lower() for phrase in
                    ("insufficient credits", "insufficient funds", "insufficient balance", "requires more credits")
                )
                if insufficient_balance:
                    self.manager.debug.notice("API: недостаточно средств. Пополните баланс провайдера.")
                    self._model_failure(exc)
                    return
                if status in {400, 401, 422} and not (status == 400 and "model" in str(exc).lower()):
                    self._model_failure(exc)
                    return
                unavailable = (
                    getattr(exc, "status_code", None) in {403, 404}
                    or (
                        getattr(exc, "status_code", None) == 400
                        and "model" in str(exc).lower()
                    )
                    or "model not found" in str(exc).lower()
                    or "model unavailable" in str(exc).lower()
                )
                if unavailable and candidate_index + 1 < len(candidates):
                    candidate_index += 1
                    active_model = candidates[candidate_index]
                    self.history[0]["content"] = self.history[0]["content"].replace(
                        self.manager.capabilities_for_model(
                            candidates[candidate_index - 1]
                        ).prompt_block(),
                        self.manager.capabilities_for_model(
                            active_model
                        ).prompt_block(),
                        1,
                    )
                    failures = 0
                    continue
                if failures >= 5 or unavailable:
                    self._model_failure(exc)
                    return
                delay = min(0.25 * 2 ** (failures - 1), 4.0)
                deadline = time.monotonic() + delay
                while (
                    time.monotonic() < deadline
                    and generation == self._generation
                    and not self._stop.is_set()
                ):
                    time.sleep(min(0.05, deadline - time.monotonic()))
                continue

            self.manager.debug.log("model_request_completed", agent_id=self.agent_id,
                                   model=active_model,
                                   seconds=round(time.monotonic() - request_started, 3))
            with self._generation_lock:
                if generation != self._generation or self._stop.is_set():
                    return
                appeared = self._scan_call_ids(
                    [{"role": "assistant", "content": content}]
                )
                repeated = appeared & self._used_call_ids
                self._used_call_ids.update(appeared)
                message = {"role": "assistant", "content": content}
                if thinking:
                    message['thinking'] = thinking
                if reasoning_details:
                    message['reasoning_details'] = reasoning_details
                self.history.append(message)
                self._needs_generation = not self._events.empty()
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
                    raise ValueError(
                        f"call_id уже использован в context: {sorted(repeated)!r}"
                    )
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
                        target = action.data["capability_id"]
                        if target in global_changes:
                            raise ValueError(
                                "Global disable and enable for one capability must be called in separate responses; wait for call_result"
                            )
                        global_changes.add(target)
                    if action.action_id not in specs:
                        raise ValueError(
                            f"Действие недоступно этому агенту: {action.action_id}"
                        )
                    if self.manager.results.has_pending(self.agent_id, action.call_id):
                        raise ValueError(
                            f"call_id повторяется в ответе: {action.call_id!r}"
                        )
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                code = (
                    "invalid_json"
                    if isinstance(exc, json.JSONDecodeError)
                    else "invalid_structure"
                )
                self._append_structure_error(
                    code, str(exc), content, generation=generation
                )
                continue

            if len(actions) == 1 and actions[0].action_id == "no_action":
                self._set_state("waiting")
                return
            self._set_state("acting", action_count=len(actions))
            if generation != self._generation or self._stop.is_set():
                return
            self._execute_actions(actions, specs)
            if not self._stop.is_set():
                self._set_state("waiting")
            return

    def _append_structure_error(
        self, code: str, message: str, response: str, *, generation: int | None = None
    ) -> None:
        with self._generation_lock:
            if self._stop.is_set() or (
                generation is not None and generation != self._generation
            ):
                return
            self._append_structure_error_locked(code, message, response)

    def _append_structure_error_locked(
        self, code: str, message: str, response: str
    ) -> None:
        event = Event(
            event_id="structure_error",
            data={"code": code, "message": message, "response": response},
            source="core",
            target=self.agent_id,
        )
        self.history.append(event.model_message())
        self._needs_generation = True
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

    def _model_messages(self, model_id=None, *, capabilities=None, history=None):
        from .media import agent_messages
        from copy import deepcopy
        with self._generation_lock:
            snapshot = deepcopy(self.history) if history is None else deepcopy(history)
        return agent_messages(self, snapshot, model_id or self.effective_model(), capabilities)
