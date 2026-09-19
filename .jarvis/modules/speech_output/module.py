from jarvis.core.protocol import object_schema
from jarvis.modules import Module, action, event, event_handler

from .actions.speak import speak
from .handlers.server import SpeechOutput


def create_module():
    controller = SpeechOutput()
    status = event(
        "speech_output.status",
        "Состояние запуска или ошибка подсистемы синтеза речи.",
        object_schema(
            {
                "ready": {"type": "boolean"},
                "message": {"type": "string"},
            }
        ),
    )
    result = event(
        "speech_output.result",
        "Адресный результат действия озвучки.",
        object_schema(
            {
                "action_id": {"type": "string"},
                "status": {"type": "string", "enum": ["success", "error"]},
                "text": {"type": "string"},
                "error": {"type": ["string", "null"]},
            }
        ),
    )
    handler = event_handler(
        "speech_output.server",
        "Управляет сервером Fish Audio и очередью воспроизведения.",
        (status,),
        controller.start,
        stop=controller.stop,
    )
    return Module(
        module_id="speech_output",
        description="Синтез и воспроизведение речи через Fish Audio",
        actions=(
            action(
                "speech_output.speak",
                "Произнести text. Допустимы инлайновые теги интонации.",
                object_schema({"text": {"type": "string"}}),
                speak,
            ),
        ),
        events=(result,),
        handlers=(handler,),
    )
