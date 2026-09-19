from jarvis.core.protocol import object_schema
from jarvis.modules import ActionQueue, Module, action, event, event_handler

from .actions.speak import submit
from .handlers.server import SpeechOutput


def create_module():
    tasks = ActionQueue()
    controller = SpeechOutput(tasks)
    status = event(
        "speech_output.status",
        "Широковещательное состояние фонового Fish Audio worker: ready=true означает готовность, ready=false содержит причину запуска.",
        object_schema({"ready": {"type": "boolean"}, "message": {"type": "string"}}),
    )
    result = event(
        "speech_output.result",
        "Адресный результат ранее поставленной реплики: success после воспроизведения, error при сбое или прерывании.",
        object_schema(
            {
                "status": {"type": "string", "enum": ["success", "error"]},
                "text": {"type": "string"},
                "error": {"type": ["string", "null"]},
            }
        ),
    )
    handler = event_handler(
        "speech_output.server",
        "Управляет Fish Audio, выполняет очередь речи и возвращает результаты.",
        (status, result),
        controller.start,
        stop=controller.stop,
    )
    return Module(
        module_id="speech_output",
        description="Синтез и воспроизведение речи через Fish Audio",
        actions=(
            action(
                "speech_output.speak",
                "Поставить text в очередь озвучки. Результат позже придёт событием speech_output.result.",
                object_schema({"text": {"type": "string"}}),
                submit(tasks),
            ),
        ),
        handlers=(handler,),
    )
