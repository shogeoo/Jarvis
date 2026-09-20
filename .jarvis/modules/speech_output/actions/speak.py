from jarvis.capabilities import action_definition
from jarvis.core.protocol import object_schema

from ..service import speak


def run(data, context):
    return speak(data["text"], context)


def create_action():
    return action_definition(
        "speech_output.speak",
        "Озвучить text и вернуть обязательный результат: spoken после "
        "воспроизведения, error при сбое, отключении или прерывании.",
        object_schema({"text": {"type": "string"}}),
        object_schema(
            {
                "spoken": {"type": "boolean"},
                "text": {"type": "string"},
                "error": {"type": ["string", "null"]},
            }
        ),
        run,
    )
