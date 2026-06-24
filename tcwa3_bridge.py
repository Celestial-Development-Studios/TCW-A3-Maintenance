"""Signed TCWA3 Bridge API client for the Discord maintenance bot.

This module intentionally only knows how to call bot-owned Discord bridge
endpoints. It does not expose progression, economy, quest, achievement, or
marketplace write helpers.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import urllib.parse
from typing import Any, Dict, Iterable, Optional


class Tcwa3BridgeConfigError(RuntimeError):
    """Raised when the bot host has not been configured for TCWA3."""


class Tcwa3BridgeError(RuntimeError):
    """Raised for non-successful TCWA3 Bridge API responses."""

    def __init__(self, status: int, error: str, message: str, payload: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.status = status
        self.error = error
        self.payload = payload or {}


class Tcwa3BridgeClient:
    def __init__(
        self,
        *,
        base_url: str,
        bot_id: str,
        secret: str,
        timeout_seconds: float = 15.0,
        user_agent: str = "tcwa3-maintenance-discord-bot/1.0",
    ) -> None:
        self.base_url = (base_url or "https://api.tcwa3.co.uk").rstrip("/")
        self.bot_id = (bot_id or "").strip()
        self.secret = secret or ""
        self.timeout_seconds = timeout_seconds
        self.user_agent = user_agent

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.bot_id and self.secret)

    def _body_bytes(self, payload: Optional[Dict[str, Any]]) -> bytes:
        if payload is None:
            return b""
        return json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")

    def _signature(self, method: str, path: str, query: str, body: bytes, timestamp: str) -> str:
        signed = b".".join(
            [
                timestamp.encode("utf-8"),
                method.upper().encode("utf-8"),
                path.encode("utf-8"),
                query.encode("utf-8"),
                body,
            ]
        )
        return hmac.new(self.secret.encode("utf-8"), signed, hashlib.sha256).hexdigest()

    def _headers(self, method: str, path: str, query: str, body: bytes, timestamp: str) -> Dict[str, str]:
        return {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": self.user_agent,
            "X-TCWA3-Bot-Id": self.bot_id,
            "X-TCWA3-Timestamp": timestamp,
            "X-TCWA3-Signature": self._signature(method, path, query, body, timestamp),
        }

    async def request(self, method: str, route: str, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        if not self.configured:
            raise Tcwa3BridgeConfigError(
                "TCWA3 bridge is not configured. Set TCWA3_BOT_SECRET on the bot host."
            )

        try:
            import aiohttp
        except ImportError as exc:
            raise Tcwa3BridgeConfigError(
                "aiohttp is required for TCWA3 bridge requests. Run pip install -r requirements.txt."
            ) from exc

        parsed = urllib.parse.urlsplit(route)
        path = parsed.path or "/"
        query = parsed.query
        body = self._body_bytes(payload)
        timestamp = str(int(time.time()))
        headers = self._headers(method, path, query, body, timestamp)
        url = self.base_url + path + (f"?{query}" if query else "")

        timeout = aiohttp.ClientTimeout(total=self.timeout_seconds)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.request(method.upper(), url, data=body if body else None, headers=headers) as response:
                text = await response.text()
                try:
                    data = json.loads(text) if text else {}
                except json.JSONDecodeError:
                    data = {"raw": text[:500]}

                if response.status >= 400:
                    error = str(data.get("error") or f"http_{response.status}")
                    message = str(data.get("message") or data.get("raw") or error)
                    raise Tcwa3BridgeError(response.status, error, message, data)
                return data

    async def create_link_code(self, member: Dict[str, Any]) -> Dict[str, Any]:
        return await self.request("POST", "/v1/bot/discord/link-code", member)

    async def link_status(self, link_id: str) -> Dict[str, Any]:
        query = urllib.parse.urlencode({"link_id": link_id})
        return await self.request("GET", f"/v1/bot/discord/link-status?{query}")

    async def member_sync(self, members: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
        return await self.request("POST", "/v1/bot/discord/member-sync", {"members": list(members)})

    async def notification_events(self, events: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
        return await self.request(
            "POST",
            "/v1/bot/discord/notification-events",
            {"events": list(events)},
        )
