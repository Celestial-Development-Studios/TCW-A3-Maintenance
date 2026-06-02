# ── Bot token ─────────────────────────────────────────────────────────────────
import os
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")

# ── Developer user IDs ────────────────────────────────────────────────────────
# These bypass all permission checks for cog management and role assignment.
DEVELOPER_IDS: set[int] = {1193600679399403665}
