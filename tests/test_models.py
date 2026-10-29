"""Model installation is tested against a fake Hub, never actual downloads."""

import hashlib
import importlib.util
import io
import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch

from jarvis.infrastructure.models import (
    download_models,
    ensure_local_model,
    ensure_whisper_model,
    model_specs,
    model_status,
)


def tensor_bytes():
    header = json.dumps(
        {"weight": {"dtype": "F32", "shape": [1], "data_offsets": [0, 4]}}
    ).encode()
    header += b" " * (-len(header) % 8)
    return struct.pack("<Q", len(header)) + header + b"\0" * 4


def asset_bytes(name):
    if name.endswith(".safetensors"):
        return tensor_bytes()
    if name.endswith(".gguf"):
        return b"GGUF" + struct.pack("<IQQ", 3, 1, 0) + b"dummy tensor data"
    if name.endswith(".json"):
        return b'{"test": true}'
    return b"test model bytes"


class ModelPreparationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.specs = model_specs(self.root)
        self.requests = []
        self.snapshot_patch = patch(
            "huggingface_hub.snapshot_download", side_effect=self.download
        )
        self.snapshot = self.snapshot_patch.start()
        self.addCleanup(self.snapshot_patch.stop)
        # Fake VAD bytes are not an ONNX graph. Verify CPU placement without loading a model.
        self.onnx_patch = patch("onnxruntime.InferenceSession")
        self.onnx = self.onnx_patch.start()
        self.addCleanup(self.onnx_patch.stop)

    def download(self, repository, *, local_dir, allow_patterns, force_download):
        self.requests.append((repository, force_download))
        spec = next(s for s in self.specs if s.repository == repository)
        stage = Path(local_dir)
        for relative in spec.files:
            path = stage / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            data = asset_bytes(relative)
            path.write_bytes(data)
            metadata = stage / ".cache/huggingface/download" / (relative + ".metadata")
            metadata.parent.mkdir(parents=True, exist_ok=True)
            metadata.write_text("commit\n" + hashlib.sha256(data).hexdigest() + "\n0\n")
        return str(stage)

    def test_all_models_install_even_when_speech_is_disabled_or_s2_missing(self):
        with (
            patch("jarvis.speech.config.STT_ENABLED", False),
            patch("jarvis.speech.config.TTS_ENABLED", False),
            patch("shutil.which", return_value=None),
        ):
            download_models(self.root)
        self.assertEqual(len(self.requests), 4)
        self.assertTrue(all(s["ready"] for s in model_status(self.root)))
        config = __import__(
            "jarvis.speech.config", fromlist=["build_config"]
        ).build_config(self.root)
        self.assertTrue(config.tts_model.exists())
        self.assertTrue(config.tts_tokenizer.exists())
        self.assertTrue((self.root / "runtime/models/silero_vad.onnx").exists())
        self.assertTrue(
            all(
                call.kwargs["providers"] == ["CPUExecutionProvider"]
                for call in self.onnx.call_args_list
            )
        )

    def test_complete_verified_models_are_reused_and_force_refreshes_everything(self):
        download_models(self.root)
        download_models(self.root)
        self.assertEqual(len(self.requests), 4)
        download_models(self.root, force=True)
        self.assertEqual(len(self.requests), 8)
        self.assertTrue(all(force for _, force in self.requests[4:]))

    def test_deleted_runtime_reports_missing_without_any_download_or_creation(self):
        self.assertFalse((self.root / "runtime").exists())
        self.assertEqual([s["ready"] for s in model_status(self.root)], [False] * 4)
        self.snapshot.assert_not_called()
        self.assertFalse((self.root / "runtime").exists())

    def test_valid_but_modified_asset_is_detected_by_saved_checksum_and_repaired(self):
        download_models(self.root)
        path = self.root / "runtime/models/Qwen3-Reranker-0.6B/config.json"
        path.write_text('{"different": true}')
        status = {s["name"]: s for s in model_status(self.root)}
        self.assertFalse(status["reranker"]["ready"])
        self.assertTrue(status["whisper"]["ready"])
        ensure_local_model(self.root, "reranker")
        self.assertEqual(len(self.requests), 5)
        self.assertTrue(all(s["ready"] for s in model_status(self.root)))

    def test_incomplete_reranker_is_not_accepted_just_because_weights_exist(self):
        directory = self.root / "runtime/models/Qwen3-Reranker-0.6B"
        directory.mkdir(parents=True)
        (directory / "model.safetensors").write_bytes(tensor_bytes())
        ensure_local_model(self.root, "reranker")
        self.assertEqual(len(self.requests), 1)
        self.assertTrue((directory / "tokenizer_config.json").exists())

    def test_bad_hub_checksum_forces_fresh_download_before_publishing(self):
        def corrupt_once(*args, **kwargs):
            result = self.download(*args, **kwargs)
            if not kwargs["force_download"]:
                (Path(result) / "config.json").write_text('{"modified":true}')
            return result

        self.snapshot.side_effect = corrupt_once
        ensure_whisper_model(self.root)
        self.assertEqual([force for _, force in self.requests], [False, True])
        self.assertTrue(model_status(self.root)[0]["ready"])

    def test_failed_download_does_not_replace_existing_weights(self):
        ensure_local_model(self.root, "reranker")
        path = self.root / "runtime/models/Qwen3-Reranker-0.6B/model.safetensors"
        original = path.read_bytes()
        self.snapshot.side_effect = OSError("network interrupted")
        with self.assertRaises(OSError):
            ensure_local_model(self.root, "reranker", force=True)
        self.assertEqual(path.read_bytes(), original)

    def test_installer_check_mode_is_offline_and_returns_failure_for_missing_models(
        self,
    ):
        source = Path(__file__).resolve().parents[1] / "scripts/install_models.py"
        spec = importlib.util.spec_from_file_location("install_models_test", source)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with patch("sys.stdout", new_callable=io.StringIO) as output:
            code = module.main(["--check", "--jarvis-dir", str(self.root)])
        self.assertEqual(code, 1)
        self.assertIn("MISSING/INVALID", output.getvalue())
        self.snapshot.assert_not_called()
        self.assertFalse((self.root / "runtime").exists())
