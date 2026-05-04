import os, json, io, asyncio, time
from typing import Dict, Any, Optional, List
from datetime import datetime
from dotenv import load_dotenv

import discord
from discord.ext import commands, tasks
from discord import app_commands

# ––––– ENV –––––

load_dotenv()
TOKEN = os.getenv(“DISCORD_TOKEN”) or os.getenv(“TOKEN”) or “”
OWNER_ID = 720061069628014652

# ––––– CONSTANTS –––––

BLUE = discord.Color.blurple()
INACTIVITY_SECONDS = 48 * 3600
REMINDER_BEFORE = 24 * 3600
SCAN_INTERVAL = 600

# ––––– FILES –––––

GCFG_FILE = “guild_configs.json”
OPEN_FILE = “opened_tickets.json”
STAFF_STATS_FILE = “staff_stats.json”
CLOSED_FILE = “closed_tickets.json”

def _ensure_file(path: str, default: Any):
if not os.path.exists(path):
with open(path, “w”, encoding=“utf-8”) as f:
json.dump(default, f, indent=4)

def _load_json(path: str) -> Any:
try:
with open(path, “r”, encoding=“utf-8”) as f:
return json.load(f)
except Exception:
return {}

def _save_json(path: str, data: Any):
with open(path, “w”, encoding=“utf-8”) as f:
json.dump(data, f, indent=4)

_ensure_file(GCFG_FILE, {})
_ensure_file(OPEN_FILE, {})
_ensure_file(STAFF_STATS_FILE, {})
_ensure_file(CLOSED_FILE, {})

def get_gcfg(gid: int) -> Dict[str, Any]:
data = _load_json(GCFG_FILE)
s = str(gid)
if s not in data:
data[s] = {
“tickets_created”: 0,
“categories”: [],
“staff_role_id”: None,
“log_channel_id”: None,
“reopen_channel_id”: None,
“panel_description”: “Open a ticket using the buttons below.”,
“auto_close_enabled”: True,
“log_transcripts”: True,
“panel_channel_id”: None,
“panel_message_id”: None
}
_save_json(GCFG_FILE, data)
return data[s]

def set_gcfg(gid: int, val: Dict[str, Any]):
data = _load_json(GCFG_FILE)
data[str(gid)] = val
_save_json(GCFG_FILE, data)

def get_open(gid: int) -> Dict[str, Any]:
data = _load_json(OPEN_FILE)
return data.get(str(gid), {})

def set_open(gid: int, val: Dict[str, Any]):
data = _load_json(OPEN_FILE)
data[str(gid)] = val
_save_json(OPEN_FILE, data)

def add_open_ticket(gid: int, channel_id: int, owner_id: int, num: int, reason: str = “”):
cur = get_open(gid)
now = int(time.time())
cur[str(channel_id)] = {
“owner_id”: owner_id,
“num”: num,
“created_at”: now,
“last_activity”: now,
“reminded24”: False,
“hold”: False,
“reason”: reason
}
set_open(gid, cur)

def remove_open_ticket(gid: int, channel_id: int):
cur = get_open(gid)
if str(channel_id) in cur:
del cur[str(channel_id)]
set_open(gid, cur)

def find_open_ticket(gid: int, channel_id: int) -> Optional[Dict[str, Any]]:
return get_open(gid).get(str(channel_id))

def save_closed_ticket(gid: int, ticket_info: Dict[str, Any], closer_id: int, reason: str):
data = _load_json(CLOSED_FILE)
s = str(gid)
if s not in data:
data[s] = {}
num = str(ticket_info.get(“num”, int(time.time())))
data[s][num] = {
“owner_id”: ticket_info.get(“owner_id”),
“closed_by”: closer_id,
“closed_at”: int(time.time()),
“close_reason”: reason,
“created_at”: ticket_info.get(“created_at”),
“reason”: ticket_info.get(“reason”, “”)
}
_save_json(CLOSED_FILE, data)

def get_staff_stats(gid: int) -> Dict[str, Any]:
data = _load_json(STAFF_STATS_FILE)
return data.get(str(gid), {})

def set_staff_stats(gid: int, val: Dict[str, Any]):
data = _load_json(STAFF_STATS_FILE)
data[str(gid)] = val
_save_json(STAFF_STATS_FILE, data)

def add_claim(gid: int, staff_id: int, ticket_num: int):
stats = get_staff_stats(gid)
sid = str(staff_id)
if sid not in stats:
stats[sid] = {“claimed”: 0, “closed”: 0, “response_times”: []}
stats[sid][“claimed”] += 1
set_staff_stats(gid, stats)

def add_close(gid: int, staff_id: int, response_time: int):
stats = get_staff_stats(gid)
sid = str(staff_id)
if sid not in stats:
stats[sid] = {“claimed”: 0, “closed”: 0, “response_times”: []}
stats[sid][“closed”] += 1
stats[sid][“response_times”].append(response_time)
set_staff_stats(gid, stats)

def build_transcript_embed(ticket_info: Dict[str, Any], closer: discord.User, channel: discord.TextChannel, guild: discord.Guild) -> discord.Embed:
opener_id = ticket_info.get(“owner_id”)
created = ticket_info.get(“created_at”, int(time.time()))
now = int(time.time())
duration = now - created
hours = duration // 3600
minutes = (duration % 3600) // 60
time_str = f”{hours}h {minutes}m” if hours > 0 else f”{minutes}m”
created_at = datetime.fromtimestamp(created).strftime(”%B %d, %Y at %I:%M %p”)
closed_at = datetime.fromtimestamp(now).strftime(”%B %d, %Y at %I:%M %p”)
e = discord.Embed(title=“🔒 Ticket Closed”, color=discord.Color.red())
e.add_field(name=“Ticket ID”, value=f”{ticket_info.get(‘num’, ‘N/A’)}”, inline=True)
e.add_field(name=“Opened By”, value=f”<@{opener_id}>”, inline=True)
e.add_field(name=“Closed By”, value=f”{closer.mention}”, inline=True)
e.add_field(name=“Open Time”, value=time_str, inline=True)
e.add_field(name=“Channel”, value=channel.mention, inline=True)
e.add_field(name=“Created”, value=created_at, inline=False)
e.add_field(name=“Closed”, value=closed_at, inline=False)
e.set_footer(text=f”{guild.name}”)
return e

# ––––– DISCORD SETUP –––––

intents = discord.Intents.default()
intents.guilds = True
intents.members = True
intents.messages = True

bot = commands.Bot(command_prefix=”!”, intents=intents)
tree = bot.tree

# ––––– HELPERS –––––

def is_admin_or_owner(inter: discord.Interaction) -> bool:
if inter.user.id == OWNER_ID:
return True
if isinstance(inter.user, discord.Member):
return inter.user.guild_permissions.administrator
return False

async def send_log(guild: discord.Guild, embed: discord.Embed, file: discord.File | None = None):
gcfg = get_gcfg(guild.id)
cid = gcfg.get(“log_channel_id”)
if not cid:
return
ch = guild.get_channel(cid)
if not ch:
return
embed.timestamp = discord.utils.utcnow()
try:
await ch.send(embed=embed, file=file)
except discord.Forbidden:
pass

# ––––– SHARED CLOSE LOGIC –––––

async def do_close_ticket(interaction: discord.Interaction, ticket_info: Dict[str, Any], reason_text: str):
lines: List[str] = []
async for m in interaction.channel.history(limit=1000, oldest_first=True):
ts = m.created_at.strftime(”%Y-%m-%d %H:%M:%S”)
content = m.content or “”
lines.append(f”[{ts}] {m.author}: {content}”)

```
text = "\n".join(lines) if lines else "(empty)"
filename = f"ticket_{interaction.channel.id}_{int(time.time())}.txt"

try:
    u = await bot.fetch_user(int(ticket_info.get("owner_id")))
    dm_file = discord.File(io.BytesIO(text.encode("utf-8")), filename=filename)
    await u.send(f"Your ticket in **{interaction.guild.name}** was closed.\n\n**Reason:** {reason_text}", file=dm_file)
except Exception:
    pass

e = build_transcript_embed(ticket_info, interaction.user, interaction.channel, interaction.guild)
e.add_field(name="Close Reason", value=reason_text, inline=False)
if get_gcfg(interaction.guild.id).get("log_transcripts", True):
    log_file = discord.File(io.BytesIO(text.encode("utf-8")), filename=filename)
    await send_log(interaction.guild, e, file=log_file)
else:
    await send_log(interaction.guild, e)

response_time = int(time.time()) - ticket_info.get("created_at", int(time.time()))
add_close(interaction.guild.id, interaction.user.id, response_time)
save_closed_ticket(interaction.guild.id, ticket_info, interaction.user.id, reason_text)
remove_open_ticket(interaction.guild.id, interaction.channel.id)

try:
    await interaction.channel.delete(reason=f"Ticket closed by {interaction.user}")
except Exception:
    pass
```

# ––––– CLOSE REASON MODAL –––––

class CloseReasonModal(discord.ui.Modal, title=“Close Reason”):
def **init**(self, interaction, ticket_info):
super().**init**()
self.interaction = interaction
self.ticket_info = ticket_info

```
reason = discord.ui.TextInput(
    label="Why are you closing this ticket?",
    style=discord.TextInputStyle.paragraph,
    required=False
)

async def on_submit(self, modal_inter: discord.Interaction):
    await modal_inter.response.defer(thinking=True, ephemeral=True)
    reason_text = self.reason.value or "No reason provided"
    await do_close_ticket(self.interaction, self.ticket_info, reason_text)
    try:
        await modal_inter.followup.send("✅ Ticket closed.", ephemeral=True)
    except Exception:
        pass
```

# ––––– PERSISTENT BUTTONS –––––

class TicketButtons(discord.ui.View):
def **init**(self):
super().**init**(timeout=None)

```
@discord.ui.button(label="Claim", style=discord.ButtonStyle.blurple, custom_id="ticket_claim_btn")
async def claim_btn(self, inter: discord.Interaction, button: discord.ui.Button):
    if not inter.guild:
        return
    gcfg = get_gcfg(inter.guild.id)
    staff_role_id = gcfg.get("staff_role_id")
    if staff_role_id and isinstance(inter.user, discord.Member):
        if staff_role_id not in [r.id for r in inter.user.roles]:
            return await inter.response.send_message("🚫 Only staff can claim this ticket.", ephemeral=True)
    info = find_open_ticket(inter.guild.id, inter.channel.id)
    if not info:
        return await inter.response.send_message("❌ This isn't a ticket channel.", ephemeral=True)
    try:
        await inter.channel.edit(name=f"claimed-{inter.channel.name}")
    except Exception:
        pass
    await inter.response.send_message(f"✅ Ticket claimed by {inter.user.mention}")
    add_claim(inter.guild.id, inter.user.id, info.get("num", 0))
    e = discord.Embed(title="🎟️ Ticket Claimed", description=f"By {inter.user.mention} in {inter.channel.mention}", color=discord.Color.green())
    await send_log(inter.guild, e)

@discord.ui.button(label="Close", style=discord.ButtonStyle.red, custom_id="ticket_close_btn")
async def close_btn(self, inter: discord.Interaction, button: discord.ui.Button):
    if not inter.guild:
        return
    info = find_open_ticket(inter.guild.id, inter.channel.id)
    if not info:
        return await inter.response.send_message("❌ This isn't a ticket channel.", ephemeral=True)

    class ConfirmClose(discord.ui.View):
        def __init__(self):
            super().__init__(timeout=30)

        @discord.ui.button(label="Yes, Close", style=discord.ButtonStyle.red)
        async def yes_btn(self, confirm_inter: discord.Interaction, confirm_button: discord.ui.Button):
            await confirm_inter.response.send_modal(CloseReasonModal(inter, info))

        @discord.ui.button(label="Cancel", style=discord.ButtonStyle.gray)
        async def cancel_btn(self, confirm_inter: discord.Interaction, cancel_button: discord.ui.Button):
            await confirm_inter.response.defer()
            await confirm_inter.followup.send("❌ Cancelled.", ephemeral=True)

    await inter.response.send_message("Are you sure you want to close this ticket?", view=ConfirmClose(), ephemeral=True)
```

# ––––– PANEL BUILDER –––––

def build_panel_view(guild: discord.Guild):
view = discord.ui.View(timeout=None)
gcfg = get_gcfg(guild.id)
cats = gcfg.get(“categories”, [])[:25]

```
options = []
for idx, c in enumerate(cats):
    label = c.get("name", f"Category {idx+1}")[:100]
    desc = c.get("description", "")[:100]
    opt = discord.SelectOption(label=label, value=str(idx))
    if desc:
        opt.description = desc
    options.append(opt)

if not options:
    return view

class TicketSelect(discord.ui.Select):
    def __init__(self):
        super().__init__(
            placeholder="📋 Select a ticket category...",
            min_values=1,
            max_values=1,
            options=options,
            custom_id=f"ticket_select:{guild.id}"
        )

    async def callback(self, inter: discord.Interaction):
        idx = int(self.values[0])
        gcfg_local = get_gcfg(inter.guild.id)
        categories = gcfg_local.get("categories", [])
        if idx >= len(categories):
            return await inter.response.send_message("⚠️ That category no longer exists.", ephemeral=True)
        await inter.response.send_modal(TicketReasonModal(inter, idx, categories, gcfg_local))

view.add_item(TicketSelect())
return view
```

# ––––– TICKET REASON MODAL –––––

class TicketReasonModal(discord.ui.Modal, title=“Describe Your Issue”):
def **init**(self, interaction, category_idx, categories, gcfg_data):
super().**init**()
self.interaction = interaction
self.category_idx = category_idx
self.categories = categories
self.gcfg_data = gcfg_data

```
reason = discord.ui.TextInput(
    label="What is your question or issue?",
    style=discord.TextInputStyle.paragraph,
    placeholder="Please describe why you're opening a ticket...",
    required=True
)

async def on_submit(self, modal_inter: discord.Interaction):
    await modal_inter.response.defer(thinking=True, ephemeral=True)
    reason_text = self.reason.value

    self.gcfg_data["tickets_created"] = int(self.gcfg_data.get("tickets_created", 0)) + 1
    num = self.gcfg_data["tickets_created"]
    set_gcfg(self.interaction.guild.id, self.gcfg_data)

    cat = self.categories[self.category_idx]
    disc_cat = discord.utils.get(self.interaction.guild.categories, name=cat["name"])
    if disc_cat is None:
        try:
            disc_cat = await self.interaction.guild.create_category(cat["name"])
        except Exception:
            disc_cat = None

    overwrites = {
        self.interaction.guild.default_role: discord.PermissionOverwrite(view_channel=False),
        self.interaction.user: discord.PermissionOverwrite(view_channel=True, send_messages=True, attach_files=True, embed_links=True, read_message_history=True),
    }
    staff_role_id = self.gcfg_data.get("staff_role_id")
    if staff_role_id:
        role = self.interaction.guild.get_role(staff_role_id)
        if role:
            overwrites[role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)

    ch_name = f"ticket-{self.interaction.user.name}-{num}"
    try:
        tchan = await self.interaction.guild.create_text_channel(
            name=ch_name,
            category=disc_cat,
            overwrites=overwrites,
            reason=f"Ticket opened by {self.interaction.user}"
        )
    except discord.Forbidden:
        return await modal_inter.followup.send("❌ I don't have permission to create channels.", ephemeral=True)

    add_open_ticket(self.interaction.guild.id, tchan.id, self.interaction.user.id, num, reason_text)

    role_ping = ""
    ping_role_id = cat.get("role_id")
    if ping_role_id:
        role = self.interaction.guild.get_role(int(ping_role_id))
        if role:
            role_ping = role.mention

    cat_description = cat.get("description", "")

    embed = discord.Embed(
        title="🎫 Ticket Opened",
        description="Thank you for contacting support.\nPlease describe your issue and wait for a response.",
        color=BLUE
    )
    embed.add_field(name="Category", value=cat["name"], inline=True)
    if cat_description:
        embed.add_field(name="Category Info", value=cat_description, inline=True)
    embed.add_field(name="Issue", value=reason_text, inline=False)
    embed.set_footer(text="Powered by TicketMastery")

    await tchan.send(
        content=f"{self.interaction.user.mention} {role_ping}".strip(),
        embed=embed,
        view=TicketButtons()
    )
    await modal_inter.followup.send(f"✅ Ticket created: {tchan.mention}", ephemeral=True)
```

# ––––– EVENTS –––––

@bot.event
async def on_message(message: discord.Message):
if message.author.bot or not message.guild:
return
info = find_open_ticket(message.guild.id, message.channel.id)
if info:
open_map = get_open(message.guild.id)
info[“last_activity”] = int(time.time())
open_map[str(message.channel.id)] = info
set_open(message.guild.id, open_map)
await bot.process_commands(message)

@bot.event
async def on_ready():
try:
synced = await tree.sync()
print(f”✅ Synced {len(synced)} global slash commands.”)
except Exception as e:
print(f”Slash sync error: {e}”)

```
try:
    bot.add_view(TicketButtons())
except Exception as e:
    print(f"add_view warning: {e}")

all_gcfg = _load_json(GCFG_FILE)
for gid_str, gcfg_data in all_gcfg.items():
    gid = int(gid_str)
    guild = bot.get_guild(gid)
    if not guild:
        continue

    panel_ch_id = gcfg_data.get("panel_channel_id")
    panel_msg_id = gcfg_data.get("panel_message_id")
    cats = gcfg_data.get("categories", [])

    if not panel_ch_id or not cats:
        continue

    channel = guild.get_channel(panel_ch_id)
    if not channel:
        continue

    embed = discord.Embed(
        title="🎫 Open a Ticket",
        description=gcfg_data.get("panel_description") or "Open a ticket using the buttons below.",
        color=BLUE
    )
    view = build_panel_view(guild)

    # Edit existing panel instead of delete + repost
    try:
        existing_msg = await channel.fetch_message(panel_msg_id)
        await existing_msg.edit(embed=embed, view=view)
    except discord.NotFound:
        try:
            new_msg = await channel.send(embed=embed, view=view)
            gcfg_data["panel_message_id"] = new_msg.id
            all_gcfg[gid_str] = gcfg_data
            _save_json(GCFG_FILE, all_gcfg)
        except Exception:
            pass
    except Exception:
        pass

    await asyncio.sleep(1)

presence_loop.start()
inactivity_scan.start()
print(f"Bot ready as {bot.user}")
```

# ––––– PRESENCE –––––

@tasks.loop(minutes=5)
async def presence_loop():
statuses = [
(“watching”, “tickets 🎫”),
(“watching”, “support requests 📋”),
(“playing”, “Managing tickets 🎫”),
(“listening”, “Tickets | /help”),
(“watching”, “supporting servers 💬”),
]
current_status = statuses[int(time.time() / 300) % len(statuses)]
activity_type = discord.ActivityType.watching if current_status[0] == “watching” else   
discord.ActivityType.playing if current_status[0] == “playing” else   
discord.ActivityType.listening
try:
await bot.change_presence(activity=discord.Activity(type=activity_type, name=current_status[1]))
except Exception:
pass

# ––––– INACTIVITY SCANNER –––––

@tasks.loop(seconds=SCAN_INTERVAL)
async def inactivity_scan():
if not bot.is_ready():
return
now = int(time.time())
open_all = _load_json(OPEN_FILE)

```
for g_key, channels in list(open_all.items()):
    gid = int(g_key)
    guild = bot.get_guild(gid)
    if not guild:
        continue
    gcfg = get_gcfg(gid)
    if not gcfg.get("auto_close_enabled", True):
        continue

    for ch_key, info in list(channels.items()):
        ch_id = int(ch_key)
        if info.get("hold", False):
            continue
        last = int(info.get("last_activity", info.get("created_at", now)))
        elapsed = now - last
        remaining = INACTIVITY_SECONDS - elapsed

        if remaining <= REMINDER_BEFORE and remaining > 0 and not info.get("reminded24", False):
            channel = guild.get_channel(ch_id)
            if isinstance(channel, discord.TextChannel):
                try:
                    await channel.send("⏳ This ticket will close in **24 hours** due to inactivity.")
                    info["reminded24"] = True
                    channels[ch_key] = info
                    open_all[g_key] = channels
                    _save_json(OPEN_FILE, open_all)
                except Exception:
                    pass
            continue

        if elapsed >= INACTIVITY_SECONDS:
            channel = guild.get_channel(ch_id)
            if isinstance(channel, discord.TextChannel):
                lines: List[str] = []
                try:
                    async for m in channel.history(limit=1000, oldest_first=True):
                        ts = m.created_at.strftime("%Y-%m-%d %H:%M:%S")
                        lines.append(f"[{ts}] {m.author}: {m.content or ''}")
                except Exception:
                    pass

                transcript_text = "\n".join(lines) if lines else "(empty)"
                filename = f"ticket_{ch_id}_{now}.txt"

                try:
                    opener = await bot.fetch_user(int(info.get("owner_id", 0)))
                    if opener:
                        dm_file = discord.File(io.BytesIO(transcript_text.encode("utf-8")), filename=filename)
                        await opener.send(f"⏳ Your ticket in **{guild.name}** was closed due to inactivity (48h).", file=dm_file)
                except Exception:
                    pass

                e = discord.Embed(title="🔒 Ticket Auto-Closed", description=f"Channel: <#{ch_id}>\nReason: **48h inactivity**", color=discord.Color.red())
                if get_gcfg(gid).get("log_transcripts", True):
                    log_file = discord.File(io.BytesIO(transcript_text.encode("utf-8")), filename=filename)
                    await send_log(guild, e, file=log_file)
                else:
                    await send_log(guild, e)

                try:
                    await channel.delete(reason="Auto-close after 48h inactivity")
                except Exception:
                    pass

            del channels[ch_key]
            open_all[g_key] = channels
            _save_json(OPEN_FILE, open_all)
```

# ––––– COMMANDS –––––

def admin_owner_check():
async def predicate(inter: discord.Interaction):
if not is_admin_or_owner(inter):
await inter.response.send_message(“🚫 Admins (or bot owner) only.”, ephemeral=True)
return False
return True
return app_commands.check(predicate)

@tree.command(name=“ping”, description=“Show bot latency.”)
async def ping(inter: discord.Interaction):
await inter.response.send_message(f”🏓 {round(bot.latency*1000)}ms”)

@tree.command(name=“help”, description=“Get help and support info”)
async def help_command(inter: discord.Interaction):
embed = discord.Embed(title=“📘 TicketMastery Help”, color=BLUE)
embed.add_field(name=“🎫 Tickets”, value=”`/ticket_close` – Close your ticket\n`/claim` – Claim a ticket\n`/ticket_reopen` – Request to reopen\n`/ticket_status` – Server ticket stats\n`/ticket_hold` – Hold ticket\n`/ticket_unhold` – Unhold ticket”, inline=False)
embed.add_field(name=“⚙️ Setup”, value=”`/set_staff` – Set staff role\n`/set_logs` – Set log channel\n`/set_reopen_channel` – Set reopen channel\n`/categories_add` – Add category”, inline=False)
embed.add_field(name=“🛠️ Admin”, value=”`/panel` – Post ticket panel\n`/auto_close` – Toggle auto-close\n`/categories_manage` – View/edit categories\n`/staff_stats` – View staff performance”, inline=False)
embed.add_field(name=“🔗 Support”, value=”[Join Support Server](https://discord.gg/TsbRPDXWJS)”, inline=False)
embed.set_footer(text=“TicketMastery • Always here to help”)
await inter.response.send_message(embed=embed)

@tree.command(name=“set_staff”, description=“Set the staff role (Admin/Owner only).”)
@admin_owner_check()
async def set_staff(inter: discord.Interaction, role: discord.Role | None):
gcfg = get_gcfg(inter.guild.id)
gcfg[“staff_role_id”] = role.id if role else None
set_gcfg(inter.guild.id, gcfg)
await inter.response.send_message(f”✅ Staff role set to {role.mention}” if role else “✅ Staff role **cleared**.”, ephemeral=True)

@tree.command(name=“set_logs”, description=“Set the log channel (Admin/Owner only).”)
@admin_owner_check()
async def set_logs(inter: discord.Interaction, channel: discord.TextChannel | None):
gcfg = get_gcfg(inter.guild.id)
gcfg[“log_channel_id”] = channel.id if channel else None
set_gcfg(inter.guild.id, gcfg)
await inter.response.send_message(f”✅ Log channel set to {channel.mention}” if channel else “✅ Log channel **cleared**.”, ephemeral=True)

@tree.command(name=“set_reopen_channel”, description=“Set reopen requests channel (Admin/Owner only).”)
@admin_owner_check()
async def set_reopen_channel(inter: discord.Interaction, channel: discord.TextChannel):
gcfg = get_gcfg(inter.guild.id)
gcfg[“reopen_channel_id”] = channel.id
set_gcfg(inter.guild.id, gcfg)
await inter.response.send_message(f”✅ Reopen channel set to {channel.mention}”, ephemeral=True)

@tree.command(name=“set_panel_desc”, description=“Set the panel description (Admin/Owner only).”)
@admin_owner_check()
async def set_panel_desc(inter: discord.Interaction, description: str):
gcfg = get_gcfg(inter.guild.id)
gcfg[“panel_description”] = description[:1000]
set_gcfg(inter.guild.id, gcfg)
await inter.response.send_message(“✅ Panel description updated.”, ephemeral=True)

@tree.command(name=“auto_close”, description=“Enable/disable auto-close (Admin/Owner only).”)
@admin_owner_check()
async def auto_close(inter: discord.Interaction, enabled: bool):
gcfg = get_gcfg(inter.guild.id)
gcfg[“auto_close_enabled”] = enabled
set_gcfg(inter.guild.id, gcfg)
await inter.response.send_message(f”✅ Auto-close set to **{enabled}**.”, ephemeral=True)

@tree.command(name=“log_transcripts”, description=“Toggle transcript logging (Admin/Owner only).”)
@admin_owner_check()
async def log_transcripts(inter: discord.Interaction, enabled: bool):
gcfg = get_gcfg(inter.guild.id)
gcfg[“log_transcripts”] = enabled
set_gcfg(inter.guild.id, gcfg)
await inter.response.send_message(f”✅ Log transcripts = **{enabled}**.”, ephemeral=True)

@tree.command(name=“categories_add”, description=“Add a ticket category (Admin/Owner only).”)
@admin_owner_check()
async def categories_add(inter: discord.Interaction, name: str, description: str = None, ping_role: Optional[discord.Role] = None):
gcfg = get_gcfg(inter.guild.id)
cats = gcfg.get(“categories”, [])
if len(cats) >= 10:
return await inter.response.send_message(“❌ Maximum 10 categories.”, ephemeral=True)
cats.append({“name”: name, “description”: description or “”, “role_id”: ping_role.id if ping_role else None})
gcfg[“categories”] = cats
set_gcfg(inter.guild.id, gcfg)
msg = f”✅ Added category `{name}`”
if description:
msg += f”\n> {description}”
if ping_role:
msg += f”\n> Pings {ping_role.mention}”
await inter.response.send_message(msg, ephemeral=True)

@tree.command(name=“categories_edit”, description=“Edit an existing category (Admin/Owner only).”)
@admin_owner_check()
async def categories_edit(inter: discord.Interaction, number: int, name: str = None, description: str = None, ping_role: Optional[discord.Role] = None):
gcfg = get_gcfg(inter.guild.id)
cats = gcfg.get(“categories”, [])
idx = number - 1
if idx < 0 or idx >= len(cats):
return await inter.response.send_message(“❌ Invalid category number.”, ephemeral=True)
if name:
cats[idx][“name”] = name
if description is not None:
cats[idx][“description”] = description
if ping_role:
cats[idx][“role_id”] = ping_role.id
gcfg[“categories”] = cats
set_gcfg(inter.guild.id, gcfg)
await inter.response.send_message(f”✅ Category {number} updated.”, ephemeral=True)

@tree.command(name=“categories_manage”, description=“View and manage all ticket categories (Admin/Owner only).”)
@admin_owner_check()
async def categories_manage(inter: discord.Interaction):
gcfg = get_gcfg(inter.guild.id)
cats = gcfg.get(“categories”, [])
if not cats:
return await inter.response.send_message(“No categories set. Use `/categories_add`.”, ephemeral=True)
lines = []
for i, c in enumerate(cats):
name = c.get(“name”, f”Category {i+1}”)
desc = c.get(“description”) or “No description set”
rid = c.get(“role_id”)
ping = f”<@&{rid}>” if rid else “No ping”
lines.append(f”**{i+1}. {name}**\n> {desc}\n> Ping: {ping}”)
embed = discord.Embed(title=“📂 Ticket Categories”, description=”\n\n”.join(lines), color=BLUE)
embed.set_footer(text=“Use /categories_edit <number> to edit a category”)
await inter.response.send_message(embed=embed, ephemeral=True)

@tree.command(name=“categories_clear”, description=“Clear all ticket categories (Admin/Owner only).”)
@admin_owner_check()
async def categories_clear(inter: discord.Interaction):
gcfg = get_gcfg(inter.guild.id)
gcfg[“categories”] = []
set_gcfg(inter.guild.id, gcfg)
await inter.response.send_message(“🧹 Cleared all categories.”, ephemeral=True)

@tree.command(name=“categories_list”, description=“List all ticket categories.”)
async def categories_list(inter: discord.Interaction):
gcfg = get_gcfg(inter.guild.id)
cats = gcfg.get(“categories”, [])
if not cats:
return await inter.response.send_message(“No categories set.”, ephemeral=True)
lines = []
for i, c in enumerate(cats, 1):
nm = c.get(“name”, f”Category {i}”)
desc = c.get(“description”) or “No description”
rid = c.get(“role_id”)
rp = f”<@&{rid}>” if rid else “No ping”
lines.append(f”{i}. **{nm}** — {rp}\n> {desc}”)
e = discord.Embed(title=“📂 Categories”, description=”\n”.join(lines), color=BLUE)
await inter.response.send_message(embed=e, ephemeral=True)

@tree.command(name=“panel”, description=“Post the ticket panel. (Admin/Owner only)”)
@admin_owner_check()
async def panel(inter: discord.Interaction, description: Optional[str] = None):
gcfg = get_gcfg(inter.guild.id)
cats = gcfg.get(“categories”, [])
if not cats:
return await inter.response.send_message(“⚠️ No categories configured. Use `/categories_add`.”, ephemeral=True)

```
if description is not None:
    gcfg["panel_description"] = description[:1000]
    set_gcfg(inter.guild.id, gcfg)

embed = discord.Embed(
    title="🎫 Open a Ticket",
    description=gcfg.get("panel_description") or "Open a ticket using the buttons below.",
    color=BLUE
)
view = build_panel_view(inter.guild)

if gcfg.get("panel_message_id") and gcfg.get("panel_channel_id"):
    try:
        ch = inter.guild.get_channel(gcfg["panel_channel_id"])
        if ch:
            old_msg = await ch.fetch_message(gcfg["panel_message_id"])
            await old_msg.delete()
    except Exception:
        pass

await inter.response.send_message(embed=embed, view=view)
try:
    msg = await inter.original_response()
    gcfg["panel_channel_id"] = inter.channel.id
    gcfg["panel_message_id"] = msg.id
    set_gcfg(inter.guild.id, gcfg)
except Exception:
    pass
```

@tree.command(name=“claim”, description=“Claim the current ticket.”)
async def claim(inter: discord.Interaction):
if not inter.guild:
return await inter.response.send_message(“Guild only.”, ephemeral=True)
info = find_open_ticket(inter.guild.id, inter.channel.id)
if not info:
return await inter.response.send_message(“❌ This isn’t a ticket channel.”, ephemeral=True)
gcfg = get_gcfg(inter.guild.id)
staff_role_id = gcfg.get(“staff_role_id”)
if staff_role_id and isinstance(inter.user, discord.Member):
if staff_role_id not in [r.id for r in inter.user.roles]:
return await inter.response.send_message(“🚫 Only staff can claim this ticket.”, ephemeral=True)
try:
await inter.channel.edit(name=f”claimed-{inter.channel.name}”)
except Exception:
pass
await inter.response.send_message(f”✅ Ticket claimed by {inter.user.mention}”)
add_claim(inter.guild.id, inter.user.id, info.get(“num”, 0))
e = discord.Embed(title=“🎟️ Ticket Claimed”, description=f”By {inter.user.mention} in {inter.channel.mention}”, color=discord.Color.green())
await send_log(inter.guild, e)

@tree.command(name=“ticket_close”, description=“Close this ticket.”)
async def ticket_close(inter: discord.Interaction):
if not inter.guild:
return await inter.response.send_message(“Guild only.”, ephemeral=True)
info = find_open_ticket(inter.guild.id, inter.channel.id)
if not info:
return await inter.response.send_message(“❌ This isn’t a ticket channel.”, ephemeral=True)

```
class ConfirmClose(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=30)

    @discord.ui.button(label="Yes, Close", style=discord.ButtonStyle.red)
    async def yes_btn(self, confirm_inter: discord.Interaction, confirm_button: discord.ui.Button):
        await confirm_inter.response.send_modal(CloseReasonModal(inter, info))

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.gray)
    async def cancel_btn(self, confirm_inter: discord.Interaction, cancel_button: discord.ui.Button):
        await confirm_inter.response.defer()
        await confirm_inter.followup.send("❌ Cancelled.", ephemeral=True)

await inter.response.send_message("Are you sure you want to close this ticket?", view=ConfirmClose(), ephemeral=True)
```

@tree.command(name=“ticket_hold”, description=“Prevent this ticket from auto-closing (staff only if set).”)
async def ticket_hold(inter: discord.Interaction):
if not inter.guild:
return await inter.response.send_message(“Guild only.”, ephemeral=True)
info = find_open_ticket(inter.guild.id, inter.channel.id)
if not info:
return await inter.response.send_message(“❌ This isn’t a ticket channel.”, ephemeral=True)
gcfg = get_gcfg(inter.guild.id)
staff_role_id = gcfg.get(“staff_role_id”)
if staff_role_id and isinstance(inter.user, discord.Member):
if staff_role_id not in [r.id for r in inter.user.roles]:
return await inter.response.send_message(“🚫 Only staff can hold tickets.”, ephemeral=True)
info[“hold”] = True
om = get_open(inter.guild.id)
om[str(inter.channel.id)] = info
set_open(inter.guild.id, om)
await inter.response.send_message(“⛔ Ticket is now **on hold** (no auto-close).”)

@tree.command(name=“ticket_unhold”, description=“Allow this ticket to auto-close again (staff only if set).”)
async def ticket_unhold(inter: discord.Interaction):
if not inter.guild:
return await inter.response.send_message(“Guild only.”, ephemeral=True)
info = find_open_ticket(inter.guild.id, inter.channel.id)
if not info:
return await inter.response.send_message(“❌ This isn’t a ticket channel.”, ephemeral=True)
gcfg = get_gcfg(inter.guild.id)
staff_role_id = gcfg.get(“staff_role_id”)
if staff_role_id and isinstance(inter.user, discord.Member):
if staff_role_id not in [r.id for r in inter.user.roles]:
return await inter.response.send_message(“🚫 Only staff can unhold tickets.”, ephemeral=True)
info[“hold”] = False
om = get_open(inter.guild.id)
om[str(inter.channel.id)] = info
set_open(inter.guild.id, om)
await inter.response.send_message(“▶️ Ticket is **off hold** (auto-close active).”)

@tree.command(name=“ticket_reopen”, description=“Request to reopen a closed ticket.”)
async def ticket_reopen(inter: discord.Interaction):
if not inter.guild:
return await inter.response.send_message(“Guild only.”, ephemeral=True)
gcfg = get_gcfg(inter.guild.id)
reopen_channel_id = gcfg.get(“reopen_channel_id”)
if not reopen_channel_id:
return await inter.response.send_message(“⚠️ Reopen channel not configured.”, ephemeral=True)
channel = inter.guild.get_channel(reopen_channel_id)
if not channel:
return await inter.response.send_message(“⚠️ Reopen channel not found.”, ephemeral=True)

```
embed = discord.Embed(title="🔄 Reopen Request", description=f"User {inter.user.mention} requested to reopen a ticket.", color=discord.Color.orange())
view = discord.ui.View()

async def approve_callback(btn_inter: discord.Interaction):
    await btn_inter.response.defer()
    try:
        await inter.user.send(f"Your ticket reopen request in **{inter.guild.name}** was **approved**!")
    except Exception:
        pass

async def deny_callback(btn_inter: discord.Interaction):
    await btn_inter.response.defer()
    try:
        await inter.user.send(f"Your ticket reopen request in **{inter.guild.name}** was **denied**.")
    except Exception:
        pass

approve_btn = discord.ui.Button(label="Approve", style=discord.ButtonStyle.green)
deny_btn = discord.ui.Button(label="Deny", style=discord.ButtonStyle.red)
approve_btn.callback = approve_callback
deny_btn.callback = deny_callback
view.add_item(approve_btn)
view.add_item(deny_btn)

await channel.send(embed=embed, view=view)
await inter.response.send_message("✅ Reopen request sent to staff.", ephemeral=True)
```

@tree.command(name=“ticket_status”, description=“Show server ticket statistics.”)
async def ticket_status(inter: discord.Interaction):
if not inter.guild:
return await inter.response.send_message(“Guild only.”, ephemeral=True)
open_tickets = get_open(inter.guild.id)
embed = discord.Embed(title=“🎫 Ticket Status”, color=BLUE)
embed.add_field(name=“Open Tickets”, value=str(len(open_tickets)), inline=True)
embed.add_field(name=“Total Created”, value=str(get_gcfg(inter.guild.id).get(“tickets_created”, 0)), inline=True)
await inter.response.send_message(embed=embed, ephemeral=True)

@tree.command(name=“staff_stats”, description=“View staff performance stats (Admin/Owner only).”)
@admin_owner_check()
async def staff_stats(inter: discord.Interaction):
stats = get_staff_stats(inter.guild.id)
if not stats:
return await inter.response.send_message(“No staff stats yet.”, ephemeral=True)
lines = []
for staff_id_str, data in stats.items():
try:
staff = inter.guild.get_member(int(staff_id_str))
if not staff:
staff = await inter.client.fetch_user(int(staff_id_str))
name = staff.mention if staff else f”<@{staff_id_str}>”
except Exception:
name = f”<@{staff_id_str}>”
claimed = data.get(“claimed”, 0)
closed = data.get(“closed”, 0)
times = data.get(“response_times”, [])
avg_mins = (sum(times) // len(times)) // 60 if times else 0
lines.append(f”{name}: **{claimed}** claimed, **{closed}** closed, avg **{avg_mins}m** response”)
e = discord.Embed(title=“📊 Staff Stats”, description=”\n”.join(lines), color=BLUE)
await inter.response.send_message(embed=e, ephemeral=True)

# ––––– RUN –––––

if **name** == “**main**”:
if not TOKEN:
print(“ERROR: DISCORD_TOKEN missing in .env”)
else:
bot.run(TOKEN)
