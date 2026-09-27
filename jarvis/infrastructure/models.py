"""Explicit model preparation; never starts speech or calls the model API."""

from pathlib import Path
import shutil


def ensure_whisper_model(root: Path) -> Path:
    from ..speech.config import STT_MODEL
    from faster_whisper.utils import download_model

    destination = Path(root) / "runtime" / "models" / "whisper-large-v3-turbo"
    required = ("model.bin", "config.json", "tokenizer.json", "vocabulary.json", "preprocessor_config.json")
    if not all((destination / name).is_file() for name in required):
        download_model(STT_MODEL, output_dir=str(destination))
    return destination


def download_models(root: Path) -> None:
    from ..speech.config import STT_ENABLED, TTS_ENABLED, build_config

    if STT_ENABLED:
        from ..speech.vad import ensure_model

        print(f"STT model: {ensure_whisper_model(root)}")
        ensure_model(str(root / "runtime" / "models" / "silero_vad.onnx"))
    config = build_config(root)
    if TTS_ENABLED and shutil.which(str(config.tts_server_bin)):
        from huggingface_hub import hf_hub_download

        for filename, destination in (
            (config.tts_model.name, config.tts_model),
            ("tokenizer.json", config.tts_tokenizer),
        ):
            if not destination.is_file():
                destination.parent.mkdir(parents=True, exist_ok=True)
                downloaded = hf_hub_download("rodrigomt/s2-pro-gguf", filename, local_dir=str(destination.parent))
                if Path(downloaded).resolve() != destination.resolve():
                    temporary = destination.with_suffix(destination.suffix + ".tmp")
                    shutil.copyfile(downloaded, temporary)
                    temporary.replace(destination)
            print(f"TTS asset: {destination}")
    elif TTS_ENABLED:
        print(
            "s2 is not installed: TTS weights skipped, speech action will be unavailable."
        )
