# ── Bot token ─────────────────────────────────────────────────────────────────
import os
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")

# TCWA3 Bridge API. Keep the secret in PebbleHost/host environment variables,
# never in Git. The bot id must match TCW_DISCORD_BOT_KEYS_JSON in TCWA3.
TCWA3_API_BASE_URL = os.getenv("TCWA3_API_BASE_URL", "https://api.tcwa3.co.uk").rstrip("/")
TCWA3_BOT_ID = os.getenv("TCWA3_BOT_ID", "tcw-discord-bot").strip()
TCWA3_BOT_SECRET = os.getenv("TCWA3_BOT_SECRET", "").strip()

# ── Developer user IDs ────────────────────────────────────────────────────────
# These bypass all permission checks for cog management and role assignment.
DEVELOPER_IDS: set[int] = {1193600679399403665, 186867094476816384}
