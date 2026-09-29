import datetime as dt
import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest import mock

spec = importlib.util.spec_from_file_location('retry', Path(__file__).resolve().parents[1] / 'codex_auto_retry.py')
plugin = importlib.util.module_from_spec(spec)
spec.loader.exec_module(plugin)

NOTICE = "You've hit your usage limit. Upgrade to Pro (https://chatgpt.com/explore/pro), visit https://chatgpt.com/codex/settings/usage to purchase more credits or try again at 4:42 PM."

class MissingSessionTests(unittest.TestCase):
    def pane(self):
        return dict(agent='codex', agent_status='idle', pane_id='w1:p13', terminal_id='term123', agent_session=None)

    def make_proc(self, root, pid='42', pane='w1:p13', start='100', args=b'codex\0'):
        p = root / pid
        p.mkdir(exist_ok=True)
        (p/'comm').write_text('codex\n')
        (p/'environ').write_bytes(f'HERDR_PANE_ID={pane}\0'.encode())
        (p/'cmdline').write_bytes(args)
        (p/'stat').write_text(f'{pid} (codex) ' + ' '.join(['S'] + ['0'] * 18 + [start]))

    def test_process_identity_and_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_proc(root)
            first = plugin.process_key(self.pane(), root)
            self.assertEqual(first, 'process-term123-42-100')
            self.make_proc(root, start='101')
            self.assertNotEqual(plugin.process_key(self.pane(), root), first)

    def test_other_pane_and_exec_child_do_not_match(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_proc(root, pane='w2:p1')
            self.make_proc(root, pid='43', args=b'codex\0exec\0')
            self.assertIsNone(plugin.process_key(self.pane(), root))

    def test_ambiguous_processes_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_proc(root)
            self.make_proc(root, pid='43')
            self.assertIsNone(plugin.process_key(self.pane(), root))

    def test_screenshot_notice_schedules_at_1644_without_session_id(self):
        now = dt.datetime(2026,9,29,16,44,tzinfo=dt.timezone(dt.timedelta(hours=-3)))
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(plugin, 'state_dir', return_value=Path(tmp)), mock.patch.object(plugin, 'process_key', return_value='process-term123-42-100'), mock.patch.object(plugin, 'pane_text', return_value=NOTICE), mock.patch.object(plugin, 'launch_worker', return_value='scheduled') as launch:
            self.assertEqual(plugin.inspect_pane(self.pane(), now=now), 'scheduled')
            self.assertEqual(launch.call_args.args[1], now.replace(minute=42))

    def test_fallback_rechecks_quota_before_sending(self):
        with mock.patch.object(plugin, 'pane_lookup', return_value=self.pane()), mock.patch.object(plugin, 'process_key', return_value='process-term123-42-100'), mock.patch.object(plugin, 'pane_text', return_value='Task completed.'):
            self.assertEqual(plugin.ready_to_send('w1:p13', 'process-term123-42-100'), (False, 'quota notice no longer present'))

    def test_explicit_session_id_still_preferred(self):
        pane = self.pane()
        pane['agent_session'] = {'value':'session123'}
        with mock.patch.object(plugin, 'process_key') as fallback:
            self.assertEqual(plugin.session_key(pane), 'session123')
            fallback.assert_not_called()
