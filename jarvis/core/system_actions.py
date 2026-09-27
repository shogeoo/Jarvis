"""System actions for Jarvis-owned state; no capability host is needed."""

from dataclasses import replace
from jarvis.capabilities import action_definition
from jarvis.core.protocol import object_schema



def run_spawn_agent(data, context):
    manager = context.agent_manager
    created = manager.spawn(
        parent_id=context.agent_id,
        name=data["name"],
        preset=data["preset"],
    )
    return {
        "spawned": True,
        "agent_id": created["agent_id"],
        "name": created["name"],
        "preset": created["preset"],
    }


def define_spawn_agent():
    return action_definition(
        "Создать независимый живой экземпляр с указанным читаемым name "
        "из существующего preset. Задачу передавай после получения результата "
        "отдельным сообщением через действие send_message_to_agent.",
        object_schema(
            {
                "name": {"type": "string"},
                "preset": {"type": "string"},
            }
        ),
        object_schema(
            {
                "spawned": {"type": "boolean"},
                "agent_id": {"type": "string"},
                "name": {"type": "string"},
                "preset": {"type": "string"},
            }
        ),
        run_spawn_agent,
    )



def run_interrupt_agent(data, context):
    return context.agent_manager.interrupt(
        agent_id=data["agent_id"], requester_id=context.agent_id
    )


def define_interrupt_agent():
    return action_definition(
        "Прервать текущий цикл агента agent_id: обращение к модели и передача "
        "действий прекращаются, необработанные события остаются в очереди, "
        "контекст сохраняется, агент сразу готов к новым событиям. "
        "Защищённого агента нельзя прервать извне его самого. Возвращает "
        "agent_id и state: waiting.",
        object_schema({"agent_id": {"type": "string"}}),
        object_schema(
            {
                "agent_id": {"type": "string"},
                "state": {"type": "string"},
            }
        ),
        run_interrupt_agent,
    )



def run_delete_agent(data, context):
    return context.agent_manager.delete(agent_id=data["agent_id"])


def define_delete_agent():
    return action_definition(
        "Удалить живой экземпляр agent_id вместе со всеми его экземплярами-"
        "потомками: остановить потоки, отвязать события, снять незавершённые "
        "вызовы и стереть долговременную память каждого удалённого. Файлы "
        "пресетов не удаляются; защищённого агента удалить нельзя. "
        "Возвращает agent_id и deleted: true.",
        object_schema({"agent_id": {"type": "string"}}),
        object_schema(
            {
                "agent_id": {"type": "string"},
                "deleted": {"type": "boolean"},
            }
        ),
        run_delete_agent,
    )



STRING = {"type": "string"}
NULLABLE_STRING = {"type": ["string", "null"]}
STRING_LIST = {"type": "array", "items": {"type": "string"}}


def run_list_agents(data, context):
    return {"agents": context.agent_manager.list_agents()}


def define_list_agents():
    return action_definition(
        "Получить agent_id, name, preset, parent_id, state и активные "
        "capabilities всех живых экземпляров.",
        object_schema({}),
        object_schema(
            {
                "agents": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "agent_id": STRING,
                            "name": STRING,
                            "parent_id": NULLABLE_STRING,
                            "state": STRING,
                            "preset": STRING,
                            "modules": STRING_LIST,
                            "actions": STRING_LIST,
                            "handlers": STRING_LIST,
                        },
                        "required": [
                            "agent_id",
                            "name",
                            "parent_id",
                            "state",
                            "preset",
                            "modules",
                            "actions",
                            "handlers",
                        ],
                        "additionalProperties": False,
                    },
                }
            }
        ),
        run_list_agents,
    )



def run_send_message_to_agent(data, context):
    return context.agent_manager.deliver_message(
        sender_id=context.agent_id,
        target_id=data["agent_id"],
        text=data["text"],
        action_id=context.action_id,
        module_id=context.module_id,
    )


def define_send_message_to_agent():
    return action_definition(
        "Адресно передать живому экземпляру по agent_id прямой текст text. "
        "Пиши в text саму задачу без служебного префикса и без слов "
        "«сообщение от агента». Получатель увидит обычное событие "
        "в своей FIFO.",
        object_schema(
            {
                "agent_id": {"type": "string"},
                "text": {"type": "string"},
            }
        ),
        object_schema(
            {
                "delivered": {"type": "boolean"},
                "agent_id": {"type": "string"},
            }
        ),
        run_send_message_to_agent,
    )



STRING = {"type": "string"}
STRING_LIST = {"type": "array", "items": {"type": "string"}}


def run_list_agent_presets(data, context):
    return {"presets": context.agent_manager.presets_list()}


def define_list_agent_presets():
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
        run_list_agent_presets,
    )



def run_enable_capability(data, context):
    return context.agent_manager.enable(
        agent_id=context.agent_id,
        kind=data["kind"],
        capability_id=data["id"],
    )


def define_enable_capability():
    return action_definition(
        "Загрузить capability при необходимости и включить её текущему "
        "экземпляру: модуль целиком либо отдельное автономное действие или "
        "handler. Части модуля отдельно не переключаются: id вида "
        "«модуль.юнит» запрещён — включайте модуль целиком (kind=module, "
        "id=модуль). Если действие вызвал root-agent main, capability также "
        "навсегда добавляется в пресет main.",
        object_schema(
            {
                "kind": {
                    "type": "string",
                    "enum": ["module", "action", "handler"],
                },
                "id": {"type": "string"},
            }
        ),
        object_schema(
            {
                "enabled": {"type": "boolean"},
                "kind": {"type": "string"},
                "id": {"type": "string"},
                "persistent": {"type": "boolean"},
            }
        ),
        run_enable_capability,
    )



def run_disable_capability(data, context):
    kind, capability_id = data["kind"], data["id"]
    if kind == "module" and capability_id == context.module_id:
        raise ValueError("Исполняемая capability не может выключить сама себя")
    if kind == "action" and capability_id == context.action_id:
        raise ValueError("Исполняемое действие не может выключить само себя")
    return context.agent_manager.disable(
        agent_id=context.agent_id,
        kind=kind,
        capability_id=capability_id,
    )


def define_disable_capability():
    return action_definition(
        "Убрать capability только из RAM текущего экземпляра. Если "
        "пользователей больше нет, её runtime полностью выгружается. "
        "Части модуля отдельно не переключаются: id вида «модуль.юнит» "
        "запрещён — выключайте модуль целиком (kind=module, id=модуль). "
        "Исполняемая capability не может выключить сама себя.",
        object_schema(
            {
                "kind": {
                    "type": "string",
                    "enum": ["module", "action", "handler"],
                },
                "id": {"type": "string"},
            }
        ),
        object_schema(
            {
                "enabled": {"type": "boolean"},
                "kind": {"type": "string"},
                "id": {"type": "string"},
            }
        ),
        run_disable_capability,
    )



def run_list_active_capabilities(data, context):
    snapshot = context.agent_manager.agent_snapshot(context.agent_id)
    return {
        "modules": sorted(snapshot["modules"]),
        "actions": sorted(snapshot["actions"]),
        "handlers": sorted(snapshot["handlers"]),
    }


def define_list_active_capabilities():
    string_list = {"type": "array", "items": {"type": "string"}}
    return action_definition(
        "Показать точный набор capabilities текущего живого экземпляра; "
        "preset и другие экземпляры не учитываются.",
        object_schema({}),
        object_schema(
            {
                "modules": string_list,
                "actions": string_list,
                "handlers": string_list,
            }
        ),
        run_list_active_capabilities,
    )



def run_list_available_capabilities(data, context):
    known = context.agent_manager.known_snapshot(context.agent_id)
    return context.capabilities.list_available(known)


def define_list_available_capabilities():
    entry = object_schema({
        "id": {"type": "string"},
        "description": {"type": "string"},
    })
    return action_definition(
        "Перечислить все capabilities системы, ещё не назначенные этому агенту. "
        "Уже известные, в том числе отключённые, не включаются в ответ.",
        object_schema({}),
        object_schema({
            "modules": {"type": "array", "items": entry},
            "actions": {"type": "array", "items": entry},
            "handlers": {"type": "array", "items": entry},
        }),
        run_list_available_capabilities,
    )



def run_list_capabilities(data, context):
    return context.capabilities.list_existing()


def define_list_capabilities():
    item = object_schema(
        {
            "id": {"type": "string"},
            "description": {"type": "string"},
            "loaded": {"type": "boolean"},
            "globally_running": {"type": "boolean"},
            "globally_paused": {"type": "boolean"},
        }
    )
    return action_definition(
        "List every capability physically present in .jarvis and report its "
        "runtime and global state: loaded is true when the runtime is currently "
        "loaded, globally_running and globally_paused reflect the global "
        "pause state that overrides every agent assignment.",
        object_schema({}),
        object_schema(
            {
                "modules": {"type": "array", "items": item},
                "actions": {"type": "array", "items": item},
                "handlers": {"type": "array", "items": item},
            }
        ),
        run_list_capabilities,
    )



def run_capability_info(data, context):
    return {
        "kind": data["kind"],
        "id": data["id"],
        "info": context.capabilities.capability_info(kind=data["kind"], capability_id=data["id"]),
    }


def define_capability_info():
    open_object = {"type": "object", "x-jarvis-open-object": True, "additionalProperties": True}
    return action_definition(
        "Вернуть декларацию capability из каталога диска без запуска runtime. "
        "kind=module — описание модуля и перечни его actions/handlers; "
        "kind=action или handler — описание, args_schema и result_schema. "
        "id указывается одиночной единицей; составные id вида «модуль.юнит» "
        "отдельно не проверяются — модуль запрашивай целиком через kind=module.",
        object_schema(
            {"kind": {"type": "string", "enum": ["module", "action", "handler"]}, "id": {"type": "string"}}
        ),
        object_schema({"kind": {"type": "string"}, "id": {"type": "string"}, "info": open_object}),
        run_capability_info,
    )



def run_toggle_capability(data, context):
    result = context.agent_manager.toggle_capability(
        kind=data["kind"], capability_id=data["id"]
    )
    return {"kind": data["kind"], "id": data["id"], **result}


def define_toggle_capability():
    return action_definition(
        "Toggle a capability between globally running and paused. Pause unloads the runtime and stops active calls without changing local assignments or disabled settings. Resume reloads current source for agents where this capability is assigned and locally enabled.",
        object_schema(
            {
                "kind": {"type": "string", "enum": ["module", "action", "handler"]},
                "id": {"type": "string"},
            }
        ),
        object_schema(
            {
                "kind": {"type": "string"},
                "id": {"type": "string"},
                "state": {"type": "string", "enum": ["paused", "running"]},
                "affected_agent_ids": {"type": "array", "items": {"type": "string"}},
            }
        ),
        run_toggle_capability,
    )


SYSTEM_MAIN = frozenset(["spawn_agent","interrupt_agent","delete_agent","list_agents","list_agent_presets","enable_capability","disable_capability","list_active_capabilities","list_available_capabilities"] + ["create_preset", "edit_preset", "remove_preset", "create_automation", "edit_automation", "remove_automation", "list_automations"])
SYSTEM_DEVELOPER = frozenset(["list_capabilities", "capability_info", "toggle_capability", "read_file", "write_file", "edit_file", "execute_command"])
SYSTEM_ALL = frozenset(["send_message_to_agent"])
SYSTEM_MAIN = SYSTEM_MAIN | frozenset({"reply"})


def reply(data, context):
    context.agent_manager._manager.debug.reply(data["text"], agent_id=context.agent_id)
    return {"status": "successful"}


def state_action(data, context):
    if context.agent_id != "main":
        if context.action_id.endswith("automation") or context.action_id == "list_automations":
            raise ValueError("Only main may manage automations")
        return {"status": "not_" + {"create_preset": "created", "edit_preset": "edited", "remove_preset": "removed"}[context.action_id], "preset_id": data["preset_id"], "error": "Only main may manage presets"}
    manager = context.agent_manager._manager
    action_id = context.action_id
    if action_id == "list_automations":
        return {"automations": manager.automations.identified()}
    if action_id.endswith("automation"):
        store = manager.automations
        operation = action_id.split("_", 1)[0]
        identifier = data.get("automation_id")
        try:
            if operation == "create":
                identifier = store.create(data)
            elif operation == "edit":
                store.edit(identifier, data["automation"])
            else:
                store.remove_id(identifier)
        except Exception as exc:
            return {"status": {"create": "not_created", "edit": "not_edited", "remove": "not_removed"}[operation], "automation_id": identifier, "error": str(exc)}
        return {"status": {"create": "created", "edit": "edited", "remove": "removed"}[operation], "automation_id": identifier, "error": None}
    operation = action_id.split("_", 1)[0]
    identifier = data["preset_id"]
    try:
        if operation == "remove":
            manager.remove_preset(identifier)
        else:
            capabilities = {key: data[key] for key in ("actions", "handlers", "modules")}
            for key, values in capabilities.items():
                for value in values:
                    spec = manager.actions.get(value) if key == "actions" else None
                    if spec is not None and spec.owner.startswith("core"):
                        continue
                    context.capabilities.validate(kind={"actions": "action", "handlers": "handler", "modules": "module"}[key], capability_id=value)
            if operation == "create":
                manager.presets.create(identifier, data["person_prompt"], capabilities)
            else:
                if manager.presets.load(identifier).protected and context.metadata["preset"] != identifier:
                    raise ValueError("Protected preset may only be edited by its own agent")
                manager.presets.edit(identifier, data["person_prompt"], capabilities)
    except Exception as exc:
        return {"status": {"create": "not_created", "edit": "not_edited", "remove": "not_removed"}[operation], "preset_id": identifier, "error": str(exc)}
    return {"status": {"create": "created", "edit": "edited", "remove": "removed"}[operation], "preset_id": identifier, "error": None}


def register_state_actions(registry):
    string = {"type": "string"}
    open_object = {"type": "object", "additionalProperties": True, "x-jarvis-open-object": True}
    for noun in ("preset", "automation"):
        for operation in ("create", "edit", "remove"):
            action_id = operation + "_" + noun
            if noun == "preset":
                properties = {"preset_id": string}
                if operation != "remove":
                    properties.update({"person_prompt": string, **{key: {"type": "array", "items": string} for key in ("actions", "handlers", "modules")}})
                args = object_schema(properties)
            else:
                args = open_object if operation == "create" else object_schema({"automation_id": string, **({"automation": open_object} if operation == "edit" else {})})
            success = {"create": "created", "edit": "edited", "remove": "removed"}[operation]
            result = object_schema({"status": {"type": "string", "enum": [success, "not_" + success]}, noun + "_id": {"type": ["string", "null"]} if noun == "automation" else string, "error": {"type": ["string", "null"]}})
            registry.register(replace(action_definition(action_id + ": update Jarvis state. Editing a preset affects only future instances. Removing a preset recursively deletes its instances and descendants.", args, result, state_action), id=action_id, owner="core:primary"))
    registry.register(replace(action_definition("List saved automations and their automation_id values.", object_schema({}), object_schema({"automations": {"type": "array", "items": open_object}}), state_action), id="list_automations", owner="core:primary"))

def register_system_actions(registry):
    from .developer_actions import register_developer_actions
    register_developer_actions(registry)
    registry.register(replace(action_definition("Reply to the user with plain text in the terminal. Available only when the configured s2 binary is absent.", object_schema({"text": {"type": "string"}}), object_schema({"status": {"type": "string", "enum": ["successful"]}}), reply), id="reply", owner="core:reply"))
    register_state_actions(registry)
    for action_id in ["spawn_agent","interrupt_agent","delete_agent","list_agents","send_message_to_agent","list_agent_presets","enable_capability","disable_capability","list_active_capabilities","list_available_capabilities","list_capabilities","capability_info","toggle_capability"]:
        definition = globals()["define_" + action_id]()
        owner = "core" if action_id in SYSTEM_ALL else "core:developer" if action_id in SYSTEM_DEVELOPER else "core:primary"
        registry.register(replace(definition, id=action_id, owner=owner))
