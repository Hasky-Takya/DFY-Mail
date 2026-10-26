import base64
import logging
import os
import time
from getpass import getpass

from openai import OpenAI, OpenAIError

from mail_logging import close_logging, set_log_secrets, start_logging
from mail_images import validate_image_contract


logger = logging.getLogger("dfy.chat")
logger.addHandler(logging.NullHandler())


class DeepSeekChat:
    """简单的 DeepSeek 问答客户端，每次提问相互独立，不保存历史。"""

    def __init__(self, api_key: str | None = None, model: str = "deepseek-flash"):
        api_key = api_key or os.getenv("DEEPSEEK_API_KEY")
        if not api_key or not api_key.strip():
            raise ValueError("请传入 api_key，或设置 DEEPSEEK_API_KEY 环境变量。")

        set_log_secrets(api_key)
        self.model = model
        self.client = OpenAI(
            api_key=api_key.strip(),
            base_url="https://api.deepseek.com",
            timeout=60.0,
            max_retries=0,
        )

    def ask(self, question: str, *, system_prompt: str | None = None, json_mode: bool = False,
            images=()) -> str:
        """输入问题，返回回答；可选系统提示词与问题分开，不保存历史。"""
        if not question.strip():
            raise ValueError("问题不能为空。")
        messages = []
        if system_prompt is not None:
            if not system_prompt.strip():
                raise ValueError("系统提示词不能为空。")
            messages.append({"role": "system", "content": system_prompt.strip()})
        if images:
            validate_image_contract(images)
            content = [{"type": "text", "text": question.strip()}]
            for image in images:
                content.append({"type": "text", "text": "图片编号：" + image["id"]})
                encoded = base64.b64encode(image["data"]).decode("ascii")
                content.append({"type": "image_url", "image_url": {
                    "url": f"data:{image['mime']};base64,{encoded}",
                }})
        else:
            content = question.strip()
        messages.append({"role": "user", "content": content})

        options = {"response_format": {"type": "json_object"}} if json_mode else {}
        started = time.perf_counter()
        logger.info("DeepSeek 请求开始，model=%s json_mode=%s images=%s。", self.model, json_mode, len(images))
        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                stream=False,
                extra_body={"thinking": {"type": "disabled"}},
                **options,
            )
            if not response.choices:
                raise ValueError("DeepSeek 未返回回答。")
            if response.choices[0].finish_reason != "stop":
                raise ValueError("DeepSeek 回答未正常完成，可能被截断，未采用本次输出。")
            answer = response.choices[0].message.content
            if not answer or not answer.strip():
                raise ValueError("DeepSeek 返回了空回答。")
        except Exception as exc:
            logger.error("DeepSeek 请求失败，type=%s duration=%.2fs。",
                         type(exc).__name__, time.perf_counter() - started, exc_info=True)
            raise
        logger.info("DeepSeek 请求完成，duration=%.2fs。", time.perf_counter() - started)
        return answer.strip()


def main() -> None:
    start_logging("dfy_test.py")
    logger.info("交互问答启动。")
    try:
        api_key = os.getenv("DEEPSEEK_API_KEY") or getpass("请输入 DeepSeek API Key（不显示）：")
        set_log_secrets(api_key)
        bot = DeepSeekChat(api_key=api_key)
        print("输入问题开始对话，输入 exit 或 quit 退出（每次提问独立）。")
        while True:
            question = input("你：").strip()
            if question.lower() in {"exit", "quit"}:
                break
            if not question:
                continue
            try:
                print(f"DeepSeek：{bot.ask(question)}\n")
            except OpenAIError as exc:
                logger.warning("请求失败，type=%s，请检查密钥、余额或网络。", type(exc).__name__)
            except ValueError:
                logger.warning("输入或模型输出无效，本次回答未采用。")
    except (KeyboardInterrupt, EOFError):
        logger.info("用户中断，已退出。")
    except ValueError:
        logger.error("问答配置或输入无效。", exc_info=True)
    except Exception as exc:
        logger.critical("问答意外停止，type=%s。", type(exc).__name__, exc_info=True)
        raise SystemExit(1) from None
    finally:
        logger.info("交互问答结束。")
        close_logging()


if __name__ == "__main__":
    main()
