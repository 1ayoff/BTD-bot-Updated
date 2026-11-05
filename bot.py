"""
BTD Bot - Banish The Doubt
Prefix: ,   (or @mention the bot)

Storage: SQLite file at DB_PATH (default data/btd.db) - levels, warnings,
reminders and per-server settings survive restarts.
"""
import os
import random
import re
import sqlite3
import time
import traceback
from typing import Optional

import aiohttp
import discord
from discord.ext import commands, tasks
from discord.utils import format_dt, utcnow
from datetime import timedelta
from dotenv import load_dotenv

load_dotenv()

# ============================================================
# Config
# ============================================================
TOKEN = os.getenv("DISCORD_TOKEN")
PREFIX = os.getenv("PREFIX", ",")
DB_PATH = os.getenv("DB_PATH", "data/btd.db")
HEARTBEAT_URL = os.getenv("HEARTBEAT_URL", "")      # optional: status page
HEARTBEAT_TOKEN = os.getenv("HEARTBEAT_TOKEN", "")
OWNER_ID = int(os.getenv("OWNER_ID", "0") or 0)       # your Discord user ID (for ,status)
LASTFM_API_KEY = os.getenv("LASTFM_API_KEY", "")      # optional: enables ,fm

COLOR = 0x173D2B        # BTD dark green
ERROR_COLOR = 0xE5484D
FOOTER = "BTD • Banish The Doubt"
# Statuses the bot rotates through (shown under its name). {members} = total members.
STATUSES = [
    "discord.gg/banish",
    "banish the doubt.",
    ",help",
    "{members} members",
]
STATUS_SECONDS = 60
START_TIME = time.time()

MAX_TIMEOUT = 28 * 86400          # Discord's limit
MAX_REMINDER = 365 * 86400
MAX_REMINDERS_PER_USER = 25
XP_COOLDOWN = 60                  # seconds between XP gains per user

# ============================================================
# Database
# ============================================================
os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
db = sqlite3.connect(DB_PATH)
db.row_factory = sqlite3.Row
db.executescript(
    """
    CREATE TABLE IF NOT EXISTS settings (
        guild_id        INTEGER PRIMARY KEY,
        log_channel     INTEGER,
        welcome_channel INTEGER,
        autorole        INTEGER
    );
    CREATE TABLE IF NOT EXISTS warnings (
        id       INTEGER PRIMARY KEY AUTOINCREMENT,
        guild_id INTEGER NOT NULL,
        user_id  INTEGER NOT NULL,
        mod_id   INTEGER NOT NULL,
        reason   TEXT,
        ts       INTEGER NOT NULL
    );
    CREATE TABLE IF NOT EXISTS levels (
        guild_id INTEGER NOT NULL,
        user_id  INTEGER NOT NULL,
        xp       INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (guild_id, user_id)
    );
    CREATE TABLE IF NOT EXISTS reputation (
        guild_id    INTEGER NOT NULL,
        giver_id    INTEGER NOT NULL,
        receiver_id INTEGER NOT NULL,
        ts          INTEGER NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_rep_giver ON reputation (guild_id, giver_id, ts);
    CREATE TABLE IF NOT EXISTS lastfm (
        user_id  INTEGER PRIMARY KEY,
        username TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS reminders (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id    INTEGER NOT NULL,
        channel_id INTEGER NOT NULL,
        due        INTEGER NOT NULL,
        text       TEXT NOT NULL
    );
    """
)
db.commit()

SETTING_KEYS = {"log_channel", "welcome_channel", "autorole"}


def get_setting(guild_id, key):
    assert key in SETTING_KEYS  # keys go into SQL, so only allow known ones
    row = db.execute(f"SELECT {key} FROM settings WHERE guild_id = ?", (guild_id,)).fetchone()
    return row[0] if row else None


def set_setting(guild_id, key, value):
    assert key in SETTING_KEYS
    db.execute(
        f"""INSERT INTO settings (guild_id, {key}) VALUES (?, ?)
            ON CONFLICT(guild_id) DO UPDATE SET {key} = excluded.{key}""",
        (guild_id, value),
    )
    db.commit()


# ============================================================
# Helpers
# ============================================================
UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def parse_duration(text):
    """'10m', '2h', '1d', '1h30m' -> seconds. A bare number means minutes."""
    text = text.strip().lower().replace(" ", "")
    if text.isdigit():
        return int(text) * 60
    if not re.fullmatch(r"(\d+[smhdw])+", text):
        return None
    return sum(int(n) * UNITS[u] for n, u in re.findall(r"(\d+)([smhdw])", text))


def fmt_duration(seconds):
    seconds = int(seconds)
    parts = []
    for name, size in (("d", 86400), ("h", 3600), ("m", 60), ("s", 1)):
        if seconds >= size:
            parts.append(f"{seconds // size}{name}")
            seconds %= size
    return " ".join(parts) or "0s"


def xp_for_level(level):
    """Total XP needed to reach a level: 100, 300, 600, 1000, 1500..."""
    return 50 * level * (level + 1)


def level_from_xp(xp):
    level = 0
    while xp_for_level(level + 1) <= xp:
        level += 1
    return level


class Duration(commands.Converter):
    async def convert(self, ctx, argument):
        seconds = parse_duration(argument)
        if not seconds:
            raise commands.BadArgument("Use a time like `10m`, `2h`, `1d` or `1h30m`.")
        return seconds


def embed(description=None, title=None, color=COLOR):
    e = discord.Embed(title=title, description=description, color=color)
    e.set_footer(text=FOOTER)
    return e


def ok(text):
    return discord.Embed(description=f"✅ {text}", color=COLOR)


def err(text):
    return discord.Embed(description=f"❌ {text}", color=ERROR_COLOR)


async def can_act_on(ctx, member, action):
    """Role-hierarchy checks shared by kick/ban/timeout/warn/nick."""
    problem = None
    if member == ctx.author:
        problem = f"You can't {action} yourself."
    elif member == ctx.guild.me:
        problem = f"I'm not going to {action} myself."
    elif member.id == ctx.guild.owner_id:
        problem = f"You can't {action} the server owner."
    elif member.top_role >= ctx.author.top_role and ctx.author.id != ctx.guild.owner_id:
        problem = f"You can't {action} someone with an equal or higher role."
    elif member.top_role >= ctx.guild.me.top_role:
        problem = f"I can't {action} them, their role is higher than mine."
    if problem:
        await ctx.send(embed=err(problem))
        return False
    return True


async def modlog(guild, title, description):
    channel_id = get_setting(guild.id, "log_channel")
    channel = guild.get_channel(channel_id) if channel_id else None
    if channel is None:
        return
    e = discord.Embed(title=title, description=description, color=COLOR, timestamp=utcnow())
    try:
        await channel.send(embed=e)
    except discord.HTTPException:
        pass


# ============================================================
# Bot
# ============================================================
intents = discord.Intents.default()
intents.message_content = True
intents.members = True


class BTDBot(commands.Bot):
    async def setup_hook(self):
        self.http_session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))
        status_loop.start()
        reminder_loop.start()
        if HEARTBEAT_URL:
            heartbeat_loop.start()

    async def close(self):
        if hasattr(self, "http_session"):
            await self.http_session.close()
        await super().close()


bot = BTDBot(
    command_prefix=commands.when_mentioned_or(PREFIX),
    intents=intents,
    help_command=None,
    case_insensitive=True,
    owner_id=OWNER_ID or None,   # falls back to the app owner if not set
    activity=discord.CustomActivity(name=STATUSES[0]),
    # Nobody can make the bot ping @everyone / roles by accident (or on purpose)
    # through afk reasons, reminders, polls, etc. ,announce opts in explicitly.
    allowed_mentions=discord.AllowedMentions(everyone=False, roles=False, users=True, replied_user=False),
)

afk_users = {}   # (guild_id, user_id) -> (reason, since_datetime)
last_xp = {}     # (guild_id, user_id) -> unix time of last XP gain
sniped = {}      # channel_id -> (content, author, deleted_at)
fixed_status = None   # set by ,status; None = rotate through STATUSES


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} in {len(bot.guilds)} server(s)")


# ============================================================
# Background loops
# ============================================================
@tasks.loop(seconds=15)
async def reminder_loop():
    due = db.execute("SELECT * FROM reminders WHERE due <= ?", (int(time.time()),)).fetchall()
    for r in due:
        db.execute("DELETE FROM reminders WHERE id = ?", (r["id"],))
        db.commit()
        text = f"⏰ <@{r['user_id']}> reminder: {r['text']}"
        try:
            channel = bot.get_channel(r["channel_id"])
            if channel is not None:
                await channel.send(text)
            else:
                user = await bot.fetch_user(r["user_id"])
                await user.send(text)
        except discord.HTTPException:
            pass


@reminder_loop.before_loop
async def _before_reminders():
    await bot.wait_until_ready()


@tasks.loop(seconds=60)
async def heartbeat_loop():
    """Tells the status page the bot is alive (only while connected to Discord)."""
    if bot.is_closed() or not bot.is_ready():
        return
    try:
        async with aiohttp.ClientSession() as session:
            await session.post(
                HEARTBEAT_URL,
                headers={"Authorization": f"Bearer {HEARTBEAT_TOKEN}"},
                timeout=aiohttp.ClientTimeout(total=10),
            )
    except Exception as e:
        print("heartbeat failed:", type(e).__name__)


@heartbeat_loop.before_loop
async def _before_heartbeat():
    await bot.wait_until_ready()


@tasks.loop(seconds=STATUS_SECONDS)
async def status_loop():
    if fixed_status:
        text = fixed_status
    else:
        members = sum(g.member_count or 0 for g in bot.guilds)
        text = STATUSES[status_loop.current_loop % len(STATUSES)].format(members=f"{members:,}")
    await bot.change_presence(activity=discord.CustomActivity(name=text))


@status_loop.before_loop
async def _before_status():
    await bot.wait_until_ready()


# ============================================================
# Events: messages, AFK, XP
# ============================================================
@bot.event
async def on_message(message):
    if message.author.bot:
        return

    if message.guild is not None:
        await handle_afk(message)
        await handle_xp(message)

    await bot.process_commands(message)


async def handle_afk(message):
    key = (message.guild.id, message.author.id)
    is_afk_command = message.content.lower().startswith(f"{PREFIX}afk")

    # They talked, so they're back
    if key in afk_users and not is_afk_command:
        afk_users.pop(key)
        await message.channel.send(
            embed=ok(f"Welcome back {message.author.mention}, removed your AFK."), delete_after=5
        )

    # Someone pinged an AFK person
    for user in message.mentions[:5]:
        afk = afk_users.get((message.guild.id, user.id))
        if afk:
            reason, since = afk
            await message.channel.send(
                embed=embed(f"💤 **{user.display_name}** is AFK: {reason} ({format_dt(since, 'R')})")
            )


async def handle_xp(message):
    if message.content.startswith(PREFIX):
        return  # commands don't earn XP
    key = (message.guild.id, message.author.id)
    now = time.time()
    if now - last_xp.get(key, 0) < XP_COOLDOWN:
        return
    last_xp[key] = now

    row = db.execute(
        "SELECT xp FROM levels WHERE guild_id = ? AND user_id = ?", key
    ).fetchone()
    old_xp = row["xp"] if row else 0
    new_xp = old_xp + random.randint(15, 25)
    db.execute(
        """INSERT INTO levels (guild_id, user_id, xp) VALUES (?, ?, ?)
           ON CONFLICT(guild_id, user_id) DO UPDATE SET xp = excluded.xp""",
        (*key, new_xp),
    )
    db.commit()

    if level_from_xp(new_xp) > level_from_xp(old_xp):
        await message.channel.send(
            embed=embed(f"🔥 {message.author.mention} just hit **level {level_from_xp(new_xp)}**")
        )


# ============================================================
# Events: joins, leaves, deletes
# ============================================================
@bot.event
async def on_member_join(member):
    guild = member.guild

    role_id = get_setting(guild.id, "autorole")
    role = guild.get_role(role_id) if role_id else None
    if role and role < guild.me.top_role:
        try:
            await member.add_roles(role, reason="Autorole")
        except discord.HTTPException:
            pass

    channel_id = get_setting(guild.id, "welcome_channel")
    channel = guild.get_channel(channel_id) if channel_id else None
    if channel:
        e = embed(
            f"Welcome to **{guild.name}**, {member.mention}.\nYou're member **#{guild.member_count}**.",
            title="banish the doubt.",
        )
        e.set_thumbnail(url=member.display_avatar.url)
        try:
            await channel.send(content=member.mention, embed=e)
        except discord.HTTPException:
            pass

    await modlog(guild, "Member joined", f"{member.mention} (`{member.id}`)\nAccount created {format_dt(member.created_at, 'R')}")


@bot.event
async def on_member_remove(member):
    await modlog(member.guild, "Member left", f"{member} (`{member.id}`)")


@bot.event
async def on_message_delete(message):
    if message.author.bot or message.guild is None or not message.content:
        return
    sniped[message.channel.id] = (message.content, message.author, utcnow())
    content = message.content[:1000]
    await modlog(
        message.guild,
        "Message deleted",
        f"**Author:** {message.author.mention}\n**Channel:** {message.channel.mention}\n\n{content}",
    )


@bot.event
async def on_message_edit(before, after):
    if after.guild is None or after.author.bot or before.content == after.content:
        return
    await modlog(
        after.guild,
        "Message edited",
        f"**Author:** {after.author.mention} · [jump]({after.jump_url})\n"
        f"**Before:** {before.content[:900]}\n**After:** {after.content[:900]}",
    )


# ============================================================
# Core
# ============================================================
@bot.command()
async def btd(ctx):
    await ctx.send("banish the doubt.")


@bot.command()
async def tp(ctx):
    await ctx.send("lock in.")


@bot.command()
async def ping(ctx):
    await ctx.send(embed=embed(
        f"🏓 **{round(bot.latency * 1000)} ms**\n⏱️ Up for {fmt_duration(time.time() - START_TIME)}"
    ))


@bot.command(aliases=["av", "pfp"])
async def avatar(ctx, member: discord.Member = None):
    member = member or ctx.author
    e = embed(title=f"{member.display_name}'s avatar")
    e.set_image(url=member.display_avatar.url)
    await ctx.send(embed=e)


@bot.command()
@commands.guild_only()
async def serverinfo(ctx):
    guild = ctx.guild
    e = embed(title=f"{guild.name}")
    if guild.icon:
        e.set_thumbnail(url=guild.icon.url)
    e.add_field(name="Owner", value=f"<@{guild.owner_id}>")
    e.add_field(name="Members", value=guild.member_count)
    e.add_field(name="Roles", value=len(guild.roles))
    e.add_field(name="Text Channels", value=len(guild.text_channels))
    e.add_field(name="Voice Channels", value=len(guild.voice_channels))
    e.add_field(name="Boosts", value=f"{guild.premium_subscription_count} (tier {guild.premium_tier})")
    e.add_field(name="Created", value=format_dt(guild.created_at, "D"), inline=False)
    await ctx.send(embed=e)


@bot.command(aliases=["whois", "ui"])
@commands.guild_only()
async def userinfo(ctx, member: discord.Member = None):
    member = member or ctx.author
    e = embed(title=str(member))
    e.set_thumbnail(url=member.display_avatar.url)
    e.add_field(name="User", value=member.mention)
    e.add_field(name="ID", value=f"`{member.id}`")
    e.add_field(name="Top Role", value=member.top_role.mention)
    e.add_field(name="Joined Server", value=format_dt(member.joined_at, "R") if member.joined_at else "Unknown")
    e.add_field(name="Account Created", value=format_dt(member.created_at, "R"))
    e.add_field(name="Roles", value=len(member.roles) - 1)
    await ctx.send(embed=e)


@bot.command()
@commands.guild_only()
@commands.has_permissions(manage_messages=True)
async def say(ctx, *, message):
    await ctx.message.delete()
    await ctx.send(embed=discord.Embed(description=message, color=COLOR))


@bot.command()
@commands.guild_only()
@commands.has_permissions(administrator=True)
async def announce(ctx, *, message):
    await ctx.message.delete()
    e = embed(message, title="hey")
    e.set_footer(text="BTD Official")
    await ctx.send("@everyone", embed=e, allowed_mentions=discord.AllowedMentions(everyone=True))


@bot.command()
@commands.is_owner()
async def status(ctx, *, text: str):
    """,status <text> = fixed status   ,status cycle = rotate again"""
    global fixed_status
    if text.lower() in ("cycle", "reset", "auto"):
        fixed_status = None
        await ctx.send(embed=ok("Back to rotating statuses."))
    else:
        if len(text) > 128:
            return await ctx.send(embed=err("Keep it under 128 characters."))
        fixed_status = text
        await ctx.send(embed=ok(f"Status set to **{text}**. Use `{PREFIX}status cycle` to rotate again."))
    status_loop.restart()


# ============================================================
# Fun
# ============================================================
EIGHT_BALL = [
    "It is certain.", "Without a doubt.", "Yes, definitely.", "You may rely on it.",
    "Most likely.", "Outlook good.", "Signs point to yes.", "Ask again later.",
    "Better not tell you now.", "Cannot predict now.", "Don't count on it.",
    "My reply is no.", "My sources say no.", "Outlook not so good.", "Very doubtful.",
]
NUMBER_EMOJIS = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]


@bot.command(name="8ball")
@commands.cooldown(1, 3, commands.BucketType.user)
async def eightball(ctx, *, question):
    await ctx.send(embed=embed(f"**Q:** {question}\n🎱 {random.choice(EIGHT_BALL)}"))


@bot.command(aliases=["flip", "cf"])
@commands.cooldown(1, 3, commands.BucketType.user)
async def coinflip(ctx):
    await ctx.send(embed=embed(f"🪙 **{random.choice(['Heads', 'Tails'])}**"))


@bot.command()
@commands.cooldown(1, 3, commands.BucketType.user)
async def roll(ctx, dice: str = "1d6"):
    match = re.fullmatch(r"(\d*)d(\d+)", dice.lower())
    if not match:
        return await ctx.send(embed=err("Use dice like `d20`, `2d6` or `3d8`."))
    count, sides = int(match.group(1) or 1), int(match.group(2))
    if not (1 <= count <= 20 and 2 <= sides <= 1000):
        return await ctx.send(embed=err("Up to 20 dice with 2 to 1000 sides."))
    rolls = [random.randint(1, sides) for _ in range(count)]
    detail = f" ({', '.join(map(str, rolls))})" if count > 1 else ""
    await ctx.send(embed=embed(f"🎲 **{sum(rolls)}**{detail}"))


@bot.command()
async def choose(ctx, *, options):
    choices = [c.strip() for c in options.split("|") if c.strip()]
    if len(choices) < 2:
        return await ctx.send(embed=err("Give me at least 2 options split by `|`."))
    await ctx.send(embed=embed(f"🤔 I choose **{random.choice(choices)}**"))


@bot.command()
@commands.guild_only()
@commands.cooldown(1, 10, commands.BucketType.user)
async def poll(ctx, *, text):
    parts = [p.strip() for p in text.split("|") if p.strip()]
    if not parts:
        return await ctx.send(embed=err(f"Try `{PREFIX}poll pizza or tacos? | pizza | tacos`"))
    question, options = parts[0], parts[1:]
    if len(options) == 1 or len(options) > 10:
        return await ctx.send(embed=err("Use 2-10 options, or none for a yes/no poll."))

    if options:
        body = "\n".join(f"{NUMBER_EMOJIS[i]} {opt}" for i, opt in enumerate(options))
        reactions = NUMBER_EMOJIS[: len(options)]
    else:
        body, reactions = "", ["👍", "👎"]

    e = embed(body, title=f"📊 {question}")
    e.set_author(name=ctx.author.display_name, icon_url=ctx.author.display_avatar.url)
    msg = await ctx.send(embed=e)
    for r in reactions:
        await msg.add_reaction(r)


# ============================================================
# Utility
# ============================================================
@bot.command()
@commands.guild_only()
async def afk(ctx, *, reason="AFK"):
    afk_users[(ctx.guild.id, ctx.author.id)] = (reason[:200], utcnow())
    await ctx.send(embed=ok(f"{ctx.author.mention} is now AFK: {reason[:200]}"))


@bot.command(aliases=["remind", "reminder"])
async def remindme(ctx, when: Duration, *, text="(no message)"):
    if when > MAX_REMINDER:
        return await ctx.send(embed=err("Reminders can be at most 1 year away."))
    count = db.execute("SELECT COUNT(*) FROM reminders WHERE user_id = ?", (ctx.author.id,)).fetchone()[0]
    if count >= MAX_REMINDERS_PER_USER:
        return await ctx.send(embed=err(f"You already have {MAX_REMINDERS_PER_USER} reminders. Delete some with `{PREFIX}delreminder`."))

    due = int(time.time()) + when
    cur = db.execute(
        "INSERT INTO reminders (user_id, channel_id, due, text) VALUES (?, ?, ?, ?)",
        (ctx.author.id, ctx.channel.id, due, text[:500]),
    )
    db.commit()
    await ctx.send(embed=ok(f"I'll remind you <t:{due}:R> (id `{cur.lastrowid}`)."))


@bot.command()
async def reminders(ctx):
    rows = db.execute(
        "SELECT id, due, text FROM reminders WHERE user_id = ? ORDER BY due", (ctx.author.id,)
    ).fetchall()
    if not rows:
        return await ctx.send(embed=embed("You have no reminders."))
    lines = [f"`{r['id']}` · <t:{r['due']}:R> · {r['text'][:60]}" for r in rows]
    await ctx.send(embed=embed("\n".join(lines), title="⏰ Your reminders"))


@bot.command(aliases=["delremind"])
async def delreminder(ctx, reminder_id: int):
    cur = db.execute("DELETE FROM reminders WHERE id = ? AND user_id = ?", (reminder_id, ctx.author.id))
    db.commit()
    if cur.rowcount:
        await ctx.send(embed=ok(f"Deleted reminder `{reminder_id}`."))
    else:
        await ctx.send(embed=err("You don't have a reminder with that id."))


@bot.command()
@commands.guild_only()
async def snipe(ctx):
    data = sniped.get(ctx.channel.id)
    if not data or (utcnow() - data[2]).total_seconds() > 600:
        return await ctx.send(embed=err("Nothing to snipe."))
    content, author, when = data
    e = embed(content[:2000])
    e.set_author(name=author.display_name, icon_url=author.display_avatar.url)
    e.add_field(name="Deleted", value=format_dt(when, "R"))
    await ctx.send(embed=e)


# ============================================================
# Levels
# ============================================================
@bot.command(aliases=["level", "lvl"])
@commands.guild_only()
async def rank(ctx, member: discord.Member = None):
    member = member or ctx.author
    row = db.execute(
        "SELECT xp FROM levels WHERE guild_id = ? AND user_id = ?", (ctx.guild.id, member.id)
    ).fetchone()
    xp = row["xp"] if row else 0
    level = level_from_xp(xp)
    position = db.execute(
        "SELECT COUNT(*) + 1 FROM levels WHERE guild_id = ? AND xp > ?", (ctx.guild.id, xp)
    ).fetchone()[0]

    floor, ceiling = xp_for_level(level), xp_for_level(level + 1)
    progress = (xp - floor) / (ceiling - floor)
    bar = "▰" * round(progress * 12) + "▱" * (12 - round(progress * 12))

    e = embed(title=f"{member.display_name}")
    e.set_thumbnail(url=member.display_avatar.url)
    e.add_field(name="Level", value=level)
    e.add_field(name="Rank", value=f"#{position}")
    e.add_field(name="XP", value=f"{xp:,}")
    e.add_field(name="Next level", value=f"{bar}  {xp - floor}/{ceiling - floor}", inline=False)
    await ctx.send(embed=e)


@bot.command(aliases=["lb", "top"])
@commands.guild_only()
async def leaderboard(ctx):
    rows = db.execute(
        "SELECT user_id, xp FROM levels WHERE guild_id = ? ORDER BY xp DESC LIMIT 10", (ctx.guild.id,)
    ).fetchall()
    if not rows:
        return await ctx.send(embed=embed("Nobody has XP yet. Start talking."))
    medals = ["🥇", "🥈", "🥉"]
    lines = [
        f"{medals[i] if i < 3 else f'`{i + 1}.`'} <@{r['user_id']}> · level {level_from_xp(r['xp'])} · {r['xp']:,} xp"
        for i, r in enumerate(rows)
    ]
    await ctx.send(embed=embed("\n".join(lines), title=f"🏆 {ctx.guild.name} leaderboard"))


@bot.command()
@commands.guild_only()
async def rep(ctx, member: discord.Member = None):
    if member is None:
        total = rep_total(ctx.guild.id, ctx.author.id)
        return await ctx.send(embed=embed(f"You have **{total}** rep. Give some with `{PREFIX}rep @user`."))
    if member == ctx.author or member.bot:
        return await ctx.send(embed=err("Give rep to another person."))
    now = int(time.time())
    last = db.execute(
        "SELECT MAX(ts) FROM reputation WHERE guild_id = ? AND giver_id = ?", (ctx.guild.id, ctx.author.id)
    ).fetchone()[0]
    if last and now - last < 86400:
        return await ctx.send(embed=err(f"You can give rep again <t:{last + 86400}:R>."))
    db.execute(
        "INSERT INTO reputation (guild_id, giver_id, receiver_id, ts) VALUES (?, ?, ?, ?)",
        (ctx.guild.id, ctx.author.id, member.id, now),
    )
    db.commit()
    await ctx.send(embed=ok(f"+1 rep for {member.mention} · **{rep_total(ctx.guild.id, member.id)}** total"))


def rep_total(guild_id, user_id):
    return db.execute(
        "SELECT COUNT(*) FROM reputation WHERE guild_id = ? AND receiver_id = ?", (guild_id, user_id)
    ).fetchone()[0]


@bot.command()
@commands.guild_only()
async def reps(ctx, member: discord.Member = None):
    member = member or ctx.author
    await ctx.send(embed=embed(f"**{member.display_name}** has **{rep_total(ctx.guild.id, member.id)}** rep"))


@bot.command(aliases=["replb"])
@commands.guild_only()
async def repleaderboard(ctx):
    rows = db.execute(
        """SELECT receiver_id, COUNT(*) AS n FROM reputation WHERE guild_id = ?
           GROUP BY receiver_id ORDER BY n DESC LIMIT 10""",
        (ctx.guild.id,),
    ).fetchall()
    if not rows:
        return await ctx.send(embed=embed("No rep yet."))
    lines = [f"`{i}.` <@{r['receiver_id']}> · {r['n']} rep" for i, r in enumerate(rows, 1)]
    await ctx.send(embed=embed("\n".join(lines), title="⭐ Rep leaderboard"))


# ---------- Last.fm ----------
async def lastfm(method, username, **extra):
    if not LASTFM_API_KEY:
        raise commands.BadArgument("Last.fm isn't set up yet (missing LASTFM_API_KEY).")
    params = {"method": method, "user": username, "api_key": LASTFM_API_KEY, "format": "json", **extra}
    try:
        async with bot.http_session.get("https://ws.audioscrobbler.com/2.0/", params=params) as r:
            data = await r.json(content_type=None)
    except Exception:
        raise commands.BadArgument("Couldn't reach Last.fm. Try again later.") from None
    if not isinstance(data, dict) or "error" in data:
        raise commands.BadArgument("Last.fm couldn't find that account.")
    return data


@bot.group(invoke_without_command=True, aliases=["lastfm", "np"])
@commands.cooldown(1, 5, commands.BucketType.user)
async def fm(ctx, member: discord.Member = None):
    member = member or ctx.author
    row = db.execute("SELECT username FROM lastfm WHERE user_id = ?", (member.id,)).fetchone()
    if not row:
        return await ctx.send(embed=err(f"No Last.fm linked. Use `{PREFIX}fm set <username>`."))
    data = await lastfm("user.getRecentTracks", row["username"], limit="1")
    tracks = data.get("recenttracks", {}).get("track", [])
    if isinstance(tracks, dict):
        tracks = [tracks]
    if not tracks:
        return await ctx.send(embed=embed("No recent tracks."))
    t = tracks[0]
    playing = t.get("@attr", {}).get("nowplaying") == "true"
    name = discord.utils.escape_markdown(t.get("name", "Unknown")[:200])
    artist = discord.utils.escape_markdown(t.get("artist", {}).get("#text", "Unknown")[:200])
    album = discord.utils.escape_markdown(t.get("album", {}).get("#text", "")[:200])
    e = embed(f"**{name}**\n{artist}" + (f" · *{album}*" if album else ""),
              title="🎧 now playing" if playing else "🎧 last played")
    image = next((i.get("#text") for i in reversed(t.get("image", [])) if i.get("#text", "").startswith("https://")), None)
    if image:
        e.set_thumbnail(url=image)
    e.set_author(name=member.display_name, icon_url=member.display_avatar.url)
    await ctx.send(embed=e)


@fm.command(name="set")
async def fm_set(ctx, username: str):
    if not re.fullmatch(r"[A-Za-z0-9_-]{2,64}", username):
        return await ctx.send(embed=err("Just your Last.fm username, not a link."))
    await lastfm("user.getInfo", username)  # make sure it exists
    db.execute(
        "INSERT INTO lastfm (user_id, username) VALUES (?, ?) ON CONFLICT(user_id) DO UPDATE SET username = excluded.username",
        (ctx.author.id, username),
    )
    db.commit()
    await ctx.send(embed=ok(f"Linked **{username}**. Use `{PREFIX}fm` to show what you're playing."))


@fm.command(name="remove")
async def fm_remove(ctx):
    db.execute("DELETE FROM lastfm WHERE user_id = ?", (ctx.author.id,))
    db.commit()
    await ctx.send(embed=ok("Last.fm unlinked."))


# ============================================================
# Moderation
# ============================================================
@bot.command(aliases=["clear"])
@commands.guild_only()
@commands.has_permissions(manage_messages=True)
@commands.bot_has_permissions(manage_messages=True)
async def purge(ctx, amount: int, member: discord.Member = None):
    if not 1 <= amount <= 500:
        return await ctx.send(embed=err("Pick a number from 1 to 500."))
    await ctx.message.delete()
    if member:
        deleted = await ctx.channel.purge(limit=amount, check=lambda m: m.author == member)
    else:
        deleted = await ctx.channel.purge(limit=amount)
    await ctx.send(embed=ok(f"Deleted {len(deleted)} messages."), delete_after=3)
    await modlog(ctx.guild, "Purge", f"{ctx.author.mention} deleted {len(deleted)} messages in {ctx.channel.mention}")


@bot.command()
@commands.guild_only()
@commands.has_permissions(kick_members=True)
@commands.bot_has_permissions(kick_members=True)
async def kick(ctx, member: discord.Member, *, reason="No reason provided"):
    if not await can_act_on(ctx, member, "kick"):
        return
    await member.kick(reason=f"{ctx.author}: {reason}")
    await ctx.send(embed=ok(f"Kicked {member.mention} | {reason}"))
    await modlog(ctx.guild, "Kick", f"{member} (`{member.id}`) by {ctx.author.mention}\n**Reason:** {reason}")


@bot.command()
@commands.guild_only()
@commands.has_permissions(ban_members=True)
@commands.bot_has_permissions(ban_members=True)
async def ban(ctx, member: discord.Member, *, reason="No reason provided"):
    if not await can_act_on(ctx, member, "ban"):
        return
    await member.ban(reason=f"{ctx.author}: {reason}", delete_message_seconds=0)
    await ctx.send(embed=ok(f"Banned {member.mention} | {reason}"))
    await modlog(ctx.guild, "Ban", f"{member} (`{member.id}`) by {ctx.author.mention}\n**Reason:** {reason}")


@bot.command()
@commands.guild_only()
@commands.has_permissions(ban_members=True)
@commands.bot_has_permissions(ban_members=True)
async def unban(ctx, user_id: int, *, reason="No reason provided"):
    try:
        user = await bot.fetch_user(user_id)
        await ctx.guild.unban(user, reason=f"{ctx.author}: {reason}")
    except discord.NotFound:
        return await ctx.send(embed=err("That user isn't banned (or doesn't exist)."))
    await ctx.send(embed=ok(f"Unbanned {user} | {reason}"))
    await modlog(ctx.guild, "Unban", f"{user} (`{user.id}`) by {ctx.author.mention}\n**Reason:** {reason}")


@bot.command(aliases=["mute"])
@commands.guild_only()
@commands.has_permissions(moderate_members=True)
@commands.bot_has_permissions(moderate_members=True)
async def timeout(ctx, member: discord.Member, duration: Duration, *, reason="No reason provided"):
    if duration > MAX_TIMEOUT:
        return await ctx.send(embed=err("Timeouts can be at most 28 days."))
    if not await can_act_on(ctx, member, "timeout"):
        return
    await member.timeout(timedelta(seconds=duration), reason=f"{ctx.author}: {reason}")
    await ctx.send(embed=ok(f"Timed out {member.mention} for {fmt_duration(duration)} | {reason}"))
    await modlog(ctx.guild, "Timeout", f"{member.mention} for {fmt_duration(duration)} by {ctx.author.mention}\n**Reason:** {reason}")


@bot.command(aliases=["unmute"])
@commands.guild_only()
@commands.has_permissions(moderate_members=True)
@commands.bot_has_permissions(moderate_members=True)
async def untimeout(ctx, member: discord.Member):
    await member.timeout(None, reason=f"{ctx.author}: untimeout")
    await ctx.send(embed=ok(f"Removed timeout from {member.mention}"))
    await modlog(ctx.guild, "Timeout removed", f"{member.mention} by {ctx.author.mention}")


@bot.command()
@commands.guild_only()
@commands.has_permissions(moderate_members=True)
async def warn(ctx, member: discord.Member, *, reason="No reason provided"):
    if not await can_act_on(ctx, member, "warn"):
        return
    db.execute(
        "INSERT INTO warnings (guild_id, user_id, mod_id, reason, ts) VALUES (?, ?, ?, ?, ?)",
        (ctx.guild.id, member.id, ctx.author.id, reason[:500], int(time.time())),
    )
    db.commit()
    total = db.execute(
        "SELECT COUNT(*) FROM warnings WHERE guild_id = ? AND user_id = ?", (ctx.guild.id, member.id)
    ).fetchone()[0]
    await ctx.send(embed=ok(f"Warned {member.mention} | {reason} (warning #{total})"))
    try:
        await member.send(embed=embed(f"You were warned in **{ctx.guild.name}**: {reason}"))
    except discord.HTTPException:
        pass  # DMs closed
    await modlog(ctx.guild, "Warn", f"{member.mention} by {ctx.author.mention} (#{total})\n**Reason:** {reason}")


@bot.command(aliases=["warns"])
@commands.guild_only()
@commands.has_permissions(moderate_members=True)
async def warnings(ctx, member: discord.Member):
    rows = db.execute(
        "SELECT id, mod_id, reason, ts FROM warnings WHERE guild_id = ? AND user_id = ? ORDER BY ts DESC LIMIT 15",
        (ctx.guild.id, member.id),
    ).fetchall()
    if not rows:
        return await ctx.send(embed=embed(f"{member.mention} has no warnings."))
    lines = [f"`{r['id']}` · <t:{r['ts']}:R> · by <@{r['mod_id']}> · {r['reason']}" for r in rows]
    await ctx.send(embed=embed("\n".join(lines)[:4000], title=f"⚠️ Warnings for {member.display_name}"))


@bot.command()
@commands.guild_only()
@commands.has_permissions(moderate_members=True)
async def delwarn(ctx, warning_id: int):
    cur = db.execute("DELETE FROM warnings WHERE id = ? AND guild_id = ?", (warning_id, ctx.guild.id))
    db.commit()
    if cur.rowcount:
        await ctx.send(embed=ok(f"Deleted warning `{warning_id}`."))
    else:
        await ctx.send(embed=err("No warning with that id in this server."))


@bot.command()
@commands.guild_only()
@commands.has_permissions(administrator=True)
async def clearwarns(ctx, member: discord.Member):
    cur = db.execute("DELETE FROM warnings WHERE guild_id = ? AND user_id = ?", (ctx.guild.id, member.id))
    db.commit()
    await ctx.send(embed=ok(f"Cleared {cur.rowcount} warning(s) for {member.mention}."))


@bot.command()
@commands.guild_only()
@commands.has_permissions(manage_nicknames=True)
@commands.bot_has_permissions(manage_nicknames=True)
async def nick(ctx, member: discord.Member, *, nickname=None):
    if member != ctx.author and not await can_act_on(ctx, member, "rename"):
        return
    await member.edit(nick=nickname)
    if nickname:
        await ctx.send(embed=ok(f"Changed {member.mention}'s nickname to `{nickname}`"))
    else:
        await ctx.send(embed=ok(f"Reset {member.mention}'s nickname"))


@bot.command()
@commands.guild_only()
@commands.has_permissions(manage_roles=True)
@commands.bot_has_permissions(manage_roles=True)
async def role(ctx, member: discord.Member, *, role: discord.Role):
    if role.is_default() or role.managed:
        return await ctx.send(embed=err("That role can't be given out."))
    if role >= ctx.author.top_role and ctx.author.id != ctx.guild.owner_id:
        return await ctx.send(embed=err("That role is equal to or higher than yours."))
    if role >= ctx.guild.me.top_role:
        return await ctx.send(embed=err("That role is higher than mine."))

    if role in member.roles:
        await member.remove_roles(role, reason=f"By {ctx.author}")
        await ctx.send(embed=ok(f"Removed {role.mention} from {member.mention}"))
    else:
        await member.add_roles(role, reason=f"By {ctx.author}")
        await ctx.send(embed=ok(f"Gave {role.mention} to {member.mention}"))


@bot.command()
@commands.guild_only()
@commands.has_permissions(manage_channels=True)
@commands.bot_has_permissions(manage_channels=True)
async def slowmode(ctx, seconds: int = 0):
    if not 0 <= seconds <= 21600:
        return await ctx.send(embed=err("Slowmode must be 0 to 21600 seconds (6h)."))
    await ctx.channel.edit(slowmode_delay=seconds)
    await ctx.send(embed=ok("Slowmode off." if seconds == 0 else f"Slowmode set to {fmt_duration(seconds)}."))


@bot.command()
@commands.guild_only()
@commands.has_permissions(manage_channels=True)
@commands.bot_has_permissions(manage_roles=True)
async def lock(ctx):
    overwrite = ctx.channel.overwrites_for(ctx.guild.default_role)
    overwrite.send_messages = False
    await ctx.channel.set_permissions(ctx.guild.default_role, overwrite=overwrite, reason=f"Locked by {ctx.author}")
    await ctx.send(embed=ok("🔒 Channel locked."))
    await modlog(ctx.guild, "Channel locked", f"{ctx.channel.mention} by {ctx.author.mention}")


@bot.command()
@commands.guild_only()
@commands.has_permissions(manage_channels=True)
@commands.bot_has_permissions(manage_roles=True)
async def unlock(ctx):
    overwrite = ctx.channel.overwrites_for(ctx.guild.default_role)
    overwrite.send_messages = None
    await ctx.channel.set_permissions(ctx.guild.default_role, overwrite=overwrite, reason=f"Unlocked by {ctx.author}")
    await ctx.send(embed=ok("🔓 Channel unlocked."))
    await modlog(ctx.guild, "Channel unlocked", f"{ctx.channel.mention} by {ctx.author.mention}")


# ============================================================
# Server setup (admins)
# ============================================================
@bot.command()
@commands.guild_only()
@commands.has_permissions(manage_guild=True)
async def setlog(ctx, channel: Optional[discord.TextChannel] = None):
    set_setting(ctx.guild.id, "log_channel", channel.id if channel else None)
    await ctx.send(embed=ok(f"Mod logs will go to {channel.mention}." if channel else "Mod logs turned off."))


@bot.command()
@commands.guild_only()
@commands.has_permissions(manage_guild=True)
async def setwelcome(ctx, channel: Optional[discord.TextChannel] = None):
    set_setting(ctx.guild.id, "welcome_channel", channel.id if channel else None)
    await ctx.send(embed=ok(f"Welcome messages will go to {channel.mention}." if channel else "Welcome messages turned off."))


@bot.command()
@commands.guild_only()
@commands.has_permissions(manage_guild=True)
async def autorole(ctx, *, role: Optional[discord.Role] = None):
    if role and (role.is_default() or role.managed or role >= ctx.guild.me.top_role):
        return await ctx.send(embed=err("I can't give that role (it's managed, @everyone, or above my role)."))
    set_setting(ctx.guild.id, "autorole", role.id if role else None)
    await ctx.send(embed=ok(f"New members will get {role.mention}." if role else "Autorole turned off."))


@bot.command()
@commands.guild_only()
@commands.has_permissions(manage_guild=True)
async def settings(ctx):
    def show(key, kind):
        value = get_setting(ctx.guild.id, key)
        if not value:
            return "off"
        return f"<#{value}>" if kind == "channel" else f"<@&{value}>"

    e = embed(title="⚙️ Server settings")
    e.add_field(name="Mod logs", value=show("log_channel", "channel"))
    e.add_field(name="Welcome", value=show("welcome_channel", "channel"))
    e.add_field(name="Autorole", value=show("autorole", "role"))
    await ctx.send(embed=e)


# ============================================================
# Help  (,help = menu with a dropdown, ,help <command> = details)
# ============================================================
HELP = {
    "Core": {
        "emoji": "⭐", "blurb": "basics",
        "cmds": [
            ("btd", "", "banish the doubt"),
            ("tp", "", "needed sometimes"),
            ("ping", "", "latency and uptime"),
            ("avatar", "[@user]", "show someone's avatar"),
            ("serverinfo", "", "server stats"),
            ("userinfo", "[@user]", "user stats"),
            ("say", "<text>", "send a message as an embed (mods)"),
            ("announce", "<text>", "@everyone announcement (admins)"),
        ],
    },
    "Fun": {
        "emoji": "🎲", "blurb": "games and randomness",
        "cmds": [
            ("8ball", "<question>", "ask the magic 8 ball"),
            ("coinflip", "", "heads or tails"),
            ("roll", "[2d6]", "roll dice"),
            ("choose", "a | b | c", "let the bot pick"),
            ("poll", "question | opt1 | opt2", "make a poll (no options = yes/no)"),
        ],
    },
    "Utility": {
        "emoji": "🧰", "blurb": "reminders, afk, levels",
        "cmds": [
            ("remindme", "<10m/2h/1d> <text>", "set a reminder"),
            ("reminders", "", "list your reminders"),
            ("delreminder", "<id>", "delete a reminder"),
            ("afk", "[reason]", "go AFK, pings get an auto-reply"),
            ("snipe", "", "show the last deleted message here"),
            ("rank", "[@user]", "level and XP"),
            ("leaderboard", "", "top 10 by XP"),
        ],
    },
    "Music & Rep": {
        "emoji": "🎧", "blurb": "Last.fm and reputation",
        "cmds": [
            ("fm", "[@user]", "what you're listening to (Last.fm)"),
            ("fm set", "<username>", "link your Last.fm"),
            ("fm remove", "", "unlink your Last.fm"),
            ("rep", "[@user]", "give +1 rep (once a day) or check yours"),
            ("reps", "[@user]", "see someone's rep"),
            ("replb", "", "top 10 by rep"),
        ],
    },
    "Moderation": {
        "emoji": "🔨", "blurb": "keep the server clean",
        "cmds": [
            ("purge", "<amount> [@user]", "delete messages (optionally only one user's)"),
            ("kick", "@user [reason]", "kick a member"),
            ("ban", "@user [reason]", "ban a member"),
            ("unban", "<user_id>", "unban by user ID"),
            ("timeout", "@user <10m/2h/1d> [reason]", "time someone out (max 28d)"),
            ("untimeout", "@user", "remove a timeout"),
            ("warn", "@user [reason]", "warn a member (they get a DM)"),
            ("warnings", "@user", "list someone's warnings"),
            ("delwarn", "<id>", "delete one warning"),
            ("clearwarns", "@user", "delete all their warnings (admins)"),
        ],
    },
    "Channels & Roles": {
        "emoji": "🛡️", "blurb": "nicknames, roles, locks",
        "cmds": [
            ("nick", "@user [name]", "change a nickname (blank = reset)"),
            ("role", "@user <role>", "give or take a role"),
            ("slowmode", "<seconds>", "set slowmode (0 = off)"),
            ("lock", "", "stop @everyone talking here"),
            ("unlock", "", "undo lock"),
        ],
    },
    "Setup": {
        "emoji": "⚙️", "blurb": "logs, welcome, autorole",
        "cmds": [
            ("setlog", "[#channel]", "mod log channel (blank = off)"),
            ("setwelcome", "[#channel]", "welcome channel (blank = off)"),
            ("autorole", "[role]", "role new members get (blank = off)"),
            ("settings", "", "show the current setup"),
            ("status", "<text | cycle>", "set the bot's status (owner only)"),
        ],
    },
}


def help_home():
    total = sum(len(c["cmds"]) for c in HELP.values())
    e = embed(
        f"banish the doubt.\n\nPrefix is `{PREFIX}` · {total} commands\n"
        f"Pick a category below, or `{PREFIX}help <command>` for details.",
        title="BTD Bot",
    )
    if bot.user:
        e.set_thumbnail(url=bot.user.display_avatar.url)
    for name, cat in HELP.items():
        e.add_field(
            name=f"{cat['emoji']} {name}",
            value=" ".join(f"`{c[0]}`" for c in cat["cmds"]),
            inline=True,
        )
    e.set_footer(text="discord.gg/banish • [ ] optional  < > required")
    return e


def help_category(name):
    cat = HELP[name]
    lines = [
        f"**`{PREFIX}{cmd}{' ' + args if args else ''}`**\n{desc}"
        for cmd, args, desc in cat["cmds"]
    ]
    e = embed("\n\n".join(lines), title=f"{cat['emoji']} {name}")
    e.set_footer(text="discord.gg/banish • [ ] optional  < > required")
    return e


def help_command(command):
    for cat in HELP.values():
        for cmd, args, desc in cat["cmds"]:
            if cmd == command.qualified_name:
                e = embed(desc, title=f"{PREFIX}{cmd}")
                e.add_field(name="Usage", value=f"`{PREFIX}{cmd}{' ' + args if args else ''}`", inline=False)
                if command.aliases:
                    e.add_field(name="Also works", value=" ".join(f"`{PREFIX}{a}`" for a in command.aliases), inline=False)
                return e
    return None


class HelpMenu(discord.ui.View):
    def __init__(self, author_id):
        super().__init__(timeout=120)
        self.author_id = author_id
        self.message = None
        options = [discord.SelectOption(label="Home", emoji="🏠", value="__home__")]
        options += [
            discord.SelectOption(label=name, emoji=cat["emoji"], description=cat["blurb"], value=name)
            for name, cat in HELP.items()
        ]
        self.select = discord.ui.Select(placeholder="Pick a category", options=options)
        self.select.callback = self.on_select
        self.add_item(self.select)

    async def interaction_check(self, interaction):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(f"Run `{PREFIX}help` yourself to use the menu.", ephemeral=True)
            return False
        return True

    async def on_select(self, interaction):
        choice = self.select.values[0]
        e = help_home() if choice == "__home__" else help_category(choice)
        await interaction.response.edit_message(embed=e, view=self)

    async def on_timeout(self):
        self.select.disabled = True
        self.select.placeholder = f"Menu expired, run {PREFIX}help again"
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


@bot.command(aliases=["commands", "h"])
async def help(ctx, *, command_name: str = None):
    if command_name:
        command = bot.get_command(command_name.lstrip(PREFIX).lower())
        e = help_command(command) if command else None
        if e is None:
            return await ctx.send(embed=err(f"No command called `{command_name}`."))
        return await ctx.send(embed=e)

    view = HelpMenu(ctx.author.id)
    view.message = await ctx.send(embed=help_home(), view=view)


# ============================================================
# Errors
# ============================================================
@bot.event
async def on_command_error(ctx, error):
    error = getattr(error, "original", error)

    if isinstance(error, commands.CommandNotFound):
        return
    if isinstance(error, commands.MissingPermissions):
        msg = "You don't have permission to use that command."
    elif isinstance(error, commands.BotMissingPermissions):
        missing = ", ".join(p.replace("_", " ") for p in error.missing_permissions)
        msg = f"I'm missing permissions: **{missing}**"
    elif isinstance(error, commands.NoPrivateMessage):
        msg = "That command only works in a server."
    elif isinstance(error, commands.MissingRequiredArgument):
        msg = f"Missing `{error.param.name}`. Check `{PREFIX}help`."
    elif isinstance(error, commands.MemberNotFound):
        msg = "I couldn't find that member."
    elif isinstance(error, commands.RoleNotFound):
        msg = "I couldn't find that role."
    elif isinstance(error, commands.ChannelNotFound):
        msg = "I couldn't find that channel."
    elif isinstance(error, commands.CommandOnCooldown):
        msg = f"Slow down, try again in {error.retry_after:.0f}s."
    elif isinstance(error, commands.BadArgument):
        msg = str(error) or "Invalid argument."
    elif isinstance(error, discord.Forbidden):
        msg = "Discord won't let me do that. Check that my role is high enough."
    else:
        traceback.print_exception(type(error), error, error.__traceback__)
        msg = "Something went wrong running that command."

    try:
        await ctx.send(embed=err(msg))
    except discord.HTTPException:
        pass


# ============================================================
# Run
# ============================================================
if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("DISCORD_TOKEN is missing. Put it in your .env file.")
    bot.run(TOKEN)
