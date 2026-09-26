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
    def test_time_only_uses_today_and_keeps_overdue_reset(self):
        now = dt.datetime(2026, 9, 26, 10, tzinfo=dt.timezone.utc)
        self.assertEqual(plugin.parse_reset("You've hit your usage limit. Try again at 9:00 AM.", now), now.replace(hour=9))

    def test_month_date_keeps_yesterdays_reset(self):
        now = dt.datetime(2026, 9, 26, 10, tzinfo=dt.timezone.utc)
        self.assertEqual(plugin.parse_reset("You've hit your usage limit. Try again at Sep 25 11:00 PM.", now), now.replace(day=25, hour=23))

    def test_latest_limit_notice_wins(self):
        now = dt.datetime(2026, 9, 26, 10, tzinfo=dt.timezone.utc)
        text = "You've hit your usage limit. Try again at 9:00 AM. You've hit your usage limit. Try again at 11:00 AM."
        self.assertEqual(plugin.parse_reset(text, now), now.replace(hour=11))

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
    def test_does_not_resume_a_session_already_working(self):
        with tempfile.TemporaryDirectory() as temp, mock.patch.dict(os.environ, {"HERDR_PLUGIN_STATE_DIR": temp}), mock.patch.object(plugin.time, "sleep"), mock.patch.object(plugin, "pane_list", return_value=[{"pane_id": "w1:p1", "agent_session": {"value": "session-123"}, "agent_status": "working"}]), mock.patch.object(plugin.subprocess, "run") as run:
            self.assertEqual(plugin.resume_session("session-123", temp, dt.datetime.now().astimezone().isoformat(), "w1:p1"), 0)
            run.assert_not_called()

    def test_relative_reset_does_not_spawn_duplicate_active_workers(self):
        with tempfile.TemporaryDirectory() as temp:
            pane = {"agent_session": {"value": "session-123"}, "cwd": temp}
            reset = dt.datetime.now().astimezone()
            with mock.patch.dict(os.environ, {"HERDR_PLUGIN_STATE_DIR": temp}), mock.patch.object(plugin.subprocess, "Popen", return_value=mock.Mock(pid=os.getpid())) as popen:
                plugin.launch_worker(pane, reset)
                plugin.launch_worker(pane, reset + dt.timedelta(seconds=12))
            self.assertEqual(popen.call_count, 1)

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


class InspectTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        patch = mock.patch.dict(os.environ, {"HERDR_PLUGIN_STATE_DIR": temp.name})
        patch.start()
        self.addCleanup(patch.stop)

    def test_relative_reset_is_stable_across_polls_and_finished_worker(self):
        now = dt.datetime(2026, 9, 26, 10, tzinfo=dt.timezone.utc)
        pane = {"pane_id": "w1:p1", "agent_status": "idle", "agent_session": {"value": "session-123"}, "cwd": str(plugin.state_dir())}
        with mock.patch.object(plugin, "pane_text", return_value="You've hit your usage limit. Try again in 2 hours."), mock.patch.object(plugin.subprocess, "Popen", return_value=mock.Mock(pid=os.getpid())) as popen:
            self.assertEqual(plugin.inspect_pane(pane, now=now), "scheduled")
            record = plugin.worker_path("session-123")
            state = json.loads(record.read_text())
            state["finished"] = True
            record.write_text(json.dumps(state))
            self.assertEqual(plugin.inspect_pane(pane, now=now + dt.timedelta(hours=3)), "reset already handled")
            self.assertEqual(popen.call_count, 1)

    def test_idle_done_blocked_and_unknown_schedule_overdue_reset(self):
        now = dt.datetime(2026, 9, 26, 10, tzinfo=dt.timezone.utc)
        for status in ("idle", "done", "blocked", "unknown"):
            with self.subTest(status=status), mock.patch.object(plugin, "pane_text", return_value="You've hit your usage limit. Try again at 9:00 AM."), mock.patch.object(plugin, "launch_worker", return_value="scheduled") as launch:
                pane = {"pane_id": "w1:p1", "agent_status": status, "agent_session": {"value": "session-123"}}
                self.assertEqual(plugin.inspect_pane(pane, now=now), "scheduled")
                self.assertEqual(launch.call_args.args[1], now.replace(hour=9))

    def test_working_pane_is_not_read_or_scheduled(self):
        with mock.patch.object(plugin, "pane_text") as read, mock.patch.object(plugin, "launch_worker") as launch:
            plugin.inspect_pane({"agent_status": "working"})
            read.assert_not_called()
            launch.assert_not_called()

    def test_normal_status_and_old_reset_do_not_schedule(self):
        now = dt.datetime(2026, 9, 26, 10, tzinfo=dt.timezone.utc)
        pane = {"pane_id": "w1:p1", "agent_status": "idle", "agent_session": {"value": "session-123"}}
        for text in ("Usage: 95%. The rate limit resets at 1:30 AM.", "You've hit your usage limit. Try again at Sep 24, 2026 9:00 AM."):
            with self.subTest(text=text), mock.patch.object(plugin, "pane_text", return_value=text), mock.patch.object(plugin, "launch_worker") as launch:
                plugin.inspect_pane(pane, now=now)
                launch.assert_not_called()

    def test_diagnose_never_launches_worker(self):
        pane = {"pane_id": "w1:p1", "agent_status": "idle", "agent_session": {"value": "session-123"}}
        with mock.patch.object(plugin, "pane_text", return_value="You've hit your usage limit. Try again in 2 hours."), mock.patch.object(plugin, "launch_worker") as launch:
            self.assertIn("eligible", plugin.inspect_pane(pane, dry_run=True))
            launch.assert_not_called()

    def test_reads_unwrapped_terminal_text(self):
        with mock.patch.object(plugin, "herdr", return_value="text") as read:
            plugin.pane_text("w1:p1")
            self.assertIn("recent-unwrapped", read.call_args.args)


if __name__ == "__main__":
    unittest.main()
