import datetime as dt
import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

spec = importlib.util.spec_from_file_location("retry_regression", Path(__file__).resolve().parents[1] / "codex_auto_retry.py")
plugin = importlib.util.module_from_spec(spec)
spec.loader.exec_module(plugin)

class PostSubmitQuotaTests(unittest.TestCase):
    def pane(self, status):
        return {"pane_id": "p1", "agent_status": status, "agent_session": {"value": "s1"}}

    def expired_notice(self):
        return "You've hit your usage limit. Try again at " + (dt.datetime.now().astimezone() - dt.timedelta(minutes=1)).isoformat()

    def test_transient_working_then_same_expired_limit_is_not_success(self):
        with mock.patch.object(plugin, "_agent_prompt_call", return_value={"result": {}}), mock.patch.object(plugin, "pane_lookup", side_effect=[self.pane("working"), self.pane("idle")]), mock.patch.object(plugin, "pane_text", return_value=self.expired_notice()), mock.patch.object(plugin.time, "sleep"):
            outcome, reset = plugin.send_and_confirm("p1", "s1", "continue")
        self.assertEqual(outcome, "new_limit")
        self.assertIsNotNone(reset)

    def test_idle_with_expired_reset_is_retriable_quota(self):
        with mock.patch.object(plugin, "_agent_prompt_call", return_value={"result": {}}), mock.patch.object(plugin, "pane_lookup", return_value=self.pane("idle")), mock.patch.object(plugin, "pane_text", return_value=self.expired_notice()):
            outcome, reset = plugin.send_and_confirm("p1", "s1", "continue")
        self.assertEqual(outcome, "new_limit")
        self.assertIsNotNone(reset)

    def test_working_requires_followup_observation(self):
        with mock.patch.object(plugin, "_agent_prompt_call", return_value={"result": {}}), mock.patch.object(plugin, "pane_lookup", return_value=self.pane("working")) as lookup, mock.patch.object(plugin.time, "sleep"):
            outcome, reset = plugin.send_and_confirm("p1", "s1", "continue")
        self.assertEqual(outcome, "resumed")
        self.assertGreater(lookup.call_count, 1)

class RepeatedExpiredResetTests(unittest.TestCase):
    def test_expired_reset_backoff_then_success(self):
        past = dt.datetime.now().astimezone() - dt.timedelta(minutes=1)
        with tempfile.TemporaryDirectory() as temp, mock.patch.dict(os.environ, {"HERDR_PLUGIN_STATE_DIR": temp}), mock.patch.object(plugin, "ready_to_send", return_value=(True, "ready")), mock.patch.object(plugin, "send_and_confirm", side_effect=[("new_limit", past), ("resumed", None)]), mock.patch.object(plugin.time, "sleep") as sleep:
            result = plugin.resume_session("s1", temp, past.isoformat(), "p1")
            record = json.loads(plugin.worker_path("s1").read_text())
        self.assertEqual(result, 0)
        self.assertEqual(record["phase"], "resumed")
        self.assertGreater(sleep.call_args_list[1].args[0], 120)
        self.assertEqual(record["last_reset_handled"], past.isoformat())

    def test_repeated_expired_limits_stop_at_chain_cap_without_success(self):
        past = dt.datetime.now().astimezone() - dt.timedelta(minutes=1)
        with tempfile.TemporaryDirectory() as temp, mock.patch.dict(os.environ, {"HERDR_PLUGIN_STATE_DIR": temp}), mock.patch.object(plugin, "ready_to_send", return_value=(True, "ready")), mock.patch.object(plugin, "send_and_confirm", return_value=("new_limit", past)) as send, mock.patch.object(plugin.time, "sleep"):
            result = plugin.resume_session("s1", temp, past.isoformat(), "p1")
            record = json.loads(plugin.worker_path("s1").read_text())
        self.assertEqual(result, 1)
        self.assertEqual(send.call_count, plugin.MAX_CHAIN_HOPS)
        self.assertEqual(record["phase"], "needs_intervention")
        self.assertNotIn("last_reset_handled", record)

class PostStartIdentityTests(unittest.TestCase):
    def test_session_switch_during_post_start_check_cancels(self):
        panes = [{"agent_status": "working", "agent_session": {"value": "s1"}}, {"agent_status": "idle", "agent_session": {"value": "s2"}}]
        with mock.patch.object(plugin, "_agent_prompt_call", return_value={"result": {}}), mock.patch.object(plugin, "pane_lookup", side_effect=panes), mock.patch.object(plugin.time, "sleep"):
            self.assertEqual(plugin.send_and_confirm("p1", "s1", "continue"), ("cancelled", None))

    def test_approval_after_transient_working_requires_intervention(self):
        panes = [{"agent_status": "working", "agent_session": {"value": "s1"}}, {"agent_status": "blocked", "agent_session": {"value": "s1"}}]
        with mock.patch.object(plugin, "_agent_prompt_call", return_value={"result": {}}), mock.patch.object(plugin, "pane_lookup", side_effect=panes), mock.patch.object(plugin.time, "sleep"):
            self.assertEqual(plugin.send_and_confirm("p1", "s1", "continue"), ("needs_intervention", None))
