import os
from getpass import getpass

from openai import OpenAI, OpenAIError

class DeepSeekChat:
    """简单的 DeepSeek 问答客户端，每次提问相互独立，不保存历史。"""

    def __init__(self, api_key: str | None = None, model: str = "deepseek-chat"):
        api_key = api_key or os.getenv("DEEPSEEK_API_KEY")
        if not api_key or not api_key.strip():
            raise ValueError("请传入 api_key，或设置 DEEPSEEK_API_KEY 环境变量。")

        self.model = model
        self.client = OpenAI(
            api_key=api_key.strip(),
            base_url="https://api.deepseek.com",
            timeout=60.0,
            max_retries=0,
        )

    def ask(self, question: str, *, system_prompt: str | None = None, json_mode: bool = False) -> str:
        """输入问题，返回回答；可选系统提示词与问题分开，不保存历史。"""
        if not question.strip():
            raise ValueError("问题不能为空。")
        messages = []
        if system_prompt is not None:
            if not system_prompt.strip():
                raise ValueError("系统提示词不能为空。")
            messages.append({"role": "system", "content": system_prompt.strip()})
        messages.append({"role": "user", "content": question.strip()})

        options = {"response_format": {"type": "json_object"}} if json_mode else {}
        response = self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            stream=False,
            **options,
        )
        if not response.choices:
            raise ValueError("DeepSeek 未返回回答。")

        if response.choices[0].finish_reason != "stop":
            raise ValueError("DeepSeek 回答未正常完成，可能被截断，未采用本次输出。")
        answer = response.choices[0].message.content
        if not answer or not answer.strip():
            raise ValueError("DeepSeek 返回了空回答。")
        return answer.strip()


def main() -> None:
    api_key = os.getenv("DEEPSEEK_API_KEY") or getpass("请输入 DeepSeek API Key（不显示）：")
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
            print(f"请求失败（{type(exc).__name__}），请检查密钥、余额或网络。")
        except ValueError as exc:
            print(exc)


if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, EOFError):
        print("\n已退出。")
    except ValueError as exc:
        print(exc)
