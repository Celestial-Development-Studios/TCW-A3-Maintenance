# TCW A3 Maintenance Bot

A private Discord bot for the TCW ArmA 3 server. Handles ticketing, self-role panels, rank management, CO chat access control, and broadcast management.

---

## Features

### Panel Management
- **Self-Role Panels** — Create interactive panels where members toggle roles via buttons; supports pagination, search filtering, and multi-role selection
- **Ticket Panels** — Create ticket panels with four categories: Management, Support, General, and Recruitment
- **Ticket Lifecycle** — Staff can claim, close (60-minute deletion window), delete, or reopen tickets; creators receive DM notifications

### Rank System
- Request promotions and demotions with a built-in approval workflow
- Remove members from units with Unit Leader verification
- Configure ranks, role mappings, runner roles, approver roles, and a dedicated request channel
- DM notifications sent to requesters and targets on approval or denial

### CO Chat Access Management
- Automatically grants and revokes access to unit leadership channels based on member roles
- Links unit roles to specific leadership channels
- Only manages overwrites it created — never touches manual admin permissions
- Configurable auto-refresh interval

### Broadcast System
- Broadcast images, videos, and relays to designated channels
- Automatically pings the configured broadcast role on each post
- Supports file attachments and URLs (including YouTube links)

### Role Assignment
- Configure the management role and staff role used across ticketing and permissions

### Utility Commands
- `/help` — Lists all available commands with permission requirements
- `/userinfo` — Detailed user stats including roles, status, join date, and account age
- `/serverinfo` — Guild statistics including member counts, channels, roles, and boost level

---

## TCWA3 Website Bridge

- `/tcwa3 link` - privately creates a short-lived TCWA3 Discord-to-Steam link code.
- `/tcwa3 link-status` - checks whether the latest private code was claimed.
- `/tcwa3 sync-me` - refreshes your linked Discord metadata on TCWA3.
- `/tcwa3 sync-member` - staff-only metadata sync for one member.
- `/tcwa3 sync-guild` - staff-only metadata sync for guild members in safe batches.
- `/tcwa3 bridge-status` - staff-only host configuration check.

The bridge only sends Discord identity, nickname, guild id, and role names to
TCWA3. It cannot grant XP, credits, quests, achievements, marketplace ownership,
or roster rank/unit authority.

Required host variables for the bridge:

```env
TCWA3_API_BASE_URL=https://api.tcwa3.co.uk
TCWA3_BOT_ID=tcw-discord-bot
TCWA3_BOT_SECRET=<secret from TCWA3 production env>
```

Keep `TCWA3_BOT_SECRET` in PebbleHost/host environment variables only. Do not
commit it or paste it into Discord.

---

## Developers

- **Caleb**
- **Gree**
