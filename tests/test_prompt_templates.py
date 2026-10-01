"""User-editable system text with named substitutions."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from jarvis.core.prompts import agent_system_prompt
from jarvis.infrastructure.model_capabilities import ModelCapabilities
from jarvis.infrastructure.prompt_templates import render_template


class PromptTemplateTests(unittest.TestCase):
    def test_custom_templates_are_used_for_dynamic_sections(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            assets = root / "assets"
            assets.mkdir()
            templates = {
                "memory_instruction.txt": "Custom memory instruction.",
                "model_info.txt": "Selected model: {model_id}\nInputs: {modalites}.",
                "agent_info.txt": "Identity: {name} / {agent_id}.",
            }
            for filename, content in templates.items():
                (assets / filename).write_text(content, encoding="utf-8")
            with patch("jarvis.infrastructure.prompt_templates.files", return_value=root):
                system = agent_system_prompt(
                    "Person", "Environment", {}, {}, {}, agent_id="main", agent_name="Jarvis",
                    model_capabilities=ModelCapabilities("current-model", ("text", "image")),
                    semantic_memory={"entries": []},
                )
            self.assertIn('MEMORY:\nCustom memory instruction.\n{\n  "entries": []\n}', system)
            self.assertIn("MODEL INFO:\nSelected model: current-model\nInputs: text, image.", system)
            self.assertIn("AGENT_INFO:\nIdentity: Jarvis / main.", system)
            self.assertLess(system.index("MODEL INFO:"), system.index("AGENT_INFO:"))

    def test_values_containing_braces_are_not_reformatted(self):
        self.assertEqual(render_template("agent_info.txt", name="Agent {custom}", agent_id="agent-001"),
                         "Тебя зовут Agent {custom}. Твой идентификатор agent-001.")

    def test_missing_field_reports_which_template_is_invalid(self):
        with self.assertRaisesRegex(ValueError, "agent_info.txt"):
            render_template("agent_info.txt", name="Jarvis")
