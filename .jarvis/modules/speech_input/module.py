from jarvis.core.protocol import object_schema
from jarvis.modules import Module, event, event_handler

from .cuda import prepare_cuda_env
from .handlers.microphone import SpeechInput


def create_module():
    controller = SpeechInput()
    speech = event(
        "speech_input.speech",
        "Текст завершённой реплики пользователя, распознанной с микрофона.",
        object_schema({"text": {"type": "string"}}),
    )
    error = event(
        "speech_input.error",
        "Ошибка захвата или распознавания речи.",
        object_schema({"message": {"type": "string"}}),
    )
    handler = event_handler(
        "speech_input.microphone",
        "Непрерывно слушает микрофон и публикует завершённые реплики.",
        (speech, error),
        controller.start,
        stop=controller.stop,
    )
    return Module(
        module_id="speech_input",
        description="Микрофон, VAD и локальное распознавание речи",
        handlers=(handler,),
        prepare=prepare_cuda_env,
    )
