"""提示词格式测试：不连接模型或邮箱。"""

import json
import tempfile
import unittest
from pathlib import Path

from test_receive import memory_module, proactive, receive


class PromptTests(unittest.TestCase):
    def test_render_preserves_lines_and_section_order(self):
        value = {"角色设定": ["第一行", "", "第三行"], "输出要求": ["只输出正文。"]}
        self.assertEqual(
            memory_module.render_prompt(value),
            "【角色设定】\n第一行\n\n第三行\n\n【输出要求】\n只输出正文。",
        )

    def test_legacy_string_preserved(self):
        self.assertEqual(memory_module.render_prompt("旧提示词\n第二行"), "旧提示词\n第二行")

    def test_invalid_sections_rejected(self):
        for value in (None, "", {}, {"": ["内容"]}, {"角色": "不是数组"},
                      {"角色": []}, {"角色": [1]}, {"角色": ["", " "]}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                memory_module.render_prompt(value)

    def test_loaders_accept_sections_and_legacy_strings(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "prompt.json"
            for value, expected in (
                ("旧提示词", "旧提示词"),
                ({"角色": ["内容", "", "下一行"]}, "【角色】\n内容\n\n下一行"),
            ):
                with self.subTest(value=value):
                    path.write_text(json.dumps({
                        "subject": "测试主题", "prompt": value,
                        "summary_prompt": "原有梗概提示词",
                    }, ensure_ascii=False), encoding="utf-8")
                    self.assertEqual(proactive.load_prompt(path)["prompt"], expected)
                    self.assertEqual(receive.load_reply_prompt(path), expected)
                    self.assertEqual(memory_module.load_summary_prompt(path), "原有梗概提示词")

    def test_shared_persona_with_separate_tasks(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "prompt.json"
            path.write_text(json.dumps({
                "subject": "测试主题", "prompt": {"角色": ["共同人设"]},
                "send_task": ["写问候信", "", "简短一些"],
                "reply_task": ["回答用户来信"],
            }, ensure_ascii=False), encoding="utf-8")
            self.assertEqual(proactive.load_prompt(path)["prompt"],
                             "【角色】\n共同人设\n\n【邮件任务与输出要求】\n写问候信\n\n简短一些")
            self.assertEqual(receive.load_reply_prompt(path),
                             "【角色】\n共同人设\n\n【邮件任务与输出要求】\n回答用户来信")

    def test_invalid_task_rejected(self):
        for task in (None, [], [1], [""]):
            with self.subTest(task=task), self.assertRaises(ValueError):
                memory_module.render_email_prompt({"prompt": "共同人设", "reply_task": task}, "reply_task")


if __name__ == "__main__":
    unittest.main()
