"""
title: Emissions Display Filter
author: Maceo
version: 1.0.1
required_open_webui_version: 0.6.10

Displays the estimated carbon footprint (CO2eq + energy) of each response, computed by LiteLLM's EcoLogits callback. Always-on filter — no toggle, runs on every request like a logging filter.
"""

from pydantic import BaseModel, Field
from typing import Optional
import requests


class Filter:
    class Valves(BaseModel):
        IMPACTS_API_URL: str = Field(
            default="http://litellm:4001/impacts/latest",
            description="URL of the impacts endpoint exposed by the EcoLogits callback",
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
                self.valves.IMPACTS_API_URL,
                params=params,
                timeout=3,
            )

            if response.status_code == 200:
                data = response.json()
                gwp_g = data.get("gwp_g") or 0
                energy_kwh = data.get("energy_kwh") or 0
                tokens = data.get("tokens", 0)
                footer = f"\n\n---\n🟢 Estimated impact: {gwp_g:.4f} gCO2eq ({energy_kwh:.6f} kWh, {tokens} tokens)"
                session_gwp_g = data.get("session_total_gwp_g")
                session_energy_kwh = data.get("session_total_energy_kwh")
                if session_gwp_g is not None:
                    footer += f" | Session total: {session_gwp_g:.4f} gCO2eq ({session_energy_kwh:.6f} kWh)"

        except Exception as e:
            print(f"[EmissionsDisplay] error: {e}")

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
