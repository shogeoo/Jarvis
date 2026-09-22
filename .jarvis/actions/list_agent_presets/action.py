from jarvis.capabilities import action_definition
from jarvis.core.protocol import object_schema


STRING = {"type": "string"}
STRING_LIST = {"type": "array", "items": {"type": "string"}}


def run(data, context):
    return {"presets": context.agent_manager.presets_list()}


def create_action():
    return action_definition(
        "Получить сохранённые presets, их стартовые capabilities и признак protected.",
        object_schema({}),
        object_schema(
            {
                "presets": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": STRING,
                            "modules": STRING_LIST,
                            "actions": STRING_LIST,
                            "handlers": STRING_LIST,
                            "protected": {"type": "boolean"},
                        },
                        "required": [
                            "name",
                            "modules",
                            "actions",
                            "handlers",
                            "protected",
                        ],
                        "additionalProperties": False,
                    },
                }
            }
        ),
        run,
    )
