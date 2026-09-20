"""Модуль уведомлений: action показывает уведомление через notify-send."""

from jarvis.core.protocol import object_schema
from jarvis.modules import Module, action

from .actions.send import build_send


MODULE_ID = "notify"
MODULE_DESCRIPTION = (
    "Отправляет десктопные уведомления через notify-send в демон SwayNC. "
    "Action notify.send показывает текстовое уведомление от имени Jarvis; "
    "выполнение fire-and-forget, событие результата не предусмотрено."
)


def create_module():
    send = action(
        "notify.send",
        "Показать экранное уведомление через notify-send (демон SwayNC). "
        "Поле message содержит текст уведомления; заголовком служит имя Jarvis. "
        "Команда выполняется fire-and-forget: без ожидания, таймаутов и проверки "
        "кода возврата; событие результата не отправляется.",
        object_schema({"message": {"type": "string"}}),
        build_send,
    )
    return Module(
        module_id=MODULE_ID,
        description=MODULE_DESCRIPTION,
        actions=(send,),
    )
