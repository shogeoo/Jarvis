"""Модуль снимков экрана: action выполняет hyprshot, handler публикует файл."""

from jarvis.core.protocol import object_schema
from jarvis.modules import Module, action, event, event_handler

from .actions.capture import build_capture
from .handlers.monitor import build_monitor


MODULE_ID = "screenshots"
MODULE_DESCRIPTION = (
    "Создаёт PNG активного монитора Hyprland через screenshots.capture. "
    "Action запускает hyprshot fire-and-forget, а handler обнаруживает новый "
    "файл в Images/Screenshots и рассылает событие screenshot всем агентам "
    "с включённым модулем."
)


def create_module():
    captured = event(
        "screenshot",
        "Новый PNG-снимок обнаружен в Images/Screenshots. Событие рассылается "
        "всем агентам с включённым screenshots. В data присутствует только "
        "text с именем файла. Сам снимок прикреплён отдельной image-частью, "
        "а не текстом base64.",
        object_schema({"text": {"type": "string"}}),
    )
    monitor = event_handler(
        "screenshots.monitor",
        "Мониторит Images/Screenshots и публикует каждый новый файл, имя которого "
        "соответствует YYYY-MM-DD-HHMMSS_jarvis.png. Старые файлы игнорируются.",
        (captured,),
        build_monitor,
    )
    capture = action(
        "screenshots.capture",
        "Запустить hyprshot для активного монитора и сохранить PNG в "
        "~/Images/Screenshots с именем YYYY-MM-DD-HHMMSS_jarvis.png. Команда "
        "запускается без ожидания, таймаутов и проверки кода возврата; после "
        "появления файла handler рассылает событие screenshot.",
        object_schema({}),
        build_capture,
    )
    return Module(
        module_id=MODULE_ID,
        description=MODULE_DESCRIPTION,
        actions=(capture,),
        handlers=(monitor,),
    )
