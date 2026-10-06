"""BTD: shi ill prob add more commands later but it has moderation, logging, reputation and Last.fm. Python 3.11+."""
import asyncio
import logging
import random
import time
from collections import OrderedDict
import os
from pathlib import Path
import sqlite3
from datetime import timedelta

import aiohttp
import discord
from discord.ext import commands, tasks
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / '.env')
COLOR = 0x000000
log = logging.getLogger('btd')

class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS config (guild INTEGER PRIMARY KEY, channel INTEGER);
        CREATE TABLE IF NOT EXISTS preferences (user INTEGER PRIMARY KEY, color INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS snipe_settings (guild INTEGER PRIMARY KEY, public INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS autoroles (guild INTEGER PRIMARY KEY, role INTEGER);
        CREATE TABLE IF NOT EXISTS lastfm (user INTEGER PRIMARY KEY, username TEXT);
        CREATE TABLE IF NOT EXISTS reputation (guild INTEGER, giver INTEGER, receiver INTEGER, at REAL);
        CREATE INDEX IF NOT EXISTS rep_lookup ON reputation(guild, giver, at);
        ''')
    def channel(self, guild):
        row = self.db.execute('SELECT channel FROM config WHERE guild=?', (guild,)).fetchone()
        return row[0] if row else None
    def set_channel(self, guild, channel):
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO config VALUES (?,?)', (guild, channel))
    def username(self, user):
        row = self.db.execute('SELECT username FROM lastfm WHERE user=?', (user,)).fetchone()
        return row[0] if row else None
    def set_username(self, user, name):
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO lastfm VALUES (?,?)', (user, name))
    def give_rep(self, guild, giver, receiver, now):
        with self.db:
            row = self.db.execute('SELECT MAX(at) FROM reputation WHERE guild=? AND giver=?', (guild, giver)).fetchone()
            if row[0] is not None and now - row[0] < 86400:
                return 86400 - (now - row[0])
            self.db.execute('INSERT INTO reputation VALUES (?,?,?,?)', (guild, giver, receiver, now))
        return 0
    def total(self, guild, user):
        return self.db.execute('SELECT COUNT(*) FROM reputation WHERE guild=? AND receiver=?', (guild, user)).fetchone()[0]

class BTDBot(commands.Bot):
    async def setup_hook(self):
        path = Path(os.getenv('BTD_DATA_DIR', str(ROOT / 'data')))
        path.mkdir(parents=True, exist_ok=True)
        self.store = Store(path / 'btd.sqlite3')
        self.http_session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))
        try:
            synced = await self.tree.sync()
            log.info('Registered %s slash command entries', len(synced))
        except discord.HTTPException:
            log.warning('Slash registration failed; reinvite with applications.commands and restart.')
    async def close(self):
        status_cycle.cancel()
        if hasattr(self, 'http_session'):
            await self.http_session.close()
        if hasattr(self, 'store'):
            self.store.db.close()
        await super().close()

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
owner = os.getenv('OWNER_ID', '').strip()
bot = BTDBot(command_prefix=os.getenv('BOT_PREFIX', ','), intents=intents,
             help_command=None, owner_id=int(owner) if owner else None,
             allowed_mentions=discord.AllowedMentions.none())

@bot.check
async def server_only(ctx):
    if ctx.guild is None:
        raise commands.NoPrivateMessage()
    return True

def embed(title=None, description=None):
    return discord.Embed(title=title, description=description, color=COLOR)

async def audit(guild, title, details):
    channel = guild.get_channel(bot.store.channel(guild.id) or 0)
    if not isinstance(channel, discord.TextChannel):
        return
    try:
        await channel.send(embed=embed(title, details[:4000]))
    except discord.HTTPException:
        log.warning('Could not send log in guild %s', guild.id)

def target_check(ctx, member):
    if member.id in (ctx.author.id, ctx.guild.owner_id, ctx.me.id):
        raise commands.BadArgument('You cannot target yourself, the server owner, or this bot.')
    if ctx.author.id != ctx.guild.owner_id and member.top_role >= ctx.author.top_role:
        raise commands.BadArgument('That member has an equal or higher role than you.')
    if member.top_role >= ctx.me.top_role:
        raise commands.BadArgument('Move my role above that member’s role first.')

@bot.before_invoke
async def acknowledge_slash(ctx):
    if ctx.interaction and not ctx.interaction.response.is_done():
        await ctx.defer(ephemeral=ctx.command.name in ('help', 'statuscycle') and await bot.is_owner(ctx.author))

@bot.event
async def on_ready():
    log.info('BTD connected as %s to %s servers', bot.user, len(bot.guilds))

@bot.hybrid_command()
async def btd(ctx):
    await ctx.send('banish the doubt.')

@bot.hybrid_command()
async def tp(ctx):
    await ctx.send('lock in')

@bot.hybrid_command()
async def ping(ctx):
    await ctx.send(f'{round(bot.latency * 1000)} ms')

@bot.hybrid_command()
@commands.is_owner()
async def status(ctx, *, text: str):
    if len(text) > 128:
        raise commands.BadArgument('Keep the status under 129 characters.')
    status_cycle.cancel()
    await bot.change_presence(activity=discord.Game(text))
    await ctx.send('Status updated.')

@bot.hybrid_command()
@commands.is_owner()
async def logchannel(ctx, channel: discord.TextChannel):
    perms = channel.permissions_for(ctx.me)
    if not (perms.view_channel and perms.send_messages and perms.embed_links):
        raise commands.BadArgument('I need View Channel, Send Messages and Embed Links there.')
    bot.store.set_channel(ctx.guild.id, channel.id)
    await ctx.send(f'Logging to {channel.mention}.')

@bot.hybrid_command()
@commands.is_owner()
async def logoff(ctx):
    bot.store.set_channel(ctx.guild.id, None)
    await ctx.send('Logging disabled.')

@bot.hybrid_command()
@commands.has_permissions(manage_messages=True)
@commands.bot_has_permissions(send_messages=True, embed_links=True)
async def say(ctx, *, message: str):
    if len(message) > 4000:
        raise commands.BadArgument('Keep the message under 4,001 characters.')
    await ctx.send(embed=embed(description=message))

@bot.hybrid_command()
@commands.has_permissions(administrator=True)
@commands.bot_has_permissions(mention_everyone=True, embed_links=True)
async def announce(ctx, *, message: str):
    if len(message) > 4000:
        raise commands.BadArgument('Keep the announcement under 4,001 characters.')
    await ctx.send('@everyone', embed=embed('btd', message),
                   allowed_mentions=discord.AllowedMentions(everyone=True))

@bot.hybrid_command()
@commands.has_permissions(manage_messages=True)
@commands.bot_has_permissions(manage_messages=True, read_message_history=True)
async def purge(ctx, amount: int):
    if not 1 <= amount <= 100:
        raise commands.BadArgument('Choose 1–100 messages.')
    # Keep the invoking message out of the count.
    deleted = await ctx.channel.purge(limit=amount, before=ctx.interaction.created_at if ctx.interaction else ctx.message)
    await ctx.send(f'Deleted {len(deleted)} messages.', delete_after=5)
    await audit(ctx.guild, 'Messages purged', f'{ctx.author} • {ctx.channel.mention} • {len(deleted)} messages')

@bot.hybrid_command()
@commands.has_permissions(kick_members=True)
@commands.bot_has_permissions(kick_members=True)
async def kick(ctx, member: discord.Member, *, reason='No reason provided'):
    target_check(ctx, member)
    await member.kick(reason=f'{ctx.author}: {reason}'[:512])
    await ctx.send(f'Kicked {member}.')
    await audit(ctx.guild, 'Kick', f'{member} ({member.id}) • by {ctx.author}\n{reason}')

@bot.hybrid_command()
@commands.has_permissions(ban_members=True)
@commands.bot_has_permissions(ban_members=True)
async def ban(ctx, member: discord.Member, *, reason='No reason provided'):
    target_check(ctx, member)
    await member.ban(reason=f'{ctx.author}: {reason}'[:512], delete_message_seconds=0)
    await ctx.send(f'Banned {member}.')
    await audit(ctx.guild, 'Ban', f'{member} ({member.id}) • by {ctx.author}\n{reason}')

@bot.hybrid_command()
@commands.has_permissions(ban_members=True)
@commands.bot_has_permissions(ban_members=True)
async def unban(ctx, user_id: str, *, reason='No reason provided'):
    if not user_id.isdecimal() or not 1 <= int(user_id) < 2**64:
        raise commands.BadArgument('Enter a numeric Discord user ID.')
    await ctx.guild.unban(discord.Object(id=int(user_id)), reason=f'{ctx.author}: {reason}'[:512])
    await ctx.send(f'Unbanned {user_id}.')
    await audit(ctx.guild, 'Unban', f'{user_id} • by {ctx.author}\n{reason}')

@bot.hybrid_command()
@commands.has_permissions(moderate_members=True)
@commands.bot_has_permissions(moderate_members=True)
async def timeout(ctx, member: discord.Member, minutes: int, *, reason='No reason provided'):
    target_check(ctx, member)
    if not 1 <= minutes <= 40320:
        raise commands.BadArgument('Choose 1–40,320 minutes (up to 28 days).')
    if member.guild_permissions.administrator:
        raise commands.BadArgument('Discord does not allow timeouts for administrators.')
    await member.timeout(timedelta(minutes=minutes), reason=f'{ctx.author}: {reason}'[:512])
    await ctx.send(f'Timed out {member} for {minutes} minutes.')
    await audit(ctx.guild, 'Timeout', f'{member} • by {ctx.author} • {minutes} minutes\n{reason}')

@bot.hybrid_command()
@commands.has_permissions(moderate_members=True)
@commands.bot_has_permissions(moderate_members=True)
async def untimeout(ctx, member: discord.Member):
    target_check(ctx, member)
    await member.timeout(None, reason=f'Removed by {ctx.author}')
    await ctx.send(f'Removed timeout from {member}.')
    await audit(ctx.guild, 'Timeout removed', f'{member} • by {ctx.author}')

@bot.hybrid_command()
@commands.has_permissions(manage_nicknames=True)
@commands.bot_has_permissions(manage_nicknames=True)
async def nick(ctx, member: discord.Member, *, nickname: str = None):
    target_check(ctx, member)
    if nickname and len(nickname) > 32:
        raise commands.BadArgument('Nicknames can have up to 32 characters.')
    await member.edit(nick=nickname, reason=f'Changed by {ctx.author}')
    await ctx.send(f'Nickname updated for {member}.')
    await audit(ctx.guild, 'Nickname changed', f'{member} • by {ctx.author} • {nickname or "reset"}')

@bot.hybrid_command()
async def serverinfo(ctx):
    g = ctx.guild
    e = embed(g.name)
    if g.icon:
        e.set_thumbnail(url=g.icon.url)
    for name, value in [('Members', g.member_count), ('Owner', f'<@{g.owner_id}>'),
                        ('Roles', len(g.roles)), ('Channels', len(g.channels)),
                        ('Created', discord.utils.format_dt(g.created_at, 'D'))]:
        e.add_field(name=name, value=str(value))
    await ctx.send(embed=e)

@bot.hybrid_command()
async def userinfo(ctx, member: discord.Member = None):
    m = member or ctx.author
    e = embed(str(m))
    e.set_thumbnail(url=m.display_avatar.url)
    e.add_field(name='Account created', value=discord.utils.format_dt(m.created_at, 'D'))
    e.add_field(name='Joined', value=discord.utils.format_dt(m.joined_at, 'D') if m.joined_at else 'Unknown')
    e.add_field(name='Role', value=m.top_role.mention)
    e.set_footer(text=str(m.id))
    await ctx.send(embed=e)

@bot.hybrid_command()
async def rep(ctx, member: discord.Member = None):
    if member is None:
        return await ctx.send(f'You have {bot.store.total(ctx.guild.id, ctx.author.id)} rep. Give rep with `,rep @user`.')
    if member.id == ctx.author.id or member.bot:
        raise commands.BadArgument('Give rep to another person.')
    wait = bot.store.give_rep(ctx.guild.id, ctx.author.id, member.id, discord.utils.utcnow().timestamp())
    if wait:
        return await ctx.send(f'You can give rep again in {max(1, int(wait // 3600) + 1)} hours.')
    await ctx.send(f'+1 rep for {member.mention} • {bot.store.total(ctx.guild.id, member.id)} total')

@bot.hybrid_command()
async def reps(ctx, member: discord.Member = None):
    m = member or ctx.author
    await ctx.send(f'{m.display_name} • {bot.store.total(ctx.guild.id, m.id)} rep')

@bot.hybrid_command()
async def leaderboard(ctx):
    rows = bot.store.db.execute('SELECT receiver, COUNT(*) AS n FROM reputation WHERE guild=? GROUP BY receiver ORDER BY n DESC, receiver LIMIT 10', (ctx.guild.id,)).fetchall()
    await ctx.send(embed=embed('rep', '\n'.join(f'{i}. <@{u}> • {n}' for i, (u, n) in enumerate(rows, 1)) or 'No rep yet.'))

async def lastfm_request(method, username, **extra):
    key = os.getenv('LASTFM_API_KEY', '').strip()
    if not key:
        raise commands.BadArgument('Last.fm needs LASTFM_API_KEY in the bot’s environment first.')
    params = {'method': method, 'user': username, 'api_key': key, 'format': 'json', **extra}
    try:
        async with bot.http_session.get('https://ws.audioscrobbler.com/2.0/', params=params) as r:
            status = r.status
            try:
                data = await r.json(content_type=None)
            except (ValueError, aiohttp.ContentTypeError):
                data = {}
    except (aiohttp.ClientError, asyncio.TimeoutError):
        raise commands.BadArgument('Could not reach Last.fm. Try again shortly.') from None
    code = data.get('error') if isinstance(data, dict) else None
    if code is not None:
        try:
            code = int(code)
        except (ValueError, TypeError):
            code = 0
        errors = {
            6: 'Last.fm could not find that username. Check your Last.fm profile name.',
            10: 'Last.fm rejected the API key. Set LASTFM_API_KEY to the API key, not the shared secret, in Railway Variables and deploy the change.',
            26: 'Last.fm has suspended this API key. Check your Last.fm API account.',
            29: 'Last.fm rate limit reached. Wait a minute and try again.',
            11: 'Last.fm is temporarily offline. Try again shortly.',
            16: 'Last.fm is temporarily unavailable. Try again shortly.',
        }
        raise commands.BadArgument(errors.get(code, f'Last.fm returned API error {code}. Try again shortly.'))
    if status == 429:
        raise commands.BadArgument('Last.fm rate limit reached. Wait a minute and try again.')
    if status in (401, 403):
        raise commands.BadArgument(f'Last.fm denied the request (HTTP {status}). Check LASTFM_API_KEY in Railway Variables, then redeploy.')
    if status != 200:
        raise commands.BadArgument(f'Last.fm returned HTTP {status}. Try again shortly.')
    if not isinstance(data, dict) or not data:
        raise commands.BadArgument('Last.fm returned an unreadable response. Try again shortly.')
    return data

@bot.hybrid_group(invoke_without_command=True, aliases=['lastfm'], fallback='now')
@commands.cooldown(1, 5, commands.BucketType.user)
async def fm(ctx, member: discord.Member = None):
    m = member or ctx.author
    username = bot.store.username(m.id)
    if not username:
        raise commands.BadArgument('Link your account with `,fm set username`.')
    data = await lastfm_request('user.getRecentTracks', username, limit='1')
    tracks = data.get('recenttracks', {}).get('track', [])
    if isinstance(tracks, dict):
        tracks = [tracks]
    if not tracks:
        return await ctx.send('No recent tracks found.')
    track = tracks[0]
    playing = track.get('@attr', {}).get('nowplaying') == 'true'
    e = personal_embed(m.id, 'now playing' if playing else 'last played',
              f'**{discord.utils.escape_markdown(track.get("name", "Unknown")[:200])}**\n{discord.utils.escape_markdown(track.get("artist", {}).get("#text", "Unknown")[:200])}')
    image = next((im.get('#text') for im in reversed(track.get('image', [])) if im.get('#text', '').startswith('https://')), None)
    if image:
        e.set_thumbnail(url=image)
    e.set_footer(text=f'{m.display_name} • {username}'[:200])
    await ctx.send(embed=e)

@fm.command(name='set')
async def fm_set(ctx, username: str):
    if not 1 <= len(username) <= 64 or not all(c.isascii() and (c.isalnum() or c in '_-') for c in username):
        raise commands.BadArgument('Enter your Last.fm username, not a URL.')
    await lastfm_request('user.getInfo', username)
    bot.store.set_username(ctx.author.id, username)
    await ctx.send(f'Linked {username}. Use `,fm` to show your latest track.')

@fm.command(name='remove')
async def fm_remove(ctx):
    with bot.store.db:
        bot.store.db.execute('DELETE FROM lastfm WHERE user=?', (ctx.author.id,))
    await ctx.send('Last.fm account unlinked.')

@bot.event
async def on_member_join(member):
    row = bot.store.db.execute('SELECT role FROM autoroles WHERE guild=?', (member.guild.id,)).fetchone()
    if row and not member.bot:
        role = member.guild.get_role(row[0])
        me = member.guild.me
        if role and me and me.guild_permissions.manage_roles and not role.is_default() and not role.managed and role < me.top_role:
            try:
                await member.add_roles(role, reason='BTD autorole')
            except discord.HTTPException:
                log.warning('Autorole failed in guild %s', member.guild.id)
    await audit(member.guild, 'Member joined', f'{member} ({member.id})\nAccount created {discord.utils.format_dt(member.created_at, "R")}')

@bot.event
async def on_member_remove(member):
    await audit(member.guild, 'Member left', f'{member} ({member.id})')

@bot.event
async def on_raw_message_delete(payload):
    guild = bot.get_guild(payload.guild_id) if payload.guild_id else None
    if not guild or payload.channel_id == bot.store.channel(guild.id):
        return
    msg = payload.cached_message
    if msg and msg.author.bot:
        return
    if msg and msg.content:
        remember_snipe(deleted_snipes, msg.channel.id, str(msg.author), msg.content)
    await audit(guild, 'Message deleted', f'Channel <#{payload.channel_id}> • message {payload.message_id}\nAuthor: {msg.author if msg else "not cached"}')

@bot.event
async def on_message_edit(before, after):
    if after.guild and not after.author.bot and before.content != after.content:
        remember_snipe(edited_snipes, after.channel.id, str(before.author), before.content)
        if after.channel.id != bot.store.channel(after.guild.id):
            await audit(after.guild, 'Message edited', f'{after.author} • {after.jump_url}')


# i used ai for these quotes lol
BTD_QUOTES = [
    'You can be angry and still choose what happens next.',
    'A bad chapter does not get to name the whole book.',
    'Protect your peace without abandoning your future.',
    'Doubt can ride with you. It does not get to drive.',
    'Being understood by everyone is not a requirement for moving forward.',
    'Small progress still counts on the days nobody sees it.',
    'You cannot control every outcome. You can choose your next move.',
    'Rest, then return. You do not have to disappear.',
]

@bot.hybrid_command(description='An original BTD thought to sit with.')
async def quote(ctx):
    e = embed(description=random.choice(BTD_QUOTES))
    e.set_footer(text='btd • original thought')
    await ctx.send(embed=e)

@bot.hybrid_command(description='A little dadaman energy.')
async def dadaman(ctx):
    await ctx.send(random.choice([
        'dadaman has entered the chat.',
        'dadaman said handle your business.',
        'dadaman is observing.',
    ]))

@bot.hybrid_command(description='Sam, banish the doubt.')
async def sam(ctx):
    await ctx.send(random.choice([
        'Sam. One thing at a time.',
        'Sam, banish the doubt.',
        'Sam is still writing his story.',
    ]))

@bot.hybrid_command(description='Show the latest track from a linked Last.fm account.')
@commands.cooldown(1, 5, commands.BucketType.user)
async def np(ctx, member: discord.Member = None):
    await fm.callback(ctx, member)

async def show_top(ctx, member, period, kind):
    periods = {'week': '7day', 'month': '1month', 'year': '12month', 'all': 'overall'}
    if period not in periods:
        raise commands.BadArgument('Choose week, month, year, or all.')
    member = member or ctx.author
    username = bot.store.username(member.id)
    if not username:
        raise commands.BadArgument('Link an account with `,fm set username` or `/fm set`.')
    method, root, item = ('user.getTopTracks', 'toptracks', 'track') if kind == 'tracks' else ('user.getTopArtists', 'topartists', 'artist')
    data = await lastfm_request(method, username, period=periods[period], limit='10')
    rows = data.get(root, {}).get(item, [])
    if isinstance(rows, dict):
        rows = [rows]
    lines = []
    for i, row in enumerate(rows, 1):
        name = discord.utils.escape_markdown(row.get('name', 'Unknown')[:100])
        artist = discord.utils.escape_markdown(row.get('artist', {}).get('name', '')[:100]) if kind == 'tracks' else ''
        lines.append(f'{i}. **{name}**' + (f' • {artist}' if artist else '') + f' — {row.get("playcount", 0)} plays')
    e = personal_embed(member.id, f'top {kind} • {period}', '\n'.join(lines) or 'No listening history for this period.')
    e.set_footer(text=f'{member.display_name} • {username}'[:200])
    await ctx.send(embed=e)

@bot.hybrid_command(description='Your most played tracks on Last.fm.')
@commands.cooldown(1, 5, commands.BucketType.user)
async def toptracks(ctx, period: str = 'month', member: discord.Member = None):
    await show_top(ctx, member, period, 'tracks')

@bot.hybrid_command(description='Your most played artists on Last.fm.')
@commands.cooldown(1, 5, commands.BucketType.user)
async def topartists(ctx, period: str = 'month', member: discord.Member = None):
    await show_top(ctx, member, period, 'artists')


# lawful citizen
deleted_snipes = OrderedDict()
edited_snipes = OrderedDict()

def remember_snipe(cache, channel, author, content):
    now = time.monotonic()
    for key in list(cache):
        if now - cache[key][0] > 600:
            del cache[key]
    cache[channel] = (now, author, content[:3500])
    cache.move_to_end(channel)
    while len(cache) > 500:
        cache.popitem(last=False)

async def show_snipe(ctx, cache, title):
    row = cache.get(ctx.channel.id)
    if not row or time.monotonic() - row[0] > 600:
        cache.pop(ctx.channel.id, None)
        return await ctx.send('Nothing recent to snipe here.', ephemeral=bool(ctx.interaction))
    e = embed(title, discord.utils.escape_markdown(row[2])[:3900])
    e.set_footer(text=row[1][:200])
    # show or hide
    await ctx.send(embed=e, ephemeral=bool(ctx.interaction))

async def snipe_access(ctx):
    if not ctx.guild:
        return False
    row = bot.store.db.execute('SELECT public FROM snipe_settings WHERE guild=?', (ctx.guild.id,)).fetchone()
    if (row and row[0]) or ctx.author.guild_permissions.manage_messages or await bot.is_owner(ctx.author):
        return True
    raise commands.BadArgument('Snipes are staff-only here. A server admin can enable public snipes with `snipepublic true`.')

@bot.hybrid_command(description='Show the latest cached deletion in this channel (10 minutes).')
@commands.check(snipe_access)
async def snipe(ctx):
    await show_snipe(ctx, deleted_snipes, 'deleted message')

@bot.hybrid_command(description='Show the previous text of the latest edit in this channel.')
@commands.check(snipe_access)
async def editsnipe(ctx):
    await show_snipe(ctx, edited_snipes, 'before edit')

@bot.hybrid_command(description='Clear recent snipes in this channel.')
@commands.has_permissions(manage_messages=True)
async def clearsnipe(ctx):
    deleted_snipes.pop(ctx.channel.id, None)
    edited_snipes.pop(ctx.channel.id, None)
    await ctx.send('Snipes cleared.', ephemeral=bool(ctx.interaction))

@bot.hybrid_command(description='Show a member’s avatar.')
async def avatar(ctx, member: discord.Member = None):
    m = member or ctx.author
    e = embed(m.display_name)
    e.set_image(url=m.display_avatar.with_size(1024).url)
    await ctx.send(embed=e)

@bot.hybrid_command(description='Show a member’s profile banner.')
@commands.cooldown(1, 5, commands.BucketType.user)
async def banner(ctx, member: discord.Member = None):
    user = await bot.fetch_user((member or ctx.author).id)
    if not user.banner:
        return await ctx.send('No profile banner set.')
    e = embed(user.display_name)
    e.set_image(url=user.banner.with_size(1024).url)
    await ctx.send(embed=e)

@bot.hybrid_command(description='Show the server icon.')
async def servericon(ctx):
    if not ctx.guild.icon:
        return await ctx.send('This server has no icon.')
    e = embed(ctx.guild.name)
    e.set_image(url=ctx.guild.icon.with_size(1024).url)
    await ctx.send(embed=e)

@bot.hybrid_command(description='Show server member and bot counts.')
async def membercount(ctx):
    e = embed('members')
    e.add_field(name='Total', value=str(ctx.guild.member_count))
    e.add_field(name='Bots in cache', value=str(sum(m.bot for m in ctx.guild.members)))
    await ctx.send(embed=e)

@bot.hybrid_command(description='Show a role’s details.')
async def roleinfo(ctx, role: discord.Role):
    e = embed(role.name)
    e.add_field(name='ID', value=str(role.id))
    e.add_field(name='Members in cache', value=str(len(role.members)))
    e.add_field(name='Color', value=str(role.color))
    e.add_field(name='Created', value=discord.utils.format_dt(role.created_at, 'D'))
    await ctx.send(embed=e)

@bot.hybrid_command(description='Flip a coin.')
async def coinflip(ctx):
    await ctx.send(random.choice(['heads', 'tails']))

@bot.hybrid_command(description='Roll a die with 2–1000 sides.')
async def roll(ctx, sides: int = 6):
    if not 2 <= sides <= 1000:
        raise commands.BadArgument('Choose 2–1000 sides.')
    await ctx.send(f'{random.randint(1, sides)} / {sides}')

@bot.hybrid_command(description='Pick between options separated by a vertical bar.')
async def choose(ctx, *, options: str):
    choices = [x.strip() for x in options.split('|') if x.strip()]
    if not 2 <= len(choices) <= 20 or any(len(x) > 200 for x in choices):
        raise commands.BadArgument('Use 2–20 options, up to 200 characters each: `choose val | sleep`.')
    await ctx.send(random.choice(choices))

@bot.hybrid_command(name='8ball', description='Ask the magic 8 ball a question.')
async def eightball(ctx, *, question: str):
    await ctx.send(random.choice(['yes.', 'no.', 'probably.', 'unlikely.', 'ask again later.', 'you already know.']))


def dangerous_role(role):
    permissions = role.permissions
    return any(getattr(permissions, name, False) for name in (
        'administrator', 'manage_guild', 'manage_roles', 'manage_channels',
        'manage_webhooks', 'kick_members', 'ban_members', 'moderate_members',
        'manage_messages', 'mention_everyone', 'manage_threads',
        'manage_events', 'manage_expressions', 'manage_emojis_and_stickers'))

def check_assignable(ctx, role):
    if role.is_default() or role.managed:
        raise commands.BadArgument('Choose an ordinary role, not @everyone or an integration role.')
    if role >= ctx.me.top_role:
        raise commands.BadArgument('Move my role above the role you want to assign.')
    if ctx.author.id != ctx.guild.owner_id and role >= ctx.author.top_role:
        raise commands.BadArgument('Choose a role below your highest role.')
    if dangerous_role(role) and ctx.author.id != ctx.guild.owner_id:
        raise commands.BadArgument('Only the server owner can bulk-assign a staff role.')

@bot.hybrid_command(description='Set the role given to new human members.')
@commands.has_permissions(manage_roles=True)
@commands.bot_has_permissions(manage_roles=True)
async def autorole(ctx, role: discord.Role):
    check_assignable(ctx, role)
    with bot.store.db:
        bot.store.db.execute('INSERT OR REPLACE INTO autoroles VALUES (?,?)', (ctx.guild.id, role.id))
    await ctx.send(f'New members will receive {role.mention}.')
    await audit(ctx.guild, 'Autorole configured', f'{ctx.author} • {role.name} ({role.id})')

@bot.hybrid_command(description='Disable automatic roles for new members.')
@commands.has_permissions(manage_roles=True)
async def autoroleoff(ctx):
    with bot.store.db:
        bot.store.db.execute('DELETE FROM autoroles WHERE guild=?', (ctx.guild.id,))
    await ctx.send('Autorole disabled.')
    await audit(ctx.guild, 'Autorole disabled', str(ctx.author))

@bot.hybrid_command(description='Give a role to all current human members; preview first.')
@commands.has_permissions(manage_roles=True)
@commands.bot_has_permissions(manage_roles=True)
@commands.max_concurrency(1, per=commands.BucketType.guild, wait=False)
@commands.cooldown(1, 15, commands.BucketType.guild)
async def roleall(ctx, role: discord.Role, confirm: bool = False):
    check_assignable(ctx, role)
    if not ctx.guild.chunked:
        await ctx.guild.chunk()
    members = [m for m in ctx.guild.members if not m.bot and role not in m.roles]
    if not confirm:
        return await ctx.send(f'{len(members)} members would receive {role.mention}. Run again with `confirm: True` in slash, or `,roleall {role.id} true`.')
    await ctx.send(f'Assigning {role.mention} to {len(members)} members…')
    success = failed = 0
    for member in members:
        if ctx.guild.get_role(role.id) is None or not ctx.me.guild_permissions.manage_roles or role >= ctx.me.top_role:
            failed += len(members) - success - failed
            break
        try:
            await member.add_roles(role, reason=f'BTD roleall by {ctx.author}')
            success += 1
        except discord.HTTPException:
            failed += 1
    await ctx.send(f'Done. Assigned: {success}. Failed or stopped: {failed}.')
    await audit(ctx.guild, 'Bulk role assignment', f'{ctx.author} • {role.name} • assigned {success} • failed {failed}')


cycle_texts = []
cycle_index = 0

@tasks.loop(seconds=60)
async def status_cycle():
    global cycle_index
    if cycle_texts:
        await bot.change_presence(activity=discord.Game(cycle_texts[cycle_index % len(cycle_texts)]))
        cycle_index += 1

@status_cycle.before_loop
async def wait_for_status_ready():
    await bot.wait_until_ready()

@status_cycle.error
async def status_cycle_error(error):
    log.error('Status cycle stopped (%s)', type(error).__name__)

@bot.hybrid_command(description='Owner: cycle statuses separated by |; use off to stop.')
@commands.is_owner()
async def statuscycle(ctx, *, statuses: str):
    global cycle_texts, cycle_index
    if statuses.strip().lower() == 'off':
        status_cycle.cancel()
        await ctx.send('Status cycle stopped.', ephemeral=bool(ctx.interaction))
        return
    choices = [x.strip() for x in statuses.split('|') if x.strip()]
    if not 2 <= len(choices) <= 10 or any(len(x) > 128 for x in choices):
        raise commands.BadArgument('Use 2–10 statuses, up to 128 characters each, separated by |.')
    status_cycle.cancel()
    # yes indeed let a canceled loop finish before starting its replacement.
    previous = status_cycle.get_task()
    if previous:
        try:
            await previous
        except asyncio.CancelledError:
            pass
    cycle_texts, cycle_index = choices, 0
    status_cycle.start()
    await ctx.send('Status cycle started. Changes every 60 seconds.', ephemeral=bool(ctx.interaction))


@bot.hybrid_command(description='Enable or disable public snipe commands in this server.')
@commands.has_permissions(manage_guild=True)
async def snipepublic(ctx, enabled: bool):
    with bot.store.db:
        bot.store.db.execute('INSERT OR REPLACE INTO snipe_settings VALUES (?,?)', (ctx.guild.id, int(enabled)))
    await ctx.send('Snipes are now public in this server.' if enabled else 'Snipes are now staff-only.')

@bot.hybrid_command(description='Set your personal embed color with a hex code, or reset.')
async def embedcolor(ctx, color: str):
    if color.lower() == 'reset':
        with bot.store.db:
            bot.store.db.execute('DELETE FROM preferences WHERE user=?', (ctx.author.id,))
        return await ctx.send('Your embed color is back to black.')
    value = color.removeprefix('#').removeprefix('0x')
    if len(value) != 6 or any(c not in '0123456789abcdefABCDEF' for c in value):
        raise commands.BadArgument('Use a six-digit hex color, like #000000 or #173d2b.')
    with bot.store.db:
        bot.store.db.execute('INSERT OR REPLACE INTO preferences VALUES (?,?)', (ctx.author.id, int(value, 16)))
    e = personal_embed(ctx.author.id, 'embed color', '#' + value)
    await ctx.send(embed=e)

def personal_embed(user_id, title=None, description=None):
    e = embed(title, description)
    row = bot.store.db.execute('SELECT color FROM preferences WHERE user=?', (user_id,)).fetchone()
    if row:
        e.color = row[0]
    return e

@bot.hybrid_command(name='embed', description='Send your own embed, using your saved color.')
@commands.cooldown(1, 5, commands.BucketType.user)
async def custom_embed(ctx, title: str, *, description: str):
    if len(title) > 256 or len(description) > 3500:
        raise commands.BadArgument('Use up to 256 characters for the title and 3,500 for the description.')
    e = personal_embed(ctx.author.id, title, description)
    e.set_footer(text=f'by {ctx.author.display_name}'[:200])
    await ctx.send(embed=e)

@bot.hybrid_command(description='Show your 10 most recent Last.fm tracks.')
@commands.cooldown(1, 5, commands.BucketType.user)
async def recent(ctx, member: discord.Member = None):
    m = member or ctx.author
    username = bot.store.username(m.id)
    if not username:
        raise commands.BadArgument('Link your Last.fm with `fm set username`.')
    data = await lastfm_request('user.getRecentTracks', username, limit='10')
    rows = data.get('recenttracks', {}).get('track', [])
    if isinstance(rows, dict):
        rows = [rows]
    lines = []
    for i, track in enumerate(rows[:10], 1):
        name = discord.utils.escape_markdown(track.get('name', 'Unknown')[:100])
        artist = discord.utils.escape_markdown(track.get('artist', {}).get('#text', 'Unknown')[:100])
        lines.append(f'{i}. **{name}** • {artist}')
    await ctx.send(embed=personal_embed(m.id, 'recent tracks', '\n'.join(lines) or 'No recent tracks.'))

@bot.hybrid_command(description='Show the most played albums on Last.fm.')
@commands.cooldown(1, 5, commands.BucketType.user)
async def topalbums(ctx, period: str = 'month', member: discord.Member = None):
    periods = {'week': '7day', 'month': '1month', 'year': '12month', 'all': 'overall'}
    if period not in periods:
        raise commands.BadArgument('Choose week, month, year, or all.')
    m = member or ctx.author
    username = bot.store.username(m.id)
    if not username:
        raise commands.BadArgument('Link your Last.fm with `fm set username`.')
    data = await lastfm_request('user.getTopAlbums', username, period=periods[period], limit='10')
    rows = data.get('topalbums', {}).get('album', [])
    if isinstance(rows, dict):
        rows = [rows]
    lines = []
    for i, row in enumerate(rows[:10], 1):
        name = discord.utils.escape_markdown(row.get('name', 'Unknown')[:100])
        artist = discord.utils.escape_markdown(row.get('artist', {}).get('name', 'Unknown')[:100])
        lines.append(f'{i}. **{name}** • {artist} — {row.get("playcount", 0)} plays')
    await ctx.send(embed=personal_embed(m.id, f'top albums • {period}', '\n'.join(lines) or 'No albums for this period.'))

@bot.hybrid_command(description="Show BTD commands.")
async def help(ctx):
    e = embed('btd', 'Use slash commands or prefix `' + str(bot.command_prefix) + '`.\nLast.fm now playing: `/np` or `/fm now` or `,fm`.')
    e.add_field(name='customization', value='`embedcolor #hex` · `embedcolor reset` · `embed title description`', inline=False)
    e.add_field(name='general', value='`btd` · `tp` · `ping` · `serverinfo` · `userinfo [@user]`', inline=False)
    e.add_field(name='utilities', value='`avatar` · `banner` · `servericon` · `membercount` · `roleinfo @role`', inline=False)
    e.add_field(name='fun', value='`coinflip` · `roll [sides]` · `choose option | option` · `8ball question`', inline=False)
    e.add_field(name='music', value='`fm set username` · `np [@user]` · `fm remove`\n`toptracks [week/month/year/all]` · `topartists [week/month/year/all]`\n`recent [@user]` · `topalbums [week/month/year/all]`', inline=False)
    e.add_field(name='btd', value='`quote` · `dadaman` · `sam`', inline=False)
    e.add_field(name='rep', value='`rep @user` · `reps [@user]` · `leaderboard`\nOne rep per person every 24 hours.', inline=False)
    e.add_field(name='roles', value='`autorole @role` · `autoroleoff` · `roleall @role [true]`', inline=False)
    e.add_field(name='moderation', value='`purge 1–100` · `kick @user [reason]` · `ban @user [reason]`\n`unban user_id [reason]` · `timeout @user minutes [reason]`\n`untimeout @user` · `nick @user [nickname]`\n`say message` · `announce message`\n`snipe` · `editsnipe` · `clearsnipe` · `snipepublic true/false`', inline=False)
    is_owner = await bot.is_owner(ctx.author)
    if is_owner:
        e.add_field(name='owner • just you', value='`logchannel #channel` · `logoff` · `status text`\n`statuscycle text | text` · `statuscycle off`', inline=False)
    if is_owner and not ctx.interaction:
        try:
            await ctx.author.send(embed=e)
            await ctx.send('Sent your owner help in DMs.')
        except discord.Forbidden:
            await ctx.send('Enable DMs or use `/help` for your private owner menu.')
    else:
        await ctx.send(embed=e, ephemeral=bool(ctx.interaction and is_owner))

@bot.event
async def on_command_error(ctx, error):
    original = getattr(error, 'original', error)
    if isinstance(error, commands.CommandNotFound):
        return
    if isinstance(error, commands.NoPrivateMessage):
        text = 'Use this command in your server.'
    elif isinstance(error, commands.NotOwner):
        text = 'That command is owner only.'
    elif isinstance(error, commands.BotMissingPermissions):
        text = 'I need: ' + ', '.join(error.missing_permissions).replace('_', ' ')
    elif isinstance(error, commands.MissingPermissions):
        text = 'You do not have permission for that command.'
    elif isinstance(error, commands.MaxConcurrencyReached):
        text = 'A bulk role assignment is already running in this server.'
    elif isinstance(error, commands.CommandOnCooldown):
        text = f'Try again in {error.retry_after:.0f} seconds.'
    elif isinstance(error, commands.MissingRequiredArgument):
        text = f'Missing `{error.param.name}`. Use `,help`.'
    elif isinstance(error, commands.BadArgument):
        text = str(error)
    elif isinstance(original, discord.Forbidden):
        text = 'Discord blocked that action. Check my permissions and role position.'
    elif isinstance(original, discord.NotFound):
        text = 'That user, ban, or message could not be found.'
    elif isinstance(original, discord.HTTPException):
        text = 'Discord could not complete that action. Try again later.'
    else:
      
        log.error('Command %s failed (%s)', ctx.command, type(original).__name__)
        text = 'Something went wrong. Check the bot console.'
    await ctx.send(text)

if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s: %(message)s')
    token = os.getenv('DISCORD_TOKEN', '').strip()
    if not token:
        raise SystemExit('Set DISCORD_TOKEN in .env or your host environment.')
    bot.run(token)
