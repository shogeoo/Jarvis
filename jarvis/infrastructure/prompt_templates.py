"""Read editable prompt text and substitute named fields without evaluating code."""

from importlib.resources import files


def render_template(filename: str, **values) -> str:
    text = files("jarvis").joinpath("assets", filename).read_text(encoding="utf-8").strip()
    if not text:
        raise ValueError(f"Empty prompt template: {filename}")
    try:
        return text.format(**values)
    except (KeyError, ValueError, IndexError, AttributeError) as exc:
        raise ValueError(f"Invalid fields in prompt template {filename}: {exc}") from exc
