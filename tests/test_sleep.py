"""Tests for scripts/sleep.py, run against a copy of examples/demo-memory."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "skills", "memory-sleep", "scripts", "sleep.py")
DEMO = os.path.join(ROOT, "examples", "demo-memory")


class SleepTooling(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.memory = os.path.join(self.tmp, "project", "memory")
        shutil.copytree(DEMO, self.memory)
        self.state = os.path.join(self.tmp, "state")
        rows = [
            {
                "type": "user",
                "timestamp": "2026-03-06T10:00:00Z",
                "message": {
                    "content": "Always open markdown in the editor, not in the browser"
                },
            },
            {
                "type": "user",
                "timestamp": "2026-03-06T10:01:00Z",
                "message": {"content": "<system-reminder>injected</system-reminder>"},
            },
            {
                "type": "user",
                "isSidechain": True,
                "timestamp": "2026-03-06T10:02:00Z",
                "message": {"content": "subagent prompt"},
            },
            {
                "type": "user",
                "timestamp": "2026-03-06T10:03:00Z",
                "message": {
                    "content": [
                        {"type": "tool_result", "content": "output"},
                        {"type": "text", "text": "I told you: never force push"},
                    ]
                },
            },
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "ok"}]},
            },
        ]
        with open(
            os.path.join(self.tmp, "project", "session-1.jsonl"), "w", encoding="utf-8"
        ) as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")
        self.run_dir = self.call("start", "--memory", self.memory, "--minutes", "15")[
            "run"
        ]

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def call(self, *args, ok=True, parse=True):
        res = subprocess.run(
            [sys.executable, SCRIPT, "--state", self.state, *args],
            capture_output=True,
            text=True,
        )
        if ok:
            self.assertEqual(res.returncode, 0, res.stderr)
        if not parse:
            return res
        return json.loads(res.stdout)

    def test_survey_measures_index(self):
        out = self.call("survey", "--memory", self.memory)
        m = out["memories"][0]
        self.assertEqual(m["links"], 8)
        self.assertEqual(m["broken_links"], [])
        self.assertEqual(m["lines_over_220"], 1)
        self.assertEqual(m["transcripts_7d"], 1)
        self.assertEqual(out["last_run"]["memory"], self.memory)

    def test_start_makes_backup_and_log(self):
        for name in (
            "memory-before.tgz",
            "MEMORY.before.md",
            "metrics-before.json",
            "phases.log",
        ):
            self.assertTrue(os.path.exists(os.path.join(self.run_dir, name)), name)
        res = self.call("log", self.run_dir, "phase done", parse=False)
        self.assertIn("left", res.stdout)
        with open(os.path.join(self.run_dir, "phases.log"), encoding="utf-8") as f:
            self.assertIn("phase done", f.read())

    def test_hygiene_finds_problems(self):
        rep = self.call("hygiene", self.run_dir)
        self.assertEqual(rep["no_frontmatter"], ["note_without_frontmatter.md"])
        self.assertIn("note_without_frontmatter.md", rep["not_in_index"])
        self.assertEqual(rep["not_utf8"], [])

    def test_batches_cover_every_entry(self):
        out = self.call("batches", self.run_dir, "--n", "3")
        self.assertEqual(sum(b["entries"] for b in out["batches"]), 8)
        self.assertEqual(out["unlinked_lines"], 1)

    def test_pairs_are_reproducible(self):
        a = self.call(
            "pairs", self.run_dir, "--n", "2", "--seed", "1", parse=False
        ).stdout
        b = self.call(
            "pairs", self.run_dir, "--n", "2", "--seed", "1", parse=False
        ).stdout
        self.assertEqual(a, b)
        self.assertEqual(a.count(" + "), 2)

    def test_turns_keep_only_owner_text(self):
        out = self.call("turns", self.run_dir, "--days", "7")
        self.assertEqual(out["turns"], 2)
        with open(out["file"], encoding="utf-8") as f:
            text = f.read()
        self.assertIn("never force push", text)
        self.assertNotIn("system-reminder", text)
        self.assertNotIn("subagent prompt", text)

    def candidate(self, old, new):
        with open(os.path.join(self.memory, "MEMORY.md"), encoding="utf-8") as f:
            text = f.read()
        self.assertIn(old, text)
        path = os.path.join(self.run_dir, "MEMORY.new.md")
        with open(path, "w", encoding="utf-8") as f:
            f.write(text.replace(old, new))
        return path

    def test_check_reports_fact_missing_from_target(self):
        # the hash a1b2c3d lives only in the index line, not in projects/recipe-bot.md
        cand = self.candidate(", последний коммит a1b2c3d от 2026-03-02, 41 тест", "")
        res = self.call(
            "check", self.run_dir, "--candidate", cand, ok=False, parse=False
        )
        self.assertEqual(res.returncode, 1)
        lost = {x["fact"] for x in json.loads(res.stdout)["lost_facts"]}
        self.assertIn("a1b2c3d", lost)
        self.assertNotIn("2026-03-02", lost)  # the date is in the linked file

    def test_check_rejects_dropped_link(self):
        cand = self.candidate(
            "- [old-prototype](projects/old-prototype.md) — первый набросок бота; заменен recipe-bot\n",
            "",
        )
        res = self.call(
            "check", self.run_dir, "--candidate", cand, ok=False, parse=False
        )
        self.assertEqual(
            json.loads(res.stdout)["links_dropped"], ["projects/old-prototype.md"]
        )

    def test_install_replaces_index_and_keeps_copy(self):
        cand = self.candidate(", 41 тест", "")  # 41 is recorded in the linked file
        self.call("check", self.run_dir, "--candidate", cand)
        self.call("install", self.run_dir, "--candidate", cand, parse=False)
        with open(os.path.join(self.memory, "MEMORY.md"), encoding="utf-8") as f:
            self.assertNotIn("41 тест", f.read())
        self.assertTrue(
            os.path.exists(os.path.join(self.run_dir, "MEMORY.pre-install-1.md"))
        )
        out = self.call("finish", self.run_dir)
        before, after = out["before_after"]["index_bytes"]
        self.assertLess(after, before)

    def test_install_refuses_failed_check(self):
        cand = self.candidate("a1b2c3d", "")
        res = self.call(
            "install", self.run_dir, "--candidate", cand, ok=False, parse=False
        )
        self.assertNotEqual(res.returncode, 0)
        with open(os.path.join(self.memory, "MEMORY.md"), encoding="utf-8") as f:
            self.assertIn("a1b2c3d", f.read())


if __name__ == "__main__":
    unittest.main()
