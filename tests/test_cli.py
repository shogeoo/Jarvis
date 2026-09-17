import unittest

from jarvis.cli import publish_stt_event


class _Bus:
    def __init__(self, calls):
        self.calls = calls

    def publish(self, event):
        self.calls.append(("publish", event))


class CliTests(unittest.TestCase):
    def test_stt_event_is_published_without_tts_interrupt_at_transcription(self):
        calls = []
        publish_stt_event(_Bus(calls), "Новый запрос")

        self.assertEqual(calls[0][0], "publish")
        self.assertEqual(
            calls[0][1].model_value(),
            {"type": "speech", "data": {"text": "Новый запрос"}},
        )


if __name__ == "__main__":
    unittest.main()
