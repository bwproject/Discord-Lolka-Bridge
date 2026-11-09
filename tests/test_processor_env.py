from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from processor.server import load_env_file


class ProcessorEnvTests(unittest.TestCase):
    def test_loads_env_file_and_preserves_existing_environment(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            env_path = Path(directory) / ".env"
            env_path.write_text(
                "# comment\n"
                "PROCESSOR_IP=0.0.0.0\n"
                "PROCESSOR_PORT='9443'\n"
                "PROCESSOR_SECRET=secret-value-that-is-long-enough\n"
                "export EXTRA_SETTING=hello world\n",
                encoding="utf-8",
            )
            environment = {"PROCESSOR_PORT": "9555", "PROCESSOR_SECRET": ""}

            self.assertTrue(load_env_file(env_path, environment))
            self.assertEqual(environment["PROCESSOR_IP"], "0.0.0.0")
            self.assertEqual(environment["PROCESSOR_PORT"], "9555")
            self.assertEqual(
                environment["PROCESSOR_SECRET"],
                "secret-value-that-is-long-enough",
            )
            self.assertEqual(environment["EXTRA_SETTING"], "hello world")

    def test_missing_env_file_is_not_an_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / ".env"
            self.assertFalse(load_env_file(missing, {}))


if __name__ == "__main__":
    unittest.main()
