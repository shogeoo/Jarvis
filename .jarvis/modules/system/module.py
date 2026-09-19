from jarvis.core.protocol import object_schema
from jarvis.modules import Module, action, event

from .actions.shutdown import shutdown


def create_module():
    requested = event(
        "system.shutdown_requested",
        "Штатное завершение Jarvis принято системой.",
        object_schema(
            {
                "action_id": {"type": "string"},
                "reason": {"type": "string"},
            }
        ),
    )
    return Module(
        module_id="system",
        description="Управление жизненным циклом Jarvis",
        actions=(
            action(
                "system.shutdown",
                "Штатно завершить текущий цикл бодрствования Jarvis.",
                object_schema({"reason": {"type": "string"}}),
                shutdown,
            ),
        ),
        events=(requested,),
    )
