import importlib
import tempfile
import unittest
from pathlib import Path

import numpy as np

from jarvis.core.registry import ActionRegistry, EventRegistry
from jarvis.core.runtime import EventBus
from jarvis.infrastructure.debug import Debugger
from jarvis.modules.manager import ModuleManager


class _VAD:
    def __init__(self, values):
        self.values = iter(values)

    def is_speech(self, frame):
        return next(self.values)

    def reset(self):
        pass


class VadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        events = EventRegistry()
        actions = ActionRegistry()
        bus = EventBus(events, debug=Debugger(enabled=False))
        cls.manager = ModuleManager(
            bus,
            actions,
            events,
            modules_dir=Path(".jarvis/modules"),
            debug=Debugger(enabled=False),
        )
        cls.manager.load("speech_input", start_handlers=False)
        package = cls.manager._loaded["speech_input"].python_module.__name__
        cls.vad = importlib.import_module(package + ".vad")

    @classmethod
    def tearDownClass(cls):
        cls.manager.unload("speech_input")

    def test_segmenter_signals_speech_onset_before_transcription(self):
        with tempfile.TemporaryDirectory() as directory:
            segmenter = self.vad.Segmenter(
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
