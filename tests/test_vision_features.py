"""视觉来信及动态主题，所有模型/邮箱均为mock；Pillow缺失时明确跳过真实解码。"""

import hashlib
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr
from email.message import EmailMessage
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from test_receive import llm, mail, mail_images, mail_logging, memory_module as memory, receive, vision, message, mock_send, story_module as story


def image():
    data = b"synthetic_image_payload"
    return {"id": "image-1", "sha256": hashlib.sha256(data).hexdigest(), "mime": "image/png", "data": data}


def observation():
    return {"image_id": "image-1", "sha256": image()["sha256"], "model": "deepseek-flash", "description": "蓝色小猫在桌上", "ocr": "明天游戏", "uncertainty": "无法确认人物身份"}


class VisionFeaturesTests(unittest.TestCase):
    def test_sdk_default_and_multimodal_format(self):
        client = MagicMock()
        client.chat.completions.create.return_value = SimpleNamespace(choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content="answer"))])
        with patch.object(llm, "OpenAI", return_value=client):
            bot = llm.DeepSeekChat("fake")
            self.assertEqual(bot.ask("看图", images=[image()]), "answer")
            options = client.chat.completions.create.call_args.kwargs
            self.assertEqual(options["model"], "deepseek-flash")
            self.assertEqual(options["extra_body"], {"thinking": {"type": "disabled"}})
            self.assertTrue(options["messages"][-1]["content"][-1]["image_url"]["url"].startswith("data:image/png;base64,"))
            bot.ask("plain")
            self.assertEqual(client.chat.completions.create.call_args.kwargs["messages"][-1]["content"], "plain")

    def test_mime_attachment_dedup_and_public_projection(self):
        raw = EmailMessage()
        raw["From"] = "user@example.com"
        raw.set_content("body")
        raw.add_attachment(b"same", maintype="image", subtype="png", filename="private.png")
        raw.add_attachment(b"same", maintype="image", subtype="png", filename="copy.png")
        with patch.object(mail_images, "_decode", return_value=(b"same", "image/png", None)):
            images, warnings = mail_images.extract_images(raw)
        self.assertEqual(len(images), 1)
        self.assertFalse(warnings)
        self.assertNotIn("data", mail_images.public_mail({"images": images})["images"][0])
        self.assertNotIn("private.png", str(images))

    def test_cid_not_remote_or_forwarded(self):
        raw = EmailMessage()
        raw.set_content("text")
        raw.add_alternative('<img src="cid:one"><img src="https://example.com/tracker.png">', subtype="html")
        raw.get_payload()[1].add_related(b"one", maintype="image", subtype="png", cid="<one>")
        raw.get_payload()[1].add_related(b"two", maintype="image", subtype="png", cid="<two>")
        with patch.object(mail_images, "_decode", side_effect=lambda data: (data, "image/png", None)):
            images, _ = mail_images.extract_images(raw)
        self.assertEqual(len(images), 1)
        self.assertEqual(images[0]["data"], b"one")

    def test_unreferenced_inline_and_forwarded_image_are_ignored(self):
        raw = EmailMessage()
        raw.set_content("body")
        raw.make_mixed()
        inline = EmailMessage()
        inline.set_content(b"tracking", maintype="image", subtype="png")
        inline["Content-ID"] = "<unreferenced>"
        inline.add_header("Content-Disposition", "inline", filename="tracker.png")
        raw.attach(inline)
        forwarded = EmailMessage()
        forwarded.set_content("old body")
        forwarded.add_attachment(b"old", maintype="image", subtype="png", filename="old.png")
        raw.add_attachment(forwarded)
        with patch.object(mail_images, "_decode") as decode:
            images, warnings = mail_images.extract_images(raw)
        self.assertFalse(images)
        self.assertFalse(warnings)
        decode.assert_not_called()

    def test_percent_encoded_cid_is_supported(self):
        raw = EmailMessage()
        raw.set_content("text")
        raw.add_alternative('<img src="cid:pic%40example.com">', subtype="html")
        raw.get_payload()[1].add_related(b"picture", maintype="image", subtype="png", cid="<pic@example.com>")
        with patch.object(mail_images, "_decode", return_value=(b"picture", "image/png", None)):
            images, notices = mail_images.extract_images(raw)
        self.assertEqual(len(images), 1)
        self.assertFalse(notices)

    def test_warning_text_cannot_be_user_evidence(self):
        text, evidence = vision.normalize_user_content("", [], [{"index": 1, "code": "invalid_image"}])
        update = {"target_id": None, "title": "notice", "content": "notice", "source": "user", "domain": "user_report", "importance": 1, "people": [], "status": "ongoing", "tense": "present", "event_time": {"raw": None, "start": None, "end": None}, "evidence": "部分内容未处理"}
        with self.assertRaises(story.StoryValidationError):
            story.validate_updates({"event_updates": [update], "memory_updates": []}, story_context=story.empty_context(), user_body=text, assistant_body="reply", user_evidence=evidence)

    def test_invalid_image_warnings_and_limits(self):
        raw = EmailMessage()
        raw.set_content("")
        for index in range(5):
            raw.add_attachment(bytes([index]), maintype="image", subtype="png", filename="x.png")
        with patch.object(mail_images, "_decode", side_effect=lambda data: (data, "image/png", None)):
            images, warnings = mail_images.extract_images(raw)
        self.assertEqual(len(images), 4)
        self.assertEqual(warnings[-1]["code"], "image_count_exceeded")
        with patch.object(mail_images, "_decode", return_value=(None, None, "invalid_image")):
            images, warnings = mail_images.extract_images(raw)
        self.assertFalse(images)
        self.assertTrue(warnings)

    def test_bounded_fetch_never_fetches_large_body(self):
        box = mail.MailClient.__new__(mail.MailClient)
        connection = MagicMock()
        connection.uid.side_effect = [("OK", [b"1 (RFC822.SIZE 40000000)"]), ("OK", [(b"1 (BODY[HEADER] {1}", b"From: user@example.com\r\n\r\n")])]
        raw, _, warning = box._fetch_bounded(connection, b"1")
        self.assertEqual(warning, "mail_too_large")
        self.assertIn("HEADER", connection.uid.call_args.args[-1])
        self.assertIsNotNone(raw)

    def test_observe_validation_and_normalization(self):
        bot = MagicMock(model="deepseek-flash")
        bot.ask.return_value = json.dumps({"images": [{key: value for key, value in observation().items() if key in {"image_id", "description", "ocr", "uncertainty"}}]})
        result = vision.observe_images(bot, "body", [image()])
        text, evidence = vision.normalize_user_content("user text", result)
        self.assertIn("不是用户原话", text)
        self.assertEqual(evidence["text"], "user text")
        self.assertEqual(evidence["images"][0]["image_id"], "image-1")
        self.assertNotIn("data", str(result))
        bot.ask.return_value = '{"images":[]}'
        with self.assertRaises(vision.VisionError):
            vision.observe_images(bot, "", [image()])

    def test_image_only_reply_caches_vision_and_memory_provenance(self):
        with tempfile.TemporaryDirectory() as directory:
            box, bot = MagicMock(), MagicMock(model="deepseek-flash")
            box.get_mailbox_cursor.return_value = (10, 0)
            incoming = message(1, body="")
            incoming["images"] = [image()]
            box.read_user_emails_since.return_value = ((10, 1), [incoming])
            box.send_email.side_effect = mock_send
            observed = {key: value for key, value in observation().items() if key in {"image_id", "description", "ocr", "uncertainty"}}
            event = {"target_id": None, "title": "猫图", "content": "图中有蓝色小猫", "source": "user", "domain": "uncertain", "importance": 2, "people": [], "status": "ongoing", "tense": "present", "event_time": {"raw": None, "start": None, "end": None}, "evidence": "蓝色小猫在桌上"}
            extracted = json.dumps({"user_summary": "用户附图小猫。", "assistant_summary": "回答图片内容。", "event_updates": [event], "memory_updates": []})
            bot.ask.side_effect = [json.dumps({"images": [observed]}), "回复图片", "invalid json", extracted]
            callback = MagicMock()
            listener = receive.MailReceiver(box, bot, "persona", memory_path=Path(directory) / "dialog.json", summary_prompt="summary", extraction_prompt="extract", on_user_message=callback)
            listener.start()
            listener.poll_once()
            listener.poll_once()
            callback.assert_called_once()
            self.assertEqual(sum("images" in call.kwargs for call in bot.ask.call_args_list), 1)
            box.send_email.assert_called_once()
            data = listener.memory.snapshot(include_story=True)
            self.assertEqual(data["dialogs"][0]["image_observations"][0]["image_id"], "image-1")
            ref = data["story_context"]["events"][0]["source_refs"][0]
            self.assertEqual(ref["evidence_origin"], "image_observation")
            self.assertNotIn("images", incoming)

    def test_visual_payload_and_observations_not_in_logs(self):
        with tempfile.TemporaryDirectory() as directory, redirect_stderr(io.StringIO()):
            path = mail_logging.configure_logging(directory, script="dfy_receive.py")
            try:
                client = MagicMock()
                client.chat.completions.create.return_value = SimpleNamespace(choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content="PRIVATE IMAGE OBSERVATION"))])
                with patch.object(llm, "OpenAI", return_value=client):
                    bot = llm.DeepSeekChat("fake-key")
                    bot.ask("PRIVATE IMAGE QUESTION", images=[image()])
                text = path.read_text(encoding="utf-8")
                for private in ("PRIVATE IMAGE", "data:image", "synthetic_image_payload", "fake-key"):
                    self.assertNotIn(private, text)
                self.assertIn("images=1", text)
                self.assertIn("deepseek-flash", text)
            finally:
                mail_logging.close_logging()

    def test_dynamic_subject_all_modes_and_repair(self):
        for enabled in (False, True):
            bot = MagicMock()
            value = {"user_summary": None, "assistant_summary": "正文梗概", "mail_subject": "小猫今天的新发现"}
            if enabled:
                value.update(event_updates=[], memory_updates=[])
            bot.ask.return_value = json.dumps(value)
            kwargs = {"story_context": story.empty_context(), "extraction_prompt": "extract"} if enabled else {}
            result = memory.summarize(bot, "summary", None, "正文", generate_subject=True, **kwargs)
            self.assertEqual(result["mail_subject"], value["mail_subject"])
            with self.assertRaises(memory.SummaryError) as failure:
                memory.summarize(bot, "summary", None, "正文", generate_subject=True, recent_subjects=[value["mail_subject"]], **kwargs)
            self.assertEqual(failure.exception.details["path"], "mail_subject")

    def test_unsafe_subjects(self):
        for subject in ("", "a\nb", "a\rb", "a\x00b", "a b", "Subject: test", "Re: test", "Fwd：test", "x" * 61):
            with self.subTest(subject=subject), self.assertRaises(ValueError):
                memory.validate_subject(subject)

    def test_bad_visual_format_is_retried_only_three_times(self):
        with tempfile.TemporaryDirectory() as directory:
            box, bot = MagicMock(), MagicMock(model="deepseek-flash")
            box.get_mailbox_cursor.return_value = (10, 0)
            incoming = message(1, body="")
            incoming["images"] = [image()]
            box.read_user_emails_since.return_value = ((10, 1), [incoming])
            bot.ask.return_value = '{"images":[]}'
            listener = receive.MailReceiver(box, bot, "persona", memory_path=Path(directory) / "dialog.json", summary_prompt="summary")
            listener.start()
            listener.poll_once()
            listener.poll_once()
            with self.assertRaises(RuntimeError):
                listener.poll_once()
            self.assertEqual(bot.ask.call_count, 3)
            box.send_email.assert_not_called()

    def test_only_bad_attachment_gets_explicit_response_without_vision(self):
        with tempfile.TemporaryDirectory() as directory:
            box, bot = MagicMock(), MagicMock(model="deepseek-flash")
            box.get_mailbox_cursor.return_value = (10, 0)
            incoming = message(1, body="")
            incoming["image_warnings"] = [{"index": 1, "code": "invalid_image"}]
            box.read_user_emails_since.return_value = ((10, 1), [incoming])
            box.send_email.side_effect = mock_send
            bot.ask.side_effect = ["附件无法读取，请重新发送。", json.dumps({"user_summary": "用户发送无法读取的附件。", "assistant_summary": "提示重新发送。"})]
            listener = receive.MailReceiver(box, bot, "persona", memory_path=Path(directory) / "dialog.json", summary_prompt="summary")
            listener.start()
            listener.poll_once()
            self.assertEqual(bot.ask.call_count, 2)
            self.assertTrue(all("images" not in call.kwargs for call in bot.ask.call_args_list))
            self.assertIn("未处理", bot.ask.call_args_list[0].args[0])
            box.send_email.assert_called_once()

    def test_real_pillow_formats_if_available(self):
        try:
            from PIL import Image
        except ImportError:
            self.skipTest("Pillow未安装：真实图像解码测试未执行")
        for format_name in ("PNG", "JPEG", "GIF", "WEBP"):
            output = io.BytesIO()
            Image.new("RGB", (2, 2)).save(output, format=format_name)
            data, mime, warning = mail_images._decode(output.getvalue())
            self.assertTrue(data)
            self.assertIn(mime, mail_images.MIME_BY_FORMAT.values())
            self.assertIsNone(warning)


if __name__ == "__main__":
    unittest.main()
