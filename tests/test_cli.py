import unittest

from jarvis.cli import publish_stt_event


class _Bus:
    def __init__(self, calls):
        self.calls = calls

    def publish(self, event):
        self.calls.append(("publish", event))


class _Speaker:
    def __init__(self, calls):
        self.calls = calls

    def interrupt(self):
        self.calls.append(("interrupt",))


class CliTests(unittest.TestCase):
    def test_stt_event_is_published_before_tts_interrupt(self):
        calls = []
        publish_stt_event(_Bus(calls), _Speaker(calls), "Новый запрос")

        self.assertEqual(calls[0][0], "publish")
        self.assertEqual(calls[1], ("interrupt",))
        self.assertEqual(
            calls[0][1].model_value(),
            {"type": "speech", "data": {"text": "Новый запрос"}},
        )


if __name__ == "__main__":
    unittest.main()
