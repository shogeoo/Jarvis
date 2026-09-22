import io
import json
import unittest

from jarvis.infrastructure.debug import Debugger
from jarvis.core.protocol import ActionResult, Event, InputPart


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
        assistant_output = '{"actions":[{"action_id":"say-1","type":"no_action","data":{}}]}'
        action_result = ActionResult(action_id="say-1", data={"spoken": True})

        debug.input(event, agent_id="main")
        debug.model("main", assistant_output)
        debug.result(action_result, agent_id="main")
        debug.log("event", event=event.debug_value())
        debug.state("main", "acting")

        self.assertEqual(
            output.getvalue(),
            json.dumps(event.model_value(), ensure_ascii=False, indent=2)
            + "\n"
            + "\n"
            + json.dumps(
                json.loads(assistant_output), ensure_ascii=False, indent=2
            )
            + "\n"
            + "\n"
            + json.dumps(action_result.model_value(), ensure_ascii=False, indent=2)
            + "\n",
        )
        self.assertNotIn("base64", output.getvalue())
        self.assertNotIn("image_url", output.getvalue())

    def test_subagent_output_is_orange_and_separated(self):
        class _Tty(io.StringIO):
            def isatty(self):
                return True

        output = _Tty()
        debug = Debugger(stream=output)
        event = Event(type="tick.event", data={"text": "ok"})
        action_result = ActionResult(action_id="r-1", data={"value": 1})

        debug.input(event, agent_id="agent-001")
        debug.result(action_result, agent_id="agent-001")

        text = output.getvalue()
        self.assertIn("\033[38;5;208m", text)
        self.assertNotIn("\033[32m", text)
        self.assertTrue(text.startswith("\n"))
        self.assertIn("\033[0m\n\n\033[38;5;208m", text)

    def test_main_output_is_green_on_terminal(self):
        class _Tty(io.StringIO):
            def isatty(self):
                return True

        output = _Tty()
        debug = Debugger(stream=output)
        debug.result(ActionResult(action_id="r-1", data={}), agent_id="main")
        self.assertIn("\033[32m", output.getvalue())
        self.assertNotIn("\n\n", output.getvalue())


if __name__ == "__main__":
    unittest.main()
