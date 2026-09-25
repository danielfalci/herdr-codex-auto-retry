import datetime as dt
import importlib.util
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock


PLUGIN_DIR = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("codex_auto_retry", PLUGIN_DIR / "codex_auto_retry.py")
plugin = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(plugin)


class ParseResetTests(unittest.TestCase):
    def test_parses_codex_absolute_reset_with_ordinal_day(self):
        message = "You've hit your usage limit. Try again at Oct 1st, 2026 1:16 AM."
        reset = plugin.parse_reset(message, dt.datetime(2026, 9, 25, 12, tzinfo=dt.timezone.utc))
        self.assertEqual(reset, dt.datetime(2026, 10, 1, 1, 16, tzinfo=dt.timezone.utc))

    def test_parses_relative_reset(self):
        now = dt.datetime(2026, 9, 25, 12, tzinfo=dt.timezone.utc)
        reset = plugin.parse_reset("You've hit your usage limit; try again in 2 hours 15 minutes.", now)
        self.assertEqual(reset, now + dt.timedelta(hours=2, minutes=15))

    def test_ignores_normal_usage_status_without_limit_error(self):
        self.assertIsNone(plugin.parse_reset("Usage: 95%. The rate limit resets at 1:30 AM."))


class ResumeTests(unittest.TestCase):
    def test_resumes_same_session_and_waits_for_a_later_quota_window(self):
        with tempfile.TemporaryDirectory() as temp:
            cwd = Path(temp)
            reset = dt.datetime.now().astimezone() - dt.timedelta(seconds=1)
            later = dt.datetime.now().astimezone() + dt.timedelta(hours=4)
            outputs = [
                subprocess.CompletedProcess([], 1, "", f"You've hit your usage limit. Try again at {later.strftime('%b %d, %Y %I:%M %p')}"),
                subprocess.CompletedProcess([], 0, "task completed", ""),
            ]
            with mock.patch.dict(os.environ, {"HERDR_PLUGIN_STATE_DIR": temp}), mock.patch.object(plugin.time, "sleep"), mock.patch.object(plugin.subprocess, "run", side_effect=outputs) as run:
                result = plugin.resume_session("session-123", str(cwd), reset.isoformat())

            self.assertEqual(result, 0)
            self.assertEqual(run.call_count, 2)
            for call in run.call_args_list:
                args, kwargs = call
                self.assertEqual(args[0][:5], ["codex", "exec", "resume", "--all", "session-123"])
                self.assertEqual(kwargs["cwd"], str(cwd))
                self.assertIn("Continue the interrupted task", args[0][5])
            record = json.loads((Path(temp) / "worker-session-123.json").read_text())
            self.assertTrue(record["finished"])

    def test_schedules_a_matching_blocked_pane_once(self):
        with tempfile.TemporaryDirectory() as temp:
            pane = {"agent": "codex", "agent_status": "blocked", "agent_session": {"value": "session-123"}, "cwd": temp}
            reset = dt.datetime.now().astimezone() + dt.timedelta(hours=1)
            with mock.patch.dict(os.environ, {"HERDR_PLUGIN_STATE_DIR": temp}), mock.patch.object(plugin.subprocess, "Popen", return_value=mock.Mock(pid=os.getpid())) as popen:
                plugin.launch_worker(pane, reset)
                plugin.launch_worker(pane, reset)
            self.assertEqual(popen.call_count, 1)


if __name__ == "__main__":
    unittest.main()
