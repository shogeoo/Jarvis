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

        self.assertEqual(capabilities.input_modalities, ("text", "image", "audio"))
        get.assert_called_once()

    @patch("jarvis.infrastructure.model_capabilities.requests.get", side_effect=OSError("offline"))
    def test_uses_safe_text_only_fallback(self, get):
        capabilities = discover_model_capabilities(
            "provider/model", "https://openrouter.ai/api/v1", "secret"
        )
        self.assertEqual(capabilities.input_modalities, ("text",))
        self.assertIn("offline", capabilities.discovery_error)

    def test_prompt_explains_unsupported_native_modalities(self):
        from jarvis.infrastructure.model_capabilities import ModelCapabilities

        prompt = ModelCapabilities("model", ("text", "image")).prompt_block()

        self.assertIn("Текущие поддерживаемые модальности: text, image", prompt)
        self.assertIn("Нативно не поддерживаются: audio, video", prompt)


if __name__ == "__main__":
    unittest.main()
