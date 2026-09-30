"""Точка сборки ядра, файловых пресетов и подключаемых модулей."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import shutil

from openai import OpenAI

from .core.protocol import Event
from .core.prompts import read_environment
from .core.registry import ActionRegistry, EventRegistry
from .core.runtime import AgentManager, EventBus, register_core_protocol
from .infrastructure.config import Config
from .infrastructure.context import MemoryStore
from .infrastructure.debug import Debugger
from .infrastructure.console import configure_logging
from .infrastructure.model_capabilities import ModelCapabilities, SUPPORTED_INPUT_MODALITIES, discover_model_capabilities
from .infrastructure.runtime_layout import ensure_runtime_layout
from .capabilities.manager import CapabilityManager
from .presets import PresetStore
from .speech import service as speech_service
from .speech.config import build_config


class JarvisApplication:
    def __init__(
        self, config: Config, *, memory: MemoryStore | None = None, stream=None
    ):
        if not config.llm_enabled:
            raise RuntimeError("LLM_API_MODEL не задан или режим подписки выключен")
        if config.subscription_enabled and (
            not config.model or not config.api_key or not config.subscription_model
        ):
            raise RuntimeError(
                "Subscription mode requires LLM_SUBSCRIPTION_MODEL, LLM_API_MODEL and LLM_API_KEY for fallback"
            )
        self.config = config
        configure_logging(config.jarvis_dir)
        self.debug = Debugger(enabled=True, stream=stream, buffered=True)
        self.debug.initializing()
        ensure_runtime_layout(config.jarvis_dir)
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
        api_client = OpenAI(
            base_url=config.base_url or None,
            api_key=config.api_key or None,
            timeout=60.0,
        )
        if config.subscription_enabled:
            from .infrastructure.subscription import (
                SubscriptionClient,
                SUBSCRIPTION_MODALITIES,
            )
            from .infrastructure.subscription_auth import SubscriptionAuth

            api_capabilities = ModelCapabilities(config.model or "unknown", SUPPORTED_INPUT_MODALITIES)
            try:
                self.client = SubscriptionClient(
                    api_client=api_client,
                    api_model=config.model,
                    subscription_model=config.subscription_model,
                    api_capabilities=api_capabilities,
                    auth=SubscriptionAuth(config.jarvis_dir),
                    api_capabilities_loader=lambda: discover_model_capabilities(
                        config.model, config.base_url, config.api_key
                    ),
                    on_fallback=lambda value: setattr(self.agents, "model_capabilities", value),
                )
                self.client.ensure_chatgpt_login(self.debug.initialization_notice)
            except Exception:
                if hasattr(self, "client"):
                    self.client.close()
                else:
                    api_client.close()
                raise
            capabilities = type(SUBSCRIPTION_MODALITIES)(
                config.subscription_model, SUBSCRIPTION_MODALITIES.input_modalities
            )
        else:
            self.client = api_client
            capabilities = ModelCapabilities(config.model or "unknown", SUPPORTED_INPUT_MODALITIES)
        environment = read_environment()
        self.agents = AgentManager(
            model=config.model,
            client=self.client,
            actions=self.actions,
            events=self.events,
            bus=self.bus,
            capabilities=self.capabilities,
            presets=self.presets,
            environment=environment,
            config=config,
            services=self.services,
            debug=self.debug,
            model_capabilities=capabilities,
            memory=self.memory,
        )

    def start(self) -> "JarvisApplication":
        has_s2 = bool(shutil.which(str(build_config(self.config.jarvis_dir).tts_server_bin)))
        if has_s2:
            self.actions.unregister_owner("core:reply")
        else:
            self.actions.unregister_owner("core:speech")
        if self.config.subscription_enabled:
            speech_service.begin_background(
                self.config.jarvis_dir,
                emit=self._emit_speech,
                debug=self.debug,
                on_ready=lambda: self.actions.unregister_owner("core:speech")
                if not speech_service.can_speak else None,
            )
        else:
            with ThreadPoolExecutor(max_workers=1, thread_name_prefix="jarvis-model-info") as pool:
                model_info = pool.submit(
                    discover_model_capabilities,
                    self.config.model, self.config.base_url, self.config.api_key,
                )
                speech_service.start(
                    self.config.jarvis_dir,
                    emit=self._emit_speech,
                    debug=self.debug,
                )
                self.agents.model_capabilities = model_info.result()
            if not speech_service.can_speak:
                self.actions.unregister_owner("core:speech")
        self.main_agent = self.agents.restore(name="main", preset="main")
        if self.main_agent is not None:
            self.debug.initialized()
            self.bus.publish(
                Event(
                    event_id="system_started",
                    data={
                        "datetime": datetime.now()
                        .astimezone()
                        .isoformat(timespec="seconds")
                    },
                    source="core",
                    target=self.main_agent.agent_id,
                )
            )
        return self

    def _emit_speech(self, data: dict) -> None:
        self.bus.publish(
            Event(
                event_id="speech_detected",
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
