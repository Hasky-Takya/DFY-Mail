"""局部暂停、缺日志保护与认证异常链，全部临时mock。"""

import ssl
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from test_receive import receive, memory_module as memory, message, mock_send, SUMMARY, retry_policy


class RecoveryEdgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'dialog.json'
        self.box, self.bot = MagicMock(), MagicMock()
        self.box.get_mailbox_cursor.return_value = (10, 0)
        self.box.read_user_emails_since.return_value = ((10, 1), [message(1)])
        self.box.send_email.side_effect = mock_send
        self.bot.ask.side_effect = lambda q, **kw: SUMMARY if kw.get('json_mode') else 'answer'
        self.now = [1000.0]

    def make(self):
        return receive.MailReceiver(self.box, self.bot, 'persona', memory_path=self.path,
                                    summary_prompt='summary', clock=lambda: self.now[0])

    def test_imap_pause_still_processes_already_received_task(self):
        item = self.make()
        item.start()
        item.store.ingest(item.account, (10, 1), [dict(message(1), received_at='2026-10-27T10:00:00+00:00', received_at_source='imap_internaldate')])
        item.cursor = (10, 1)
        item.paused = 'imap_auth_failed'
        item.store.set_control('service:imap:paused', item.paused)
        self.assertFalse(item.poll_once())
        self.box.read_user_emails_since.assert_not_called()
        self.box.send_email.assert_called_once()

    def test_restart_auth_pause_not_bypassed(self):
        first = self.make()
        first.start()
        first.store.set_control('service:imap:paused', 'imap_auth_failed')
        self.box.get_mailbox_cursor.reset_mock()
        second = self.make()
        second.start()
        self.box.get_mailbox_cursor.assert_not_called()
        self.assertEqual(second.cursor, (10, 0))

    def test_missing_dialog_with_bound_plan_blocks_requeue(self):
        item = self.make()
        item.start()
        row = item.store.ingest(item.account, (10, 1), [message(1)])[0]
        item.store.save(row['id'], row['payload'], status='sending', dialog_id='bound')
        self.assertIsNone(item.recover_memory())
        self.assertEqual(item.store.get(row['id'])['status'], 'sending')
        self.box.send_email.assert_not_called()

    def test_smtp_pause_blocks_only_sending(self):
        item = self.make()
        item.start()
        item.store.set_control('service:smtp:paused', 'smtp_auth_failed')
        item.poll_once()
        self.box.send_email.assert_not_called()
        self.assertEqual(item.cursor, (10, 1))
        self.assertEqual(len(item.store.list_tasks(account=item.account)), 1)

    def test_cert_in_wrapped_api_error_is_fatal(self):
        error = receive.APIConnectionError()
        error.__cause__ = ssl.SSLCertVerificationError('sensitive')
        self.assertEqual(retry_policy.classify_chain(error), ('fatal', 'tls_verification_failed'))


if __name__ == '__main__':
    unittest.main()
