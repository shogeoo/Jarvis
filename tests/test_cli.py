import unittest

from jarvis.cli import build_parser


class CliTests(unittest.TestCase):
    def test_cli_has_no_public_environment_scaffolding(self):
        self.assertNotIn("--init-env", build_parser().format_help())

    def test_cli_only_configures_core_startup(self):
        args = build_parser().parse_args(["--env-file", "custom.env"])
        self.assertEqual(args.env_file, "custom.env")


if __name__ == "__main__":
    unittest.main()
