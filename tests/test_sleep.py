"""Tests for scripts/sleep.py, run against a copy of examples/demo-memory."""

import datetime
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "skills", "memory-sleep", "scripts", "sleep.py")
DEMO = os.path.join(ROOT, "examples", "demo-memory")


class SleepTooling(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp())
        fresh = datetime.datetime.now(datetime.timezone.utc).isoformat()
        self.claude = os.path.join(self.tmp, "claude")
        self.codex = os.path.join(self.tmp, "codex")
        self.project = os.path.join(self.claude, "projects", "project")
        self.memory = os.path.join(self.project, "memory")
        shutil.copytree(DEMO, self.memory)
        self.state = os.path.join(self.tmp, "state")
        rows = [
            {
                "type": "user",
                "timestamp": fresh,
                "message": {
                    "content": "Always open markdown in the editor, not in the browser"
                },
            },
            {
                "type": "user",
                "timestamp": fresh,
                "message": {"content": "<system-reminder>injected</system-reminder>"},
            },
            {
                "type": "user",
                "isSidechain": True,
                "timestamp": fresh,
                "message": {"content": "subagent prompt"},
            },
            {
                "type": "user",
                "timestamp": fresh,
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
            os.path.join(self.project, "session-1.jsonl"), "w", encoding="utf-8"
        ) as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")
        os.makedirs(self.codex)
        with open(os.path.join(self.codex, "history.jsonl"), "w", encoding="utf-8") as f:
            f.write(json.dumps({"session_id": "abc12345-1", "ts": time.time() - 3600,
                                "text": "By default deploy to staging first"}) + "\n")
            f.write(json.dumps({"session_id": "abc12345-1", "ts": time.time() - 90 * 86400,
                                "text": "an old prompt outside the window"}) + "\n")
            f.write("not json\n")
        self.env = dict(os.environ, CLAUDE_CONFIG_DIR=self.claude, CODEX_HOME=self.codex)
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
            env=self.env,
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
        self.assertEqual(out["claude"]["turns"], 2)
        self.assertEqual(out["codex"]["turns"], 1)
        with open(out["file"], encoding="utf-8") as f:
            text = f.read()
        self.assertIn("never force push", text)
        self.assertNotIn("system-reminder", text)
        self.assertNotIn("subagent prompt", text)
        self.assertIn("deploy to staging first", text)
        self.assertNotIn("old prompt", text)

    def test_turns_source_can_be_limited(self):
        out = self.call("turns", self.run_dir, "--source", "codex")
        self.assertEqual((out["claude"]["turns"], out["codex"]["turns"]), (0, 1))

    def test_survey_discovers_memory_and_sources(self):
        out = self.call("survey")
        self.assertIn(os.path.realpath(self.memory), [os.path.realpath(m["memory"]) for m in out["memories"]])
        self.assertTrue(out["sources"]["claude_code"] and out["sources"]["codex"])
        self.assertEqual(out["sources"]["codex_turns_7d"], 1)

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

    # --- regressions found by an independent review ---

    def test_backup_through_symlink_contains_files(self):
        link = os.path.join(self.tmp, "linked-memory")
        os.symlink(self.memory, link)
        out = self.call("start", "--memory", link, "--minutes", "5")
        with tarfile.open(os.path.join(out["run"], "memory-before.tgz")) as tf:
            names = [m.name for m in tf.getmembers() if m.isfile()]
        self.assertIn("./MEMORY.md", names)
        self.assertIn("./projects/recipe-bot.md", names)

    def test_check_catches_short_number_and_extended_number(self):
        with open(os.path.join(self.memory, "MEMORY.md"), "a", encoding="utf-8") as f:
            f.write("- [garden-log](projects/garden-log.md) — лимит 12 грядок, бюджет 100\n")
        cand = self.candidate("лимит 12 грядок, бюджет 100", "бюджет 1000")
        res = self.call("check", self.run_dir, "--candidate", cand, ok=False, parse=False)
        lost = {x["fact"] for x in json.loads(res.stdout)["lost_facts"]}
        self.assertTrue({"12", "100"} <= lost, lost)

    def test_check_sees_links_with_anchor_and_spaces(self):
        with open(os.path.join(self.memory, "a note.md"), "w", encoding="utf-8") as f:
            f.write("note\n")
        with open(os.path.join(self.memory, "MEMORY.md"), "a", encoding="utf-8") as f:
            f.write("- [Статус](projects/tax-helper.md#status) — смотри раздел\n")
            f.write("- [Заметка](<a note.md>) — заметка\n")
        m = self.call("survey", "--memory", self.memory)["memories"][0]
        self.assertEqual((m["links"], m["broken_links"]), (10, []))
        cand = self.candidate("- [Заметка](<a note.md>) — заметка\n", "")
        with open(cand, encoding="utf-8") as f:
            cand_text = f.read()
        with open(cand, "w", encoding="utf-8") as f:
            f.write(cand_text.replace("- [Статус](projects/tax-helper.md#status) — смотри раздел\n", ""))
        res = self.call("check", self.run_dir, "--candidate", cand, ok=False, parse=False)
        self.assertEqual(
            json.loads(res.stdout)["links_dropped"],
            ["a note.md", "projects/tax-helper.md#status"],
        )

    def test_install_rejects_broken_encoding_even_when_waived(self):
        cand = os.path.join(self.run_dir, "MEMORY.new.md")
        with open(os.path.join(self.memory, "MEMORY.md"), "rb") as f:
            raw = f.read()
        with open(cand, "wb") as f:
            f.write(raw.replace("коротко".encode(), b"\xff\xfe"))
        res = self.call("install", self.run_dir, "--candidate", cand, "--accept-report", ok=False, parse=False)
        self.assertNotEqual(res.returncode, 0)
        with open(os.path.join(self.memory, "MEMORY.md"), "rb") as f:
            self.assertEqual(f.read(), raw)

    def write_rollout(self, name, meta, rows):
        d = os.path.join(self.codex, "sessions", "2026", "03", "06")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, name), "w", encoding="utf-8") as f:
            f.write(json.dumps({"type": "session_meta", "payload": meta}) + "\n")
            for ts, text in rows:
                f.write(json.dumps({"type": "response_item", "timestamp": ts, "payload": {
                    "type": "message", "role": "user",
                    "content": [{"type": "input_text", "text": text}]}}) + "\n")

    def test_turns_read_codex_rollouts_without_duplicates_and_subagents(self):
        fresh = datetime.datetime.now(datetime.timezone.utc).isoformat()
        self.write_rollout("rollout-a.jsonl", {"id": "abc12345-1"}, [
            (fresh, "# AGENTS.md instructions\n\ninjected"),
            (fresh, "<environment_context>cwd</environment_context>"),
            (fresh, "By default deploy to staging first"),
            (fresh, "Never edit generated files by hand"),
            ("2025-01-01T00:00:00Z", "a prompt from last year"),
        ])
        self.write_rollout("rollout-b.jsonl", {"id": "sess-b", "parent_thread_id": "sess-a"}, [
            (fresh, "prompt written by another agent"),
        ])
        out = self.call("turns", self.run_dir, "--source", "codex")
        with open(out["file"], encoding="utf-8") as f:
            text = f.read()
        self.assertEqual(out["codex"]["turns"], 2)
        self.assertEqual(text.count("deploy to staging first"), 1)
        self.assertIn("Never edit generated files", text)
        for bad in ("AGENTS.md instructions", "environment_context", "last year", "another agent"):
            self.assertNotIn(bad, text)

    def test_turns_only_run_needs_no_memory(self):
        out = self.call("start", "--turns-only", "--minutes", "5")
        self.assertIsNone(out["memory"])
        turns = self.call("turns", out["run"])
        self.assertEqual((turns["claude"]["turns"], turns["codex"]["turns"]), (2, 1))
        self.call("finish", out["run"])
        res = self.call("batches", out["run"], ok=False, parse=False)
        self.assertNotEqual(res.returncode, 0)

    def test_turns_window_applies_to_each_message_and_long_ones_are_cut(self):
        fresh = datetime.datetime.now(datetime.timezone.utc).isoformat()
        with open(os.path.join(self.project, "session-2.jsonl"), "w", encoding="utf-8") as f:
            f.write(json.dumps({"type": "user", "timestamp": "2025-01-01T00:00:00Z",
                                "message": {"content": "months-old instruction"}}) + "\n")
            f.write(json.dumps({"type": "user", "timestamp": fresh,
                                "message": {"content": "Remember: " + "x" * 3000}}) + "\n")
        out = self.call("turns", self.run_dir, "--source", "claude")
        with open(out["file"], encoding="utf-8") as f:
            text = f.read()
        self.assertNotIn("months-old", text)
        self.assertIn("Remember: xxx", text)
        self.assertIn("3010 chars in the original", text)

    def test_turns_drop_templates_and_in_session_duplicates(self):
        fresh = datetime.datetime.now(datetime.timezone.utc).isoformat()
        template = "You receive the first message of a session. " + "Write a title. " * 20
        for n in range(3):
            with open(os.path.join(self.project, f"auto-{n}.jsonl"), "w", encoding="utf-8") as f:
                for text in (template, "check it", "check it"):
                    f.write(json.dumps({"type": "user", "timestamp": fresh,
                                        "message": {"content": text}}) + "\n")
        out = self.call("turns", self.run_dir, "--source", "claude")
        with open(out["file"], encoding="utf-8") as f:
            text = f.read()
        self.assertNotIn("You receive the first message", text)
        self.assertEqual(text.count("check it"), 3)  # once per session: a repeated correction is kept
        self.assertEqual(out["dropped"], {"templates": 3, "duplicates": 3})

    def test_check_ignores_slash_between_plain_words(self):
        with open(os.path.join(self.memory, "MEMORY.md"), "a", encoding="utf-8") as f:
            f.write("- [garden-log](projects/garden-log.md) — текст лендингов/дашбордов без брифа\n")
        cand = self.candidate("текст лендингов/дашбордов без брифа", "тексты лендингов и дашбордов без брифа")
        rep = self.call("check", self.run_dir, "--candidate", cand)
        self.assertEqual(rep["lost_facts"], [])


if __name__ == "__main__":
    unittest.main()
