"""DeepSeek 调用。只做两件事：细筛排序、写评论草稿。

每次运行有调用上限（config.llm.max_calls_per_run）：一个 bug 不该烧掉一整月的额度。
错误信息里绝不带响应体 —— 免得把 key 或账号内容带进日志。
"""

from __future__ import annotations

import json

import requests

from . import config as cfgmod, quota
from .client import UA
from .logs import get

log = get("llm")


class LlmError(RuntimeError):
    pass


class Llm:
    def __init__(self, cfg: cfgmod.Config) -> None:
        self.cfg = cfg
        self.calls = 0
        self.tokens_in = 0
        self.tokens_out = 0
        if not cfg.llm.api_key:
            raise LlmError("没有可用的 DeepSeek 密钥（config.yaml / 环境变量 / [llm].api_key 都没找到）")

    @property
    def usage_line(self) -> str:
        return f"{self.calls} 次调用 / 输入 {self.tokens_in} tok / 输出 {self.tokens_out} tok"

    def chat(
        self,
        messages: list[dict],
        *,
        json_mode: bool = False,
        max_tokens: int = 900,
        temperature: float | None = None,
    ) -> str:
        if self.calls >= self.cfg.llm.max_calls_per_run:
            raise LlmError(f"本次运行已达模型调用上限 {self.cfg.llm.max_calls_per_run}")
        if quota.over(self.cfg, "llm"):
            raise LlmError(f"本小时模型调用已达上限 {quota.limit(self.cfg, 'llm')} 次，下个整点恢复")
        quota.bump("llm")

        body = {
            "model": self.cfg.llm.model,
            "messages": messages,
            "temperature": self.cfg.llm.temperature if temperature is None else temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }
        if json_mode:
            body["response_format"] = {"type": "json_object"}

        headers = {
            "Authorization": f"Bearer {self.cfg.llm.api_key}",
            "Content-Type": "application/json",
            "User-Agent": UA,
        }
        try:
            resp = requests.post(
                f"{self.cfg.llm.base_url}/chat/completions",
                json=body,
                headers=headers,
                timeout=60,
            )
        except requests.RequestException as exc:
            raise LlmError(f"网络失败：{type(exc).__name__}") from None

        self.calls += 1
        if resp.status_code != 200:
            raise LlmError(f"模型接口返回 HTTP {resp.status_code}")
        try:
            payload = resp.json()
        except ValueError:
            raise LlmError("模型接口返回的不是 JSON") from None
        usage = payload.get("usage") or {}
        self.tokens_in += int(usage.get("prompt_tokens") or 0)
        self.tokens_out += int(usage.get("completion_tokens") or 0)
        try:
            return payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            raise LlmError("模型返回结构异常") from None

    def chat_json(self, messages: list[dict], **kwargs) -> dict:
        text = self.chat(messages, json_mode=True, **kwargs)
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            raise LlmError("模型返回的不是合法 JSON") from None
        if not isinstance(data, dict):
            raise LlmError("模型返回的 JSON 顶层不是对象")
        return data
