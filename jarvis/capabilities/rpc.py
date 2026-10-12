"""Загрузка, диспетчеризация и полная выгрузка capabilities.

Каждая единица (``actions/<id>``, ``handlers/<id>``, ``modules/<id>``) —
самодостаточный каталог со своим ``.venv``. Ядро не импортирует код единиц:
оно запускает unit-host из окружения единицы, получает JSON-каталог и
общается с ним по JSON Lines. Управляющие вызовы к живому состоянию ядра
приходят от единиц отдельными RPC-сообщениями.
"""

from __future__ import annotations

from typing import Any

from .hosts import UnitHost


class CapabilityRpc:
    # --- RPC от единиц ------------------------------------------------
    def _handle_rpc(self, host: UnitHost, message: dict[str, Any]) -> None:
        rpc_id = message.get("rpc_id")
        try:
            target = message.get("target")
            if target == "capabilities":
                obj: Any = self
            elif target == "agent_manager":
                obj = self._agent_api
                method = str(message.get("method", ""))
                if (
                    method
                    in {
                        "spawn",
                        "delete",
                        "interrupt",
                        "list_agents",
                        "get_agent",
                        "edit_agent",
                        "presets_list",
                    }
                    and message.get("caller_agent_id") != "main"
                ):
                    raise ValueError("Only main may manage agents and presets")
                if method in {"enable", "disable"}:
                    kwargs = message.get("kwargs", {})
                    target_id = (
                        kwargs.get("agent_id") or (message.get("args") or [None])[0]
                    )
                    if target_id != message.get("caller_agent_id"):
                        raise ValueError(
                            "Capability assignment targets the calling agent only"
                        )
            else:
                raise ValueError(f"Неизвестная цель RPC: {target!r}")
            func = obj
            for part in str(message.get("method", "")).split("."):
                func = getattr(func, part)
            value = func(
                *message.get("args", []),
                **message.get("kwargs", {}),
            )
            response = {
                "kind": "rpc_result",
                "rpc_id": rpc_id,
                "ok": True,
                "value": value,
                "error": None,
            }
        except Exception as exc:  # noqa: BLE001
            response = {
                "kind": "rpc_result",
                "rpc_id": rpc_id,
                "ok": False,
                "value": None,
                "error": str(exc),
            }
        try:
            self._send_host(host, response)
        except Exception:  # noqa: BLE001
            pass
