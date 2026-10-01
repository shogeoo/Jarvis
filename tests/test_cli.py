import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from jarvis.cli import build_parser, main


class CliTests(unittest.TestCase):
    def test_cuda_reexec_preparation_happens_before_console_redirection(self):
        order = []
        @contextmanager
        def console(root):
            order.append("console")
            yield object()
        with patch("jarvis.cli.load_config", return_value=SimpleNamespace(jarvis_dir=Path("/unused"))), patch("jarvis.speech.cuda.prepare", side_effect=lambda: order.append("cuda")), patch("jarvis.cli.runtime_console", side_effect=console), patch("jarvis.cli._run", return_value=0):
            self.assertEqual(main([]), 0)
        self.assertEqual(order, ["cuda", "console"])
    def test_cli_has_no_public_environment_scaffolding(self):
        self.assertNotIn("--init-env", build_parser().format_help())
        self.assertNotIn("--message", build_parser().format_help())

    def test_cli_only_configures_core_startup(self):
        args = build_parser().parse_args(["--env-file", "custom.env"])
        self.assertEqual(args.env_file, "custom.env")


if __name__ == "__main__":
    unittest.main()
