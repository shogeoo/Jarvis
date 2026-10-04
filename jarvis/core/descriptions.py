"""Editable descriptions for built-in capabilities; schemas stay in code."""

from copy import deepcopy
from dataclasses import replace
from importlib.resources import files
import json
from pathlib import Path

from .protocol import validate_catalog_text


def read_descriptions(path: Path | None = None) -> dict:
    resource = path if path is not None else files("jarvis").joinpath("assets", "capability_descriptions.json")
    value = json.loads(resource.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or set(value) != {"actions", "handlers", "modules"}:
        raise ValueError("Description catalog requires actions, handlers and modules arrays")

    def validate_tree(node):
        if isinstance(node, dict):
            for key, child in node.items():
                if key == "description":
                    if not isinstance(child, str):
                        raise ValueError("description must be a string")
                elif not isinstance(child, (dict, list)):
                    raise ValueError("Only description values belong in the description catalog")
                else:
                    validate_tree(child)
        elif isinstance(node, list):
            for child in node:
                validate_tree(child)
        else:
            raise ValueError("Invalid description catalog structure")

    result = {}
    for kind, entries in value.items():
        if not isinstance(entries, list):
            raise ValueError(f"{kind} must be an array")
        index = {}
        for entry in entries:
            if not isinstance(entry, dict) or len(entry) != 1:
                raise ValueError("Each description entry must be keyed by one capability identifier")
            identifier, document = next(iter(entry.items()))
            if identifier in index or not isinstance(document, dict):
                raise ValueError(f"Invalid or duplicate description entry: {identifier}")
            validate_tree(document)
            index[identifier] = document
        result[kind] = index
    validate_catalog_text(value)
    return result


def _overlay(original, descriptions):
    """Change text only; an entry may also describe a dynamically expanded schema."""
    result = deepcopy(original)
    if isinstance(result, dict) and isinstance(descriptions, dict):
        for key, value in descriptions.items():
            if key == "description":
                result[key] = value
            elif key in result:
                result[key] = _overlay(result[key], value)
    elif isinstance(result, list) and isinstance(descriptions, list):
        result = [_overlay(item, descriptions[index]) if index < len(descriptions) else item
                  for index, item in enumerate(result)]
    return result


def apply_descriptions(actions, events, *, path: Path | None = None):
    catalog = read_descriptions(path)

    def describe(spec, document, fields):
        changes = {"description": document.get("description", spec.description)}
        for field in fields:
            changes[field] = _overlay(getattr(spec, field), document.get(field, {}))
        return replace(spec, **changes)

    return (
        {identifier: describe(spec, catalog["actions"][identifier], ("data_schema", "result_schema"))
         if spec.owner.startswith("core") and identifier in catalog["actions"] else spec
         for identifier, spec in actions.items()},
        {identifier: describe(spec, catalog["handlers"][identifier], ("data_schema",))
         if identifier in catalog["handlers"] else spec
         for identifier, spec in events.items()},
    )
