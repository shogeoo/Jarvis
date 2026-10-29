import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from jarvis.infrastructure.storage_migration import migrate_capability_directories


class StorageMigrationTests(unittest.TestCase):
    def test_manager_preset_is_renamed_without_modifying_obsolete_memory(self):
        from jarvis.infrastructure.runtime_layout import ensure_runtime_layout
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            preset = root / "presets/capabilities_manager"
            preset.mkdir(parents=True)
            (preset / "person.txt").write_text("Custom manager instructions")
            memory = root / "memory/capabilities_manager/agent-00001"
            memory.mkdir(parents=True)
            (memory / "context.json").write_text('[{"role":"user","content":"Keep history"}]')
            ensure_runtime_layout(root)
            self.assertEqual((root / "presets/capability_manager/person.txt").read_text(), "Custom manager instructions")
            self.assertEqual((root / "memory/capabilities_manager/agent-00001/context.json").read_text(), '[{"role":"user","content":"Keep history"}]')
            ensure_runtime_layout(root)

    def test_complete_units_and_binary_data_survive_idempotent_move(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            unit = root / "modules" / "telegram"
            unit.mkdir(parents=True)
            (unit / "session.bin").write_bytes(b"user session")
            (unit / "module.py").write_text("# user code")
            migrate_capability_directories(root)
            migrate_capability_directories(root)
            self.assertEqual((root / "capabilities/modules/telegram/session.bin").read_bytes(), b"user session")
            self.assertFalse((root / "modules").exists())

    def test_conflict_does_not_move_or_overwrite_any_unit(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for relative in ("actions/first", "modules/telegram", "capabilities/modules/telegram"):
                (root / relative).mkdir(parents=True)
            with self.assertRaisesRegex(ValueError, "conflict"):
                migrate_capability_directories(root)
            self.assertTrue((root / "actions/first").is_dir())
            self.assertTrue((root / "modules/telegram").is_dir())

    def test_interrupted_migration_can_resume(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for relative in ("actions/first", "actions/second"):
                (root / relative).mkdir(parents=True)
            rename = Path.rename
            count = 0

            def interrupted(path, target):
                nonlocal count
                count += 1
                if count == 2:
                    raise OSError("simulated interruption")
                return rename(path, target)

            with patch.object(Path, "rename", interrupted):
                with self.assertRaises(OSError):
                    migrate_capability_directories(root)
            migrate_capability_directories(root)
            self.assertTrue((root / "capabilities/actions/first").exists())
            self.assertTrue((root / "capabilities/actions/second").exists())
