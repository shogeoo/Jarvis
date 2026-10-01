import io
import json
import re
import unittest
from unittest.mock import patch

from jarvis.infrastructure.debug import Debugger
from jarvis.core.protocol import CallResult, Event, InputPart


class _Tty(io.StringIO):
    def isatty(self):
        return True


class DebugTests(unittest.TestCase):
    def test_initialization_precedes_buffered_json_with_one_blank_line(self):
        output = io.StringIO()
        debug = Debugger(stream=output, buffered=True)
        debug.initializing()
        event = Event("system_started", {"datetime": "2026-09-27T12:00:00+03:00"})
        debug.input(event)
        self.assertEqual(output.getvalue(), "Инициализация системы Jarvis....\n")
        debug.initialized()
        debug.result(CallResult(call_id="x", data={}))
        expected = "Инициализация системы Jarvis....\nСистема инициализирована.\n\n"
        expected += json.dumps(event.model_value(), ensure_ascii=False, indent=2) + "\n\n"
        expected += json.dumps(CallResult(call_id="x", data={}).model_value(), indent=2) + "\n"
        self.assertEqual(output.getvalue(), expected)
        self.assertNotIn("\n\n\n", output.getvalue())

    def test_non_protocol_errors_and_reply_are_file_diagnostics_only(self):
        output = io.StringIO()
        debug = Debugger(stream=output)
        with patch("jarvis.infrastructure.debug.logger") as log:
            debug.error("API unavailable")
            debug.reply("Hello")
            debug.log("technical", error="failure")
        self.assertEqual(output.getvalue(), "")
        log.error.assert_called_once()
        self.assertEqual(log.info.call_count, 2)
    def test_trace_contains_only_raw_protocol_json(self):
        output = io.StringIO()
        debug = Debugger(stream=output)
        event = Event(
            event_id="image_notice",
            data={"text": "2026-09-20-005028_jarvis.png"},
            parts=(
                InputPart("image", "image/png", "aGVsbG8="),
            ),
        )
        assistant_output = '{"actions":[{"action_id":"no_action","call_id":"say-1","data":{}}]}'
        call_result = CallResult(call_id="say-1", data={"spoken": True})

        debug.input(event, agent_id="main")
        debug.model("main", assistant_output)
        debug.result(call_result, agent_id="main")
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
            + json.dumps(call_result.model_value(), ensure_ascii=False, indent=2)
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
            event_id="structure_error",
            data={
                "code": "json",
                "message": "Невалидный JSON",
                "response": "Привет чем могу помочь?",
            },
        )
        debug.input(event, agent_id="main")
        text = output.getvalue()
        self.assertIn('"event_id": "structure_error"', text)
        self.assertIn('"response": "Привет чем могу помочь?"', text)

    def test_structure_error_is_red(self):
        output = _Tty()
        debug = Debugger(stream=output)
        event = Event(
            event_id="structure_error",
            data={"code": "json", "message": "broken", "response": "{}"},
        )
        debug.input(event, agent_id="main")
        self.assertIn("\033[31m", output.getvalue())
        self.assertNotIn("\033[32m", output.getvalue())

    def test_subagent_output_has_one_random_color_and_is_separated(self):
        output = _Tty()
        debug = Debugger(stream=output)
        event = Event(event_id="tick.event", data={"text": "ok"})
        call_result = CallResult(call_id="r-1", data={"value": 1})

        debug.input(event, agent_id="agent-001")
        debug.result(call_result, agent_id="agent-001")

        text = output.getvalue()
        colors = re.findall(r"\033\[38;2;\d+;\d+;\d+m", text)
        self.assertEqual(len(colors), 2)
        self.assertEqual(colors[0], colors[1])
        self.assertNotIn("\033[32m", text)
        self.assertFalse(text.startswith("\n"))
        self.assertIn("\033[0m\n\n" + colors[0], text)

    def test_different_subagents_get_distinct_persistent_colors(self):
        output = _Tty()
        debug = Debugger(stream=output)
        for identifier in ("agent-001", "agent-002", "agent-001", "agent-002"):
            debug.result(CallResult(call_id="call", data={}), agent_id=identifier)
        colors = re.findall(r"\033\[38;2;\d+;\d+;\d+m", output.getvalue())
        self.assertEqual(len(colors), 4)
        self.assertNotEqual(colors[0], colors[1])
        self.assertEqual(colors[:2], colors[2:])

    def test_main_output_is_green_on_terminal(self):
        output = _Tty()
        debug = Debugger(stream=output)
        debug.result(CallResult(call_id="r-1", data={}), agent_id="main")
        self.assertIn("\033[32m", output.getvalue())

    def test_subagent_error_is_red_even_for_subagent(self):
        output = _Tty()
        debug = Debugger(stream=output)
        event = Event(
            event_id="capability_error",
            data={"kind": "module", "id": "x", "error": "boom"},
        )
        debug.input(event, agent_id="agent-001")
        self.assertIn("\033[31m", output.getvalue())
        self.assertNotIn("\033[33m", output.getvalue())


if __name__ == "__main__":
    unittest.main()
