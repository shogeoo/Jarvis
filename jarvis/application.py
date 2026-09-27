"""Точка сборки ядра, файловых пресетов и подключаемых модулей."""

from __future__ import annotations

from datetime import datetime
import shutil

from openai import OpenAI

from .core.protocol import Event
from .core.prompts import read_master_prompt
from .core.registry import ActionRegistry, EventRegistry
from .core.runtime import AgentManager, EventBus, register_core_protocol
from .infrastructure.config import Config
from .infrastructure.context import MemoryStore
from .infrastructure.debug import Debugger
from .infrastructure.model_capabilities import discover_model_capabilities
from .infrastructure.runtime_layout import ensure_runtime_layout
from .capabilities.manager import CapabilityManager
from .presets import PresetStore
from .speech import service as speech_service
from .speech.config import build_config


class JarvisApplication:
    def __init__(self, config: Config, *, memory: MemoryStore | None = None):
        if not config.llm_enabled:
            raise RuntimeError("LLM_MODEL не задан")
        self.config = config
        ensure_runtime_layout(config.jarvis_dir)
        self.debug = Debugger(enabled=True)
        self.memory = memory or MemoryStore(config.jarvis_dir / "memory")
        self.actions = ActionRegistry()
        self.events = EventRegistry()
        register_core_protocol(self.actions, self.events)
        self.bus = EventBus(self.events, debug=self.debug)
        self.services: dict = {}
        self.capabilities = CapabilityManager(
            self.bus,
            self.actions,
            self.events,
            root=config.jarvis_dir,
            config=config,
            debug=self.debug,
            services=self.services,
        )
        self.presets = PresetStore(config.jarvis_dir / "presets")
        self.client = OpenAI(
            base_url=config.base_url or None,
            api_key=config.api_key or None,
            timeout=60.0,
        )
        capabilities = discover_model_capabilities(
            config.model,
            config.base_url,
            config.api_key,
        )
        master_prompt = read_master_prompt()
        self.agents = AgentManager(
            model=config.model,
            client=self.client,
            actions=self.actions,
            events=self.events,
            bus=self.bus,
            capabilities=self.capabilities,
            presets=self.presets,
            master_prompt=master_prompt,
            config=config,
            services=self.services,
            debug=self.debug,
            model_capabilities=capabilities,
            memory=self.memory,
        )

    def start(self) -> "JarvisApplication":
        if shutil.which(str(build_config(self.config.jarvis_dir).tts_server_bin)):
            self.actions.unregister_owner("core:reply")
        speech_service.start(
            self.config.jarvis_dir,
            emit=self._emit_speech,
            debug=self.debug,
        )
        if not speech_service.can_speak:
            self.actions.unregister_owner("core:speech")
        self.main_agent = self.agents.restore(name="main", preset="main")
        if self.main_agent is not None:
            self.bus.publish(
                Event(
                    type="system_started",
                    data={"datetime": datetime.now().astimezone().isoformat(timespec="seconds")},
                    source="core",
                    target=self.main_agent.agent_id,
                )
            )
        return self

    def _emit_speech(self, data: dict) -> None:
        self.bus.publish(
            Event(
                type="speech_detected",
                data=data,
                source="core:speech",
                handler_id="core:speech",
            )
        )

    def stop(self) -> None:
        self.agents.begin_shutdown()
        speech_service.shutdown()
        self.capabilities.shutdown()
        self.client.close()
        self.agents.shutdown()
