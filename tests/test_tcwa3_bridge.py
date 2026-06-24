import hashlib
import hmac
import unittest

from tcwa3_bridge import Tcwa3BridgeClient


class Tcwa3BridgeClientTests(unittest.TestCase):
    def test_signature_matches_backend_contract(self):
        client = Tcwa3BridgeClient(
            base_url="https://api.tcwa3.co.uk",
            bot_id="tcw-discord-bot",
            secret="bot-secret",
        )
        body = client._body_bytes(
            {
                "discord_id": "123456789012345678",
                "guild_id": "987654321098765432",
                "discord_username": "Hacket",
            }
        )
        timestamp = "1700000000"
        signed = b".".join(
            [
                timestamp.encode("utf-8"),
                b"POST",
                b"/v1/bot/discord/link-code",
                b"",
                body,
            ]
        )
        expected = hmac.new(b"bot-secret", signed, hashlib.sha256).hexdigest()

        self.assertEqual(
            client._signature("POST", "/v1/bot/discord/link-code", "", body, timestamp),
            expected,
        )

    def test_headers_never_include_raw_secret(self):
        client = Tcwa3BridgeClient(
            base_url="https://api.tcwa3.co.uk",
            bot_id="tcw-discord-bot",
            secret="bot-secret",
        )
        headers = client._headers("GET", "/v1/bot/discord/link-status", "link_id=abc", b"", "1700000000")

        self.assertEqual(headers["X-TCWA3-Bot-Id"], "tcw-discord-bot")
        self.assertNotIn("bot-secret", " ".join(headers.values()))


if __name__ == "__main__":
    unittest.main()
