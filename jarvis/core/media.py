"""Core-owned interpretation and native/ textual model input adaptation."""
from __future__ import annotations

import json
import re
import subprocess
from copy import deepcopy

from ..infrastructure.context import KINDS, context_files, hydrate_part, read_json
from ..memory.documents import Documents
from ..memory.serialization import api_messages, interpretation, model_interpretation, validate_provider_input

PROFILES = ("text", "text_image", "text_image_video", "text_image_audio_video")

class MediaInterpreter:
    def __init__(self, store, config, client, environment, *, stopped=None):
        self.store, self.config, self.client, self.environment = (
            store,
            config,
            client,
            environment,
        )
        self.stopped = stopped or (lambda: False)

    def check(self):
        if self.stopped():
            raise InterruptedError(
                "Session archival was interrupted; live state is preserved"
            )

    def model(self, modalities):
        if "audio" in modalities:
            profile = PROFILES[3]
        elif "video" in modalities:
            profile = PROFILES[2]
        elif "image" in modalities:
            profile = PROFILES[1]
        else:
            profile = PROFILES[0]
        selected = getattr(self.config, "memory_model_" + profile, None)
        if not selected or "/" not in selected:
            raise ValueError(
                f"MEMORY_MODEL_{profile.upper()} must contain an API provider/model ID in .env"
            )
        return selected

    def modalities(self, parts, directory):
        result = {"text"}
        for part in parts:
            modality = KINDS.get(part.get("type"))
            if modality in {"image", "audio", "video"}:
                result.add(modality)
            if modality == "video":
                path = directory / "files" / part["filename"]
                # A silent video must not select the more expensive audio profile.
                probe = subprocess.run(
                    [
                        "ffprobe",
                        "-v",
                        "error",
                        "-show_entries",
                        "stream=codec_type",
                        "-of",
                        "json",
                        str(path),
                    ],
                    capture_output=True,
                    text=True,
                    check=True,
                )
                if any(
                    s.get("codec_type") == "audio"
                    for s in json.loads(probe.stdout).get("streams", [])
                ):
                    result.add("audio")
        return result

    def request(self, messages, modalities):
        self.check()
        validate_provider_input(messages)
        subscription_model = getattr(self.config, "memory_subscription_model", None)
        if (
            getattr(self.config, "subscription_enabled", False)
            and subscription_model
            and callable(getattr(self.client, "create_memory_completion", None))
        ):
            response = self.client.create_memory_completion(
                model=subscription_model,
                api_model=lambda: self.model(modalities),
                messages=messages,
                modalities=modalities,
                cancelled=self.stopped,
            )
        else:
            # API profile settings never become subscription model identifiers.
            api_client = getattr(self.client, "api_client", self.client)
            response = api_client.chat.completions.create(
                model=self.model(modalities), messages=messages, stream=False
            )
        text = response.choices[0].message.content
        if not isinstance(text, str) or not text.strip():
            raise ValueError("Memory model returned an empty Markdown response")
        self.check()
        return text

    def interpret(self):
        session = self.store.state()["shutdown"]["session_id"]
        for record in self.store.records(include_deleted=True):
            self.interpret_instance(record["agent_id"], session)

    def interpret_instance(self, agent, session, required=None):
        directory = self.store.instances / agent
        documents = Documents(self.store, agent, session)
        # Embedded media become context metadata; checkpoint each interpretation.
        while True:
            pending = [
                f for f in self.store.files(agent)
                if (not f.get("text_representation") or (f["type"] == "file" and not f.get("document_markdown")))
                and (required is None or f["type"] in required)
            ]
            if not pending:
                break
            for item in pending:
                self.check()
                if item["type"] == "file":
                    item.pop("embedded_files", None)
                    item["document_markdown"] = documents.convert(item)
                    item["text_representation"] = item["document_markdown"]
                else:
                    prompt = (
                        "Transfer all meaningful information from this attachment into Markdown with maximum "
                        "fidelity. Preserve its language, text, structure, spatial/temporal relations, speakers, "
                        "and relevant details. Do not invent unseen content. Return only Markdown.\n\n"
                        "<ENVIRONMENT>\n" + self.environment + "\n</ENVIRONMENT>"
                    )
                    item["text_representation"] = self.request(
                        [
                            {"role": "system", "content": prompt},
                            {
                                "role": "user",
                                "content": [
                                    {
                                        "type": "text",
                                        "text": "Interpret the original attachment.",
                                    },
                                    hydrate_part(item, directory),
                                ],
                            },
                        ],
                        self.modalities([item], directory),
                    )
                self.store.update_file(agent, item)
        items = {f["file_id"]: f for f in self.store.files(agent)}
        # Insert interpretations into the archival Markdown; retain original
        # links separately for interleaving native media in the summary request.
        for item in items.values():
            if item["type"] == "file" and item.get("document_markdown"):
                item["text_representation"] = self.expand_interpretations(
                    item["document_markdown"], items
                )
                self.store.update_file(agent, item)
    @staticmethod
    def expand_interpretations(markdown, items):
        def insert(match):
            item = items.get(match.group(1))
            return (
                match.group(0) + "\n\n" + interpretation(item) + "\n\n"
                if item and item["type"] != "file"
                else match.group(0)
            )

        return re.sub(
            r"\[[^\]]*\]\(memory://[^/]+/[^/]+/(file_[0-9]+)\?[^)]*\)", insert, markdown
        )

    def document_parts(self, part, directory, *, media_part=None):
        markdown = part.get("document_markdown")
        if not markdown:
            raise ValueError(f"Document has no programmatic Markdown: {part['filename']}")
        manifest = {
            f["file_id"]: f
            for f in context_files(read_json(directory / "context.json", []))
        }
        result, offset = [], 0
        for match in re.finditer(
            r"!?\[[^\]]*\]\(memory://[^/]+/[^/]+/(file_[0-9]+)\?[^)]*\)", markdown
        ):
            nested = manifest.get(match.group(1))
            if (
                nested is not None and nested["file_id"] == part["file_id"]
            ):
                continue
            if nested is None:
                raise ValueError(f"Missing embedded document element: {match.group(1)}")
            # The document itself contains technical metadata; no extra media captions.
            result.append({"type": "text", "text": markdown[offset : match.end()]})
            if nested["type"] == "file":
                result.extend(self.document_parts(nested, directory, media_part=media_part))
            else:
                result.append(media_part(nested) if media_part else hydrate_part(nested, directory))
            offset = match.end()
        result.append({"type": "text", "text": markdown[offset:]})
        return result



class LiveMediaStore:
    """Commit interpretation checkpoints without overwriting newly accepted events."""
    def __init__(self, agent):
        self.agent = agent
        self.store = agent.manager.memory
        self.generation = agent._generation

    def __getattr__(self, name):
        return getattr(self.store, name)

    def check(self):
        if self.agent._stop.is_set() or self.agent.manager.frozen.is_set() or self.agent._generation != self.generation:
            raise InterruptedError("Input adaptation interrupted; originals are preserved")

    def register_file(self, *args, **kwargs):
        with self.agent._generation_lock:
            self.check()
            return self.store.register_file(*args, **kwargs)

    def update_file(self, agent_id, item):
        with self.agent._generation_lock:
            self.check()
            for part in context_files(self.agent.history):
                if part["file_id"] == item["file_id"]:
                    part.clear()
                    part.update(deepcopy(item))
            self.agent._persist_context()


def agent_messages(agent, history, model_id, capabilities=None):
    """Prepare a private request view; originals remain in the durable context."""
    manager = agent.manager
    caps = (manager.capabilities_for_model(model_id)
            if "/" in model_id and model_id != manager.model
            else capabilities or manager.capabilities_for_model(model_id))
    from ..infrastructure.model_capabilities import ModelCapabilities
    caps = ModelCapabilities(model_id, caps.input_modalities)
    store = manager.memory
    directory = (store.instances if store is not None else manager.presets.root.parent / "memory/instances") / agent.agent_id
    required = {"file"} | {kind for kind, modality in KINDS.items() if not caps.supports(modality)}
    interpreter = None
    if any(p["type"] in required for p in context_files(history)):
        if store is None:
            raise RuntimeError("Attachment interpretation requires persistent core memory")
        interpreter = MediaInterpreter(
            LiveMediaStore(agent), manager.config,
            manager.services.get("memory_client", manager.services.get("memory_api_client", manager.client)),
            manager.environment, stopped=lambda: agent._stop.is_set() or manager.frozen.is_set(),
        )
        with agent._media_lock:
            interpreter.interpret_instance(agent.agent_id, store.state()["session_id"], required)
        latest = {part["file_id"]: part for part in store.files(agent.agent_id)}
        for part in context_files(history):
            updated = latest.get(part["file_id"])
            if updated is not None:
                part.clear()
                part.update(deepcopy(updated))

    return render_request(history, caps, directory, agent.agent_id,
                          store.state()["session_id"] if store else None, interpreter)


def render_request(history, caps, directory, agent_id, session_id, interpreter=None):
    """Render live or archived originals for one actual receiving model."""
    interpreter = interpreter or MediaInterpreter(None, None, None, "")
    def media_part(part):
        if caps.supports(KINDS[part["type"]]):
            return hydrate_part(part, directory)
        text = part.get("text_representation")
        if not text:
            raise ValueError(f"Missing interpretation for {part.get('file_id', part['type'])}")
        return {"type": "text", "text": model_interpretation(part)}

    result = []
    for message in history[1:]:
        copy = deepcopy(message)
        if isinstance(copy.get("content"), list):
            parts = []
            for part in copy["content"]:
                if part.get("type") not in KINDS:
                    parts.append(part)
                elif part.get("type") == "file":
                    parts.extend(interpreter.document_parts(part, directory, media_part=media_part))
                else:
                    parts.append(media_part(part))
            copy["content"] = parts
        result.append(copy)
    # Strip archival/internal fields while preserving native input and roles.
    system = deepcopy(history[0])
    system["content"] = re.sub(r"<MODEL_INFO>.*?</MODEL_INFO>",
        lambda _: "<MODEL_INFO>\n" + caps.prompt_block() + "\n</MODEL_INFO>",
        system["content"], count=1, flags=re.DOTALL)
    prepared = [system, *api_messages(result, directory)]
    validate_provider_input(prepared)
    return prepared
