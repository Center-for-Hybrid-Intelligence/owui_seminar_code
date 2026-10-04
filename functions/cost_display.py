"""
title: Cost Display Filter
author: Maceo
version: 1.0.1
required_open_webui_version: 0.6.10
icon_url: data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIGZpbGw9Im5vbmUiIHZpZXdCb3g9IjAgMCAyNCAyNCIgc3Ryb2tlLXdpZHRoPSIxLjUiIHN0cm9rZT0iY3VycmVudENvbG9yIj4KICA8cGF0aCBzdHJva2UtbGluZWNhcD0icm91bmQiIHN0cm9rZS1saW5lam9pbj0icm91bmQiIGQ9Ik0xMiA2djEybTQtNi44NjJjMC0xLjY0Mi0xLjc5MS0yLjk3My00LTIuOTczcy00IDEuMzMxLTQgMi45NzMgMS43OTEgMi45NzMgNCAyLjk3MyA0IDEuMzMxIDQgMi45NzMtMS43OTEgMi45NzMtNCAyLjk3My00LTEuMzMxLTQtMi45NzMiIC8+Cjwvc3ZnPgo=

Displays the cost (combined input + output tokens) of each response, computed by LiteLLM's cost-tracking callback. Always-on filter — no toggle, runs on every request like a logging/billing filter.
"""

from pydantic import BaseModel, Field
from typing import Optional
import requests


class Filter:
    class Valves(BaseModel):
        COST_API_URL: str = Field(
            default="http://litellm:4002/cost/latest",
            description="URL of the cost-tracking endpoint exposed by the LiteLLM callback",
        )

    def __init__(self):
        self.valves = self.Valves()

    async def outlet(
        self,
        body: dict,
        __user__: Optional[dict] = None,
        __metadata__: Optional[dict] = None,
        __event_emitter__=None,
    ) -> dict:
        user_email = (__user__ or {}).get("email")
        chat_id = (__metadata__ or {}).get("chat_id")
        footer = None

        params = {}
        if user_email:
            params["user"] = user_email
        if chat_id:
            params["chat_id"] = chat_id

        try:
            response = requests.get(
                self.valves.COST_API_URL,
                params=params,
                timeout=3,
            )

            if response.status_code == 200:
                data = response.json()
                cost = data.get("cost_usd") or 0
                input_tokens = data.get("input_tokens", 0)
                output_tokens = data.get("output_tokens", 0)
                footer = f"\n\n---\n🟠 Your prompt cost: ${cost:.6f} ({input_tokens} in / {output_tokens} out)"
                session_total = data.get("session_total_cost")
                if session_total is not None:
                    footer += f" | Session total: ${session_total:.6f}"

        except Exception as e:
            print(f"[CostDisplay] error: {e}")

        if footer:
            for message in reversed(body.get("messages", [])):
                if message.get("role") != "assistant":
                    continue
                if isinstance(message.get("content"), str):
                    message["content"] += footer
                # The UI renders the visible reply from the structured `output` array
                # (output[].content[].text where type == "message"), not from `content` —
                # editing `content` alone is invisible in the chat (confirmed 24/09, see
                # https://github.com/open-webui/open-webui/issues/29319). Keep both in sync.
                for item in message.get("output") or []:
                    if item.get("type") != "message":
                        continue
                    for part in item.get("content") or []:
                        if isinstance(part.get("text"), str):
                            part["text"] += footer
                break

        return body
