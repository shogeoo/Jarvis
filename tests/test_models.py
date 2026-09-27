import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from jarvis.infrastructure.models import download_models


class ModelPreparationTests(unittest.TestCase):
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
