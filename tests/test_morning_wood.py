"""Run with: python3 -m unittest discover tests"""

import json
import re
import shutil
import sys
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import morning_wood as mw  # noqa: E402


class WorkdaysTest(unittest.TestCase):
    def test_skips_the_weekend(self):
        # Monday 26 October 2026: three workdays back is Wednesday 21 October.
        self.assertEqual(mw.workdays_before(date(2026, 10, 26), 3), date(2026, 10, 21))

    def test_zero_or_negative_is_today(self):
        self.assertEqual(mw.workdays_before(date(2026, 10, 26), 0), date(2026, 10, 26))
        self.assertEqual(mw.workdays_before(date(2026, 10, 26), -1), date(2026, 10, 26))


class ToolPrefixTest(unittest.TestCase):
    def test_server_name_is_normalised_like_claude_code(self):
        self.assertEqual(mw.tool_prefix("claude.ai Atlassian"), "mcp__claude_ai_Atlassian__")
        self.assertEqual(mw.tool_prefix("my-jira"), "mcp__my-jira__")


class NotificationLinesTest(unittest.TestCase):
    def test_first_three_bullets_as_plain_text(self):
        md = "# T\n- **[K-1](https://x/browse/K-1):** fix `a` now\n- second\n- third\n- fourth\n"
        self.assertEqual(mw.notification_lines(md), ["• K-1: fix a now", "• second", "• third"])

    def test_no_bullets(self):
        self.assertEqual(mw.notification_lines("# Nothing today\n"), ["Click to open."])


class ConfigTest(unittest.TestCase):
    def load(self, toml: str) -> mw.Config:
        with tempfile.TemporaryDirectory() as tmp:
            mw.CONFIG_FILE = Path(tmp) / "config.toml"
            mw.CONFIG_FILE.write_text(toml, encoding="utf-8")
            return mw.load_config()

    def test_paths_are_relative_to_home(self):
        cfg = self.load('output_dir = "briefs"\nworkdir = "~/code"\nmodel = "sonnet"\n')
        self.assertEqual(cfg.output_dir, mw.HOME / "briefs")
        self.assertEqual(cfg.workdir, mw.HOME / "code")
        self.assertEqual(cfg.model, "sonnet")

    def test_wrong_type_and_unknown_key_exit(self):
        with self.assertRaises(SystemExit):
            self.load('workdays_back = "3"\n')
        with self.assertRaises(SystemExit):
            self.load("jira_tools = [1]\n")
        with self.assertRaises(SystemExit):
            self.load('colour = "red"\n')


class SessionDigestTest(unittest.TestCase):
    SINCE = datetime(2026, 10, 20, tzinfo=timezone.utc)

    def digest(self, records) -> str | None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "abcdef12-3456.jsonl"
            path.write_text("\n".join(json.dumps(r) for r in records) + "\nnot json\n", encoding="utf-8")
            return mw.session_digest(path, self.SINCE)

    def test_keeps_recent_text_and_skips_the_rest(self):
        records = [
            {"type": "ai-title", "aiTitle": "first title"},
            {"type": "ai-title", "aiTitle": "latest title"},
            {"type": "user", "timestamp": "2026-10-26T08:00:00Z", "cwd": "/p", "gitBranch": "main",
             "message": {"content": "do the thing"}},
            {"type": "user", "timestamp": "2026-10-26T08:00:01Z", "message": {"content": "<command-name>/x</command-name>"}},
            {"type": "user", "timestamp": "2026-10-01T08:00:00Z", "message": {"content": "too old"}},
            {"type": "assistant", "timestamp": "2026-10-26T08:00:02Z", "isSidechain": True,
             "message": {"content": [{"type": "text", "text": "subagent"}]}},
            {"type": "assistant", "timestamp": "2026-10-26T08:00:03Z",
             "message": {"content": [{"type": "text", "text": "done"}, {"type": "tool_use"}]}},
            {"type": "assistant", "timestamp": "2026-10-26T08:00:04Z", "message": {"content": None}},
            {"type": "attachment"},
            "not a dict",
        ]
        self.assertEqual(
            self.digest(records),
            "## latest title\nproject: /p  branch: main\nuser asked:\n- do the thing\nassistant said last:\n- done",
        )

    def test_lists_pr_and_mr_urls_last_mentioned_last(self):
        mr = "https://gitlab.example.com/group/sub/project/-/merge_requests/42"
        pr = "https://github.com/owner/repo/pull/7"
        records = [
            {"type": "user", "timestamp": "2026-10-26T08:00:00Z", "message": {"content": f"look at {pr}"}},
            # A tool result: no text block, but its URL still counts.
            {"type": "user", "timestamp": "2026-10-26T08:00:01Z",
             "message": {"content": [{"type": "tool_result", "content": f"Created {mr}\n"}]}},
            {"type": "assistant", "timestamp": "2026-10-26T08:00:02Z",
             "message": {"content": [{"type": "text", "text": f"{pr} is merged"}]}},
            {"type": "user", "timestamp": "2026-10-01T08:00:00Z",
             "message": {"content": "old https://github.com/owner/repo/pull/1"}},
        ]
        self.assertTrue(self.digest(records).endswith(f"\nlinks: {mr} {pr}"))

    def test_custom_title_wins_and_empty_session_is_none(self):
        records = [
            {"type": "ai-title", "aiTitle": "generated"},
            {"type": "custom-title", "customTitle": "mine"},
            {"type": "user", "timestamp": "2026-10-26T08:00:00+02:00", "message": {"content": "hi"}},
        ]
        self.assertTrue(self.digest(records).startswith("## mine\n"))
        self.assertIsNone(self.digest([{"type": "user", "timestamp": "2026-10-01T08:00:00Z",
                                        "message": {"content": "old"}}]))


@unittest.skipUnless(shutil.which("pandoc"), "pandoc is not installed")
class RenderTest(unittest.TestCase):
    def render(self, markdown: str) -> str:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "brief.html"
            mw.render_html(markdown, out, date(2026, 1, 2))
            return out.read_text(encoding="utf-8")

    def test_hostile_markdown_is_inert(self):
        page = self.render(
            "# Morning brief, test\n\n"
            "## Jira\n\n"
            "- [K-1](https://e.x/browse/K-1) t <script>alert(1)</script> <img src=x onerror=alert(2)>\n"
            '- [a](javascript:alert(3)) [b](https://ok.example) [c](https://d.e){onmouseover="alert(4)"}\n'
            "- `code`{onclick=\"x\"} [span]{onclick=\"y\"}\n\n"
            "---\ntitle: pwned\n---\n\n"
            "text\n## Recent sessions\n\n- x\n"
        )
        self.assertNotIn("<script>alert", page)
        self.assertNotIn("<img", page)
        self.assertIsNone(re.search(r"<[^>]*\son\w+=", page), "an on* attribute got into a tag")
        self.assertNotIn("javascript:", page)
        self.assertIn('href="https://ok.example"', page)
        self.assertIn("<title>Morning brief 2026-01-02</title>", page)
        self.assertNotIn('class="title"', page)
        # The heading right after "text" is still a heading, so its tile gets its colour.
        self.assertIn('<section id="recent-sessions"', page)

    def test_csp_hash_matches_the_script(self):
        page = self.render("# Morning brief, test\n## Jira\n- x\n")
        self.assertEqual(page.count(f"<script>{mw.THEME_JS}</script>"), 1)
        self.assertIn(mw.content_security_policy(), page)
        self.assertNotIn("img-src", page)


if __name__ == "__main__":
    unittest.main()
