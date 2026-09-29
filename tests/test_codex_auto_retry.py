import datetime as dt
import importlib.util
import json
import os
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

    def test_ignores_a_notice_buried_under_a_lot_of_later_screen_content(self):
        now = dt.datetime(2026, 9, 28, 13, tzinfo=dt.timezone.utc)
        notice = "You've hit your usage limit. Try again at 9:00 AM."
        later_activity = " Worked for 1m 46s. Task summary and follow-up notes continue here. " * 20
        self.assertIsNone(plugin.parse_reset(notice + later_activity, now))

    def test_still_recognizes_a_notice_with_only_trailing_whitespace_or_prompt_chrome(self):
        now = dt.datetime(2026, 9, 28, 13, tzinfo=dt.timezone.utc)
        text = "You've hit your usage limit. Try again at 9:00 AM.\n\n> "
        self.assertEqual(plugin.parse_reset(text, now), now.replace(hour=9))


class ApprovalDialogTests(unittest.TestCase):
    def test_detects_yes_no_approval_prompt(self):
        screen = "Allow Codex to run `rm -rf build`?\n> 1. Yes\n  2. No, and tell Codex what to do differently"
        self.assertTrue(plugin.is_approval_dialog(screen))

    def test_plain_idle_quota_notice_is_not_an_approval_dialog(self):
        screen = "You've hit your usage limit. Try again at 9:00 AM."
        self.assertFalse(plugin.is_approval_dialog(screen))


class ReadyToSendTests(unittest.TestCase):
    def test_ready_when_pane_matches_and_idle(self):
        pane = {"pane_id": "w1:p1", "agent_status": "idle", "agent_session": {"value": "session-123"}}
        with mock.patch.object(plugin, "pane_lookup", return_value=pane), mock.patch.object(plugin, "pane_text", return_value="You've hit your usage limit. Try again at 9:00 AM."):
            ready, reason = plugin.ready_to_send("w1:p1", "session-123")
        self.assertTrue(ready)

    def test_not_ready_when_pane_closed(self):
        with mock.patch.object(plugin, "pane_lookup", return_value=None):
            ready, reason = plugin.ready_to_send("w1:p1", "session-123")
        self.assertFalse(ready)
        self.assertIn("closed", reason)

    def test_not_ready_when_session_changed(self):
        pane = {"pane_id": "w1:p1", "agent_status": "idle", "agent_session": {"value": "session-999"}}
        with mock.patch.object(plugin, "pane_lookup", return_value=pane):
            ready, reason = plugin.ready_to_send("w1:p1", "session-123")
        self.assertFalse(ready)
        self.assertIn("session", reason)

    def test_not_ready_when_already_working(self):
        pane = {"pane_id": "w1:p1", "agent_status": "working", "agent_session": {"value": "session-123"}}
        with mock.patch.object(plugin, "pane_lookup", return_value=pane):
            ready, reason = plugin.ready_to_send("w1:p1", "session-123")
        self.assertFalse(ready)
        self.assertIn("working", reason)

    def test_not_ready_when_approval_dialog_pending(self):
        pane = {"pane_id": "w1:p1", "agent_status": "blocked", "agent_session": {"value": "session-123"}}
        with mock.patch.object(plugin, "pane_lookup", return_value=pane), mock.patch.object(plugin, "pane_text", return_value="Allow Codex to run this command?\n1. Yes\n2. No"):
            ready, reason = plugin.ready_to_send("w1:p1", "session-123")
        self.assertFalse(ready)
        self.assertIn("approval", reason)


class SendAndConfirmTests(unittest.TestCase):
    def test_resumed_when_status_becomes_working_after_submission(self):
        working_pane = {"pane_id": "w1:p1", "agent_status": "working", "agent_session": {"value": "session-123"}}
        with mock.patch.object(plugin, "_agent_prompt_call", return_value={"result": {}}) as call, mock.patch.object(plugin, "pane_lookup", return_value=working_pane), mock.patch.object(plugin.time, "sleep"):
            outcome, next_reset = plugin.send_and_confirm("w1:p1", "session-123", "continue")
        self.assertEqual(outcome, "resumed")
        self.assertIsNone(next_reset)
        call.assert_called_once()
        self.assertEqual(call.call_args.args[0], "w1:p1")
        self.assertEqual(call.call_args.args[1], "continue")

    def test_cancelled_when_herdr_reports_the_agent_is_gone(self):
        with mock.patch.object(plugin, "_agent_prompt_call", return_value={"error": {"code": "agent_not_found"}}), mock.patch.object(plugin, "pane_lookup") as lookup:
            outcome, next_reset = plugin.send_and_confirm("w1:p1", "session-123", "continue")
        self.assertEqual(outcome, "cancelled")
        lookup.assert_not_called()

    def test_needs_intervention_when_herdr_rejects_a_blocked_agent(self):
        with mock.patch.object(plugin, "_agent_prompt_call", return_value={"error": {"code": "agent_blocked"}}), mock.patch.object(plugin, "pane_lookup") as lookup:
            outcome, next_reset = plugin.send_and_confirm("w1:p1", "session-123", "continue")
        self.assertEqual(outcome, "needs_intervention")
        lookup.assert_not_called()

    def test_new_limit_detected_after_a_stalled_submission(self):
        pane = {"pane_id": "w1:p1", "agent_status": "idle", "agent_session": {"value": "session-123"}}
        later = dt.datetime.now().astimezone() + dt.timedelta(hours=3)
        text = f"You've hit your usage limit. Try again at {later.strftime('%b %d, %Y %I:%M %p')}"
        with mock.patch.object(plugin, "_agent_prompt_call", return_value={"error": {"code": "agent_prompt_stalled"}}), mock.patch.object(plugin, "pane_lookup", return_value=pane), mock.patch.object(plugin, "pane_text", return_value=text):
            outcome, next_reset = plugin.send_and_confirm("w1:p1", "session-123", "continue")
        self.assertEqual(outcome, "new_limit")
        self.assertIsNotNone(next_reset)

    def test_unconfirmed_when_submission_times_out_with_no_visible_change(self):
        pane = {"pane_id": "w1:p1", "agent_status": "idle", "agent_session": {"value": "session-123"}}
        with mock.patch.object(plugin, "_agent_prompt_call", return_value={"error": {"code": "timeout"}}), mock.patch.object(plugin, "pane_lookup", return_value=pane), mock.patch.object(plugin, "pane_text", return_value="nothing new here"):
            outcome, next_reset = plugin.send_and_confirm("w1:p1", "session-123", "continue")
        self.assertEqual(outcome, "unconfirmed")
        self.assertIsNone(next_reset)

    def test_cancelled_when_the_pane_switched_sessions_during_submission(self):
        pane = {"pane_id": "w1:p1", "agent_status": "idle", "agent_session": {"value": "some-other-session"}}
        with mock.patch.object(plugin, "_agent_prompt_call", return_value={"result": {}}), mock.patch.object(plugin, "pane_lookup", return_value=pane):
            outcome, next_reset = plugin.send_and_confirm("w1:p1", "session-123", "continue")
        self.assertEqual(outcome, "cancelled")


class ResumeSessionTests(unittest.TestCase):
    def test_does_not_resume_a_session_already_working(self):
        with tempfile.TemporaryDirectory() as temp, mock.patch.dict(os.environ, {"HERDR_PLUGIN_STATE_DIR": temp}), mock.patch.object(plugin.time, "sleep"), mock.patch.object(plugin, "pane_lookup", return_value={"pane_id": "w1:p1", "agent_session": {"value": "session-123"}, "agent_status": "working"}), mock.patch.object(plugin, "send_and_confirm") as send:
            result = plugin.resume_session("session-123", temp, dt.datetime.now().astimezone().isoformat(), "w1:p1")
            self.assertEqual(result, 0)
            send.assert_not_called()
            record = json.loads((Path(temp) / "worker-session-123.json").read_text())
            self.assertEqual(record["phase"], "cancelled")

    def test_without_a_pane_id_the_worker_cancels_instead_of_using_a_headless_fallback(self):
        with tempfile.TemporaryDirectory() as temp, mock.patch.dict(os.environ, {"HERDR_PLUGIN_STATE_DIR": temp}), mock.patch.object(plugin.subprocess, "run") as run:
            result = plugin.resume_session("session-123", temp, dt.datetime.now().astimezone().isoformat(), "")
            self.assertEqual(result, 1)
            run.assert_not_called()
            record = json.loads((Path(temp) / "worker-session-123.json").read_text())
            self.assertEqual(record["phase"], "cancelled")

    def test_sends_through_the_original_pane_and_confirms_via_working_status(self):
        with tempfile.TemporaryDirectory() as temp:
            reset = dt.datetime.now().astimezone() - dt.timedelta(seconds=1)
            pane = {"pane_id": "w1:p1", "agent_status": "idle", "agent_session": {"value": "session-123"}}
            with mock.patch.dict(os.environ, {"HERDR_PLUGIN_STATE_DIR": temp}), mock.patch.object(plugin.time, "sleep"), mock.patch.object(plugin, "pane_lookup", return_value=pane), mock.patch.object(plugin, "pane_text", return_value="continuing..."), mock.patch.object(plugin, "send_and_confirm", return_value=("resumed", None)) as send:
                result = plugin.resume_session("session-123", temp, reset.isoformat(), "w1:p1")
            self.assertEqual(result, 0)
            send.assert_called_once_with("w1:p1", "session-123", plugin.CONTINUE_PROMPT)
            record = json.loads((Path(temp) / "worker-session-123.json").read_text())
            self.assertEqual(record["phase"], "resumed")
            self.assertEqual(record["last_reset_handled"], record["reset_at"])

    def test_pending_approval_needs_manual_intervention_instead_of_a_forced_send(self):
        with tempfile.TemporaryDirectory() as temp:
            reset = dt.datetime.now().astimezone() - dt.timedelta(seconds=1)
            pane = {"pane_id": "w1:p1", "agent_status": "blocked", "agent_session": {"value": "session-123"}}
            with mock.patch.dict(os.environ, {"HERDR_PLUGIN_STATE_DIR": temp}), mock.patch.object(plugin.time, "sleep"), mock.patch.object(plugin, "pane_lookup", return_value=pane), mock.patch.object(plugin, "pane_text", return_value="Allow Codex to run this command?\n1. Yes\n2. No"), mock.patch.object(plugin, "send_and_confirm") as send:
                result = plugin.resume_session("session-123", temp, reset.isoformat(), "w1:p1")
            self.assertEqual(result, 1)
            send.assert_not_called()
            record = json.loads((Path(temp) / "worker-session-123.json").read_text())
            self.assertEqual(record["phase"], "needs_intervention")

    def test_herdr_rejecting_a_blocked_agent_during_send_needs_manual_intervention(self):
        with tempfile.TemporaryDirectory() as temp:
            reset = dt.datetime.now().astimezone() - dt.timedelta(seconds=1)
            pane = {"pane_id": "w1:p1", "agent_status": "idle", "agent_session": {"value": "session-123"}}
            with mock.patch.dict(os.environ, {"HERDR_PLUGIN_STATE_DIR": temp}), mock.patch.object(plugin.time, "sleep"), mock.patch.object(plugin, "pane_lookup", return_value=pane), mock.patch.object(plugin, "pane_text", return_value="nothing suspicious"), mock.patch.object(plugin, "send_and_confirm", return_value=("needs_intervention", None)):
                result = plugin.resume_session("session-123", temp, reset.isoformat(), "w1:p1")
            self.assertEqual(result, 1)
            record = json.loads((Path(temp) / "worker-session-123.json").read_text())
            self.assertEqual(record["phase"], "needs_intervention")

    def test_a_lock_failure_is_recorded_as_needing_intervention_not_as_a_silent_success(self):
        with tempfile.TemporaryDirectory() as temp:
            reset = dt.datetime.now().astimezone() - dt.timedelta(seconds=1)
            pane = {"pane_id": "w1:p1", "agent_status": "idle", "agent_session": {"value": "session-123"}}
            with mock.patch.dict(os.environ, {"HERDR_PLUGIN_STATE_DIR": temp}), mock.patch.object(plugin.time, "sleep"), mock.patch.object(plugin, "pane_lookup", return_value=pane), mock.patch.object(plugin, "pane_text", return_value="nothing new here"), mock.patch.object(plugin, "send_and_confirm", return_value=("unconfirmed", None)):
                result = plugin.resume_session("session-123", temp, reset.isoformat(), "w1:p1")
            self.assertEqual(result, 1)
            record = json.loads((Path(temp) / "worker-session-123.json").read_text())
            self.assertEqual(record["phase"], "needs_intervention")
            self.assertNotIn("finished", record)

    def test_reaching_another_limit_reschedules_the_next_window(self):
        with tempfile.TemporaryDirectory() as temp:
            reset = dt.datetime.now().astimezone() - dt.timedelta(seconds=1)
            later = dt.datetime.now().astimezone() + dt.timedelta(hours=4)
            pane = {"pane_id": "w1:p1", "agent_status": "idle", "agent_session": {"value": "session-123"}}
            with mock.patch.dict(os.environ, {"HERDR_PLUGIN_STATE_DIR": temp}), mock.patch.object(plugin.time, "sleep"), mock.patch.object(plugin, "pane_lookup", return_value=pane), mock.patch.object(plugin, "pane_text", return_value="continuing..."), mock.patch.object(plugin, "send_and_confirm", side_effect=[("new_limit", later), ("resumed", None)]) as send:
                result = plugin.resume_session("session-123", temp, reset.isoformat(), "w1:p1")
            self.assertEqual(result, 0)
            self.assertEqual(send.call_count, 2)
            record = json.loads((Path(temp) / "worker-session-123.json").read_text())
            self.assertEqual(record["phase"], "resumed")
            self.assertEqual(record["source_reset_at"], reset.isoformat())


class LaunchWorkerTests(unittest.TestCase):
    def test_relative_reset_does_not_spawn_duplicate_active_workers(self):
        with tempfile.TemporaryDirectory() as temp:
            pane = {"pane_id": "w1:p1", "agent_session": {"value": "session-123"}, "cwd": temp}
            reset = dt.datetime.now().astimezone()
            with mock.patch.dict(os.environ, {"HERDR_PLUGIN_STATE_DIR": temp}), mock.patch.object(plugin.subprocess, "Popen", return_value=mock.Mock(pid=os.getpid())) as popen:
                plugin.launch_worker(pane, reset)
                plugin.launch_worker(pane, reset + dt.timedelta(seconds=12))
            self.assertEqual(popen.call_count, 1)

    def test_schedules_a_matching_blocked_pane_once(self):
        with tempfile.TemporaryDirectory() as temp:
            pane = {"pane_id": "w1:p1", "agent": "codex", "agent_status": "blocked", "agent_session": {"value": "session-123"}, "cwd": temp}
            reset = dt.datetime.now().astimezone() + dt.timedelta(hours=1)
            with mock.patch.dict(os.environ, {"HERDR_PLUGIN_STATE_DIR": temp}), mock.patch.object(plugin.subprocess, "Popen", return_value=mock.Mock(pid=os.getpid())) as popen:
                plugin.launch_worker(pane, reset)
                plugin.launch_worker(pane, reset)
            self.assertEqual(popen.call_count, 1)

    def test_requires_a_pane_id_to_schedule(self):
        with tempfile.TemporaryDirectory() as temp:
            pane = {"agent_session": {"value": "session-123"}, "cwd": temp}
            reset = dt.datetime.now().astimezone()
            with mock.patch.dict(os.environ, {"HERDR_PLUGIN_STATE_DIR": temp}), mock.patch.object(plugin.subprocess, "Popen") as popen:
                result = plugin.launch_worker(pane, reset)
            self.assertIn("pane ID", result)
            popen.assert_not_called()

    def test_a_confirmed_resume_does_not_get_rescheduled_by_the_same_stale_notice(self):
        with tempfile.TemporaryDirectory() as temp:
            pane = {"pane_id": "w1:p1", "agent_session": {"value": "session-123"}, "cwd": temp}
            reset = dt.datetime.now().astimezone()
            with mock.patch.dict(os.environ, {"HERDR_PLUGIN_STATE_DIR": temp}):
                record = plugin.worker_path("session-123")
                record.write_text(json.dumps({
                    "phase": "resumed",
                    "source_reset_at": reset.isoformat(),
                    "reset_at": reset.isoformat(),
                    "last_reset_handled": reset.isoformat(),
                }), encoding="utf-8")
                with mock.patch.object(plugin.subprocess, "Popen") as popen:
                    result = plugin.launch_worker(pane, reset)
            self.assertEqual(result, "reset already handled")
            popen.assert_not_called()

    def test_a_genuinely_new_reset_is_scheduled_even_after_a_previous_resume(self):
        with tempfile.TemporaryDirectory() as temp:
            pane = {"pane_id": "w1:p1", "agent_session": {"value": "session-123"}, "cwd": temp}
            old_reset = dt.datetime.now().astimezone() - dt.timedelta(hours=5)
            new_reset = dt.datetime.now().astimezone() + dt.timedelta(hours=1)
            with mock.patch.dict(os.environ, {"HERDR_PLUGIN_STATE_DIR": temp}):
                record = plugin.worker_path("session-123")
                record.write_text(json.dumps({
                    "phase": "resumed",
                    "source_reset_at": old_reset.isoformat(),
                    "reset_at": old_reset.isoformat(),
                    "last_reset_handled": old_reset.isoformat(),
                }), encoding="utf-8")
                with mock.patch.object(plugin.subprocess, "Popen", return_value=mock.Mock(pid=os.getpid())) as popen:
                    result = plugin.launch_worker(pane, new_reset)
            self.assertEqual(result, "scheduled")
            popen.assert_called_once()

    def test_a_pre_0_3_0_finished_flag_state_file_is_never_treated_as_clear_to_schedule(self):
        with tempfile.TemporaryDirectory() as temp:
            pane = {"pane_id": "w1:p1", "agent_session": {"value": "session-123"}, "cwd": temp}
            old_reset = dt.datetime.now().astimezone() - dt.timedelta(hours=5)
            new_reset = dt.datetime.now().astimezone() + dt.timedelta(hours=1)
            with mock.patch.dict(os.environ, {"HERDR_PLUGIN_STATE_DIR": temp}):
                record = plugin.worker_path("session-123")
                record.write_text(json.dumps({
                    "reset_at": old_reset.isoformat(),
                    "source_reset_at": old_reset.isoformat(),
                    "finished": True,
                }), encoding="utf-8")
                with mock.patch.object(plugin.subprocess, "Popen") as popen:
                    result = plugin.launch_worker(pane, new_reset)
            self.assertIn("legacy", result)
            popen.assert_not_called()

    def test_needs_intervention_for_the_same_reset_is_not_auto_retried(self):
        with tempfile.TemporaryDirectory() as temp:
            pane = {"pane_id": "w1:p1", "agent_session": {"value": "session-123"}, "cwd": temp}
            reset = dt.datetime.now().astimezone()
            with mock.patch.dict(os.environ, {"HERDR_PLUGIN_STATE_DIR": temp}):
                record = plugin.worker_path("session-123")
                record.write_text(json.dumps({
                    "phase": "needs_intervention",
                    "source_reset_at": reset.isoformat(),
                    "reset_at": reset.isoformat(),
                    "reason": "pane is waiting on an approval or question",
                }), encoding="utf-8")
                with mock.patch.object(plugin.subprocess, "Popen") as popen:
                    result = plugin.launch_worker(pane, reset)
            self.assertEqual(result, "needs manual intervention")
            popen.assert_not_called()


class InspectTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        patch = mock.patch.dict(os.environ, {"HERDR_PLUGIN_STATE_DIR": temp.name})
        patch.start()
        self.addCleanup(patch.stop)

    def test_relative_reset_is_stable_across_polls_and_a_confirmed_worker(self):
        now = dt.datetime(2026, 9, 26, 10, tzinfo=dt.timezone.utc)
        pane = {"pane_id": "w1:p1", "agent_status": "idle", "agent_session": {"value": "session-123"}, "cwd": str(plugin.state_dir())}
        with mock.patch.object(plugin, "pane_text", return_value="You've hit your usage limit. Try again in 2 hours."), mock.patch.object(plugin.subprocess, "Popen", return_value=mock.Mock(pid=os.getpid())) as popen:
            self.assertEqual(plugin.inspect_pane(pane, now=now), "scheduled")
            record = plugin.worker_path("session-123")
            state = json.loads(record.read_text())
            state["phase"] = "resumed"
            state["last_reset_handled"] = state["reset_at"]
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
