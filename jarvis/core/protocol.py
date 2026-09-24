"""Общий протокол событий и действий Jarvis.

Внутри системы событие является единицей доставки агенту. В контексте
OpenAI одно событие всегда занимает одно сообщение с ролью ``user`` и
содержит JSON следующего вида::

    {"type": "example.notice", "data": {"text": "..."}}

Ответ агента имеет единственную форму::

    {"actions": [{"action_id": "example.handle", "call_id": "act-1", "data": {"text": "..."}}]}

Технические идентификаторы, источник и адресат живут во внутреннем
конверте ``Event``. Они не заставляют модель генерировать маршрутизацию и
не меняют внешний контракт ``type`` + ``data``.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping

try:
    from jsonschema import Draft202012Validator
    from jsonschema.exceptions import SchemaError, ValidationError
except ImportError:  # pragma: no cover - dependency is declared in pyproject
    Draft202012Validator = None  # type: ignore[assignment,misc]
    SchemaError = ValueError  # type: ignore[assignment,misc]
    ValidationError = ValueError  # type: ignore[assignment,misc]


JSON = Any
JSONSchema = dict[str, Any]
INPUT_MODALITIES = ("text", "image", "audio", "video", "file")


def make_id(prefix: str) -> str:
    """Создать короткий читаемый идентификатор внутреннего объекта."""

    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def json_text(value: Any, *, indent: int | None = None) -> str:
    """Сериализовать протокольный объект без потери Unicode и полей."""

    return json.dumps(
        value,
        ensure_ascii=False,
        indent=indent,
        separators=None if indent is not None else (",", ":"),
    )


def object_schema(
    properties: Mapping[str, JSONSchema],
    *,
    required: list[str] | None = None,
    additional_properties: bool = False,
) -> JSONSchema:
    """Построить объектную схему в формате, удобном для strict outputs."""

    names = list(properties)
    return {
        "type": "object",
        "properties": dict(properties),
        "required": names if required is None else required,
        "additionalProperties": additional_properties,
    }


def empty_object_schema() -> JSONSchema:
    return object_schema({})


def actions_response_schema(action_schemas: Mapping[str, JSONSchema]) -> JSONSchema:
    """Собрать строгую схему ответа агента из доступных действий.

    ``anyOf`` связывает значение ``type`` с соответствующей схемой ``data``.
    Это позволяет одновременно разрешить несколько разных действий и не
    превращать аргументы в бесконтрольный JSON-объект.
    """

    variants = []
    no_action_variant = None
    for action_id, data_schema in sorted(action_schemas.items()):
        variant = object_schema(
            {
                "action_id": {"type": "string", "enum": [action_id]},
                "call_id": {"type": "string"},
                "data": data_schema,
            }
        )
        if action_id == "no_action":
            no_action_variant = variant
        else:
            variants.append(variant)
    if not variants and no_action_variant is None:
        raise ValueError("Для агента не зарегистрировано ни одного действия")
    action_array: dict[str, Any]
    if variants:
        action_array = {
            "type": "array",
            "minItems": 1,
            "items": {"anyOf": variants},
        }
    else:
        action_array = {
            "type": "array",
            "minItems": 1,
            "maxItems": 1,
            "items": no_action_variant,
        }
    if no_action_variant is not None:
        action_array = {
            "anyOf": [
                action_array,
                {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 1,
                    "items": no_action_variant,
                },
            ]
        }
    return object_schema(
        {
            "actions": action_array,
        }
    )


def response_format(action_schemas: Mapping[str, JSONSchema]) -> dict[str, Any]:
    """Параметр ``response_format`` для Chat Completions."""

    return {
        "type": "json_schema",
        "json_schema": {
            "name": "agent_actions",
            "strict": True,
            "schema": actions_response_schema(action_schemas),
        },
    }


def validate_json(value: Any, schema: JSONSchema, *, where: str = "value") -> None:
    """Проверить данные по JSON Schema и дать понятную ошибку."""

    if Draft202012Validator is None:  # pragma: no cover
        try:
            _fallback_validate(value, schema, where)
        except ValueError:
            raise
        return
    try:
        validator = Draft202012Validator(schema)
        validator.validate(value)
    except SchemaError as exc:
        raise ValueError(f"Некорректная схема {where}: {exc.message}") from exc
    except ValidationError as exc:
        path = "".join(f"[{part!r}]" for part in exc.absolute_path)
        suffix = f" в {where}{path}" if path else f" в {where}"
        raise ValueError(f"Данные не соответствуют схеме{suffix}: {exc.message}") from exc


def validate_schema(schema: JSONSchema, *, where: str = "schema") -> None:
    """Проверить саму схему, не требуя от неё подходящего экземпляра данных."""

    if Draft202012Validator is None:  # pragma: no cover
        if not isinstance(schema, dict) or not (
            schema.get("type")
            or any(
                isinstance(schema.get(keyword), list) and bool(schema[keyword])
                for keyword in ("anyOf", "oneOf", "allOf")
            )
        ):
            raise ValueError(f"Некорректная {where}: отсутствует type")
        return
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        raise ValueError(f"Некорректная {where}: {exc.message}") from exc


def validate_strict_schema(schema: JSONSchema, *, where: str = "schema") -> None:
    """Проверить схему на требования Structured Outputs.

    Для объектов все поля обязательны, лишние поля запрещены, а вложенные
    объекты и варианты ``anyOf`` проверяются рекурсивно.
    """

    validate_schema(schema, where=where)
    if not isinstance(schema, dict):
        raise ValueError(f"Некорректная {where}: схема должна быть объектом")
    if schema.get("x-jarvis-open-object") is True:
        return
    schema_type = schema.get("type")
    if schema_type == "object":
        properties = schema.get("properties")
        required = schema.get("required")
        if not isinstance(properties, dict):
            raise ValueError(f"В {where} отсутствует properties")
        if schema.get("additionalProperties") is not False:
            raise ValueError(f"В {where} additionalProperties должен быть false")
        if not isinstance(required, list) or set(required) != set(properties):
            raise ValueError(f"В {where} required должен содержать все properties")
        for name, child in properties.items():
            validate_strict_schema(child, where=f"{where}.{name}")
    elif schema_type == "array":
        if "items" not in schema:
            raise ValueError(f"В {where} отсутствует items")
        validate_strict_schema(schema["items"], where=f"{where}[]")
    for keyword in ("anyOf", "oneOf", "allOf"):
        for index, child in enumerate(schema.get(keyword, [])):
            validate_strict_schema(child, where=f"{where}.{keyword}[{index}]")


def _fallback_validate(value: Any, schema: JSONSchema, where: str) -> None:
    """Небольшой валидатор для схем, используемых ядром.

    Полная реализация берётся из jsonschema, когда пакет установлен. Резервный
    путь покрывает object/array/string/integer/number/boolean/null, enum,
    const, required, additionalProperties, anyOf и minItems.
    """

    if not isinstance(schema, dict):
        raise ValueError(f"Некорректная схема в {where}")
    if "anyOf" in schema:
        errors = []
        for variant in schema["anyOf"]:
            try:
                _fallback_validate(value, variant, where)
                return
            except ValueError as exc:
                errors.append(str(exc))
        raise ValueError(f"Данные не соответствуют anyOf в {where}: {'; '.join(errors)}")
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError(f"Недопустимое значение в {where}: {value!r}")
    if "const" in schema and value != schema["const"]:
        raise ValueError(f"Значение в {where} должно быть {schema['const']!r}")
    expected = schema.get("type")
    expected_types = expected if isinstance(expected, list) else [expected]
    if expected_types and not any(_is_type(value, item) for item in expected_types):
        raise ValueError(f"Ожидался тип {expected_types}, получено {type(value).__name__} в {where}")
    if isinstance(value, dict):
        for name in schema.get("required", []):
            if name not in value:
                raise ValueError(f"Отсутствует обязательное поле {name!r} в {where}")
        properties = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            unknown = sorted(set(value) - set(properties))
            if unknown:
                raise ValueError(f"Неизвестные поля {unknown} в {where}")
        for name, child in properties.items():
            if name in value:
                _fallback_validate(value[name], child, f"{where}.{name}")
    if isinstance(value, list):
        if len(value) < schema.get("minItems", 0):
            raise ValueError(f"Слишком мало элементов в {where}")
        if "items" in schema:
            for index, item in enumerate(value):
                _fallback_validate(item, schema["items"], f"{where}[{index}]")


def _is_type(value: Any, expected: str | None) -> bool:
    return {
        "object": isinstance(value, dict),
        "array": isinstance(value, list),
        "string": isinstance(value, str),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "number": isinstance(value, (int, float)) and not isinstance(value, bool),
        "boolean": isinstance(value, bool),
        "null": value is None,
    }.get(expected or "", False)


@dataclass(frozen=True, slots=True)
class InputPart:
    """Мультимодальная часть, уже подготовленная модулем.

    Ядро не разбирает файлы: ``data`` должен быть base64 без data-url
    префикса, а модуль обязан сам определить MIME и выполнить конвертацию.
    """

    type: str
    mime_type: str
    data: str
    name: str = ""

    def __post_init__(self) -> None:
        if self.type not in INPUT_MODALITIES or self.type == "text":
            raise ValueError(f"Неподдерживаемый тип input part: {self.type}")
        if not self.mime_type or not isinstance(self.data, str):
            raise ValueError("InputPart требует mime_type и base64 data")

    def api_value(self) -> dict[str, Any]:
        url = f"data:{self.mime_type};base64,{self.data}"
        if self.type == "image":
            inner: dict[str, Any] = {"url": url}
            if self.name:
                inner["name"] = self.name
            return {"type": "image_url", "image_url": inner}
        if self.type == "audio":
            audio_format = self.mime_type.split("/", 1)[-1].split(";", 1)[0]
            inner = {"data": self.data, "format": audio_format}
            if self.name:
                inner["name"] = self.name
            return {"type": "input_audio", "input_audio": inner}
        if self.type == "file":
            extension = self.mime_type.split("/", 1)[-1].split(";", 1)[0]
            filename = self.name or f"document.{extension}"
            return {
                "type": "file",
                "file": {"filename": filename, "file_data": url},
            }
        inner = {"url": url}
        if self.name:
            inner["name"] = self.name
        return {"type": "video_url", "video_url": inner}


@dataclass(frozen=True, slots=True)
class Event:
    """Внутренний конверт одного события, поступающего агенту."""

    type: str
    data: dict[str, Any]
    source: str = "system"
    target: str | None = None
    reply_to: str | None = None
    parts: tuple[InputPart, ...] = ()
    module_id: str | None = None
    handler_id: str | None = None
    id: str = field(default_factory=lambda: make_id("evt"))
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def model_value(self) -> dict[str, Any]:
        """Представление события в одном сообщении контекста модели."""
        if self.handler_id is not None and self.handler_id != "core:speech":
            return {"handler_id": self.handler_id, "data": self.data}
        return {"type": self.type, "data": self.data}

    def model_content(self) -> str:
        return json_text(self.model_value())

    def model_message(self, capabilities: Any = None) -> dict[str, Any]:
        """Собрать одно OpenAI user message для этого события."""

        supported = getattr(capabilities, "supports", lambda modality: True)
        visible_parts = [part for part in self.parts if supported(part.type)]
        if not visible_parts:
            content: Any = self.model_content()
        else:
            content = [{"type": "text", "text": self.model_content()}]
            content.extend(part.api_value() for part in visible_parts)
        return {"role": "user", "content": content}

    def model_visible_content(self, capabilities: Any = None) -> Any:
        return self.model_message(capabilities)["content"]

    def debug_value(self) -> dict[str, Any]:
        """Полное представление для консольного журнала."""

        return {
            "id": self.id,
            "type": self.type,
            "data": self.data,
            "source": self.source,
            "target": self.target,
            "reply_to": self.reply_to,
            "module_id": self.module_id,
            "handler_id": self.handler_id,
            "created_at": self.created_at,
        }


@dataclass(frozen=True, slots=True)
class ActionRequest:
    """Одно действие, извлечённое из ответа агента."""

    action_id: str
    data: dict[str, Any]
    call_id: str

    def model_value(self) -> dict[str, Any]:
        return {"action_id": self.action_id, "call_id": self.call_id, "data": self.data}


@dataclass(frozen=True, slots=True)
class CallResult:
    """Обязательный результат конкретного действия для его инициатора."""

    call_id: str
    data: dict[str, Any]
    agent_id: str | None = None
    type: str = "call_result"
    parts: tuple[InputPart, ...] = ()
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def model_value(self) -> dict[str, Any]:
        """Представление результата в одном сообщении контекста модели."""

        return {
            "type": self.type,
            "call_id": self.call_id,
            "data": self.data,
        }

    def model_content(self) -> str:
        return json_text(self.model_value())

    def model_message(self) -> dict[str, Any]:
        """Собрать одно OpenAI user message для результата действия."""

        if not self.parts:
            return {"role": "user", "content": self.model_content()}
        content: Any = [{"type": "text", "text": self.model_content()}]
        content.extend(part.api_value() for part in self.parts)
        return {"role": "user", "content": content}

    def debug_value(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "call_id": self.call_id,
            "data": self.data,
            "agent_id": self.agent_id,
            "parts": len(self.parts),
            "created_at": self.created_at,
        }


def parse_actions(value: Any) -> list[ActionRequest]:
    """Разобрать и дополнительно проверить ответ модели.

    Проверка схемы доступных действий выполняется вызывающим агентом, потому
    что только он знает свой актуальный реестр. Здесь проверяется общий
    каркас и специальное правило ``no_action``.
    """

    if not isinstance(value, dict) or set(value) != {"actions"}:
        raise ValueError("Ответ агента должен содержать только поле actions")
    raw_actions = value["actions"]
    if not isinstance(raw_actions, list) or not raw_actions:
        raise ValueError("Ответ агента должен содержать непустой массив actions")

    actions: list[ActionRequest] = []
    seen_call_ids: set[str] = set()
    for index, raw in enumerate(raw_actions):
        if not isinstance(raw, dict) or set(raw) != {"action_id", "call_id", "data"}:
            raise ValueError(
                f"Действие #{index + 1} должно содержать только action_id, call_id и data"
            )
        if (
            not isinstance(raw["call_id"], str)
            or not raw["call_id"].strip()
        ):
            raise ValueError(f"У действия #{index + 1} некорректный call_id")
        if raw["call_id"] in seen_call_ids:
            raise ValueError(
                f"call_id повторяется в ответе: {raw['call_id']!r}"
            )
        seen_call_ids.add(raw["call_id"])
        if not isinstance(raw["action_id"], str) or not raw["action_id"]:
            raise ValueError(f"У действия #{index + 1} некорректный action_id")
        if not isinstance(raw["data"], dict):
            raise ValueError(f"У действия #{index + 1} data должен быть объектом")
        actions.append(
            ActionRequest(
                action_id=raw["action_id"], data=raw["data"], call_id=raw["call_id"]
            )
        )

    no_action_count = sum(action.action_id == "no_action" for action in actions)
    if no_action_count and (no_action_count != 1 or len(actions) != 1):
        raise ValueError("no_action должен быть единственным действием в ответе")
    return actions
