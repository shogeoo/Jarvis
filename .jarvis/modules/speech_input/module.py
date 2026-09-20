from jarvis.core.protocol import object_schema
from jarvis.modules import Module, event, event_handler

from .cuda import prepare_cuda_env
from .handlers.microphone import SpeechInput


def create_module():
    controller = SpeechInput()
    speech = event(
        "speech_input.speech",
        "Новая завершённая реплика пользователя с микрофона. text содержит распознанную речь; событие рассылается всем экземплярам с speech_input.",
        object_schema({"text": {"type": "string"}}),
    )
    error = event(
        "speech_input.error",
        "Фоновая ошибка микрофона, VAD или распознавания. message содержит причину, которую следует сообщить пользователю или передать на исправление.",
        object_schema({"message": {"type": "string"}}),
    )
    handler = event_handler(
        "speech_input.microphone",
        "Непрерывно слушает микрофон, режет речь по VAD и публикует завершённые транскрипции и ошибки.",
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
