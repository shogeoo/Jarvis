import unittest

from jarvis.cli import build_parser


class CliTests(unittest.TestCase):
    def test_cli_only_configures_core_startup(self):
        args = build_parser().parse_args(["--env-file", "custom.env"])
        self.assertEqual(args.env_file, "custom.env")


if __name__ == "__main__":
    unittest.main()
