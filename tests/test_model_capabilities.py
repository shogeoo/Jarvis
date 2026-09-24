import unittest
from unittest.mock import Mock, patch

from jarvis.infrastructure.model_capabilities import discover_model_capabilities


class ModelCapabilitiesTests(unittest.TestCase):
    @patch("jarvis.infrastructure.model_capabilities.requests.get")
    def test_reads_modalities_from_openrouter_model_catalog(self, get):
        response = Mock()
        response.json.return_value = {
            "data": [
                {
                    "id": "provider/model",
                    "architecture": {
                        "input_modalities": ["text", "image", "audio", "file"]
                    },
                }
            ]
        }
        get.return_value = response

        capabilities = discover_model_capabilities(
            "provider/model", "https://openrouter.ai/api/v1", "secret"
        )

        self.assertEqual(
            capabilities.input_modalities, ("text", "image", "audio", "file")
        )
        get.assert_called_once()

    @patch("jarvis.infrastructure.model_capabilities.requests.get", side_effect=OSError("offline"))
    def test_falls_back_to_all_modalities_without_catalog(self, get):
        capabilities = discover_model_capabilities(
            "provider/model", "https://openrouter.ai/api/v1", "secret"
        )
        self.assertEqual(
            capabilities.input_modalities,
            ("text", "image", "audio", "video", "file"),
        )
        self.assertIn("offline", capabilities.discovery_error)

    def test_prompt_explains_unsupported_native_modalities(self):
        from jarvis.infrastructure.model_capabilities import ModelCapabilities

        prompt = ModelCapabilities("model", ("text", "image")).prompt_block()

        self.assertIn("Текущие поддерживаемые модальности: text, image", prompt)
        self.assertIn("Нативно не поддерживаются: audio, video, file", prompt)

    def test_all_five_modalities_render_as_model_info_content(self):
        from jarvis.infrastructure.model_capabilities import (
            SUPPORTED_INPUT_MODALITIES,
            ModelCapabilities,
        )

        self.assertEqual(
            ModelCapabilities("GPT-6 Luna", SUPPORTED_INPUT_MODALITIES).prompt_block(),
            "Текущие поддерживаемые модальности: text, image, audio, video, file.",
        )


if __name__ == "__main__":
    unittest.main()
