import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from jarvis.infrastructure.models import download_models, ensure_whisper_model


class ModelPreparationTests(unittest.TestCase):
    def test_model_paths_belong_to_runtime_models(self):
        from jarvis.speech.config import build_config
        root = Path("/test/Jarvis/.jarvis")
        config = build_config(root)
        self.assertEqual(config.tts_model, root / "runtime/models/fish-speech/s2-pro-q4_k_m.gguf")
        self.assertEqual(config.tts_tokenizer, root / "runtime/models/fish-speech/tokenizer.json")

    def test_whisper_download_uses_local_directory_and_skips_complete_model(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "runtime/models/whisper-large-v3-turbo"
            with patch("faster_whisper.utils.download_model") as download:
                self.assertEqual(ensure_whisper_model(root), target)
                download.assert_called_once_with("large-v3-turbo", output_dir=str(target))
                target.mkdir(parents=True)
                for name in ("model.bin", "config.json", "tokenizer.json", "vocabulary.json", "preprocessor_config.json"):
                    (target / name).write_bytes(b"asset")
                ensure_whisper_model(root)
                self.assertEqual(download.call_count, 1)
                (target / "config.json").unlink()
                ensure_whisper_model(root)
                self.assertEqual(download.call_count, 2)

    @patch("jarvis.speech.config.STT_ENABLED", False)
    @patch("jarvis.speech.config.TTS_ENABLED", True)
    @patch("jarvis.infrastructure.models.shutil.which", return_value=None)
    def test_missing_s2_does_not_download_tts_weights(self, which):
        with tempfile.TemporaryDirectory() as temporary:
            with patch("huggingface_hub.hf_hub_download") as download:
                download_models(Path(temporary))
            download.assert_not_called()

    @patch("jarvis.speech.config.STT_ENABLED", False)
    @patch("jarvis.speech.config.TTS_ENABLED", True)
    @patch("jarvis.infrastructure.models.shutil.which", return_value="/fake/s2")
    def test_tts_assets_are_copied_to_configured_paths(self, which):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cached = root / "cached"
            cached.write_bytes(b"asset")
            config = SimpleNamespace(
                tts_server_bin="s2",
                tts_model=root / "models" / "s2-pro-q4_k_m.gguf",
                tts_tokenizer=root / "models" / "tokenizer.json",
            )
            with (
                patch("jarvis.speech.config.build_config", return_value=config),
                patch(
                    "huggingface_hub.hf_hub_download", return_value=str(cached)
                ) as download,
            ):
                download_models(root)
                self.assertEqual(download.call_count, 2)
                self.assertEqual(config.tts_model.read_bytes(), b"asset")
                self.assertEqual(config.tts_tokenizer.read_bytes(), b"asset")
                download_models(root)
                self.assertEqual(download.call_count, 2)
