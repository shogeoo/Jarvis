import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch
from importlib.resources import files

from jarvis.cli import build_parser, main


class CliTests(unittest.TestCase):
    def test_init_env_from_package_does_not_overwrite_existing_configuration(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / ".env"
            self.assertEqual(main(["--env-file", str(path), "--init-env"]), 0)
            self.assertEqual(path.read_text(), files("jarvis").joinpath("assets", "env_example.txt").read_text())
            path.write_text("LLM_MODEL=personal\n")
            self.assertEqual(main(["--env-file", str(path), "--init-env"]), 0)
            self.assertEqual(path.read_text(), "LLM_MODEL=personal\n")

    def test_cli_only_configures_core_startup(self):
        args = build_parser().parse_args(["--env-file", "custom.env"])
        self.assertEqual(args.env_file, "custom.env")


if __name__ == "__main__":
    unittest.main()
