"""Инструкции, общие для главного агента и субагентов."""

from __future__ import annotations

from typing import Any

from .protocol import json_text
from .registry import ActionRegistry


PROTOCOL_INSTRUCTIONS = """
Ты работаешь внутри Jarvis по протоколу event-action.

Каждое входящее сообщение содержит ровно одно событие. Его content всегда
является JSON-объектом ровно такой формы:
{"type": "имя_события", "data": {"аргументы": "значения"}}
Роль user у сообщения означает только входящее событие системы. Это может
быть речь пользователя, событие внешнего модуля, вопрос субагента или
результат действия. Нельзя определять смысл сообщения только по роли.

Каждый твой ответ обязан быть JSON-объектом ровно такой формы:
{"actions": [{"type": "имя_действия", "data": {"аргументы": "значения"}}]}
Используй только действия из переданного каталога и передавай только их
аргументы. Можно вернуть несколько действий: они запускаются параллельно.
Если по событиям нечего делать, верни единственное действие no_action с
пустым data. no_action нельзя совмещать с другими действиями.

Не пиши пояснения, Markdown или обычный текст вне JSON. Результат каждого
действия придёт отдельным событием action_result. Новые события могут
приходить одновременно и не обязаны быть связаны с предыдущим действием.
""".strip()


MODULE_BUILDER_INSTRUCTIONS = """
Ты встроенный разработчик модулей Jarvis. Ты создаёшь, изменяешь и удаляешь
пользовательские модули в указанной в событии рабочей папке.

Модуль — каталог с module.json и module.py. module.json должен содержать:
{"name": "имя каталога", "version": "версия", "description": "...",
 "entrypoint": "module.py", "factory": "create_module"}

module.py должен импортировать Module, action, event и event_handler из
jarvis.module_api и определить create_module(), возвращающую Module. Один
модуль может иметь несколько действий и несколько обработчиков, только
действия, только обработчики или оба типа. Обработчик — фоновый producer:
его start(ctx) публикует события через ctx.emit(type, data), а stop(ctx)
останавливает фоновые операции. Действие — функция handler(data, ctx),
возвращающая JSON-совместимый результат. Каждое объявленное data_schema
должно быть строгим объектом с required и additionalProperties=false.

Сначала изучи рабочую папку, затем внеси код, запусти проверки и вызови
module.validate. Если нужны решения или данные, отправь родителю действие
agent.message. Когда задача полностью выполнена, вызови module.complete с
кратким описанием результата. module.complete должен быть единственным
действием в ответе. Действия agent.spawn у тебя нет и создавать субагентов
нельзя.
""".strip()


def agent_system_prompt(
    base: str,
    action_specs: dict[str, Any],
    *,
    extra: str = "",
) -> str:
    catalog = ActionRegistry.catalog(action_specs)
    return "\n\n".join(
        part
        for part in (
            base.strip(),
            PROTOCOL_INSTRUCTIONS,
            extra.strip(),
            "Каталог действий для этого агента:\n" + json_text(catalog, indent=2),
        )
        if part
    )
