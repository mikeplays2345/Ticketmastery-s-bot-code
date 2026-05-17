import os, io, asyncio, time, sqlite3
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

# Safety guard for preventing double-trigger interactions
ACTIVE_INTERACTIONS = set()
TICKET_CREATION_COOLDOWN_SECONDS = 60

# Track ticket creation cooldowns per user (runtime only)
ticket_creation_cooldowns: Dict[int, int] = {}

# ---------- FILES ----------
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DB_FILE = os.path.join(SCRIPT_DIR, "guild_config.db")

def get_db_connection():
    conn = sqlite3.connect(DB_FILE, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn

# Context manager for efficient DB connections
from contextlib import contextmanager

@contextmanager
def db():
    """Reusable DB connection context manager."""
    conn = get_db_connection()
    try:
        yield conn
    finally:
        conn.close()

def base_embed(title: str, description: str = "", color=BLUE) -> discord.Embed:
    """Standardized embed builder for consistent UI."""
    return discord.Embed(title=title, description=description, color=color)

async def audit(guild: discord.Guild, user: discord.User, action: str):
    """Log admin actions for audit trail."""
    if not guild:
        return
    e = base_embed(
        "⚙️ Admin Action",
        f"{user.mention} → {action}",
        discord.Color.gold()
    )
    await send_log(guild, e)

def check_active_interaction(user_id: int, channel_id: int, command: str) -> bool:
    """Check if user+channel already processing this command."""
    key = f"{user_id}:{channel_id}:{command}"
    return key in ACTIVE_INTERACTIONS

def add_active_interaction(user_id: int, channel_id: int, command: str):
    """Mark interaction as active."""
    key = f"{user_id}:{channel_id}:{command}"
    ACTIVE_INTERACTIONS.add(key)

def remove_active_interaction(user_id: int, channel_id: int, command: str):
    """Mark interaction as complete."""
    key = f"{user_id}:{channel_id}:{command}"
    ACTIVE_INTERACTIONS.discard(key)

def init_db():
    conn = get_db_connection()
    c = conn.cursor()

    c.execute("""CREATE TABLE IF NOT EXISTS guild_configs (
        guild_id INTEGER PRIMARY KEY,
        tickets_created INTEGER DEFAULT 0,
        staff_role_id INTEGER,
        log_channel_id INTEGER,
        panel_description TEXT DEFAULT 'Open a ticket using the buttons below.',
        auto_close_enabled INTEGER DEFAULT 1,
        log_transcripts INTEGER DEFAULT 1,
        panel_channel_id INTEGER,
        panel_message_id INTEGER
    )""")

    c.execute("""CREATE TABLE IF NOT EXISTS ticket_categories (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        guild_id INTEGER,
        name TEXT NOT NULL,
        role_id INTEGER,
        position INTEGER DEFAULT 0
    )""")

    c.execute("""CREATE TABLE IF NOT EXISTS open_tickets (
        guild_id INTEGER,
        channel_id INTEGER,
        owner_id INTEGER,
        num INTEGER,
        created_at INTEGER,
        last_activity INTEGER,
        reminded24 INTEGER DEFAULT 0,
        hold INTEGER DEFAULT 0,
        PRIMARY KEY (guild_id, channel_id)
    )""")

    c.execute("""CREATE TABLE IF NOT EXISTS staff_stats (
        guild_id INTEGER,
        staff_id INTEGER,
        claimed INTEGER DEFAULT 0,
        closed INTEGER DEFAULT 0,
        PRIMARY KEY (guild_id, staff_id)
    )""")

    c.execute("""CREATE TABLE IF NOT EXISTS staff_response_times (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        guild_id INTEGER,
        staff_id INTEGER,
        response_time INTEGER,
        created_at INTEGER
    )""")

    c.execute("""CREATE TABLE IF NOT EXISTS user_notes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        guild_id INTEGER,
        user_id INTEGER,
        note_text TEXT,
        created_by INTEGER,
        created_at INTEGER,
        UNIQUE(guild_id, user_id)
    )""")

    c.execute("""CREATE TABLE IF NOT EXISTS closed_tickets_archive (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        guild_id INTEGER,
        channel_id INTEGER,
        owner_id INTEGER,
        num INTEGER,
        category_id INTEGER,
        closed_at INTEGER,
        closed_by INTEGER
    )""")

    # Add new columns to open_tickets if they don't exist
    c.execute("PRAGMA table_info(open_tickets)")
    columns = [col[1] for col in c.fetchall()]
    
    if 'assigned_to' not in columns:
        c.execute("ALTER TABLE open_tickets ADD COLUMN assigned_to INTEGER DEFAULT NULL")
    
    if 'category_id' not in columns:
        c.execute("ALTER TABLE open_tickets ADD COLUMN category_id INTEGER DEFAULT NULL")

    conn.commit()
    conn.close()

init_db()

def get_gcfg(gid: int) -> Dict[str, Any]:
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('SELECT * FROM guild_configs WHERE guild_id = ?', (gid,))
    row = c.fetchone()
    conn.close()
    if row:
        return {
            "tickets_created": row[1],
            "staff_role_id": row[2],
            "log_channel_id": row[3],
            "panel_description": row[4],
            "auto_close_enabled": bool(row[5]),
            "log_transcripts": bool(row[6]),
            "panel_channel_id": row[7],
            "panel_message_id": row[8]
        }
    default = {
        "tickets_created": 0,
        "staff_role_id": None,
        "log_channel_id": None,
        "panel_description": "Open a ticket using the buttons below.",
        "auto_close_enabled": True,
        "log_transcripts": True,
        "panel_channel_id": None,
        "panel_message_id": None
    }
    set_gcfg(gid, default)
    return default

def get_all_gcfg() -> Dict[str, Dict[str, Any]]:
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('SELECT * FROM guild_configs')
    rows = c.fetchall()
    conn.close()
    result = {}
    for row in rows:
        gid = str(row[0])
        result[gid] = {
            "tickets_created": row[1],
            "staff_role_id": row[2],
            "log_channel_id": row[3],
            "panel_description": row[4],
            "auto_close_enabled": bool(row[5]),
            "log_transcripts": bool(row[6]),
            "panel_channel_id": row[7],
            "panel_message_id": row[8]
        }
    return result

def set_gcfg(gid: int, val: Dict[str, Any]):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('''INSERT OR REPLACE INTO guild_configs 
        (guild_id, tickets_created, staff_role_id, log_channel_id, panel_description, auto_close_enabled, log_transcripts, panel_channel_id, panel_message_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''',
        (gid, val["tickets_created"], val["staff_role_id"], val["log_channel_id"], val["panel_description"], int(val["auto_close_enabled"]), int(val["log_transcripts"]), val["panel_channel_id"], val["panel_message_id"]))
    conn.commit()
    conn.close()

def get_categories(gid: int):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("""
        SELECT id, name, role_id
        FROM ticket_categories
        WHERE guild_id = ?
        ORDER BY position ASC
    """, (gid,))
    rows = c.fetchall()
    conn.close()
    return [{"id": r[0], "name": r[1], "role_id": r[2]} for r in rows]


def get_category(gid: int, category_id: int):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("""
        SELECT id, name, role_id
        FROM ticket_categories
        WHERE guild_id = ? AND id = ?
    """, (gid, category_id))
    row = c.fetchone()
    conn.close()
    if row:
        return {"id": row[0], "name": row[1], "role_id": row[2]}
    return None


def add_category(gid: int, name: str, role_id: int | None):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("""
        INSERT INTO ticket_categories (guild_id, name, role_id)
        VALUES (?, ?, ?)
    """, (gid, name, role_id))
    conn.commit()
    conn.close()


def clear_categories(gid: int):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("DELETE FROM ticket_categories WHERE guild_id = ?", (gid,))
    conn.commit()
    conn.close()


def update_category(gid: int, category_id: int, name: str, role_id: int | None):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("""
        UPDATE ticket_categories
        SET name = ?, role_id = ?
        WHERE guild_id = ? AND id = ?
    """, (name, role_id, gid, category_id))
    conn.commit()
    conn.close()


def remove_category(gid: int, category_id: int):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute(
        "DELETE FROM ticket_categories WHERE guild_id = ? AND id = ?",
        (gid, category_id)
    )
    conn.commit()
    conn.close()


def get_open_tickets(gid: int) -> Dict[str, Dict[str, Any]]:
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('SELECT * FROM open_tickets WHERE guild_id = ?', (gid,))
    rows = c.fetchall()
    conn.close()
    result = {}
    for row in rows:
        result[str(row[1])] = {  # channel_id as key
            "owner_id": row[2],
            "num": row[3],
            "created_at": row[4],
            "last_activity": row[5],
            "reminded24": bool(row[6]),
            "hold": bool(row[7]),
            "assigned_to": row[8] if len(row) > 8 else None,
            "category_id": row[9] if len(row) > 9 else None
        }
    return result

def set_open_tickets(gid: int, tickets: Dict[str, Dict[str, Any]]):
    conn = get_db_connection()
    c = conn.cursor()
    # Delete existing tickets for this guild
    c.execute('DELETE FROM open_tickets WHERE guild_id = ?', (gid,))
    # Insert new tickets
    for channel_id, info in tickets.items():
        c.execute('''INSERT INTO open_tickets 
            (guild_id, channel_id, owner_id, num, created_at, last_activity, reminded24, hold, assigned_to, category_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
            (gid, int(channel_id), info["owner_id"], info["num"], info["created_at"], 
             info["last_activity"], int(info.get("reminded24", 0)), int(info.get("hold", 0)),
             info.get("assigned_to"), info.get("category_id")))
    conn.commit()
    conn.close()

def get_all_open_tickets() -> Dict[str, Dict[str, Dict[str, Any]]]:
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('SELECT * FROM open_tickets')
    rows = c.fetchall()
    conn.close()
    result = {}
    for row in rows:
        gid = str(row[0])
        ch_id = str(row[1])
        if gid not in result:
            result[gid] = {}
        result[gid][ch_id] = {
            "owner_id": row[2],
            "num": row[3],
            "created_at": row[4],
            "last_activity": row[5],
            "reminded24": bool(row[6]),
            "hold": bool(row[7]),
            "assigned_to": row[8] if len(row) > 8 else None,
            "category_id": row[9] if len(row) > 9 else None
        }
    return result

def add_open_ticket(gid: int, channel_id: int, owner_id: int, num: int, category_id: int = None):
    now = int(time.time())
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('''
        INSERT OR REPLACE INTO open_tickets
        (guild_id, channel_id, owner_id, num, created_at, last_activity, category_id)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    ''', (gid, channel_id, owner_id, num, now, now, category_id))
    conn.commit()
    conn.close()

def remove_open_ticket(gid: int, channel_id: int):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute(
        "DELETE FROM open_tickets WHERE guild_id=? AND channel_id=?",
        (gid, channel_id)
    )
    conn.commit()
    conn.close()

def get_open_ticket(gid: int, channel_id: int):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute(
        "SELECT * FROM open_tickets WHERE guild_id=? AND channel_id=?",
        (gid, channel_id)
    )
    row = c.fetchone()
    conn.close()

    if not row:
        return None

    return {
        "owner_id": row[2],
        "num": row[3],
        "created_at": row[4],
        "last_activity": row[5],
        "reminded24": bool(row[6]),
        "hold": bool(row[7]),
        "assigned_to": row[8] if len(row) > 8 else None,
        "category_id": row[9] if len(row) > 9 else None
    }


def get_open_ticket_by_owner(gid: int, owner_id: int):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute(
        "SELECT * FROM open_tickets WHERE guild_id=? AND owner_id=?",
        (gid, owner_id)
    )
    row = c.fetchone()
    conn.close()

    if not row:
        return None

    return {
        "channel_id": row[1],
        "owner_id": row[2],
        "num": row[3],
        "created_at": row[4],
        "last_activity": row[5],
        "reminded24": bool(row[6]),
        "hold": bool(row[7]),
        "assigned_to": row[8] if len(row) > 8 else None,
        "category_id": row[9] if len(row) > 9 else None
    }


def update_ticket_activity(gid: int, channel_id: int):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('''
        UPDATE open_tickets
        SET last_activity = ?
        WHERE guild_id=? AND channel_id=?
    ''', (int(time.time()), gid, channel_id))
    conn.commit()
    conn.close()

def get_staff_stats(gid: int) -> Dict[str, Dict[str, int]]:
    # DEPRECATED: Staff stats not currently used
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT staff_id, claimed, closed FROM staff_stats WHERE guild_id=?", (gid,))
    rows = c.fetchall()
    conn.close()

    result = {}
    for r in rows:
        result[str(r[0])] = {
            "claimed": r[1],
            "closed": r[2]
        }
    return result

def set_staff_stats(gid: int, stats: Dict[str, Dict[str, Any]]):
    # DEPRECATED: Staff stats not currently used
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('DELETE FROM staff_stats WHERE guild_id = ?', (gid,))
    for staff_id, data in stats.items():
        c.execute('''INSERT OR REPLACE INTO staff_stats 
            (guild_id, staff_id, claimed, closed)
            VALUES (?, ?, ?, ?)''',
            (gid, int(staff_id), data["claimed"], data["closed"]))
    conn.commit()
    conn.close()

def add_claim(gid: int, staff_id: int):
    # DEPRECATED: Staff stats not currently used
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('''
        INSERT INTO staff_stats (guild_id, staff_id, claimed, closed)
        VALUES (?, ?, 1, 0)
        ON CONFLICT(guild_id, staff_id)
        DO UPDATE SET claimed = claimed + 1
    ''', (gid, staff_id))
    conn.commit()
    conn.close()

def add_close(gid: int, staff_id: int, response_time: int):
    # DEPRECATED: Staff stats not currently used
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('''
        INSERT INTO staff_stats (guild_id, staff_id, claimed, closed)
        VALUES (?, ?, 0, 1)
        ON CONFLICT(guild_id, staff_id)
        DO UPDATE SET closed = closed + 1
    ''', (gid, staff_id))
    c.execute('''
        INSERT INTO staff_response_times (guild_id, staff_id, response_time, created_at)
        VALUES (?, ?, ?, ?)
    ''', (gid, staff_id, response_time, int(time.time())))
    conn.commit()
    conn.close()

def get_avg_response_time(gid: int, staff_id: int) -> int:
    # DEPRECATED: Staff stats not currently used
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('''
        SELECT AVG(response_time)
        FROM staff_response_times
        WHERE guild_id=? AND staff_id=?
    ''', (gid, staff_id))
    avg = c.fetchone()[0]
    conn.close()
    return int(avg) if avg else 0

# ---------- NEW HELPER FUNCTIONS ----------

def add_user_note(gid: int, user_id: int, note_text: str, created_by: int):
    """Add or update a note for a user."""
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('''
        INSERT OR REPLACE INTO user_notes
        (guild_id, user_id, note_text, created_by, created_at)
        VALUES (?, ?, ?, ?, ?)
    ''', (gid, user_id, note_text, created_by, int(time.time())))
    conn.commit()
    conn.close()

def get_user_note(gid: int, user_id: int):
    """Get note for a user."""
    conn = get_db_connection()
    c = conn.cursor()
    c.execute(
        "SELECT note_text, created_by, created_at FROM user_notes WHERE guild_id=? AND user_id=?",
        (gid, user_id)
    )
    row = c.fetchone()
    conn.close()
    if row:
        return {"text": row[0], "created_by": row[1], "created_at": row[2]}
    return None

def archive_closed_ticket(gid: int, channel_id: int, owner_id: int, num: int, category_id: int, closed_by: int):
    """Archive a closed ticket for potential reopening."""
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('''
        INSERT INTO closed_tickets_archive
        (guild_id, channel_id, owner_id, num, category_id, closed_at, closed_by)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    ''', (gid, channel_id, owner_id, num, category_id, int(time.time()), closed_by))
    conn.commit()
    conn.close()

def get_recent_closed_tickets(gid: int, owner_id: int, hours: int = 24):
    """Get recently closed tickets for a user (within N hours)."""
    cutoff = int(time.time()) - (hours * 3600)
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('''
        SELECT num, closed_at, category_id FROM closed_tickets_archive
        WHERE guild_id=? AND owner_id=? AND closed_at > ?
        ORDER BY closed_at DESC
    ''', (gid, owner_id, cutoff))
    rows = c.fetchall()
    conn.close()
    return [{"num": r[0], "closed_at": r[1], "category_id": r[2]} for r in rows]

def search_tickets(gid: int, query: str):
    """Search open tickets by user mention, ticket number, or category name."""
    # Try to parse as user ID
    user_id = None
    try:
        user_id = int(query)
    except ValueError:
        pass
    
    conn = get_db_connection()
    c = conn.cursor()
    results = []
    
    if user_id:
        # Search by owner ID
        c.execute('SELECT channel_id, num, created_at, owner_id FROM open_tickets WHERE guild_id=? AND owner_id=?', (gid, user_id))
        results = c.fetchall()
    else:
        # Try ticket number
        try:
            num = int(query)
            c.execute('SELECT channel_id, num, created_at, owner_id FROM open_tickets WHERE guild_id=? AND num=?', (gid, num))
            results = c.fetchall()
        except ValueError:
            # Search by category name
            c.execute('SELECT id FROM ticket_categories WHERE guild_id=? AND name LIKE ?', (gid, f"%{query}%"))
            cat_rows = c.fetchall()
            if cat_rows:
                cat_id = cat_rows[0][0]
                c.execute('SELECT channel_id, num, created_at, owner_id FROM open_tickets WHERE guild_id=? AND category_id=?', (gid, cat_id))
                results = c.fetchall()
    
    conn.close()
    return [{"channel_id": r[0], "num": r[1], "created_at": r[2], "owner_id": r[3]} for r in results]

def assign_ticket(gid: int, channel_id: int, staff_id: int):
    """Assign a ticket to staff."""
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('''
        UPDATE open_tickets
        SET assigned_to = ?
        WHERE guild_id=? AND channel_id=?
    ''', (staff_id, gid, channel_id))
    conn.commit()
    conn.close()

def unassign_ticket(gid: int, channel_id: int):
    """Unassign a ticket."""
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('''
        UPDATE open_tickets
        SET assigned_to = NULL
        WHERE guild_id=? AND channel_id=?
    ''', (gid, channel_id))
    conn.commit()
    conn.close()

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
    """
    Scan for inactive tickets and auto-close them.
    Optimized: Pre-loads all data once instead of repeated DB calls.
    """
    if not bot.is_ready():
        return
    
    now = int(time.time())
    
    # PRE-LOAD all data ONCE
    open_all = get_all_open_tickets()
    all_gcfgs = get_all_gcfg()

    # Loop guilds & channels
    for g_key, channels in list(open_all.items()):
        gid = int(g_key)
        guild = bot.get_guild(gid)
        if not guild:
            continue
        
        # Get config for this guild (pre-loaded)
        gcfg = all_gcfgs.get(g_key, get_gcfg(gid))
        if not gcfg.get("auto_close_enabled", True):
            continue  # Server disabled auto-close

        for ch_key, info in list(channels.items()):
            ch_id = int(ch_key)
            hold = info.get("hold", False)
            if hold:
                continue
            
            last = int(info.get("last_activity", info.get("created_at", now)))
            elapsed = now - last
            remaining = INACTIVITY_SECONDS - elapsed

            # Send 24h reminder once
            if remaining <= REMINDER_BEFORE and remaining > 0 and not info.get("reminded24", False):
                channel = guild.get_channel(ch_id)
                if isinstance(channel, discord.TextChannel):
                    try:
                        await channel.send("\u23f3 This ticket will close in **24 hours** due to inactivity.")
                        # Update in memory and save
                        info["reminded24"] = True
                        channels[ch_key] = info
                        open_all[g_key] = channels
                        set_open_tickets(gid, channels)
                    except Exception:
                        pass
                continue

            # Auto-close if exceeded
            if elapsed >= INACTIVITY_SECONDS:
                channel = guild.get_channel(ch_id)
                if isinstance(channel, discord.TextChannel):
                    # Build transcript
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
                            await opener.send(
                                f"\u23f3 Your ticket in **{guild.name}** was closed due to inactivity (48h).",
                                file=file
                            )
                    except Exception:
                        pass

                    # Log close with safe delete fallback
                    e = base_embed(
                        "🔒 Ticket Auto-Closed",
                        f"Channel: <#{ch_id}>\nReason: **48h inactivity**",
                        discord.Color.red()
                    )
                    lf = file if gcfg.get("log_transcripts", True) else None
                    await send_log(guild, e, file=lf)

                    # Delete channel with fallback
                    try:
                        await channel.delete(reason="Auto-close after 48h inactivity")
                    except Exception as e:
                        await send_log(
                            guild,
                            base_embed(
                                "⚠️ Auto-Close Cleanup Failed",
                                f"Could not delete <#{ch_id}> after auto-close\nError: {str(e)[:80]}",
                                discord.Color.orange()
                            )
                        )

                # Remove from registry regardless of delete success
                del channels[ch_key]
                open_all[g_key] = channels
                set_open_tickets(gid, channels)

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

        info = get_open_ticket(inter.guild.id, inter.channel.id)
        if not info:
            return await inter.response.send_message("❌ This isn't a ticket channel.", ephemeral=True)

        try:
            await inter.channel.edit(name=f"claimed-{inter.channel.name}")
        except Exception:
            pass

        await inter.response.send_message(f"✅ Ticket claimed by {inter.user.mention}")
        add_claim(inter.guild.id, inter.user.id)
        e = discord.Embed(title="🎟️ Ticket Claimed", description=f"By {inter.user.mention} in {inter.channel.mention}", color=discord.Color.green())
        await send_log(inter.guild, e)

    @discord.ui.button(label="Close", style=discord.ButtonStyle.red, custom_id="ticket_close_btn")
    async def close_btn(self, inter: discord.Interaction, button: discord.ui.Button):
        if not inter.guild:
            return
        
        info = get_open_ticket(inter.guild.id, inter.channel.id)
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
                # Use TicketManager for safe close
                gcfg = get_gcfg(guild.id)
                success = await TicketManager.close_ticket(
                    guild,
                    channel,
                    closer,
                    ticket_info,
                    transcripts_enabled=gcfg.get("log_transcripts", True)
                )
                
                if success:
                    try:
                        await confirm_inter.followup.send("✅ Ticket closed & transcript sent.", ephemeral=True)
                    except Exception:
                        pass
                else:
                    try:
                        await confirm_inter.followup.send("❌ Error closing ticket, but channel may be deleted.", ephemeral=True)
                    except Exception:
                        pass

        view = ConfirmClose()
        await inter.response.send_message("Are you sure you want to close this ticket?", view=view, ephemeral=True)

# ---------- TICKET MANAGER (centralized ticket operations) ----------

class TicketManager:
    """Central ticket operation manager - prevents duplicated logic."""
    
    @staticmethod
    def sanitize_channel_name(name: str, max_len: int = 15) -> str:
        """Safe channel name creation."""
        return name.lower().replace(" ", "-")[:max_len]
    
    @staticmethod
    def build_ticket_channel_name(num: int, user_name: str) -> str:
        """Improved ticket naming: ticket-{num:04d}-{sanitized_name}"""
        safe_name = TicketManager.sanitize_channel_name(user_name)
        return f"ticket-{num:04d}-{safe_name}"
    
    @staticmethod
    async def create_ticket(
        inter: discord.Interaction,
        guild: discord.Guild,
        user: discord.User,
        category_data: Dict[str, Any],
        gcfg: Dict[str, Any]
    ) -> Optional[discord.TextChannel]:
        """
        Create a new ticket channel safely with interaction guard.
        Returns: created TextChannel or None if failed.
        """
        # Check if already processing ticket creation
        if check_active_interaction(user.id, inter.channel.id, "ticket_create"):
            await inter.followup.send("⏳ Already creating a ticket for you...", ephemeral=True)
            return None
        
        add_active_interaction(user.id, inter.channel.id, "ticket_create")
        
        try:
            # Get fresh config to increment counter
            gcfg = get_gcfg(guild.id)
            gcfg["tickets_created"] = int(gcfg.get("tickets_created", 0)) + 1
            num = gcfg["tickets_created"]
            set_gcfg(guild.id, gcfg)
            
            # Ensure Discord category exists
            disc_cat = discord.utils.get(guild.categories, name=category_data["name"])
            if disc_cat is None:
                try:
                    disc_cat = await guild.create_category(category_data["name"])
                except Exception:
                    disc_cat = None
            
            # Build permission overwrites
            overwrites = {
                guild.default_role: discord.PermissionOverwrite(view_channel=False),
                user: discord.PermissionOverwrite(
                    view_channel=True,
                    send_messages=True,
                    attach_files=True,
                    embed_links=True,
                    read_message_history=True
                ),
            }
            
            # Add staff role if configured
            staff_role_id = gcfg.get("staff_role_id")
            if staff_role_id:
                role = guild.get_role(staff_role_id)
                if role:
                    overwrites[role] = discord.PermissionOverwrite(
                        view_channel=True,
                        send_messages=True,
                        read_message_history=True
                    )
            
            # Create channel with improved naming
            ch_name = TicketManager.build_ticket_channel_name(num, user.name)
            tchan = await guild.create_text_channel(
                name=ch_name,
                category=disc_cat,
                overwrites=overwrites,
                reason=f"Ticket #{num} opened by {user}"
            )
            
            # Register ticket with category
            category_id = category_data.get("id")
            add_open_ticket(guild.id, tchan.id, user.id, num, category_id=category_id)
            
            return tchan
        finally:
            remove_active_interaction(user.id, inter.channel.id, "ticket_create")
    
    @staticmethod
    async def close_ticket(
        guild: discord.Guild,
        channel: discord.TextChannel,
        closer: discord.User,
        ticket_info: Dict[str, Any],
        transcripts_enabled: bool = True
    ) -> bool:
        """
        Close ticket safely with transcript and logging.
        Returns: True if successful, False if failed.
        """
        try:
            # Build transcript
            messages: List[tuple] = []
            try:
                async for m in channel.history(limit=1000, oldest_first=True):
                    ts = m.created_at.strftime("%Y-%m-%d %H:%M:%S")
                    content = m.content or ""
                    messages.append((ts, m.author.name, content))
            except Exception:
                pass
            
            # Build HTML transcript
            html_lines = [
                "<!DOCTYPE html>",
                "<html>",
                "<head>",
                "    <meta charset='UTF-8'>",
                "    <style>",
                "        body { font-family: Arial, sans-serif; background-color: #2c2f33; color: #dcddde; margin: 20px; }",
                "        .message { margin-bottom: 12px; padding: 8px; border-left: 3px solid #7289da; background-color: #36393f; }",
                "        .timestamp { color: #72767d; font-size: 12px; }",
                "        .author { color: #7289da; font-weight: bold; }",
                "        .content { margin-top: 4px; word-wrap: break-word; }",
                "    </style>",
                "</head>",
                "<body>"
            ]
            
            if messages:
                for ts, author, content in messages:
                    html_lines.append(f'    <div class="message">')
                    html_lines.append(f'        <div class="timestamp">{ts}</div>')
                    html_lines.append(f'        <div class="author">{author}</div>')
                    html_lines.append(f'        <div class="content">{content}</div>')
                    html_lines.append(f'    </div>')
            else:
                html_lines.append('    <p><em>(No messages in transcript)</em></p>')
            
            html_lines.extend([
                "</body>",
                "</html>"
            ])
            
            html_text = "\n".join(html_lines)
            filename = f"ticket_{channel.id}_{int(time.time())}.html"
            
            # DM opener
            try:
                opener = await bot.fetch_user(int(ticket_info.get("owner_id", closer.id)))
                dm_file = discord.File(io.BytesIO(html_text.encode("utf-8")), filename=filename)
                await opener.send(
                    f"📋 Your ticket transcript from **{guild.name}**.",
                    file=dm_file
                )
            except Exception:
                pass
            
            # Log with embed
            e = build_transcript_embed(ticket_info, closer, channel, guild)
            
            # Add transcript preview to embed
            if messages:
                transcript_preview = "\n".join([f"[{ts}] {author}: {content[:50]}" for ts, author, content in messages[-10:]])
                e.add_field(name="📝 Transcript Preview (Last 10 messages)", value=f"```\n{transcript_preview}\n```", inline=False)
            
            if transcripts_enabled:
                log_file = discord.File(io.BytesIO(html_text.encode("utf-8")), filename=filename)
                await send_log(guild, e, file=log_file)
            else:
                await send_log(guild, e)
            
            # Track stats
            response_time = int(time.time()) - ticket_info.get("created_at", int(time.time()))
            add_close(guild.id, closer.id, response_time)
            
            # Archive for reopening
            archive_closed_ticket(
                guild.id, channel.id, ticket_info.get("owner_id"), 
                ticket_info.get("num"), ticket_info.get("category_id"), closer.id
            )
            
            # Remove from registry
            remove_open_ticket(guild.id, channel.id)
            
            # Delete channel with fallback
            try:
                await channel.delete(reason=f"Ticket closed by {closer}")
            except Exception as e:
                # Log deletion failure
                await send_log(
                    guild,
                    base_embed(
                        "⚠️ Ticket Cleanup Failed",
                        f"Could not delete <#{channel.id}>\nError: {str(e)[:100]}",
                        discord.Color.orange()
                    )
                )
            
            return True
        except Exception:
            return False

# ---------- UTIL: Build Panel ----------
def build_panel_view(guild: discord.Guild):
    view = discord.ui.View(timeout=None)
    cats = get_categories(guild.id)[:25]  # Dropdowns support up to 25 options

    # Build dropdown options
    options = []
    for c in cats:
        label = c["name"][:100]
        options.append(discord.SelectOption(label=label, value=str(c["id"])))

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
            category_id = int(self.values[0])
            category = get_category(inter.guild.id, category_id)

            if not category:
                return await inter.response.send_message("⚠️ That category no longer exists.", ephemeral=True)

            now = int(time.time())
            if get_open_ticket_by_owner(inter.guild.id, inter.user.id):
                return await inter.response.send_message(
                    "❌ You already have an open ticket. Close it before opening a new one.",
                    ephemeral=True
                )

            last_attempt = ticket_creation_cooldowns.get(inter.user.id, 0)
            remaining = TICKET_CREATION_COOLDOWN_SECONDS - (now - last_attempt)
            if remaining > 0:
                return await inter.response.send_message(
                    f"⏳ Please wait {remaining}s before creating another ticket.",
                    ephemeral=True
                )

            await inter.response.defer(thinking=True, ephemeral=True)

            gcfg_local = get_gcfg(inter.guild.id)
            
            tchan = await TicketManager.create_ticket(
                inter,
                inter.guild,
                inter.user,
                category,
                gcfg_local
            )

            if not tchan:
                return await inter.followup.send("❌ Failed to create ticket channel.", ephemeral=True)

            ticket_creation_cooldowns[inter.user.id] = now
            role_ping = ""
            ping_role_id = category.get("role_id")
            if ping_role_id:
                role = inter.guild.get_role(int(ping_role_id))
                if role:
                    role_ping = role.mention

            embed = base_embed(
                "🎫 Ticket Opened",
                f"**Category:** {category['name']}\nUser: {inter.user.mention}"
            )
            await tchan.send(
                content=f"{inter.user.mention} {role_ping}".strip(),
                embed=embed,
                view=TicketButtons()
            )

            await inter.followup.send(f"✅ Ticket created: {tchan.mention}", ephemeral=True)

    view.add_item(TicketSelect())
    return view

# ---------- ADMIN PANEL SYSTEM ----------

# Embed builders
def build_settings_embed(guild: discord.Guild) -> discord.Embed:
    gcfg = get_gcfg(guild.id)
    embed = base_embed(
        "⚙️ Settings Panel",
        "Configure your ticket system settings."
    )
    
    staff_role = f"<@&{gcfg['staff_role_id']}>" if gcfg['staff_role_id'] else "Not set"
    log_channel = f"<#{gcfg['log_channel_id']}>" if gcfg['log_channel_id'] else "Not set"
    
    embed.add_field(name="👮 Staff Role", value=staff_role, inline=True)
    embed.add_field(name="📢 Log Channel", value=log_channel, inline=True)
    embed.add_field(name="🔁 Auto-Close", value=str(gcfg['auto_close_enabled']), inline=True)
    embed.add_field(name="📄 Log Transcripts", value=str(gcfg['log_transcripts']), inline=True)
    
    return embed

def build_categories_embed(guild: discord.Guild) -> discord.Embed:
    cats = get_categories(guild.id)
    embed = base_embed(
        "📂 Category Manager",
        "Manage ticket categories."
    )
    
    if not cats:
        embed.add_field(name="No Categories", value="Add some categories to get started!", inline=False)
    else:
        lines = []
        for i, c in enumerate(cats, 1):
            nm = c.get("name", f"Category {i}")
            rid = c.get("role_id")
            rp = f"<@&{rid}>" if rid else "No ping"
            lines.append(f"{i}. **{nm}** (ID: `{c['id']}`) — {rp}")
        embed.add_field(name=f"Categories ({len(cats)}/10)", value="\n".join(lines), inline=False)
    
    return embed

def build_panel_embed(guild: discord.Guild) -> discord.Embed:
    gcfg = get_gcfg(guild.id)
    embed = base_embed(
        "🎫 Panel Manager",
        "Control the ticket panel."
    )
    
    desc = gcfg.get("panel_description", "Open a ticket using the buttons below.")
    embed.add_field(name="📨 Current Description", value=desc[:500] + "..." if len(desc) > 500 else desc, inline=False)
    
    panel_status = "✅ Active" if gcfg.get("panel_message_id") else "❌ Not posted"
    embed.add_field(name="📍 Panel Status", value=panel_status, inline=True)
    
    return embed

def build_stats_embed(guild: discord.Guild) -> discord.Embed:
    # DEPRECATED: Staff stats not currently used in admin panel
    stats = get_staff_stats(guild.id)
    embed = discord.Embed(
        title="📊 Staff Statistics",
        description="Performance metrics for your staff team.",
        color=BLUE
    )
    
    if not stats:
        embed.add_field(name="No Stats Yet", value="Staff stats will appear here as tickets are claimed and closed.", inline=False)
    else:
        lines = []
        for staff_id_str, data in stats.items():
            try:
                staff = guild.get_member(int(staff_id_str))
                if not staff:
                    staff = bot.get_user(int(staff_id_str))
                name = staff.mention if staff else f"<@{staff_id_str}>"
            except Exception:
                name = f"<@{staff_id_str}>"
            
            claimed = data.get("claimed", 0)
            closed = data.get("closed", 0)
            avg_time = get_avg_response_time(guild.id, int(staff_id_str))
            avg_mins = avg_time // 60
            
            lines.append(f"{name}: **{claimed}** claimed, **{closed}** closed, avg **{avg_mins}m** response")
        
        embed.add_field(name=f"Staff Performance ({len(stats)} members)", value="\n".join(lines[:10]), inline=False)  # Limit to 10
    
    return embed

# Modals
class StaffRoleModal(discord.ui.Modal, title="Set Staff Role"):
    role_id = discord.ui.TextInput(label="Role ID", placeholder="Enter the role ID (right-click role > Copy ID)")

    async def on_submit(self, inter: discord.Interaction):
        try:
            role_id = int(self.role_id.value)
            role = inter.guild.get_role(role_id)
            if not role:
                return await inter.response.send_message("❌ Role not found in this server.", ephemeral=True)
            
            gcfg = get_gcfg(inter.guild.id)
            gcfg["staff_role_id"] = role_id
            set_gcfg(inter.guild.id, gcfg)
            await audit(inter.guild, inter.user, f"set staff role to {role.mention}")
            await inter.response.send_message(f"✅ Staff role set to {role.mention}", ephemeral=True)
        except ValueError:
            await inter.response.send_message("❌ Invalid role ID. Please enter a number.", ephemeral=True)

class LogChannelModal(discord.ui.Modal, title="Set Log Channel"):
    channel_id = discord.ui.TextInput(label="Channel ID", placeholder="Enter the channel ID (right-click channel > Copy ID)")

    async def on_submit(self, inter: discord.Interaction):
        try:
            channel_id = int(self.channel_id.value)
            channel = inter.guild.get_channel(channel_id)
            if not channel or not isinstance(channel, discord.TextChannel):
                return await inter.response.send_message("❌ Text channel not found in this server.", ephemeral=True)
            
            gcfg = get_gcfg(inter.guild.id)
            gcfg["log_channel_id"] = channel_id
            set_gcfg(inter.guild.id, gcfg)
            await audit(inter.guild, inter.user, f"set log channel to {channel.mention}")
            await inter.response.send_message(f"✅ Log channel set to {channel.mention}", ephemeral=True)
        except ValueError:
            await inter.response.send_message("❌ Invalid channel ID. Please enter a number.", ephemeral=True)

class AddCategoryModal(discord.ui.Modal, title="Add Ticket Category"):
    name = discord.ui.TextInput(label="Category Name", placeholder="Enter category name")
    role_id = discord.ui.TextInput(label="Ping Role ID (optional)", placeholder="Enter role ID to ping when ticket opens", required=False)

    async def on_submit(self, inter: discord.Interaction):
        cats = get_categories(inter.guild.id)
        if len(cats) >= 10:
            return await inter.response.send_message("❌ You can only have up to 10 categories.", ephemeral=True)
        
        role_id = None
        if self.role_id.value:
            try:
                role_id = int(self.role_id.value)
                role = inter.guild.get_role(role_id)
                if not role:
                    return await inter.response.send_message("❌ Role not found in this server.", ephemeral=True)
            except ValueError:
                return await inter.response.send_message("❌ Invalid role ID. Please enter a number.", ephemeral=True)
        
        add_category(inter.guild.id, self.name.value, role_id)
        await audit(inter.guild, inter.user, f"added category '{self.name.value}'")
        msg = f"✅ Added category `{self.name.value}`"
        if role_id:
            msg += f" (pings <@&{role_id}>)"
        await inter.response.send_message(msg, ephemeral=True)

class EditCategoryModal(discord.ui.Modal, title="Edit Ticket Category"):
    category_id = discord.ui.TextInput(label="Category ID", placeholder="Enter the category ID to edit")
    name = discord.ui.TextInput(label="New Category Name", placeholder="Enter the updated category name")
    role_id = discord.ui.TextInput(label="Ping Role ID (optional)", placeholder="Enter new role ID or leave blank", required=False)

    async def on_submit(self, inter: discord.Interaction):
        try:
            category_id = int(self.category_id.value)
        except ValueError:
            return await inter.response.send_message("❌ Invalid category ID.", ephemeral=True)

        category = get_category(inter.guild.id, category_id)
        if not category:
            return await inter.response.send_message("❌ Category not found.", ephemeral=True)

        role_id = None
        if self.role_id.value:
            try:
                role_id = int(self.role_id.value)
                role = inter.guild.get_role(role_id)
                if not role:
                    return await inter.response.send_message("❌ Role not found in this server.", ephemeral=True)
            except ValueError:
                return await inter.response.send_message("❌ Invalid role ID. Please enter a number.", ephemeral=True)

        update_category(inter.guild.id, category_id, self.name.value, role_id)
        await audit(inter.guild, inter.user, f"edited category '{category['name']}' to '{self.name.value}'")
        msg = f"✅ Updated category `{self.name.value}`"
        if role_id:
            msg += f" (pings <@&{role_id}>)"
        await inter.response.send_message(msg, ephemeral=True)

class DeleteCategoryModal(discord.ui.Modal, title="Delete Ticket Category"):
    category_id = discord.ui.TextInput(label="Category ID", placeholder="Enter the category ID to delete")

    async def on_submit(self, inter: discord.Interaction):
        try:
            category_id = int(self.category_id.value)
        except ValueError:
            return await inter.response.send_message("❌ Invalid category ID.", ephemeral=True)

        category = get_category(inter.guild.id, category_id)
        if not category:
            return await inter.response.send_message("❌ Category not found.", ephemeral=True)

        remove_category(inter.guild.id, category_id)
        await audit(inter.guild, inter.user, f"deleted category '{category['name']}'")
        await inter.response.send_message(f"🗑️ Deleted category `{category['name']}`.", ephemeral=True)

class PanelDescModal(discord.ui.Modal, title="Edit Panel Description"):
    description = discord.ui.TextInput(
        label="Panel Description", 
        style=discord.TextStyle.paragraph,
        placeholder="Enter the description that appears on the ticket panel",
        max_length=1000
    )

    async def on_submit(self, inter: discord.Interaction):
        gcfg = get_gcfg(inter.guild.id)
        gcfg["panel_description"] = self.description.value
        set_gcfg(inter.guild.id, gcfg)
        await audit(inter.guild, inter.user, "updated panel description")
        await inter.response.send_message("✅ Panel description updated.", ephemeral=True)

# Views
class AdminPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=180)

    @discord.ui.button(label="⚙️ Settings", style=discord.ButtonStyle.blurple)
    async def settings(self, inter: discord.Interaction, button: discord.ui.Button):
        await inter.response.send_message(
            embed=build_settings_embed(inter.guild),
            view=SettingsView(),
            ephemeral=True
        )

    @discord.ui.button(label="📂 Categories", style=discord.ButtonStyle.green)
    async def categories(self, inter: discord.Interaction, button: discord.ui.Button):
        await inter.response.send_message(
            embed=build_categories_embed(inter.guild),
            view=CategoryView(),
            ephemeral=True
        )

    @discord.ui.button(label="🎫 Panel Manager", style=discord.ButtonStyle.gray)
    async def panel(self, inter: discord.Interaction, button: discord.ui.Button):
        await inter.response.send_message(
            embed=build_panel_embed(inter.guild),
            view=PanelManagerView(),
            ephemeral=True
        )

class SettingsView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=180)

    @discord.ui.button(label="👮 Set Staff Role", style=discord.ButtonStyle.blurple)
    async def staff(self, inter: discord.Interaction, button: discord.ui.Button):
        await inter.response.send_modal(StaffRoleModal())

    @discord.ui.button(label="📢 Set Log Channel", style=discord.ButtonStyle.blurple)
    async def logs(self, inter: discord.Interaction, button: discord.ui.Button):
        await inter.response.send_modal(LogChannelModal())

    @discord.ui.button(label="🔁 Toggle Auto-Close", style=discord.ButtonStyle.gray)
    async def autoclose(self, inter: discord.Interaction, button: discord.ui.Button):
        # TODO: Add setting for auto-close duration in admin panel (currently hardcoded to 48 hours)
        gcfg = get_gcfg(inter.guild.id)
        gcfg["auto_close_enabled"] = not gcfg["auto_close_enabled"]
        set_gcfg(inter.guild.id, gcfg)
        
        await audit(inter.guild, inter.user, f"toggled auto-close to {gcfg['auto_close_enabled']}")
        await inter.response.send_message(
            f"🔁 Auto-close is now **{gcfg['auto_close_enabled']}**",
            ephemeral=True
        )

    @discord.ui.button(label="📄 Toggle Transcripts", style=discord.ButtonStyle.gray)
    async def transcripts(self, inter: discord.Interaction, button: discord.ui.Button):
        gcfg = get_gcfg(inter.guild.id)
        gcfg["log_transcripts"] = not gcfg["log_transcripts"]
        set_gcfg(inter.guild.id, gcfg)
        
        await audit(inter.guild, inter.user, f"toggled transcripts to {gcfg['log_transcripts']}")
        await inter.response.send_message(
            f"📄 Transcripts logging: **{gcfg['log_transcripts']}**",
            ephemeral=True
        )

class CategoryView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=180)

    @discord.ui.button(label="➕ Add Category", style=discord.ButtonStyle.green)
    async def add(self, inter: discord.Interaction, button: discord.ui.Button):
        await inter.response.send_modal(AddCategoryModal())

    @discord.ui.button(label="✏️ Edit Category", style=discord.ButtonStyle.blurple)
    async def edit(self, inter: discord.Interaction, button: discord.ui.Button):
        await inter.response.send_modal(EditCategoryModal())

    @discord.ui.button(label="🗑️ Delete Category", style=discord.ButtonStyle.red)
    async def delete(self, inter: discord.Interaction, button: discord.ui.Button):
        await inter.response.send_modal(DeleteCategoryModal())

    @discord.ui.button(label="🗑️ Clear Categories", style=discord.ButtonStyle.gray)
    async def clear(self, inter: discord.Interaction, button: discord.ui.Button):
        clear_categories(inter.guild.id)
        await audit(inter.guild, inter.user, "cleared all categories")
        await inter.response.send_message("🧹 Categories cleared.", ephemeral=True)

    @discord.ui.button(label="📃 View Categories", style=discord.ButtonStyle.gray)
    async def view(self, inter: discord.Interaction, button: discord.ui.Button):
        cats = get_categories(inter.guild.id)
        if not cats:
            text = "No categories configured."
        else:
            text = "\n".join([f"• {c['name']} (ID: `{c['id']}`)" for c in cats])
        await inter.response.send_message(f"**Categories:**\n{text}", ephemeral=True)

class PanelManagerView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=180)

    # TODO: Ensure panel persists across bot restarts and old panels continue to work
    @discord.ui.button(label="📨 Send Panel Here", style=discord.ButtonStyle.green)
    async def send(self, inter: discord.Interaction, button: discord.ui.Button):
        await send_ticket_panel(inter)

    @discord.ui.button(label="✏️ Edit Description", style=discord.ButtonStyle.blurple)
    async def edit(self, inter: discord.Interaction, button: discord.ui.Button):
        await inter.response.send_modal(PanelDescModal())

async def send_ticket_panel(inter: discord.Interaction):
    gcfg = get_gcfg(inter.guild.id)
    cats = get_categories(inter.guild.id)
    
    if not cats:
        return await inter.response.send_message("⚠️ No categories configured. Add some categories first!", ephemeral=True)

    embed = discord.Embed(
        title="🎫 Open a Ticket",
        description=gcfg.get("panel_description", "Open a ticket using the buttons below."),
        color=BLUE
    )

    view = build_panel_view(inter.guild)
    msg = await inter.channel.send(embed=embed, view=view)

    gcfg["panel_channel_id"] = inter.channel.id
    gcfg["panel_message_id"] = msg.id
    set_gcfg(inter.guild.id, gcfg)

    await inter.response.send_message("✅ Panel sent to this channel.", ephemeral=True)

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
        value="`/admin_panel` – Manage categories, auto-close, and ticket settings",
        inline=False
    )

    embed.add_field(
        name="🛠️ Admin",
        value="All admin functions are now in `/admin_panel`",
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
    info = get_open_ticket(message.guild.id, message.channel.id)
    if info:
        update_ticket_activity(message.guild.id, message.channel.id)
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

    # Register persistent views for existing panels on startup
    all_gcfg = get_all_gcfg()
    for gid_str, gcfg_data in all_gcfg.items():
        gid = int(gid_str)
        guild = bot.get_guild(gid)
        if not guild:
            continue

        panel_msg_id = gcfg_data.get("panel_message_id")
        cats = get_categories(gid)
        if not panel_msg_id or not cats:
            continue

        try:
            view = build_panel_view(guild)
            bot.add_view(view, message_id=panel_msg_id)
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

@tree.command(name="admin_panel", description="Open the ticket system dashboard (Admin only)")
@admin_owner_check()
async def admin_panel(inter: discord.Interaction):
    embed = discord.Embed(
        title="🧭 Ticket System Dashboard",
        description="Manage the entire system from here.",
        color=BLUE
    )
    await inter.response.send_message(embed=embed, view=AdminPanelView(), ephemeral=True)

# ---- Ticket Actions ----
@tree.command(name="claim", description="Claim the current ticket (staff only if staff role is set).")
async def claim(inter: discord.Interaction):
    if not inter.guild:
        return await inter.response.send_message("Guild only.", ephemeral=True)
    info = get_open_ticket(inter.guild.id, inter.channel.id)
    if not info:
        return await inter.response.send_message("❌ This isn't a ticket channel.", ephemeral=True)

    # TODO: Add anti-spam for claiming tickets (one per user every 60 seconds)
    gcfg = get_gcfg(inter.guild.id)
    staff_role_id = gcfg.get("staff_role_id")
    if staff_role_id and isinstance(inter.user, discord.Member):
        if staff_role_id not in [r.id for r in inter.user.roles]:
            # TODO: Add warning message that staff role is unknown, tell staff who tried to claim it
            return await inter.response.send_message("🚫 Only staff can claim this ticket.", ephemeral=True)

    try:
        await inter.channel.edit(name=f"claimed-{inter.channel.name}")
    except Exception:
        pass

    await inter.response.send_message(f"✅ Ticket claimed by {inter.user.mention}")
    add_claim(inter.guild.id, inter.user.id)
    e = discord.Embed(title="🎟️ Ticket Claimed", description=f"By {inter.user.mention} in {inter.channel.mention}", color=discord.Color.green())
    await send_log(inter.guild, e)

@tree.command(name="ticket_close", description="Close this ticket and DM a transcript to the opener.")
async def ticket_close(inter: discord.Interaction):
    if not inter.guild:
        return await inter.response.send_message("Guild only.", ephemeral=True)
    
    info = get_open_ticket(inter.guild.id, inter.channel.id)
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
            # Use TicketManager for safe close
            gcfg = get_gcfg(guild.id)
            success = await TicketManager.close_ticket(
                guild,
                channel,
                closer,
                ticket_info,
                transcripts_enabled=gcfg.get("log_transcripts", True)
            )
            
            if success:
                try:
                    # TODO: Add better transcripts to send to users (improved formatting and details)
                    await confirm_inter.followup.send("✅ Ticket closed & transcript sent.", ephemeral=True)
                except Exception:
                    pass
            else:
                try:
                    await confirm_inter.followup.send("❌ Error closing ticket.", ephemeral=True)
                except Exception:
                    pass

    view = ConfirmClose()
    await inter.response.send_message("Are you sure you want to close this ticket?", view=view, ephemeral=True)

# ---- Hold / Unhold (staff-only if staff role set) ----
@tree.command(name="ticket_hold", description="Prevent this ticket from auto-closing (staff only if set).")
async def ticket_hold(inter: discord.Interaction):
    if not inter.guild:
        return await inter.response.send_message("Guild only.", ephemeral=True)
    info = get_open_ticket(inter.guild.id, inter.channel.id)
    if not info:
        return await inter.response.send_message("❌ This isn't a ticket channel.", ephemeral=True)

    gcfg = get_gcfg(inter.guild.id)
    staff_role_id = gcfg.get("staff_role_id")
    if staff_role_id and isinstance(inter.user, discord.Member):
        if staff_role_id not in [r.id for r in inter.user.roles]:
            return await inter.response.send_message("🚫 Only staff can hold tickets.", ephemeral=True)

    # Load → Modify → Save pattern
    info["hold"] = True
    om = get_open_tickets(inter.guild.id)
    om[str(inter.channel.id)] = info
    set_open_tickets(inter.guild.id, om)
    
    await audit(inter.guild, inter.user, "put ticket on hold")
    await inter.response.send_message("⛔ This ticket is now **on hold** (no auto-close).", ephemeral=True)

@tree.command(name="ticket_unhold", description="Allow this ticket to auto-close again (staff only if set).")
async def ticket_unhold(inter: discord.Interaction):
    if not inter.guild:
        return await inter.response.send_message("Guild only.", ephemeral=True)
    info = get_open_ticket(inter.guild.id, inter.channel.id)
    if not info:
        return await inter.response.send_message("❌ This isn't a ticket channel.", ephemeral=True)

    gcfg = get_gcfg(inter.guild.id)
    staff_role_id = gcfg.get("staff_role_id")
    if staff_role_id and isinstance(inter.user, discord.Member):
        if staff_role_id not in [r.id for r in inter.user.roles]:
            return await inter.response.send_message("🚫 Only staff can unhold tickets.", ephemeral=True)

    # Load → Modify → Save pattern
    info["hold"] = False
    om = get_open_tickets(inter.guild.id)
    om[str(inter.channel.id)] = info
    set_open_tickets(inter.guild.id, om)
    
    await audit(inter.guild, inter.user, "removed ticket hold")
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
        avg_time = get_avg_response_time(inter.guild.id, int(staff_id_str))
        avg_mins = avg_time // 60
        
        lines.append(f"{name}: **{claimed}** claimed, **{closed}** closed, avg **{avg_mins}m** response")
    
    e = discord.Embed(title="📊 Staff Stats", description="\n".join(lines), color=BLUE)
    await inter.response.send_message(embed=e, ephemeral=True)

# ---------- NEW FEATURES ----------

# ---- Ticket List ----
@tree.command(name="ticket_list", description="List all open tickets in this server.")
async def ticket_list(inter: discord.Interaction):
    if not inter.guild:
        return await inter.response.send_message("Guild only.", ephemeral=True)
    
    await inter.response.defer(ephemeral=True)
    
    tickets = get_open_tickets(inter.guild.id)
    if not tickets:
        return await inter.followup.send("📭 No open tickets.", ephemeral=True)
    
    lines = []
    for ch_id_str, info in sorted(tickets.items(), key=lambda x: -x[1]["num"]):
        num = info.get("num", "?")
        owner_id = info.get("owner_id")
        created = info.get("created_at", 0)
        assigned = info.get("assigned_to")
        
        duration = int(time.time()) - created
        hours = duration // 3600
        mins = (duration % 3600) // 60
        time_str = f"{hours}h {mins}m" if hours > 0 else f"{mins}m"
        
        owner_str = f"<@{owner_id}>" if owner_id else "Unknown"
        assigned_str = f"👤 <@{assigned}>" if assigned else "unassigned"
        
        lines.append(f"**#{num}** · {owner_str} · {assigned_str} · {time_str}")
    
    e = discord.Embed(title=f"📋 Open Tickets ({len(lines)})", description="\n".join(lines[:15]), color=BLUE)
    if len(lines) > 15:
        e.set_footer(text=f"+{len(lines) - 15} more tickets")
    await inter.followup.send(embed=e, ephemeral=True)

# ---- Search Tickets ----
@tree.command(name="search_ticket", description="Search for a ticket by user, number, or category.")
@app_commands.describe(query="User ID/mention, ticket number, or category name")
async def search_ticket(inter: discord.Interaction, query: str):
    if not inter.guild:
        return await inter.response.send_message("Guild only.", ephemeral=True)
    
    await inter.response.defer(ephemeral=True)
    
    results = search_tickets(inter.guild.id, query)
    if not results:
        return await inter.followup.send(f"❌ No tickets found for '{query}'.", ephemeral=True)
    
    lines = []
    for r in results:
        num = r.get("num", "?")
        owner_id = r.get("owner_id")
        created = r.get("created_at", 0)
        ch_id = r.get("channel_id")
        
        duration = int(time.time()) - created
        hours = duration // 3600
        mins = (duration % 3600) // 60
        time_str = f"{hours}h {mins}m" if hours > 0 else f"{mins}m"
        
        lines.append(f"**#{num}** · <#{ch_id}> · <@{owner_id}> · {time_str}")
    
    e = discord.Embed(title="🔍 Search Results", description="\n".join(lines[:10]), color=BLUE)
    await inter.followup.send(embed=e, ephemeral=True)

# ---- Claim/Unclaim Tracking ----
@tree.command(name="assign_ticket", description="Assign this ticket to a staff member (Admin/Owner only).")
@app_commands.describe(staff="The staff member to assign this ticket to")
@admin_owner_check()
async def assign_ticket_cmd(inter: discord.Interaction, staff: discord.User):
    if not inter.guild:
        return await inter.response.send_message("Guild only.", ephemeral=True)
    
    info = get_open_ticket(inter.guild.id, inter.channel.id)
    if not info:
        return await inter.response.send_message("❌ This isn't a ticket channel.", ephemeral=True)
    
    assign_ticket(inter.guild.id, inter.channel.id, staff.id)
    await inter.response.send_message(f"✅ Ticket assigned to {staff.mention}", ephemeral=False)
    await audit(inter.guild, inter.user, f"assigned ticket to {staff.mention}")

@tree.command(name="unassign_ticket", description="Unassign this ticket (Admin/Owner only).")
@admin_owner_check()
async def unassign_ticket_cmd(inter: discord.Interaction):
    if not inter.guild:
        return await inter.response.send_message("Guild only.", ephemeral=True)
    
    info = get_open_ticket(inter.guild.id, inter.channel.id)
    if not info:
        return await inter.response.send_message("❌ This isn't a ticket channel.", ephemeral=True)
    
    unassign_ticket(inter.guild.id, inter.channel.id)
    await inter.response.send_message("✅ Ticket unassigned.", ephemeral=False)
    await audit(inter.guild, inter.user, "unassigned ticket")

# ---- Bulk Close ----
@tree.command(name="bulk_close_idle", description="Close all tickets idle for X hours (Admin/Owner only).")
@app_commands.describe(hours="Hours of inactivity to consider idle (default 24)")
@admin_owner_check()
async def bulk_close_idle(inter: discord.Interaction, hours: int = 24):
    if not inter.guild:
        return await inter.response.send_message("Guild only.", ephemeral=True)
    
    await inter.response.defer(thinking=True, ephemeral=True)
    
    tickets = get_open_tickets(inter.guild.id)
    now = int(time.time())
    idle_threshold = now - (hours * 3600)
    
    closed_count = 0
    for ch_id_str, info in tickets.items():
        last_activity = info.get("last_activity", info.get("created_at", 0))
        if last_activity < idle_threshold:
            ch_id = int(ch_id_str)
            try:
                ch = inter.guild.get_channel(ch_id)
                if ch:
                    # Close the ticket
                    success = await TicketManager.close_ticket(
                        inter.guild, ch, inter.user, info,
                        transcripts_enabled=get_gcfg(inter.guild.id).get("log_transcripts", True)
                    )
                    if success:
                        closed_count += 1
            except Exception:
                pass
    
    await inter.followup.send(f"✅ Closed {closed_count} idle tickets.", ephemeral=True)
    await audit(inter.guild, inter.user, f"bulk-closed {closed_count} idle tickets")

# ---- Reopen Ticket ----
@tree.command(name="reopen_ticket", description="Reopen one of your recently closed tickets (within 24h).")
@app_commands.describe(ticket_num="The ticket number to reopen")
async def reopen_ticket(inter: discord.Interaction, ticket_num: int):
    if not inter.guild:
        return await inter.response.send_message("Guild only.", ephemeral=True)
    
    await inter.response.defer(thinking=True, ephemeral=True)
    
    # Get recent closed tickets for this user
    recent = get_recent_closed_tickets(inter.guild.id, inter.user.id, hours=24)
    
    ticket_data = None
    for t in recent:
        if t["num"] == ticket_num:
            ticket_data = t
            break
    
    if not ticket_data:
        return await inter.followup.send(f"❌ Ticket #{ticket_num} not found in your recent tickets.", ephemeral=True)
    
    # Create new ticket with same category
    gcfg = get_gcfg(inter.guild.id)
    gcfg["tickets_created"] = int(gcfg.get("tickets_created", 0)) + 1
    num = gcfg["tickets_created"]
    set_gcfg(inter.guild.id, gcfg)
    
    category_id = ticket_data.get("category_id")
    category = get_category(inter.guild.id, category_id) if category_id else None
    
    if not category:
        return await inter.followup.send("❌ The original category no longer exists.", ephemeral=True)
    
    # Create the ticket
    tchan = await TicketManager.create_ticket(inter, inter.guild, inter.user, category, gcfg)
    if tchan:
        await inter.followup.send(f"✅ Reopened as {tchan.mention}", ephemeral=True)
    else:
        await inter.followup.send("❌ Failed to reopen ticket.", ephemeral=True)

# ---- User Notes ----
@tree.command(name="note_user", description="Add or update a note for a user (Admin/Owner only).")
@app_commands.describe(user="The user to note", note="The note text")
@admin_owner_check()
async def note_user(inter: discord.Interaction, user: discord.User, note: str):
    if not inter.guild:
        return await inter.response.send_message("Guild only.", ephemeral=True)
    
    add_user_note(inter.guild.id, user.id, note, inter.user.id)
    await inter.response.send_message(f"✅ Note added for {user.mention}:\n```\n{note}\n```", ephemeral=True)
    await audit(inter.guild, inter.user, f"added note for {user.mention}")

@tree.command(name="user_note", description="View the note for a user.")
@app_commands.describe(user="The user")
async def user_note(inter: discord.Interaction, user: discord.User):
    if not inter.guild:
        return await inter.response.send_message("Guild only.", ephemeral=True)
    
    note_data = get_user_note(inter.guild.id, user.id)
    if not note_data:
        return await inter.response.send_message(f"📝 No notes for {user.mention}.", ephemeral=True)
    
    created_by = note_data.get("created_by", 0)
    created_at = note_data.get("created_at", 0)
    
    try:
        creator = await inter.client.fetch_user(created_by)
        creator_str = creator.mention
    except:
        creator_str = f"<@{created_by}>"
    
    created_str = datetime.fromtimestamp(created_at).strftime("%B %d, %Y at %I:%M %p")
    
    e = discord.Embed(
        title=f"📝 Note for {user}",
        description=note_data.get("text", ""),
        color=BLUE
    )
    e.set_footer(text=f"By {creator_str} on {created_str}")
    await inter.response.send_message(embed=e, ephemeral=True)

# ---------- RUN ----------
if __name__ == "__main__":
    if not TOKEN:
        print("ERROR: DISCORD_TOKEN missing in .env")
    else:
        bot.run(TOKEN)
