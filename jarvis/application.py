"""Точка сборки ядра, файловых пресетов и подключаемых модулей."""

from __future__ import annotations

from openai import OpenAI

from .core.prompts import read_environment_prompt
from .core.registry import ActionRegistry, EventRegistry
from .core.runtime import AgentManager, EventBus, register_core_protocol
from .infrastructure.config import Config
from .infrastructure.context import MemoryStore
from .infrastructure.debug import Debugger
from .infrastructure.model_capabilities import discover_model_capabilities
from .capabilities.manager import CapabilityManager
from .presets import PresetStore


class JarvisApplication:
    def __init__(self, config: Config, *, memory: MemoryStore | None = None):
        if not config.llm_enabled:
            raise RuntimeError("LLM_MODEL не задан")
        self.config = config
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
        environment_prompt = read_environment_prompt(
            config.jarvis_dir / "environment.txt"
        )
        self.agents = AgentManager(
            model=config.model,
            client=self.client,
            actions=self.actions,
            events=self.events,
            bus=self.bus,
            capabilities=self.capabilities,
            presets=self.presets,
            environment_prompt=environment_prompt,
            config=config,
            services=self.services,
            debug=self.debug,
            model_capabilities=capabilities,
            memory=self.memory,
        )

    def start(self) -> "JarvisApplication":
        self.main_agent = self.agents.restore(name="main", preset="main")
        return self

    def stop(self) -> None:
        self.agents.begin_shutdown()
        self.capabilities.shutdown()
        self.client.close()
        self.agents.shutdown()
