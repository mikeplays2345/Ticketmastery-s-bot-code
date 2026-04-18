import os, json, io, asyncio, time
from typing import Dict, Any, Optional, List
from datetime import datetime
from dotenv import load_dotenv

import discord
from discord.ext import commands, tasks
from discord import app_commands

# ---------- ENV ----------
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN") or os.getenv("TOKEN") or ""
OWNER_ID = 720061069628014652  # your override ID (admin bypass for setup)

# ---------- CONSTANTS ----------
BLUE = discord.Color.blurple()
INACTIVITY_SECONDS = 48 * 3600
REMINDER_BEFORE = 24 * 3600  # send reminder when <= 24h left
SCAN_INTERVAL = 600          # 10 minutes between inactivity scans

# ---------- FILES ----------
GCFG_FILE = "guild_configs.json"
OPEN_FILE = "opened_tickets.json"
STAFF_STATS_FILE = "staff_stats.json"

def _ensure_file(path: str, default: Any):
    if not os.path.exists(path):
        with open(path, "w", encoding="utf-8") as f:
            json.dump(default, f, indent=4)

def _load_json(path: str) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def _save_json(path: str, data: Any):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4)

_ensure_file(GCFG_FILE, {})
_ensure_file(OPEN_FILE, {})
_ensure_file(STAFF_STATS_FILE, {})

def get_gcfg(gid: int) -> Dict[str, Any]:
    data = _load_json(GCFG_FILE)
    s = str(gid)
    if s not in data:
        data[s] = {
            "tickets_created": 0,
            "categories": [],
            "staff_role_id": None,
            "log_channel_id": None,
            "panel_description": "Open a ticket using the buttons below.",
            "auto_close_enabled": True,
            "log_transcripts": True,
            "panel_channel_id": None,
            "panel_message_id": None
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

def add_open_ticket(gid: int, channel_id: int, owner_id: int, num: int):
    cur = get_open(gid)
    now = int(time.time())
    cur[str(channel_id)] = {
        "owner_id": owner_id,
        "num": num,
        "created_at": now,
        "last_activity": now,
        "reminded24": False,
        "hold": False
    }
    set_open(gid, cur)

def remove_open_ticket(gid: int, channel_id: int):
    cur = get_open(gid)
    if str(channel_id) in cur:
        del cur[str(channel_id)]
        set_open(gid, cur)

def find_open_ticket(gid: int, channel_id: int) -> Optional[Dict[str, Any]]:
    return get_open(gid).get(str(channel_id))

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
        stats[sid] = {"claimed": 0, "closed": 0, "response_times": []}
    stats[sid]["claimed"] += 1
    set_staff_stats(gid, stats)

def add_close(gid: int, staff_id: int, response_time: int):
    stats = get_staff_stats(gid)
    sid = str(staff_id)
    if sid not in stats:
        stats[sid] = {"claimed": 0, "closed": 0, "response_times": []}
    stats[sid]["closed"] += 1
    stats[sid]["response_times"].append(response_time)
    set_staff_stats(gid, stats)

def build_transcript_embed(ticket_info: Dict[str, Any], closer: discord.User, channel: discord.TextChannel, guild: discord.Guild) -> discord.Embed:
    opener_id = ticket_info.get("owner_id")
    created = ticket_info.get("created_at", int(time.time()))
    now = int(time.time())
    duration = now - created
    
    hours = duration // 3600
    minutes = (duration % 3600) // 60
    time_str = f"{hours}h {minutes}m" if hours > 0 else f"{minutes}m"
    
    created_at = datetime.fromtimestamp(created).strftime("%B %d, %Y at %I:%M %p")
    closed_at = datetime.fromtimestamp(now).strftime("%B %d, %Y at %I:%M %p")
    
    e = discord.Embed(title="🔒 Ticket Closed", color=discord.Color.red())
    e.add_field(name="Ticket ID", value=f"{ticket_info.get('num', 'N/A')}", inline=True)
    e.add_field(name="Opened By", value=f"<@{opener_id}>", inline=True)
    e.add_field(name="Closed By", value=f"{closer.mention}", inline=True)
    e.add_field(name="Open Time", value=time_str, inline=True)
    e.add_field(name="Channel", value=channel.mention, inline=True)
    e.add_field(name="Created", value=created_at, inline=False)
    e.add_field(name="Closed", value=closed_at, inline=False)
    e.set_footer(text=f"{guild.name}")
    
    return e

# ---------- DISCORD SETUP ----------
intents = discord.Intents.default()
intents.guilds = True
intents.members = True
intents.messages = True

bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

# ---------- HELPERS ----------
def is_admin_or_owner(inter: discord.Interaction) -> bool:
    if inter.user.id == OWNER_ID:
        return True
    if isinstance(inter.user, discord.Member):
        return inter.user.guild_permissions.administrator
    return False

async def send_log(guild: discord.Guild, embed: discord.Embed, file: discord.File | None = None):
    gcfg = get_gcfg(guild.id)
    cid = gcfg.get("log_channel_id")
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

# ---------- PRESENCE ----------
@tasks.loop(minutes=5)
async def presence_loop():
    statuses = [
        ("Watching", "tickets 🎫"),
        ("Watching", "support requests 📋"),
        ("Playing", "Managing tickets 🎫"),
        ("Listening", "Tickets | /help"),
        ("Watching", "supporting servers 💬"),
    ]
    
    current_status = statuses[int(time.time() / 300) % len(statuses)]
    activity_type = discord.ActivityType.watching if current_status[0] == "Watching" else \
                    discord.ActivityType.playing if current_status[0] == "Playing" else \
                    discord.ActivityType.listening
    
    try:
        await bot.change_presence(activity=discord.Activity(
            type=activity_type,
            name=current_status[1]
        ))
    except Exception:
        pass

# ---------- INACTIVITY SCANNER ----------
@tasks.loop(seconds=SCAN_INTERVAL)
async def inactivity_scan():
    if not bot.is_ready():
        return
    now = int(time.time())
    open_all = _load_json(OPEN_FILE)

    # Loop guilds & channels
    for g_key, channels in list(open_all.items()):
        gid = int(g_key)
        guild = bot.get_guild(gid)
        if not guild:
            continue
        gcfg = get_gcfg(gid)
        if not gcfg.get("auto_close_enabled", True):
            continue  # server disabled auto-close

        for ch_key, info in list(channels.items()):
            ch_id = int(ch_key)
            hold = info.get("hold", False)
            if hold:
                continue
            last = int(info.get("last_activity", info.get("created_at", now)))
            elapsed = now - last
            remaining = INACTIVITY_SECONDS - elapsed

            # Send 24h reminder once, when remaining <= 24h but > 0
            if remaining <= REMINDER_BEFORE and remaining > 0 and not info.get("reminded24", False):
                channel = guild.get_channel(ch_id)
                if isinstance(channel, discord.TextChannel):
                    try:
                        # Reminder message does NOT reset timer (we only update on non-bot messages)
                        await channel.send("⏳ This ticket will close in **24 hours** due to inactivity.")
                        info["reminded24"] = True
                        channels[ch_key] = info
                        open_all[g_key] = channels
                        _save_json(OPEN_FILE, open_all)
                    except Exception:
                        pass
                continue

            # Close if exceeded
            if elapsed >= INACTIVITY_SECONDS:
                channel = guild.get_channel(ch_id)
                if isinstance(channel, discord.TextChannel):
                    # Build transcript quickly
                    lines: List[str] = []
                    try:
                        async for m in channel.history(limit=1000, oldest_first=True):
                            ts = m.created_at.strftime("%Y-%m-%d %H:%M:%S")
                            content = m.content or ""
                            lines.append(f"[{ts}] {m.author}: {content}")
                    except Exception:
                        pass

                    transcript_text = "\n".join(lines) if lines else "(empty)"
                    fobj = io.BytesIO(transcript_text.encode("utf-8"))
                    filename = f"ticket_{ch_id}_{now}.txt"
                    file = discord.File(fobj, filename=filename)

                    # DM opener
                    try:
                        opener = await bot.fetch_user(int(info.get("owner_id", 0)))
                        if opener:
                            await opener.send(f"⏳ Your ticket in **{guild.name}** was closed due to inactivity (48h).", file=file)
                    except Exception:
                        pass

                    # Log close
                    e = discord.Embed(
                        title="🔒 Ticket Auto-Closed",
                        description=f"Channel: <#{ch_id}>\nReason: **48h inactivity**",
                        color=discord.Color.red()
                    )
                    lf = file if get_gcfg(gid).get("log_transcripts", True) else None
                    await send_log(guild, e, file=lf)

                    # Delete channel & remove from registry
                    try:
                        await channel.delete(reason="Auto-close after 48h inactivity")
                    except Exception:
                        pass

                # Remove regardless of channel delete success
                del channels[ch_key]
                open_all[g_key] = channels
                _save_json(OPEN_FILE, open_all)

# ---------- PERSISTENT BUTTONS ----------
class TicketButtons(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

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

        # Confirmation view
        class ConfirmClose(discord.ui.View):
            def __init__(self):
                super().__init__(timeout=30)
                self.confirmed = False

            @discord.ui.button(label="Yes, Close", style=discord.ButtonStyle.red)
            async def yes_btn(self, confirm_inter: discord.Interaction, confirm_button: discord.ui.Button):
                await confirm_inter.response.defer(thinking=True, ephemeral=True)
                await self.do_close_ticket(inter.guild, inter.user, inter.channel, info, confirm_inter)

            @discord.ui.button(label="Cancel", style=discord.ButtonStyle.gray)
            async def cancel_btn(self, confirm_inter: discord.Interaction, cancel_button: discord.ui.Button):
                await confirm_inter.response.defer()
                await confirm_inter.followup.send("❌ Ticket close cancelled.", ephemeral=True)

            async def do_close_ticket(self, guild, closer, channel, ticket_info, confirm_inter):
                # Transcript
                lines: List[str] = []
                async for m in channel.history(limit=1000, oldest_first=True):
                    ts = m.created_at.strftime("%Y-%m-%d %H:%M:%S")
                    content = m.content or ""
                    lines.append(f"[{ts}] {m.author}: {content}")

                text = "\n".join(lines) if lines else "(empty)"
                fobj = io.BytesIO(text.encode("utf-8"))
                filename = f"ticket_{channel.id}_{int(time.time())}.txt"
                file = discord.File(fobj, filename=filename)

                # DM opener
                try:
                    u = await bot.fetch_user(int(ticket_info.get("owner_id", closer.id)))
                    await u.send(f"Here is your ticket transcript from **{guild.name}**.", file=file)
                except Exception:
                    pass

                # Log with nice embed
                e = build_transcript_embed(ticket_info, closer, channel, guild)
                lf = file if get_gcfg(guild.id).get("log_transcripts", True) else None
                await send_log(guild, e, file=lf)

                # Track close
                response_time = int(time.time()) - ticket_info.get("created_at", int(time.time()))
                add_close(guild.id, closer.id, response_time)

                # Remove & delete channel
                remove_open_ticket(guild.id, channel.id)
                try:
                    await channel.delete(reason=f"Ticket closed by {closer}")
                except Exception:
                    pass

                try:
                    await confirm_inter.followup.send("✅ Ticket closed & transcript sent.", ephemeral=True)
                except Exception:
                    pass

        view = ConfirmClose()
        await inter.response.send_message("Are you sure you want to close this ticket?", view=view, ephemeral=True)

# ---------- UTIL: Build Panel ----------
def build_panel_view(guild: discord.Guild):
    view = discord.ui.View(timeout=None)
    gcfg = get_gcfg(guild.id)
    cats = gcfg.get("categories", [])[:25]  # Dropdowns support up to 25 options

    # Build dropdown options
    options = []
    for idx, c in enumerate(cats):
        label = c.get("name", f"Category {idx+1}")[:100]
        options.append(discord.SelectOption(label=label, value=str(idx)))

    if not options:
        return view  # No categories, return empty view

    # Create dropdown select
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

            await inter.response.defer(thinking=True, ephemeral=True)  # Defer immediately

            # Ticket counter
            gcfg_local["tickets_created"] = int(gcfg_local.get("tickets_created", 0)) + 1
            num = gcfg_local["tickets_created"]
            set_gcfg(inter.guild.id, gcfg_local)

            # Ensure Discord category exists
            disc_cat = discord.utils.get(inter.guild.categories, name=categories[idx]["name"])
            if disc_cat is None:
                try:
                    disc_cat = await inter.guild.create_category(categories[idx]["name"])
                except Exception:
                    disc_cat = None

            overwrites = {
                inter.guild.default_role: discord.PermissionOverwrite(view_channel=False),
                inter.user: discord.PermissionOverwrite(view_channel=True, send_messages=True, attach_files=True, embed_links=True, read_message_history=True),
            }
            staff_role_id = gcfg_local.get("staff_role_id")
            if staff_role_id:
                role = inter.guild.get_role(staff_role_id)
                if role:
                    overwrites[role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)

            ch_name = f"ticket-{inter.user.name}-{num}"
            try:
                tchan = await inter.guild.create_text_channel(
                    name=ch_name,
                    category=disc_cat,
                    overwrites=overwrites,
                    reason=f"Ticket opened by {inter.user}"
                )
            except discord.Forbidden:
                return await inter.followup.send("❌ I don't have permission to create channels.", ephemeral=True)

            add_open_ticket(inter.guild.id, tchan.id, inter.user.id, num)

            role_ping = ""
            ping_role_id = categories[idx].get("role_id")
            if ping_role_id:
                role = inter.guild.get_role(int(ping_role_id))
                if role:
                    role_ping = role.mention

            embed = discord.Embed(
                title="🎫 Ticket Opened",
                description=f"**Category:** {categories[idx]['name']}\nUser: {inter.user.mention}",
                color=BLUE
            )
            await tchan.send(
                content=f"{inter.user.mention} {role_ping}".strip(),
                embed=embed,
                view=TicketButtons()
            )

            await inter.followup.send(f"✅ Ticket created: {tchan.mention}", ephemeral=True)

    view.add_item(TicketSelect())
    return view

# ---------- HELP COMMAND ----------
@bot.tree.command(name="help", description="Get help and support info")
async def help_command(inter: discord.Interaction):
    embed = discord.Embed(
        title="📘 TicketMastery Help",
        color=BLUE
    )
    embed.add_field(
        name="🎫 Tickets",
        value="`/ticket_close` – Close your ticket\n`/claim` – Claim a ticket",
        inline=False
    )
    embed.add_field(
        name="⚙️ Setup",
        value="`/set_staff` – Set staff role\n`/set_logs` – Set log channel\n`/categories_add` – Add category",
        inline=False
    )
    embed.add_field(
        name="🛠️ Admin",
        value="`/panel` – Create ticket panel\n`/auto_close` – Toggle auto-close",
        inline=False
    )
    embed.add_field(
        name="🔗 Support",
        value="[Join Support Server](https://discord.gg/TsbRPDXWJS) for help, updates, and feedback!",
        inline=False
    )
    embed.set_footer(text="TicketMastery • Always here to help")
    
    await inter.response.send_message(embed=embed)

# ---------- EVENTS ----------
@bot.event
async def on_message(message: discord.Message):
    # Update inactivity only for non-bot messages inside open tickets
    if message.author.bot or not message.guild:
        return
    info = find_open_ticket(message.guild.id, message.channel.id)
    if info:
        open_map = get_open(message.guild.id)
        info["last_activity"] = int(time.time())
        open_map[str(message.channel.id)] = info
        set_open(message.guild.id, open_map)
    await bot.process_commands(message)

@bot.event
async def on_ready():
    try:
        synced = await tree.sync()
        print(f"✅ Synced {len(synced)} global slash commands.")
    except Exception as e:
        print(f"Slash sync error: {e}")

    try:
        bot.add_view(TicketButtons())  # persistent buttons
    except Exception as e:
        print(f"add_view warning: {e}")

    # Repost panels on startup
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

        # Delete old panel
        try:
            old_msg = await channel.fetch_message(panel_msg_id)
            await old_msg.delete()
        except Exception:
            pass

        # Repost new panel
        embed = discord.Embed(
            title="🎫 Open a Ticket",
            description=gcfg_data.get("panel_description") or "Open a ticket using the buttons below.",
            color=BLUE
        )
        view = build_panel_view(guild)
        try:
            new_msg = await channel.send(embed=embed, view=view)
            # Update config with new message ID
            gcfg_data["panel_message_id"] = new_msg.id
            all_gcfg[gid_str] = gcfg_data
            _save_json(GCFG_FILE, all_gcfg)
        except Exception:
            pass

    presence_loop.start()
    inactivity_scan.start()
    print(f"Bot ready as {bot.user}")

# ---------- COMMANDS ----------
@tree.command(name="ping", description="Show bot latency.")
async def ping(inter: discord.Interaction):
    await inter.response.send_message(f"🏓 {round(bot.latency*1000)}ms")



# ---- Admin/Owner gated helpers ----
def admin_owner_check():
    async def predicate(inter: discord.Interaction):
        if not is_admin_or_owner(inter):
            await inter.response.send_message("🚫 Admins (or bot owner) only.", ephemeral=True)
            return False
        return True
    return app_commands.check(predicate)

@tree.command(name="set_staff", description="Set the staff role used for tickets (Admin/Owner only).")
@admin_owner_check()
async def set_staff(inter: discord.Interaction, role: discord.Role | None):
    gcfg = get_gcfg(inter.guild.id)
    gcfg["staff_role_id"] = role.id if role else None
    set_gcfg(inter.guild.id, gcfg)
    msg = f"✅ Staff role set to {role.mention}" if role else "✅ Staff role **cleared**."
    await inter.response.send_message(msg, ephemeral=True)

@tree.command(name="set_logs", description="Set the log channel for transcripts & events (Admin/Owner only).")
@admin_owner_check()
async def set_logs(inter: discord.Interaction, channel: discord.TextChannel | None):
    gcfg = get_gcfg(inter.guild.id)
    gcfg["log_channel_id"] = channel.id if channel else None
    set_gcfg(inter.guild.id, gcfg)
    msg = f"✅ Log channel set to {channel.mention}" if channel else "✅ Log channel **cleared**."
    await inter.response.send_message(msg, ephemeral=True)

@tree.command(name="set_panel_desc", description="Set the panel description text (Admin/Owner only).")
@admin_owner_check()
async def set_panel_desc(inter: discord.Interaction, description: str):
    gcfg = get_gcfg(inter.guild.id)
    gcfg["panel_description"] = description[:1000]
    set_gcfg(inter.guild.id, gcfg)
    await inter.response.send_message("✅ Panel description updated.", ephemeral=True)

@tree.command(name="auto_close", description="Enable/disable server-wide auto-close (Admin/Owner only).")
@admin_owner_check()
async def auto_close(inter: discord.Interaction, enabled: bool):
    gcfg = get_gcfg(inter.guild.id)
    gcfg["auto_close_enabled"] = enabled
    set_gcfg(inter.guild.id, gcfg)
    await inter.response.send_message(f"✅ Auto-close set to **{enabled}**.", ephemeral=True)

@tree.command(name="log_transcripts", description="Also post transcripts to the log channel (Admin/Owner only).")
@admin_owner_check()
async def log_transcripts(inter: discord.Interaction, enabled: bool):
    gcfg = get_gcfg(inter.guild.id)
    gcfg["log_transcripts"] = enabled
    set_gcfg(inter.guild.id, gcfg)
    await inter.response.send_message(f"✅ Log transcripts = **{enabled}**.", ephemeral=True)

# ---- Categories ----
@tree.command(name="categories_add", description="Add a ticket category (up to 10). Optional ping role. (Admin/Owner only)")
@admin_owner_check()
async def categories_add(inter: discord.Interaction, name: str, ping_role: Optional[discord.Role] = None):
    gcfg = get_gcfg(inter.guild.id)
    cats = gcfg.get("categories", [])
    if len(cats) >= 10:
        return await inter.response.send_message("❌ You can only have up to 10 categories.", ephemeral=True)
    cats.append({"name": name, "role_id": (ping_role.id if ping_role else None)})
    gcfg["categories"] = cats
    set_gcfg(inter.guild.id, gcfg)
    msg = f"✅ Added category `{name}`"
    if ping_role:
        msg += f" (pings {ping_role.mention})"
    await inter.response.send_message(msg, ephemeral=True)

@tree.command(name="categories_clear", description="Clear all ticket categories (Admin/Owner only).")
@admin_owner_check()
async def categories_clear(inter: discord.Interaction):
    gcfg = get_gcfg(inter.guild.id)
    gcfg["categories"] = []
    set_gcfg(inter.guild.id, gcfg)
    await inter.response.send_message("🧹 Cleared all categories.", ephemeral=True)

@tree.command(name="categories_list", description="List all ticket categories.")
async def categories_list(inter: discord.Interaction):
    gcfg = get_gcfg(inter.guild.id)
    cats = gcfg.get("categories", [])
    if not cats:
        return await inter.response.send_message("No categories set.", ephemeral=True)
    lines = []
    for i, c in enumerate(cats, 1):
        nm = c.get("name", f"Category {i}")
        rid = c.get("role_id")
        rp = f"<@&{rid}>" if rid else "No ping"
        lines.append(f"{i}. **{nm}** — {rp}")
    e = discord.Embed(title="📂 Categories", description="\n".join(lines), color=BLUE)
    await inter.response.send_message(embed=e, ephemeral=True)

# ---- Panel ----
@tree.command(name="panel", description="Post the ticket panel. (Admin/Owner only)")
@admin_owner_check()
async def panel(inter: discord.Interaction, description: Optional[str] = None):
    gcfg = get_gcfg(inter.guild.id)
    cats = gcfg.get("categories", [])
    if not cats:
        return await inter.response.send_message("⚠️ No categories configured. Use `/categories_add`.", ephemeral=True)

    if description is not None:
        gcfg["panel_description"] = description[:1000]
        set_gcfg(inter.guild.id, gcfg)

    embed = discord.Embed(
        title="🎫 Open a Ticket",
        description=gcfg.get("panel_description") or "Open a ticket using the buttons below.",
        color=BLUE
    )
    view = build_panel_view(inter.guild)
    
    # Delete old panel if it exists
    if gcfg.get("panel_message_id") and gcfg.get("panel_channel_id"):
        try:
            ch = inter.guild.get_channel(gcfg["panel_channel_id"])
            if ch:
                old_msg = await ch.fetch_message(gcfg["panel_message_id"])
                await old_msg.delete()
        except Exception:
            pass
    
    msg = await inter.response.send_message(embed=embed, view=view)

    # Save new panel location to guild config
    gcfg["panel_channel_id"] = inter.channel.id
    gcfg["panel_message_id"] = msg.id


# ---- Ticket Actions ----
@tree.command(name="claim", description="Claim the current ticket (staff only if staff role is set).")
async def claim(inter: discord.Interaction):
    if not inter.guild:
        return await inter.response.send_message("Guild only.", ephemeral=True)
    info = find_open_ticket(inter.guild.id, inter.channel.id)
    if not info:
        return await inter.response.send_message("❌ This isn't a ticket channel.", ephemeral=True)

    gcfg = get_gcfg(inter.guild.id)
    staff_role_id = gcfg.get("staff_role_id")
    if staff_role_id and isinstance(inter.user, discord.Member):
        if staff_role_id not in [r.id for r in inter.user.roles]:
            return await inter.response.send_message("🚫 Only staff can claim this ticket.", ephemeral=True)

    try:
        await inter.channel.edit(name=f"claimed-{inter.channel.name}")
    except Exception:
        pass

    await inter.response.send_message(f"✅ Ticket claimed by {inter.user.mention}")
    add_claim(inter.guild.id, inter.user.id, info.get("num", 0))
    e = discord.Embed(title="🎟️ Ticket Claimed", description=f"By {inter.user.mention} in {inter.channel.mention}", color=discord.Color.green())
    await send_log(inter.guild, e)

@tree.command(name="ticket_close", description="Close this ticket and DM a transcript to the opener.")
async def ticket_close(inter: discord.Interaction):
    if not inter.guild:
        return await inter.response.send_message("Guild only.", ephemeral=True)
    
    info = find_open_ticket(inter.guild.id, inter.channel.id)
    if not info:
        return await inter.response.send_message("❌ This isn't a ticket channel.", ephemeral=True)

    # Confirmation view
    class ConfirmClose(discord.ui.View):
        def __init__(self):
            super().__init__(timeout=30)

        @discord.ui.button(label="Yes, Close", style=discord.ButtonStyle.red)
        async def yes_btn(self, confirm_inter: discord.Interaction, confirm_button: discord.ui.Button):
            await confirm_inter.response.defer(thinking=True, ephemeral=True)
            await self.do_close(inter.guild, inter.user, inter.channel, info, confirm_inter)

        @discord.ui.button(label="Cancel", style=discord.ButtonStyle.gray)
        async def cancel_btn(self, confirm_inter: discord.Interaction, cancel_button: discord.ui.Button):
            await confirm_inter.response.defer()
            await confirm_inter.followup.send("❌ Ticket close cancelled.", ephemeral=True)

        async def do_close(self, guild, closer, channel, ticket_info, confirm_inter):
            lines: List[str] = []
            async for m in channel.history(limit=1000, oldest_first=True):
                ts = m.created_at.strftime("%Y-%m-%d %H:%M:%S")
                content = m.content or ""
                lines.append(f"[{ts}] {m.author}: {content}")

            text = "\n".join(lines) if lines else "(empty)"
            fobj = io.BytesIO(text.encode("utf-8"))
            filename = f"ticket_{channel.id}_{int(time.time())}.txt"
            file = discord.File(fobj, filename=filename)

            # DM opener
            try:
                u = await bot.fetch_user(int(ticket_info.get("owner_id", closer.id)))
                await u.send(f"Here is your ticket transcript from **{guild.name}**.", file=file)
            except Exception:
                pass

            # Log with nice embed
            e = build_transcript_embed(ticket_info, closer, channel, guild)
            lf = file if get_gcfg(guild.id).get("log_transcripts", True) else None
            await send_log(guild, e, file=lf)

            # Track close
            response_time = int(time.time()) - ticket_info.get("created_at", int(time.time()))
            add_close(guild.id, closer.id, response_time)

            remove_open_ticket(guild.id, channel.id)
            try:
                await channel.delete(reason=f"Ticket closed by {closer}")
            except Exception:
                pass

            try:
                await confirm_inter.followup.send("✅ Ticket closed & transcript sent.", ephemeral=True)
            except Exception:
                pass

    view = ConfirmClose()
    await inter.response.send_message("Are you sure you want to close this ticket?", view=view, ephemeral=True)

# ---- Hold / Unhold (staff-only if staff role set) ----
@tree.command(name="ticket_hold", description="Prevent this ticket from auto-closing (staff only if set).")
async def ticket_hold(inter: discord.Interaction):
    if not inter.guild:
        return await inter.response.send_message("Guild only.", ephemeral=True)
    info = find_open_ticket(inter.guild.id, inter.channel.id)
    if not info:
        return await inter.response.send_message("❌ This isn't a ticket channel.", ephemeral=True)

    gcfg = get_gcfg(inter.guild.id)
    staff_role_id = gcfg.get("staff_role_id")
    if staff_role_id and isinstance(inter.user, discord.Member):
        if staff_role_id not in [r.id for r in inter.user.roles]:
            return await inter.response.send_message("🚫 Only staff can hold tickets.", ephemeral=True)

    info["hold"] = True
    om = get_open(inter.guild.id)
    om[str(inter.channel.id)] = info
    set_open(inter.guild.id, om)

    await inter.response.send_message("⛔ This ticket is now **on hold** (no auto-close).", ephemeral=True)

@tree.command(name="ticket_unhold", description="Allow this ticket to auto-close again (staff only if set).")
async def ticket_unhold(inter: discord.Interaction):
    if not inter.guild:
        return await inter.response.send_message("Guild only.", ephemeral=True)
    info = find_open_ticket(inter.guild.id, inter.channel.id)
    if not info:
        return await inter.response.send_message("❌ This isn't a ticket channel.", ephemeral=True)

    gcfg = get_gcfg(inter.guild.id)
    staff_role_id = gcfg.get("staff_role_id")
    if staff_role_id and isinstance(inter.user, discord.Member):
        if staff_role_id not in [r.id for r in inter.user.roles]:
            return await inter.response.send_message("🚫 Only staff can unhold tickets.", ephemeral=True)

    info["hold"] = False
    om = get_open(inter.guild.id)
    om[str(inter.channel.id)] = info
    set_open(inter.guild.id, om)

    await inter.response.send_message("▶️ This ticket is **off hold** (auto-close active).", ephemeral=True)

@tree.command(name="staff_stats", description="View staff performance stats (Admin/Owner only).")
@admin_owner_check()
async def staff_stats(inter: discord.Interaction):
    stats = get_staff_stats(inter.guild.id)
    if not stats:
        return await inter.response.send_message("No staff stats yet.", ephemeral=True)
    
    lines = []
    for staff_id_str, data in stats.items():
        try:
            staff = inter.guild.get_member(int(staff_id_str))
            if not staff:
                staff = await inter.client.fetch_user(int(staff_id_str))
            name = staff.mention if staff else f"<@{staff_id_str}>"
        except Exception:
            name = f"<@{staff_id_str}>"
        
        claimed = data.get("claimed", 0)
        closed = data.get("closed", 0)
        times = data.get("response_times", [])
        avg_time = sum(times) // len(times) if times else 0
        avg_mins = avg_time // 60
        
        lines.append(f"{name}: **{claimed}** claimed, **{closed}** closed, avg **{avg_mins}m** response")
    
    e = discord.Embed(title="📊 Staff Stats", description="\n".join(lines), color=BLUE)
    await inter.response.send_message(embed=e, ephemeral=True)

# ---------- RUN ----------
if __name__ == "__main__":
    if not TOKEN:
        print("ERROR: DISCORD_TOKEN missing in .env")
    else:
        bot.run(TOKEN)
