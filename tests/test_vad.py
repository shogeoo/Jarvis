import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

from jarvis.speech import vad


class _VAD:
    def __init__(self, values):
        self.values = iter(values)

    def is_speech(self, frame):
        return next(self.values)

    def reset(self):
        pass


class VadTests(unittest.TestCase):
    def test_vad_uses_cpu_independently_of_speech_cuda(self):
        with patch.object(vad.ort, "InferenceSession") as create:
            vad.SileroVAD("unused.onnx")
        create.assert_called_once_with("unused.onnx", providers=["CPUExecutionProvider"])

    def test_segmenter_signals_speech_onset_before_transcription(self):
        with tempfile.TemporaryDirectory() as directory:
            segmenter = vad.Segmenter(
                _VAD([True, True]),
                out_dir=Path(directory),
                pre_roll=0,
            )
            frame = np.zeros(512, dtype=np.int16)

            segmenter.feed(frame)
            self.assertTrue(segmenter.consume_speech_started())
            self.assertFalse(segmenter.consume_speech_started())

            segmenter.feed(frame)
            self.assertFalse(segmenter.consume_speech_started())
            segmenter.flush()


if __name__ == "__main__":
    unittest.main()
