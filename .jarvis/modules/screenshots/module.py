"""Модуль снимков экрана: action выполняет hyprshot, handler публикует файл."""

from jarvis.core.protocol import object_schema
from jarvis.modules import Module, action, event, event_handler

from .actions.capture import build_capture
from .handlers.monitor import build_monitor


MODULE_ID = "screenshots"
MODULE_DESCRIPTION = (
    "Создаёт PNG активного монитора Hyprland через screenshots.capture. "
    "Action сохраняет файл с суффиксом _jarvis, а handler обнаруживает новый "
    "файл в Images/Screenshots и рассылает его всем агентам с включённым модулем."
)


def create_module():
    captured = event(
        "screenshots.captured",
        "Новый PNG-снимок обнаружен в Images/Screenshots. Событие рассылается "
        "всем агентам с включённым screenshots и содержит image-часть. В data "
        "обязательно присутствует только имя файла name.",
        object_schema({"name": {"type": "string"}}),
    )
    failed = event(
        "screenshots.error",
        "Handler не смог прочитать новый PNG-снимок. name содержит имя файла, "
        "reason — причину ошибки. Событие рассылается агентам с screenshots.",
        object_schema({"name": {"type": "string"}, "reason": {"type": "string"}}),
    )
    monitor = event_handler(
        "screenshots.monitor",
        "Мониторит Images/Screenshots и публикует каждый новый файл, имя которого "
        "соответствует YYYY-MM-DD-HHMMSS_jarvis.png. Старые файлы игнорируются.",
        (captured, failed),
        build_monitor,
    )
    capture = action(
        "screenshots.capture",
        "Выполнить hyprshot для активного монитора и сохранить PNG в "
        "~/Images/Screenshots с именем YYYY-MM-DD-HHMMSS_jarvis.png. Action "
        "сам выполняет команду; после появления файла handler рассылает "
        "screenshots.captured. Ошибка запуска или ненулевой код создаёт "
        "module_error для main.",
        object_schema({}),
        build_capture,
    )
    return Module(
        module_id=MODULE_ID,
        description=MODULE_DESCRIPTION,
        actions=(capture,),
        handlers=(monitor,),
    )
