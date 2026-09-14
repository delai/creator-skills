"""Behavior checks for the sync transaction; model calls use a local fake."""

import argparse
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import sync_skills as sync


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / sync.SOURCE / "example"
        self.source.mkdir(parents=True)
        self.doc = "---\nname: example\ndescription: English description\n---\n\n# Title\n\nUse --dry-run and SUBTITLE_DEVICE.\n"
        (self.source / "SKILL.md").write_text(self.doc)
        (self.source / "tool.py").write_text("print('shared')\n")
        self.args = argparse.Namespace(check=False, skill=None, force=False, direction="en-to-zh",
                                       codex="codex", model=None, timeout=10)
        self.calls = 0

    def fake_codex(self, command, **kwargs):
        self.calls += 1
        stage = Path(command[command.index("--cd") + 1])
        output = Path(command[command.index("--output-last-message") + 1])
        pairs = json.loads(kwargs["input"].split("\nTranslate these files:\n", 1)[1])
        for pair in pairs:
            source = stage / pair["source"]
            target = stage / pair["target"]
            target.parent.mkdir(parents=True, exist_ok=True)
            text = source.read_text()
            replacements = [("English description", "中文说明"), ("Title", "标题"),
                            ("Use", "使用"), ("and", "和")]
            if pair["source"].startswith("skills-zh-CN/"):
                replacements = [(b, a) for a, b in replacements]
            for old, new in replacements:
                text = text.replace(old, new)
            target.write_text(text)
        output.write_text(json.dumps({"translated": [p["target"] for p in pairs], "notes": []}))
        return subprocess.CompletedProcess(command, 0)

    def run_sync(self, side_effect=None):
        with patch.object(sync.shutil, "which", return_value="/fake/codex"), \
             patch.object(sync.subprocess, "run", side_effect=side_effect or self.fake_codex):
            return sync.run(self.args, self.root)

    def test_root_release_archives_are_not_shared_resources(self):
        target = self.root / sync.TARGET
        target.mkdir()
        archive = target / "example.zip"
        archive.write_bytes(b"Chinese release archive")
        (self.root / sync.SOURCE / "example.zip").write_bytes(b"English release archive")
        (self.source / "assets").mkdir()
        (self.source / "assets" / "resource.zip").write_bytes(b"shared ZIP resource")
        self.assertEqual(self.run_sync(), 0)
        self.assertEqual(archive.read_bytes(), b"Chinese release archive")
        self.assertEqual((target / "example/assets/resource.zip").read_bytes(), b"shared ZIP resource")
        self.args.check = True
        self.assertEqual(self.run_sync(), 0)

    def test_success_then_no_model_call_and_check(self):
        self.assertEqual(self.run_sync(), 0)
        target = self.root / sync.TARGET / "example"
        self.assertEqual((target / "tool.py").read_bytes(), (self.source / "tool.py").read_bytes())
        self.assertIn("中文说明", (target / "SKILL.md").read_text())
        self.assertEqual(self.run_sync(), 0)
        self.assertEqual(self.calls, 1)
        self.args.check = True
        self.assertEqual(self.run_sync(), 0)
        self.assertEqual(self.calls, 1)

    def test_context_and_translation_edits_become_stale(self):
        self.run_sync()
        (self.source / "tool.py").write_text("print('new context')\n")
        self.args.check = True
        self.assertEqual(self.run_sync(), 1)
        self.args.check = False
        self.run_sync()
        target = self.root / sync.TARGET / "example" / "SKILL.md"
        target.write_text(target.read_text() + "\nManual English edit\n")
        self.args.check = True
        self.assertEqual(self.run_sync(), 1)

    def test_failure_keeps_previous_distribution_and_state(self):
        self.run_sync()
        before = sync.inventory(self.root / sync.TARGET)
        state = (self.root / sync.STATE).read_bytes()
        (self.source / "tool.py").write_text("print('changed')\n")
        with self.assertRaisesRegex(ValueError, "Codex exited"):
            self.run_sync(lambda cmd, **kwargs: subprocess.CompletedProcess(cmd, 7))
        self.assertEqual(sync.inventory(self.root / sync.TARGET), before)
        self.assertEqual((self.root / sync.STATE).read_bytes(), state)

    def test_incomplete_translation_is_rejected(self):
        def incomplete(command, **kwargs):
            result = self.fake_codex(command, **kwargs)
            stage = Path(command[command.index("--cd") + 1])
            (stage / sync.TARGET / "example" / "SKILL.md").write_text("# Summary\n")
            return result
        with self.assertRaises(ValueError):
            self.run_sync(incomplete)
        self.assertFalse((self.root / sync.STATE).exists())
        self.assertFalse((self.root / sync.TARGET).exists())

    def test_translator_cannot_publish_unlisted_edits(self):
        def unrelated(command, **kwargs):
            result = self.fake_codex(command, **kwargs)
            stage = Path(command[command.index("--cd") + 1])
            (stage / sync.SOURCE / "example" / "tool.py").write_text("wrong\n")
            return result
        with self.assertRaisesRegex(ValueError, "unlisted"):
            self.run_sync(unrelated)
        self.assertEqual((self.source / "tool.py").read_text(), "print('shared')\n")

    def test_concurrent_source_edit_is_not_overwritten(self):
        def concurrent(command, **kwargs):
            result = self.fake_codex(command, **kwargs)
            (self.source / "tool.py").write_text("human edit\n")
            return result
        with self.assertRaisesRegex(ValueError, "Repository changed"):
            self.run_sync(concurrent)
        self.assertEqual((self.source / "tool.py").read_text(), "human edit\n")
        self.assertFalse((self.root / sync.STATE).exists())

    def test_obsolete_file_is_reported_without_deleting(self):
        self.run_sync()
        target = self.root / sync.TARGET / "example" / "tool.py"
        (self.source / "tool.py").unlink()
        with self.assertRaisesRegex(ValueError, "Target-only"):
            self.run_sync()
        self.assertTrue(target.exists())

    def test_removed_skill_is_reported(self):
        self.run_sync()
        import shutil
        shutil.rmtree(self.source)
        with self.assertRaisesRegex(ValueError, "Target-only"):
            self.run_sync()

    def test_scoped_sync_skips_other_skills(self):
        other = self.root / sync.SOURCE / "other"
        other.mkdir()
        (other / "SKILL.md").write_text(self.doc.replace("name: example", "name: other"))
        self.args.skill = "example"
        self.run_sync()
        self.assertTrue((self.root / sync.TARGET / "example" / "SKILL.md").exists())
        self.assertFalse((self.root / sync.TARGET / "other").exists())
        self.assertEqual(self.calls, 1)

    def test_switching_direction_without_edits_does_not_retranslate(self):
        self.run_sync()
        self.args.direction = "zh-to-en"
        self.assertEqual(self.run_sync(), 0)
        self.assertEqual(self.calls, 1)
        self.args.check = True
        self.assertEqual(self.run_sync(), 0)

    def test_reverse_sync_uses_chinese_edits_and_copies_shared_files(self):
        self.run_sync()
        chinese = self.root / "skills-zh-CN" / "example"
        (chinese / "SKILL.md").write_text((chinese / "SKILL.md").read_text() + "\n中文说明\n")
        (chinese / "tool.py").write_text("print('reverse change')\n")
        self.args.direction = "zh-to-en"
        self.assertEqual(self.run_sync(), 0)
        english = self.root / "skills" / "example"
        self.assertTrue((english / "SKILL.md").read_text().endswith("English description\n"))
        self.assertEqual((english / "tool.py").read_bytes(), (chinese / "tool.py").read_bytes())
        self.args.direction = "en-to-zh"
        self.args.check = True
        self.assertEqual(self.run_sync(), 0)

    def test_reverse_sync_failure_keeps_english_files(self):
        self.run_sync()
        before = sync.inventory(self.root / "skills")
        chinese = self.root / "skills-zh-CN" / "example" / "tool.py"
        chinese.write_text("print('changed')\n")
        self.args.direction = "zh-to-en"
        with self.assertRaisesRegex(ValueError, "Codex exited"):
            self.run_sync(lambda cmd, **kwargs: subprocess.CompletedProcess(cmd, 9))
        self.assertEqual(sync.inventory(self.root / "skills"), before)

    def test_legacy_state_is_not_mistaken_for_a_verified_pair(self):
        self.run_sync()
        (self.root / sync.STATE).write_text(json.dumps({"version": 1, "groups": {}}))
        self.args.check = True
        self.assertEqual(self.run_sync(), 1)

    def test_missing_cli_does_not_write_files(self):
        with patch.object(sync.shutil, "which", return_value=None):
            with self.assertRaisesRegex(ValueError, "not found"):
                sync.run(self.args, self.root)
        self.assertFalse((self.root / sync.TARGET).exists())


if __name__ == "__main__":
    unittest.main()
