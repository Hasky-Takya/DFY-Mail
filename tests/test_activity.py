"""用户活动离线测试；复用 test_receive 的模块和 IMAP mock，不读取真实配置。"""

import unittest
from unittest.mock import MagicMock, call, patch

import test_receive


mail = test_receive.mail
HEADER_FETCH = (
    "(INTERNALDATE BODY.PEEK[HEADER.FIELDS "
    "(FROM AUTO-SUBMITTED PRECEDENCE RETURN-PATH LIST-ID CONTENT-TYPE)])"
)


def headers(sender="User <user@example.com>", extra=""):
    return f"From: {sender}\r\n{extra}\r\n".encode()


def fetched(raw=None, internaldate='25-Oct-2026 12:34:56 +0800'):
    metadata = b"1 (UID 100"
    if internaldate is not None:
        metadata += f' INTERNALDATE "{internaldate}"'.encode()
    metadata += b" BODY[HEADER.FIELDS (FROM)] {42}"
    return "OK", [(metadata, headers() if raw is None else raw), b")"]


class ActivityTests(unittest.TestCase):
    setUp = test_receive.MailTests.setUp

    def test_header_peek_and_metadata_only(self):
        self.connection.uid.side_effect = [("OK", [b"100"]), fetched()]
        with (
            patch.object(mail, "_body_text", side_effect=AssertionError("body read")),
            patch.object(self.client, "_parse_user_email", side_effect=AssertionError("full parse")),
            patch.object(self.client, "_email_filter_reason", wraps=self.client._email_filter_reason) as filtered,
        ):
            result = self.client.latest_user_activity((10, 100))
        self.assertEqual(result, {
            "uid": "100", "uidvalidity": "10",
            "received_at": "2026-10-25T04:34:56+00:00",
            "received_at_source": "imap_internaldate",
        })
        self.assertEqual(self.connection.uid.call_args_list, [
            call("search", None, "UID", "1:100", "FROM", '"user@example.com"'),
            call("fetch", b"100", HEADER_FETCH),
        ])
        self.assertEqual(filtered.call_args.kwargs, {"automatic": True})
        self.connection.store.assert_not_called()
        self.connection.status.assert_not_called()

    def test_search_is_numeric_descending_deduplicated_and_bounded(self):
        # 即使服务器忽略搜索范围，也不能读取基线外邮件或非数字 UID。
        self.connection.uid.side_effect = [
            ("OK", [b"1 20 101 bad 0 -1 100 20 999 1:2"]),
            fetched(headers(extra="Auto-Submitted: auto-replied\r\n")),
            fetched(),
        ]
        self.assertEqual(self.client.latest_user_activity((10, 100))["uid"], "20")
        self.assertEqual([c.args[1] for c in self.connection.uid.call_args_list[1:]], [b"100", b"20"])

    def test_zero_baseline_validates_uidvalidity_without_search(self):
        self.assertIsNone(self.client.latest_user_activity((10, 0)))
        self.connection.response.assert_called_once_with("UIDVALIDITY")
        self.connection.uid.assert_not_called()

    def test_missing_invalid_or_changed_uidvalidity_stops_even_at_zero(self):
        for baseline in (0, 100):
            for response in [
                ("UIDVALIDITY", [b"11"]), ("UIDVALIDITY", [b"0"]),
                ("UIDVALIDITY", [None]), ("UIDVALIDITY", []),
                ("UIDVALIDITY", [b"invalid"]), ("UIDVALIDITY", [b"10", b"11"]),
                ("NO", [b"10"]), (None, [None]),
            ]:
                with self.subTest(baseline=baseline, response=response):
                    self.connection.response.side_effect = None
                    self.connection.response.return_value = response
                    with self.assertRaises(mail.MailboxStateError):
                        self.client.latest_user_activity((10, baseline))
        self.connection.uid.assert_not_called()

    def test_empty_or_out_of_range_search_returns_none(self):
        for ids in (b"", b"0 101 invalid -3"):
            with self.subTest(ids=ids):
                self.connection.uid.reset_mock()
                self.connection.uid.return_value = ("OK", [ids])
                self.assertIsNone(self.client.latest_user_activity((10, 100)))
                self.assertEqual(self.connection.uid.call_count, 1)

    def test_automatic_and_sender_filters_are_reused(self):
        candidates = [
            headers(sender='"user@example.com" <evil@example.com>'),
            headers(sender="assistant@126.com"),
            headers(sender="user@example.com, evil@example.com"),
            b"Subject: missing from\r\n\r\n",
            headers(extra="From: evil@example.com\r\n"),
            headers(extra="Auto-Submitted: auto-replied\r\n"),
            headers(extra="Auto-Submitted: auto-generated\r\n"),
            headers(extra="Precedence: bulk\r\n"),
            headers(extra="Precedence: list\r\n"),
            headers(extra="Precedence: junk\r\n"),
            headers(extra="Return-Path: <>\r\n"),
            headers(extra="List-Id: <list.example.com>\r\n"),
            headers(extra="Content-Type: multipart/report\r\n"),
            headers(extra="Content-Type: message/delivery-status\r\n"),
        ]
        for candidate in candidates:
            with self.subTest(candidate=candidate):
                self.connection.uid.reset_mock()
                self.connection.uid.side_effect = [
                    ("OK", [b"99 100"]), fetched(candidate),
                    fetched(headers(extra="Auto-Submitted: no\r\n")),
                ]
                self.assertEqual(self.client.latest_user_activity((10, 100))["uid"], "99")
                self.assertEqual(self.connection.uid.call_count, 3)

    def test_all_filtered_returns_none(self):
        self.connection.uid.side_effect = [
            ("OK", [b"99 100"]),
            fetched(headers(extra="Auto-Submitted: auto-replied\r\n")),
            fetched(headers(sender="evil@example.com")),
        ]
        self.assertIsNone(self.client.latest_user_activity((10, 100)))

    def test_disappeared_message_is_skipped(self):
        self.connection.uid.side_effect = [("OK", [b"99 100"]), ("OK", [None]), fetched()]
        self.assertEqual(self.client.latest_user_activity((10, 100))["uid"], "99")

    def test_missing_or_invalid_internaldate_does_not_use_older_mail_or_date(self):
        for internaldate in (None, "invalid", "31-Feb-2026 12:00:00 +0000"):
            with self.subTest(internaldate=internaldate):
                self.connection.uid.reset_mock()
                self.connection.uid.side_effect = [
                    ("OK", [b"99 100"]),
                    fetched(headers(extra="Date: Sun, 25 Oct 2099 12:00:00 +0000\r\n"), internaldate),
                    fetched(),
                ]
                self.assertEqual(self.client.latest_user_activity((10, 100)), {
                    "uid": "100", "uidvalidity": "10", "received_at": None,
                    "received_at_source": "unknown",
                })
                self.assertEqual(self.connection.uid.call_count, 2)

    def test_date_does_not_affect_uid_order_or_internaldate(self):
        for date in ("", "not a date", "Sun, 25 Oct 2099 12:00:00 +0000", "Mon, 01 Jan 1900 00:00:00 +0000"):
            with self.subTest(date=date):
                self.connection.uid.side_effect = [
                    ("OK", [b"99 100"]), fetched(headers(extra=f"Date: {date}\r\n")),
                ]
                result = self.client.latest_user_activity((10, 100))
                self.assertEqual(result["uid"], "100")
                self.assertEqual(result["received_at"], "2026-10-25T04:34:56+00:00")

    def test_highest_uid_wins_even_with_older_internaldate(self):
        self.connection.uid.side_effect = [
            ("OK", [b"99 100"]), fetched(internaldate="01-Jan-2000 00:00:00 +0000"),
            fetched(),
        ]
        result = self.client.latest_user_activity((10, 100))
        self.assertEqual(result["uid"], "100")
        self.assertEqual(result["received_at"], "2000-01-01T00:00:00+00:00")
        self.assertEqual(self.connection.uid.call_count, 2)

    def test_non_ok_search_and_fetch_raise(self):
        for status in ("NO", "BAD"):
            for responses in [
                [(status, [])],
                [("OK", [b"99 100"]), (status, [])],
            ]:
                with self.subTest(status=status, responses=responses):
                    self.connection.uid.side_effect = responses
                    with self.assertRaises(mail.imaplib.IMAP4.error):
                        self.client.latest_user_activity((10, 100))

    def test_malformed_search_response_stops(self):
        for data in ([], [None], ["100"]):
            with self.subTest(data=data):
                self.connection.uid.return_value = ("OK", data)
                with self.assertRaises(mail.MailboxStateError):
                    self.client.latest_user_activity((10, 100))

    def test_imap_abort_preserves_type_and_stage(self):
        for responses, stage in [
            ([mail.imaplib.IMAP4.abort("fake")], "UID SEARCH 查询基线内用户活动"),
            ([("OK", [b"100"]), mail.imaplib.IMAP4.abort("fake")], "UID FETCH 读取用户活动邮件头"),
        ]:
            with self.subTest(stage=stage):
                self.connection.uid.side_effect = responses
                with self.assertRaises(mail.imaplib.IMAP4.abort) as caught:
                    self.client.latest_user_activity((10, 100))
                self.assertEqual(caught.exception.mail_stage, stage)

    def test_real_imap_context_uses_readonly_inbox_with_mock_connection(self):
        self.client.assistant.update({"imap_host": "imap.example.invalid", "imap_port": 993})
        self.client._auth_code = "fake-auth"
        self.client._imap = mail.MailClient._imap.__get__(self.client)
        self.connection.capabilities = ()
        self.connection.select.return_value = ("OK", [b"1"])
        self.connection.uid.side_effect = [("OK", [b"100"]), fetched()]
        with patch.object(mail.imaplib, "IMAP4_SSL", return_value=self.connection):
            self.assertEqual(self.client.latest_user_activity((10, 100))["uid"], "100")
        self.connection.select.assert_called_once_with("INBOX", readonly=True)
        self.connection.logout.assert_called_once()
        self.connection.shutdown.assert_called_once()
        self.connection.store.assert_not_called()
        self.connection.close.assert_not_called()

    def test_readonly_select_failure_stops_before_query(self):
        self.client.assistant.update({"imap_host": "imap.example.invalid", "imap_port": 993})
        self.client._auth_code = "fake-auth"
        self.client._imap = mail.MailClient._imap.__get__(self.client)
        self.connection.capabilities = ()
        self.connection.select.return_value = ("NO", [])
        with patch.object(mail.imaplib, "IMAP4_SSL", return_value=self.connection):
            with self.assertRaises(mail.imaplib.IMAP4.error):
                self.client.latest_user_activity((10, 100))
        self.connection.uid.assert_not_called()

    def test_invalid_cursor_is_rejected_before_opening_mailbox(self):
        self.client._imap = MagicMock(side_effect=AssertionError("unexpected connection"))
        for cursor in ([10, 100], (10,), (10, -1), (0, 100), (True, 100), (10, True), (10, "100")):
            with self.subTest(cursor=cursor), self.assertRaises(ValueError):
                self.client.latest_user_activity(cursor)
        self.client._imap.assert_not_called()


if __name__ == "__main__":
    unittest.main()
