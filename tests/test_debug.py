import io
import json
import unittest

from jarvis.infrastructure.debug import Debugger
from jarvis.core.protocol import ActionResult, Event, InputPart


class _Tty(io.StringIO):
    def isatty(self):
        return True


class DebugTests(unittest.TestCase):
    def test_trace_contains_only_raw_protocol_json(self):
        output = io.StringIO()
        debug = Debugger(stream=output)
        event = Event(
            type="take_screenshot",
            data={"text": "2026-09-20-005028_jarvis.png"},
            parts=(
                InputPart("image", "image/png", "aGVsbG8="),
            ),
        )
        assistant_output = '{"actions":[{"action_id":"no_action","call_id":"say-1","data":{}}]}'
        action_result = ActionResult(call_id="say-1", data={"spoken": True})

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

    def test_broken_model_output_is_silent_structure_error_shows_instead(self):
        output = io.StringIO()
        debug = Debugger(stream=output)
        debug.model("main", "Привет чем могу помочь?")
        self.assertEqual(output.getvalue(), "")

        event = Event(
            type="structure_error",
            data={
                "code": "json",
                "message": "Невалидный JSON",
                "response": "Привет чем могу помочь?",
            },
        )
        debug.input(event, agent_id="main")
        text = output.getvalue()
        self.assertIn('"type": "structure_error"', text)
        self.assertIn('"response": "Привет чем могу помочь?"', text)

    def test_structure_error_is_red(self):
        output = _Tty()
        debug = Debugger(stream=output)
        event = Event(
            type="structure_error",
            data={"code": "json", "message": "broken", "response": "{}"},
        )
        debug.input(event, agent_id="main")
        self.assertIn("\033[31m", output.getvalue())
        self.assertNotIn("\033[32m", output.getvalue())

    def test_subagent_output_is_yellow_and_separated(self):
        output = _Tty()
        debug = Debugger(stream=output)
        event = Event(type="tick.event", data={"text": "ok"})
        action_result = ActionResult(call_id="r-1", data={"value": 1})

        debug.input(event, agent_id="agent-001")
        debug.result(action_result, agent_id="agent-001")

        text = output.getvalue()
        self.assertIn("\033[33m", text)
        self.assertNotIn("\033[32m", text)
        self.assertFalse(text.startswith("\n"))
        self.assertIn("\033[0m\n\n\033[33m", text)

    def test_main_output_is_green_on_terminal(self):
        output = _Tty()
        debug = Debugger(stream=output)
        debug.result(ActionResult(call_id="r-1", data={}), agent_id="main")
        self.assertIn("\033[32m", output.getvalue())

    def test_subagent_error_is_red_even_for_subagent(self):
        output = _Tty()
        debug = Debugger(stream=output)
        event = Event(
            type="capability_error",
            data={"capability": "module:x", "error": "boom"},
        )
        debug.input(event, agent_id="agent-001")
        self.assertIn("\033[31m", output.getvalue())
        self.assertNotIn("\033[33m", output.getvalue())


if __name__ == "__main__":
    unittest.main()
