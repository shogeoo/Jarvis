"""Explicit preparation and offline integrity checks of every local model."""

from __future__ import annotations

from dataclasses import dataclass
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import struct

from .console import logger
from .context import read_json, sync_directory, write_text


@dataclass(frozen=True)
class ModelSpec:
    name: str
    repository: str
    directory: Path
    files: dict[str, str]  # repository path -> installed path relative to directory
    patterns: tuple[str, ...]


def model_specs(root):
    from ..speech.config import STT_MODEL, build_config
    from faster_whisper.utils import _MODELS

    base = Path(root) / "runtime/models"
    speech = build_config(Path(root))
    whisper_files = (
        "model.bin",
        "config.json",
        "tokenizer.json",
        "vocabulary.json",
        "preprocessor_config.json",
    )
    reranker_files = (
        "model.safetensors",
        "config.json",
        "tokenizer.json",
        "tokenizer_config.json",
    )
    return (
        ModelSpec(
            "whisper",
            _MODELS.get(STT_MODEL, STT_MODEL),
            base / "whisper-large-v3-turbo",
            {f: f for f in whisper_files},
            whisper_files,
        ),
        ModelSpec(
            "vad",
            "onnx-community/silero-vad",
            base,
            {"onnx/model.onnx": "silero_vad.onnx"},
            ("onnx/model.onnx",),
        ),
        ModelSpec(
            "tts",
            "rodrigomt/s2-pro-gguf",
            speech.tts_model.parent,
            {
                speech.tts_model.name: speech.tts_model.name,
                speech.tts_tokenizer.name: speech.tts_tokenizer.name,
            },
            (speech.tts_model.name, speech.tts_tokenizer.name),
        ),
        ModelSpec(
            "reranker",
            "Qwen/Qwen3-Reranker-0.6B",
            base / "Qwen3-Reranker-0.6B",
            {f: f for f in reranker_files},
            ("*.json", "*.safetensors", "*.txt", "*.jinja"),
        ),
    )


def _digest(path, algorithm="sha256", *, git_blob=False):
    digest = hashlib.new(algorithm)
    if git_blob:
        digest.update(f"blob {path.stat().st_size}\0".encode())
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _validate_file(path):
    if not path.is_file() or path.stat().st_size == 0:
        raise ValueError(f"Missing or empty model asset: {path}")
    if path.suffix == ".json":
        json.loads(path.read_text(encoding="utf-8"))
    elif path.suffix == ".safetensors":
        from safetensors import safe_open

        with safe_open(str(path), framework="numpy") as model:
            if not model.keys():
                raise ValueError(f"No tensors in {path}")
    elif path.suffix == ".gguf":
        with path.open("rb") as stream:
            header = stream.read(24)
        if len(header) != 24 or header[:4] != b"GGUF":
            raise ValueError(f"Invalid GGUF header: {path}")
        version, tensors, _ = struct.unpack("<IQQ", header[4:])
        if version not in (2, 3) or tensors == 0:
            raise ValueError(f"Invalid GGUF model: {path}")
    elif path.suffix == ".onnx":
        import onnxruntime

        onnxruntime.InferenceSession(str(path), providers=["CPUExecutionProvider"])


def _verify_hub_file(stage, relative):
    path = stage / relative
    _validate_file(path)
    metadata = stage / ".cache/huggingface/download" / (relative + ".metadata")
    if not metadata.exists():
        return
    lines = metadata.read_text().splitlines()
    expected = lines[1].strip('"') if len(lines) > 1 else ""
    if len(expected) == 64:
        actual = _digest(path)
    elif len(expected) == 40:
        actual = _digest(path, "sha1", git_blob=True)
    else:
        raise ValueError(f"Unknown Hub checksum format: {relative}")
    if actual != expected:
        raise ValueError(f"Hub checksum mismatch: {relative}")


def _status(spec, base):
    problems = []
    try:
        manifest = read_json(base / "manifest.json", {}).get(spec.name, {})
        for relative in spec.files.values():
            _validate_file(spec.directory / relative)
        if not manifest:
            raise ValueError(
                f"Integrity manifest missing for {spec.name}; run scripts/install_models.py"
            )
        for relative, expected in manifest.items():
            path = base / relative
            if not path.is_file() or _digest(path) != expected:
                raise ValueError(f"Installed checksum mismatch: {path}")
    except Exception as exc:
        problems.append(str(exc))
    return {
        "name": spec.name,
        "repository": spec.repository,
        "path": str(spec.directory),
        "ready": not problems,
        "problems": problems,
    }


def model_status(root):
    """Offline only: no Hub access, GPU model loading or runtime initialization."""
    base = Path(root) / "runtime/models"
    return [_status(spec, base) for spec in model_specs(root)]


def _install(spec, base, *, force=False):
    from huggingface_hub import snapshot_download

    base.mkdir(parents=True, exist_ok=True)
    stage = base / ".downloads" / spec.name
    # A failed/interrupted transfer remains here for the next invocation to resume.
    for refresh in [True] if force else [False, True]:
        snapshot_download(
            spec.repository,
            local_dir=str(stage),
            allow_patterns=list(spec.patterns),
            force_download=refresh,
        )
        try:
            for relative in spec.files:
                _verify_hub_file(stage, relative)
            sources = [
                p
                for p in stage.rglob("*")
                if p.is_file() and ".cache" not in p.relative_to(stage).parts
            ]
            for source in sources:
                _verify_hub_file(stage, source.relative_to(stage).as_posix())
            break
        except Exception:
            if refresh:
                raise
            logger.warning("Invalid cached %s assets; fetching a fresh copy", spec.name)
    hashes = {}
    for source in sources:
        relative = source.relative_to(stage).as_posix()
        destination = spec.directory / spec.files.get(relative, relative)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name("." + destination.name + ".installing")
        try:
            shutil.copyfile(source, temporary)
            with temporary.open("rb") as stream:
                os.fsync(stream.fileno())
            os.replace(temporary, destination)
            sync_directory(destination.parent)
        finally:
            temporary.unlink(missing_ok=True)
        hashes[destination.relative_to(base).as_posix()] = _digest(destination)
    try:
        manifest = read_json(base / "manifest.json", {})
    except ValueError:
        logger.warning("Replacing an invalid model integrity manifest")
        manifest = {}
    manifest[spec.name] = hashes
    write_text(base / "manifest.json", json.dumps(manifest, indent=2) + "\n")
    shutil.rmtree(stage)
    return spec.directory


def _ensure_spec(spec, base, *, force=False):
    base.mkdir(parents=True, exist_ok=True)
    with (base / ".install.lock").open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if force or not _status(spec, base)["ready"]:
            _install(spec, base, force=force)
    return spec.directory


def ensure_local_model(root, name, *, force=False):
    base = Path(root) / "runtime/models"
    spec = next(s for s in model_specs(root) if s.name == name)
    return _ensure_spec(spec, base, force=force)


def ensure_vad_model(path):
    path = Path(path)
    spec = ModelSpec(
        "vad",
        "onnx-community/silero-vad",
        path.parent,
        {"onnx/model.onnx": path.name},
        ("onnx/model.onnx",),
    )
    _ensure_spec(spec, path.parent)
    return path


def ensure_whisper_model(root):
    return ensure_local_model(root, "whisper")


def download_models(root, *, force=False):
    """Prepare all four models, regardless of enabled features or s2 installation."""
    errors = []
    for spec in model_specs(root):
        try:
            ensure_local_model(root, spec.name, force=force)
            logger.info("Model ready: %s at %s", spec.name, spec.directory)
        except Exception as exc:
            logger.exception("Model preparation failed: %s", spec.name)
            errors.append(f"{spec.name}: {exc}")
    if errors:
        raise RuntimeError("Model preparation incomplete: " + "; ".join(errors))
