import io
import json
import unittest

from jarvis.infrastructure.debug import Debugger
from jarvis.core.protocol import Event


class DebugTests(unittest.TestCase):
    def test_trace_contains_only_exact_model_message_contents(self):
        output = io.StringIO()
        debug = Debugger(stream=output)
        event = Event(type="speech", data={"text": "Привет"})
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


if __name__ == "__main__":
    unittest.main()
