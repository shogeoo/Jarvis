import io
import json
import unittest

from jarvis.infrastructure.debug import Debugger
from jarvis.core.protocol import Event, InputPart


class DebugTests(unittest.TestCase):
    def test_trace_contains_only_raw_protocol_json(self):
        output = io.StringIO()
        debug = Debugger(stream=output)
        event = Event(
            type="screenshot",
            data={"text": "2026-09-20-005028_jarvis.png"},
            parts=(
                InputPart("image", "image/png", "aGVsbG8="),
            ),
        )
        assistant_output = '{"actions":[{"type":"no_action","data":{}}]}'

        debug.input(event)
        debug.model("main", assistant_output)
        debug.log("event", event=event.debug_value())
        debug.state("main", "acting")

        self.assertEqual(
            output.getvalue(),
            json.dumps(event.model_value(), ensure_ascii=False, indent=2)
            + "\n"
            + json.dumps(
                json.loads(assistant_output), ensure_ascii=False, indent=2
            )
            + "\n",
        )
        self.assertNotIn("base64", output.getvalue())
        self.assertNotIn("image_url", output.getvalue())


if __name__ == "__main__":
    unittest.main()
