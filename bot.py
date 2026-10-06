"""BTD: shi ill prob add more commands later but it has moderation, logging, reputation and Last.fm. Python 3.11+."""
import re
import json
import asyncio
import logging
import random
import time
from collections import OrderedDict
import os
from pathlib import Path
import sqlite3
from datetime import timedelta
from urllib.parse import quote as urlquote, urlparse

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
        CREATE TABLE IF NOT EXISTS security_trust (guild INTEGER, user INTEGER, PRIMARY KEY (guild, user));
        CREATE TABLE IF NOT EXISTS quarantine (guild INTEGER, member INTEGER, roles TEXT, PRIMARY KEY (guild, member));
        CREATE TABLE IF NOT EXISTS warnings (id INTEGER PRIMARY KEY AUTOINCREMENT, guild INTEGER NOT NULL, member INTEGER NOT NULL, moderator INTEGER NOT NULL, reason TEXT NOT NULL, at REAL NOT NULL, active INTEGER NOT NULL DEFAULT 1);
        CREATE TABLE IF NOT EXISTS channel_locks (guild INTEGER, channel INTEGER PRIMARY KEY, previous INTEGER);
        CREATE TABLE IF NOT EXISTS runtime_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS cleanup (guild INTEGER PRIMARY KEY, seconds INTEGER NOT NULL DEFAULT 8, requests INTEGER NOT NULL DEFAULT 1);
        CREATE TABLE IF NOT EXISTS scheduled_deletes (guild INTEGER, channel INTEGER, message INTEGER PRIMARY KEY, due REAL);
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

TRANSIENT = {'purge', 'delete', 'cleanup', 'kick', 'ban', 'unban', 'timeout',
             'untimeout', 'nick', 'autorole', 'autoroleoff', 'clearsnipe',
             'snipepublic', 'status', 'statuscycle', 'logchannel', 'logoff', 'warn', 'unwarn', 'slowmode', 'lock', 'unlock', 'canceldelete', 'cleanbots'}

class CleanContext(commands.Context):
    async def send(self, content=None, **kwargs):
        name = self.command.qualified_name if self.command else ''
        if name in TRANSIENT and hasattr(self.bot, 'store'):
            kwargs.setdefault('delete_after', cleanup_settings(self.guild.id)[0])
        return await super().send(content, **kwargs)

class BTDBot(commands.Bot):
    async def get_context(self, origin, /, *, cls=CleanContext):
        return await super().get_context(origin, cls=cls)

    async def setup_hook(self):
        path = Path(os.getenv('BTD_DATA_DIR', str(ROOT / 'data')))
        path.mkdir(parents=True, exist_ok=True)
        self.store = Store(path / 'btd.sqlite3')
        self.started_at = time.monotonic()
        deletion_worker.start()
        self.http_session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))
        try:
            synced = await self.tree.sync()
            log.info('Registered %s slash command entries', len(synced))
        except discord.HTTPException:
            log.warning('Slash registration failed; reinvite with applications.commands and restart.')
    async def close(self):
        status_cycle.cancel()
        deletion_worker.cancel()
        if hasattr(self, 'http_session'):
            await self.http_session.close()
        if hasattr(self, 'store'):
            self.store.db.close()
        await super().close()

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.moderation = True
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
        await ctx.defer(ephemeral=ctx.command.name in ('help', 'dashboard', 'status', 'statuscycle', 'logchannel', 'logoff', 'ownerstats', 'activity', 'synccommands', 'fmtest', 'clearcache'))

@bot.event
async def on_ready():
    log.info('BTD connected as %s to %s servers', bot.user, len(bot.guilds))
    if not getattr(bot, 'presence_restored', False):
        bot.presence_restored = True
        await restore_presence()

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
    save_runtime('presence', {'mode': 'single', 'texts': [text]})
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
async def purge(ctx, amount: int, member: discord.Member = None, bots_only: bool = False):
    if not 1 <= amount <= 100:
        raise commands.BadArgument('Choose 1–100 messages.')
    # Keep the invoking message out of the count.
    deleted = await ctx.channel.purge(limit=amount, before=ctx.interaction.created_at if ctx.interaction else ctx.message,
        check=lambda m: not m.pinned and (member is None or m.author.id == member.id) and (not bots_only or m.author.bot))
    await ctx.send(f'Deleted {len(deleted)} messages from the last {amount} scanned. Pinned messages kept.')
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

async def _lastfm_request(method, account, **extra):
    key = os.getenv('LASTFM_API_KEY', '').strip()
    if not key:
        raise commands.BadArgument('Last.fm needs LASTFM_API_KEY in the bot’s environment first.')
    params = {'method': method, 'user': account, 'api_key': key, 'format': 'json', **extra}
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

fm_cache = OrderedDict()
fm_pending = {}

async def lastfm_request(method, account, **extra):
    key = (method, account.casefold(), tuple(sorted(extra.items())))
    cached = fm_cache.get(key)
    if cached and time.monotonic()-cached[0] < 20:
        return cached[1]
    if key in fm_pending:
        return await asyncio.shield(fm_pending[key])
    task = asyncio.create_task(_lastfm_request(method, account, **extra))
    fm_pending[key] = task
    try:
        data = await task
        fm_cache[key] = (time.monotonic(), data)
        fm_cache.move_to_end(key)
        while len(fm_cache) > 200:
            fm_cache.popitem(last=False)
        return data
    finally:
        fm_pending.pop(key, None)

def music_rows(data, root, item):
    container = data.get(root, {})
    rows = container.get(item, []) if isinstance(container, dict) else []
    return [rows] if isinstance(rows, dict) else rows if isinstance(rows, list) else []

def fm_username(member):
    username = bot.store.username(member.id)
    if not username:
        raise commands.BadArgument(f'Link Last.fm first with `{bot.command_prefix}fm set username`.')
    return username

def music_image(images):
    return next((im.get('#text') for im in reversed(images or []) if isinstance(im, dict) and im.get('#text', '').startswith('https://')), None)

def number(value):
    try:
        return f'{int(value):,}'
    except (TypeError, ValueError):
        return '—'

def lastfm_url(url):
    if isinstance(url, str) and urlparse(url).hostname in ('www.last.fm', 'last.fm'):
        return 'https://' + url.split('://', 1)[-1]
    return None

async def nowplaying_card(member):
    username = fm_username(member)
    data = await lastfm_request('user.getRecentTracks', username, limit='1', extended='1')
    tracks = music_rows(data, 'recenttracks', 'track')
    if not tracks:
        raise commands.BadArgument('No recent tracks found for this profile.')
    track = tracks[0]
    artist = track.get('artist', {}).get('name') or track.get('artist', {}).get('#text', 'Unknown')
    name = track.get('name', 'Unknown')
    playing = track.get('@attr', {}).get('nowplaying') == 'true'
    profile_url = 'https://www.last.fm/user/' + urlquote(username, safe='')
    e = personal_embed(member.id, ('● now playing' if playing else 'last played'),
        f'**{discord.utils.escape_markdown(name[:200])}**\n{discord.utils.escape_markdown(artist[:200])}')
    e.set_author(name=member.display_name[:256], icon_url=member.display_avatar.url, url=profile_url)
    album = track.get('album', {}).get('#text')
    if album:
        e.add_field(name='Album', value=discord.utils.escape_markdown(album[:200]), inline=False)
    requests = [lastfm_request('track.getInfo', username, username=username, artist=artist, track=name, autocorrect='1'),
                lastfm_request('artist.getInfo', username, username=username, artist=artist, autocorrect='1')]
    if album:
        requests.append(lastfm_request('album.getInfo', username, username=username, artist=artist, album=album, autocorrect='1'))
    results = await asyncio.gather(*requests, return_exceptions=True)
    info = results[0].get('track', {}) if isinstance(results[0], dict) else {}
    artist_info = results[1].get('artist', {}).get('stats', {}) if isinstance(results[1], dict) else {}
    e.add_field(name='Song plays', value=number(info.get('userplaycount')))
    e.add_field(name='Artist plays', value=number(artist_info.get('userplaycount')))
    if album:
        album_info = results[2].get('album', {}) if isinstance(results[2], dict) else {}
        e.add_field(name='Album plays', value=number(album_info.get('userplaycount')))
    if str(info.get('userloved', track.get('loved', '0'))) == '1':
        e.add_field(name='Saved', value='♡ loved')
    total = data.get('recenttracks', {}).get('@attr', {}).get('total')
    if total is not None:
        e.add_field(name='Scrobbles', value=number(total))
    artwork = music_image(track.get('image'))
    style = bot.store.db.execute('SELECT value FROM runtime_settings WHERE key=?', (f'fmstyle:{member.id}',)).fetchone()
    if artwork:
        if style and json.loads(style[0]) == 'cover':
            e.set_image(url=artwork)
        else:
            e.set_thumbnail(url=artwork)
    if not playing:
        at = track.get('date', {}).get('uts', '')
        if str(at).isdecimal():
            e.add_field(name='Played', value=f'<t:{at}:R>')
    e.set_footer(text=f'BTD / {username} • Last.fm'[:200])
    return e, profile_url, lastfm_url(track.get('url')), artwork

class MusicPanel(discord.ui.View):
    def __init__(self, ctx, member, profile, track, artwork):
        super().__init__(timeout=120)
        self.ctx, self.member = ctx, member
        self.last_refresh = 0
        self.add_item(discord.ui.Button(label='Last.fm', style=discord.ButtonStyle.link, url=profile))
        if track:
            self.add_item(discord.ui.Button(label='Track', style=discord.ButtonStyle.link, url=track))
        if artwork:
            self.add_item(discord.ui.Button(label='Artwork', style=discord.ButtonStyle.link, url=artwork))
    @discord.ui.button(label='Refresh', style=discord.ButtonStyle.secondary)
    async def refresh_card(self, interaction, button):
        if interaction.user.id != self.ctx.author.id:
            return await interaction.response.send_message('Run your own fm command to refresh a card.', ephemeral=True)
        if time.monotonic()-self.last_refresh < 5:
            return await interaction.response.send_message('Give it a few seconds.', ephemeral=True)
        self.last_refresh = time.monotonic()
        await interaction.response.defer()
        try:
            e, profile, track, artwork = await nowplaying_card(self.member)
            for child in list(self.children):
                if isinstance(child, discord.ui.Button) and child.url:
                    self.remove_item(child)
            self.add_item(discord.ui.Button(label='Last.fm', style=discord.ButtonStyle.link, url=profile))
            if track:
                self.add_item(discord.ui.Button(label='Track', style=discord.ButtonStyle.link, url=track))
            if artwork:
                self.add_item(discord.ui.Button(label='Artwork', style=discord.ButtonStyle.link, url=artwork))
            await interaction.edit_original_response(embed=e, view=self)
        except commands.BadArgument as error:
            await interaction.followup.send(str(error), ephemeral=True)

@bot.hybrid_group(invoke_without_command=True, aliases=['lastfm'], fallback='now')
@commands.cooldown(1, 5, commands.BucketType.user)
async def fm(ctx, member: discord.Member = None):
    member = member or ctx.author
    e, profile, track, artwork = await nowplaying_card(member)
    await ctx.send(embed=e, view=MusicPanel(ctx, member, profile, track, artwork))

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
    joins = hit_window(('joins', member.guild.id), 20)
    if len(joins) == 10:
        await audit(member.guild, 'Join surge', '10 joins in 20 seconds. Review recent members; no automatic bans.')
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
    if msg and msg.content and not msg.content.startswith(str(bot.command_prefix)):
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

@bot.hybrid_command(description='Show the latest track from a linked Last.fm account.')
@commands.cooldown(1, 5, commands.BucketType.user)
async def np(ctx, member: discord.Member = None):
    await fm.callback(ctx, member)

@fm.command(name='style', aliases=['layout'])
async def fm_style(ctx, style: str = 'compact'):
    style = style.lower()
    if style not in ('compact', 'cover'):
        raise commands.BadArgument('Choose compact or cover.')
    save_runtime(f'fmstyle:{ctx.author.id}', style)
    await ctx.send(f'Last.fm card style: {style}.', delete_after=8)

@fm.command(name='profile', aliases=['stats'])
async def fm_profile(ctx, member: discord.Member = None):
    member = member or ctx.author
    username = fm_username(member)
    user = (await lastfm_request('user.getInfo', username)).get('user', {})
    e = personal_embed(member.id, 'music / ' + username[:100])
    e.url = 'https://www.last.fm/user/' + urlquote(username, safe='')
    e.set_author(name=member.display_name[:256], icon_url=member.display_avatar.url)
    for title, key in [('Scrobbles', 'playcount'), ('Artists', 'artist_count'), ('Tracks', 'track_count'), ('Albums', 'album_count')]:
        if user.get(key) is not None:
            e.add_field(name=title, value=number(user[key]))
    registered = str(user.get('registered', {}).get('unixtime', ''))
    if registered.isdecimal():
        e.add_field(name='Since', value=f'<t:{registered}:D>')
    image = music_image(user.get('image'))
    if image:
        e.set_thumbnail(url=image)
    await ctx.send(embed=e)

@fm.command(name='loved', aliases=['favorites', 'favs'])
async def fm_loved(ctx, member: discord.Member = None):
    member = member or ctx.author
    data = await lastfm_request('user.getLovedTracks', fm_username(member), limit='10')
    rows = music_rows(data, 'lovedtracks', 'track')
    lines = [f'{i}. **{discord.utils.escape_markdown(row.get("name", "Unknown")[:100])}** · {discord.utils.escape_markdown(row.get("artist", {}).get("name", "Unknown")[:100])}' for i, row in enumerate(rows[:10],1)]
    await ctx.send(embed=personal_embed(member.id, 'loved tracks / ' + member.display_name[:100], '\n'.join(lines) or 'No loved tracks yet.'))

@fm.command(name='taste', aliases=['compare', 'match'])
@commands.cooldown(1, 10, commands.BucketType.user)
async def fm_taste(ctx, member: discord.Member, period: str = 'month'):
    periods = music_periods()
    if period not in periods:
        raise commands.BadArgument('Choose week, month, quarter, halfyear, year, or all.')
    accounts = [fm_username(ctx.author), fm_username(member)]
    data = await asyncio.gather(*(lastfm_request('user.getTopArtists', account, period=periods[period], limit='50') for account in accounts))
    lists = [{row.get('name', '').casefold():row.get('name', '') for row in music_rows(result,'topartists','artist') if row.get('name')} for result in data]
    shared = [key for key in lists[0] if key in lists[1]]
    denominator = min(len(lists[0]),len(lists[1]))
    score = round(len(shared)/denominator*100) if denominator else 0
    e = embed('taste / ' + period, f'**{score}% top-artist overlap**\n{ctx.author.display_name} × {member.display_name}')
    e.add_field(name=f'{len(shared)} shared artists', value='\n'.join(discord.utils.escape_markdown(lists[0][key])[:100] for key in shared[:15]) or 'No overlap in this sample.', inline=False)
    e.set_footer(text='Overlap among up to 50 top artists each; a sample, not a complete compatibility score.')
    await ctx.send(embed=e)

@fm.command(name='artist', aliases=['a'])
async def fm_artist(ctx, *, artist: str = None):
    username = fm_username(ctx.author)
    if artist is None:
        recent_data = await lastfm_request('user.getRecentTracks', username, limit='1')
        rows = music_rows(recent_data,'recenttracks','track')
        if not rows:
            raise commands.BadArgument('No recent track; provide an artist name.')
        artist = rows[0].get('artist',{}).get('#text') or rows[0].get('artist',{}).get('name')
    if not artist or len(artist)>200:
        raise commands.BadArgument('Use an artist name up to 200 characters.')
    info = (await lastfm_request('artist.getInfo', username, artist=artist, username=username, autocorrect='1')).get('artist',{})
    e = personal_embed(ctx.author.id,'artist / '+str(info.get('name',artist))[:200])
    stats=info.get('stats',{})
    for title,key in [('Your plays','userplaycount'),('Listeners','listeners'),('Global plays','playcount')]:
        e.add_field(name=title,value=number(stats.get(key,0)))
    tags=music_rows(info,'tags','tag')
    if tags:
        e.add_field(name='Tags',value=' · '.join(str(t.get('name',''))[:60] for t in tags[:5]),inline=False)
    url=lastfm_url(info.get('url'))
    if url:
        e.url=url
    await ctx.send(embed=e)

@fm.command(name='track', aliases=['t', 'song'])
async def fm_track(ctx, *, query: str = None):
    username=fm_username(ctx.author)
    if query:
        if '|' not in query or len(query)>400:
            raise commands.BadArgument('Use fm track artist | song, or fm track for your latest track.')
        artist,name=[x.strip() for x in query.split('|',1)]
    else:
        rows=music_rows(await lastfm_request('user.getRecentTracks',username,limit='1'),'recenttracks','track')
        if not rows:
            raise commands.BadArgument('No recent tracks found.')
        artist=rows[0].get('artist',{}).get('#text') or rows[0].get('artist',{}).get('name')
        name=rows[0].get('name')
    if not artist or not name:
        raise commands.BadArgument('Provide both an artist and a song.')
    info=(await lastfm_request('track.getInfo',username,username=username,artist=artist,track=name,autocorrect='1')).get('track',{})
    e=personal_embed(ctx.author.id,str(info.get('name',name))[:256],discord.utils.escape_markdown(artist[:200]))
    for title,key in [('Your plays','userplaycount'),('Listeners','listeners'),('Global plays','playcount')]:
        e.add_field(name=title,value=number(info.get(key,0)))
    album=info.get('album',{})
    artwork=music_image(album.get('image'))
    if artwork:
        e.set_thumbnail(url=artwork)
    if str(info.get('userloved','0'))=='1':
        e.set_footer(text='♡ loved on Last.fm')
    url=lastfm_url(info.get('url'))
    if url:
        e.url=url
    await ctx.send(embed=e)

@fm.command(name='album', aliases=['al'])
async def fm_album(ctx, *, query: str = None):
    username = fm_username(ctx.author)
    if query:
        if '|' not in query or len(query) > 400:
            raise commands.BadArgument('Use fm album artist | album, or fm album for your latest album.')
        artist, album = [x.strip() for x in query.split('|', 1)]
    else:
        rows = music_rows(await lastfm_request('user.getRecentTracks', username, limit='1'), 'recenttracks', 'track')
        if not rows:
            raise commands.BadArgument('No recent tracks found.')
        artist = rows[0].get('artist', {}).get('#text') or rows[0].get('artist', {}).get('name')
        album = rows[0].get('album', {}).get('#text')
    if not artist or not album:
        raise commands.BadArgument('Provide both an artist and an album.')
    info = (await lastfm_request('album.getInfo', username, username=username, artist=artist, album=album, autocorrect='1')).get('album', {})
    e = personal_embed(ctx.author.id, str(info.get('name', album))[:256], discord.utils.escape_markdown(artist[:200]))
    for title, key in [('Your album plays', 'userplaycount'), ('Listeners', 'listeners'), ('Global plays', 'playcount')]:
        e.add_field(name=title, value=number(info.get(key)))
    artwork = music_image(info.get('image'))
    if artwork:
        e.set_image(url=artwork)
    tracks = music_rows(info, 'tracks', 'track')
    if tracks:
        e.add_field(name='Tracklist', value='\n'.join(f'{i}. {discord.utils.escape_markdown(row.get("name", "Unknown")[:65])}' for i, row in enumerate(tracks[:10], 1)), inline=False)
    url = lastfm_url(info.get('url'))
    if url:
        e.url = url
    await ctx.send(embed=e)

def music_periods():
    return {'week':'7day','month':'1month','quarter':'3month','halfyear':'6month','year':'12month','all':'overall'}

async def show_top(ctx, member, period, kind):
    periods = music_periods()
    if period not in periods:
        raise commands.BadArgument('Choose week, month, quarter, halfyear, year, or all.')
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
        save_runtime('presence', {'mode': 'single', 'texts': cycle_texts[:1]})
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
    save_runtime('presence', {'mode': 'cycle', 'texts': choices})
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
    periods = music_periods()
    if period not in periods:
        raise commands.BadArgument('Choose week, month, quarter, halfyear, year, or all.')
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

def cleanup_settings(guild_id):
    row = bot.store.db.execute('SELECT seconds, requests FROM cleanup WHERE guild=?', (guild_id,)).fetchone()
    return row or (8, 1)

async def remove_request(ctx):
    if not ctx.interaction and ctx.guild and ctx.channel.permissions_for(ctx.me).manage_messages:
        try:
            await ctx.message.delete()
        except discord.HTTPException:
            pass

@bot.after_invoke
async def cleanup_request(ctx):
    if not ctx.command_failed and ctx.command and ctx.command.name in TRANSIENT and cleanup_settings(ctx.guild.id)[1]:
        await remove_request(ctx)

@bot.hybrid_command(description='Set confirmation lifetime (3–120 seconds) and command cleanup.')
@commands.has_permissions(manage_guild=True)
async def cleanup(ctx, seconds: int = 8, requests: bool = True):
    if not 3 <= seconds <= 120:
        raise commands.BadArgument('Choose 3–120 seconds.')
    with bot.store.db:
        bot.store.db.execute('INSERT OR REPLACE INTO cleanup VALUES (?,?,?)', (ctx.guild.id, seconds, int(requests)))
    await ctx.send(f'Confirmations delete after {seconds}s. Command cleanup: {"on" if requests else "off"}.')

@bot.hybrid_command(name='delete', aliases=['del', 'delafter'], description='Delete a message in this channel after 0–86400 seconds.')
@commands.has_permissions(manage_messages=True)
@commands.bot_has_permissions(manage_messages=True, read_message_history=True)
async def delete_message(ctx, message_id: str, after: int = 0):
    if not message_id.isdecimal() or not 0 < int(message_id) < 2**64:
        raise commands.BadArgument('Enter a numeric message ID from this channel.')
    if not 0 <= after <= 86400:
        raise commands.BadArgument('Choose 0–86400 seconds (up to one day).')
    message = await ctx.channel.fetch_message(int(message_id))
    if message.pinned:
        raise commands.BadArgument('Unpin that message before deleting it.')
    if after == 0:
        await message.delete()
        with bot.store.db:
            bot.store.db.execute('DELETE FROM scheduled_deletes WHERE message=?', (message.id,))
    else:
        with bot.store.db:
            bot.store.db.execute('INSERT OR REPLACE INTO scheduled_deletes VALUES (?,?,?,?)',
                                 (ctx.guild.id, ctx.channel.id, message.id, time.time() + after))
    await ctx.send('Message deleted.' if not after else f'Message scheduled for deletion in {after}s.')
    await audit(ctx.guild, 'Message deletion requested', f'{ctx.author} • {message.id} • delay {after}s')

@tasks.loop(seconds=2)
async def deletion_worker():
    rows = bot.store.db.execute('SELECT guild, channel, message FROM scheduled_deletes WHERE due<=? LIMIT 20', (time.time(),)).fetchall()
    for guild_id, channel_id, message_id in rows:
        channel = bot.get_channel(channel_id)
        if channel is None:
            try:
                channel = await bot.fetch_channel(channel_id)
            except (discord.NotFound, discord.Forbidden):
                pass
            except discord.HTTPException:
                continue
        if channel is None:
            with bot.store.db:
                bot.store.db.execute('DELETE FROM scheduled_deletes WHERE message=?', (message_id,))
            continue
        try:
            msg = await channel.fetch_message(message_id)
            if not msg.pinned:
                await msg.delete()
        except discord.NotFound:
            pass
        except discord.Forbidden:
            await audit(channel.guild, 'Scheduled deletion blocked', f'Message {message_id}: check Manage Messages.')
        except discord.HTTPException:
            continue
        with bot.store.db:
            bot.store.db.execute('DELETE FROM scheduled_deletes WHERE message=?', (message_id,))

@deletion_worker.before_loop
async def deletion_ready():
    await bot.wait_until_ready()

def save_runtime(key, value):
    with bot.store.db:
        bot.store.db.execute('INSERT OR REPLACE INTO runtime_settings VALUES (?,?)', (key, json.dumps(value)))

async def restore_presence():
    global cycle_texts, cycle_index
    row = bot.store.db.execute('SELECT value FROM runtime_settings WHERE key=?', ('presence',)).fetchone()
    if not row:
        return
    try:
        saved = json.loads(row[0])
        texts = saved['texts']
        if not isinstance(texts, list) or not all(isinstance(x, str) and len(x) <= 128 for x in texts):
            return
        if saved['mode'] == 'cycle' and len(texts) >= 2:
            cycle_texts, cycle_index = texts, 0
            if not status_cycle.is_running():
                status_cycle.start()
        else:
            kind = {'playing':discord.ActivityType.playing,'listening':discord.ActivityType.listening,'watching':discord.ActivityType.watching}.get(saved.get('kind'),discord.ActivityType.playing)
            await bot.change_presence(activity=discord.Activity(type=kind,name=texts[0]) if texts else None)
    except (ValueError, KeyError, TypeError):
        log.warning('Could not restore saved presence')

@bot.hybrid_command(description='Warn a member and save a numbered moderation case.')
@commands.has_permissions(moderate_members=True)
async def warn(ctx, member: discord.Member, *, reason: str):
    target_check(ctx, member)
    if not 1 <= len(reason) <= 1000:
        raise commands.BadArgument('Keep the reason between 1 and 1,000 characters.')
    with bot.store.db:
        case = bot.store.db.execute('INSERT INTO warnings (guild, member, moderator, reason, at) VALUES (?,?,?,?,?)',
            (ctx.guild.id, member.id, ctx.author.id, reason, time.time())).lastrowid
    await ctx.send(embed=embed(f'warning / case {case}', f'{member.mention}\n{discord.utils.escape_markdown(reason)}'))
    await audit(ctx.guild, f'Warning / case {case}', f'{member} • by {ctx.author}\n{reason}')

@bot.hybrid_command(description='View a member’s last 10 active warnings.')
@commands.has_permissions(moderate_members=True)
async def warnings(ctx, member: discord.Member):
    rows = bot.store.db.execute('SELECT id, moderator, reason, at FROM warnings WHERE guild=? AND member=? AND active=1 ORDER BY id DESC LIMIT 10', (ctx.guild.id, member.id)).fetchall()
    total = bot.store.db.execute('SELECT COUNT(*) FROM warnings WHERE guild=? AND member=? AND active=1', (ctx.guild.id, member.id)).fetchone()[0]
    e = embed(f'warnings / {member.display_name}'[:256], f'{total} active cases')
    for case, moderator, reason, at in rows:
        e.add_field(name=f'Case {case}', value=f'<@{moderator}> · <t:{int(at)}:d>\n{discord.utils.escape_markdown(reason)[:450]}', inline=False)
    await ctx.send(embed=e, ephemeral=bool(ctx.interaction), delete_after=60)

@bot.hybrid_command(description='Revoke one warning by case ID; retain its audit record.')
@commands.has_permissions(moderate_members=True)
async def unwarn(ctx, case_id: int):
    row = bot.store.db.execute('SELECT member, active FROM warnings WHERE guild=? AND id=?', (ctx.guild.id, case_id)).fetchone()
    if not row or not row[1]:
        raise commands.BadArgument('No active warning with that case ID in this server.')
    member = ctx.guild.get_member(row[0])
    if member:
        target_check(ctx, member)
    with bot.store.db:
        bot.store.db.execute('UPDATE warnings SET active=0 WHERE guild=? AND id=?', (ctx.guild.id, case_id))
    await ctx.send(f'Warning {case_id} revoked.')
    await audit(ctx.guild, 'Warning revoked', f'Case {case_id} • by {ctx.author}')

@bot.hybrid_command(description='Set this channel’s slowmode; 0 disables it.')
@commands.has_permissions(manage_channels=True)
@commands.bot_has_permissions(manage_channels=True)
async def slowmode(ctx, seconds: int = 0):
    if not 0 <= seconds <= 21600:
        raise commands.BadArgument('Choose 0–21600 seconds.')
    await ctx.channel.edit(slowmode_delay=seconds, reason=f'BTD slowmode by {ctx.author}')
    await ctx.send(f'Slowmode: {seconds}s.')
    await audit(ctx.guild, 'Slowmode changed', f'{ctx.channel.mention} • {seconds}s • by {ctx.author}')

@bot.hybrid_command(description='Lock this text channel for @everyone, saving the previous overwrite.')
@commands.has_permissions(manage_roles=True)
@commands.bot_has_permissions(manage_roles=True)
async def lock(ctx):
    if not isinstance(ctx.channel, discord.TextChannel):
        raise commands.BadArgument('Use this in a regular text channel.')
    role = ctx.guild.default_role
    overwrite = ctx.channel.overwrites_for(role)
    if bot.store.db.execute('SELECT 1 FROM channel_locks WHERE channel=?', (ctx.channel.id,)).fetchone():
        raise commands.BadArgument('This channel is already locked by BTD. Use unlock.')
    previous = overwrite.send_messages
    with bot.store.db:
        bot.store.db.execute('INSERT INTO channel_locks VALUES (?,?,?)', (ctx.guild.id, ctx.channel.id, None if previous is None else int(previous)))
    overwrite.send_messages = False
    try:
        await ctx.channel.set_permissions(role, overwrite=overwrite, reason=f'BTD lock by {ctx.author}')
    except discord.HTTPException:
        with bot.store.db:
            bot.store.db.execute('DELETE FROM channel_locks WHERE channel=?', (ctx.channel.id,))
        raise
    await ctx.send('Channel locked for @everyone. Explicit role overrides and administrators may still send.')
    await audit(ctx.guild, 'Channel locked', f'{ctx.channel.mention} • by {ctx.author}')

@bot.hybrid_command(description='Restore the @everyone send permission saved by lock.')
@commands.has_permissions(manage_roles=True)
@commands.bot_has_permissions(manage_roles=True)
async def unlock(ctx):
    row = bot.store.db.execute('SELECT previous FROM channel_locks WHERE guild=? AND channel=?', (ctx.guild.id, ctx.channel.id)).fetchone()
    if row is None:
        raise commands.BadArgument('No BTD lock is saved for this channel.')
    overwrite = ctx.channel.overwrites_for(ctx.guild.default_role)
    overwrite.send_messages = None if row[0] is None else bool(row[0])
    await ctx.channel.set_permissions(ctx.guild.default_role, overwrite=overwrite if not overwrite.is_empty() else None, reason=f'BTD unlock by {ctx.author}')
    with bot.store.db:
        bot.store.db.execute('DELETE FROM channel_locks WHERE channel=?', (ctx.channel.id,))
    await ctx.send('Previous channel send permission restored.')
    await audit(ctx.guild, 'Channel unlocked', f'{ctx.channel.mention} • by {ctx.author}')

@bot.hybrid_command(description='List the next 10 pending message deletions in this channel.')
@commands.has_permissions(manage_messages=True)
async def pendingdeletes(ctx):
    rows = bot.store.db.execute('SELECT message, due FROM scheduled_deletes WHERE guild=? AND channel=? ORDER BY due LIMIT 10', (ctx.guild.id, ctx.channel.id)).fetchall()
    await ctx.send(embed=embed('scheduled deletions', '\n'.join(f'`{message}` · <t:{int(due)}:R>' for message, due in rows) or 'None scheduled.'), ephemeral=bool(ctx.interaction), delete_after=30)

@bot.hybrid_command(description='Cancel a scheduled deletion in this channel.')
@commands.has_permissions(manage_messages=True)
async def canceldelete(ctx, message_id: str):
    if not message_id.isdecimal() or not 0 < int(message_id) < 2**64:
        raise commands.BadArgument('Enter a numeric Discord message ID.')
    with bot.store.db:
        result = bot.store.db.execute('DELETE FROM scheduled_deletes WHERE guild=? AND channel=? AND message=?', (ctx.guild.id, ctx.channel.id, int(message_id)))
    if not result.rowcount:
        raise commands.BadArgument('No deletion scheduled for that message here.')
    await ctx.send('Scheduled deletion canceled.')
    await audit(ctx.guild, 'Scheduled deletion canceled', f'{message_id} • by {ctx.author}')

@bot.hybrid_command(description='Remove bot messages from the last 1–100 messages; keep pins.')
@commands.has_permissions(manage_messages=True)
@commands.bot_has_permissions(manage_messages=True, read_message_history=True)
async def cleanbots(ctx, amount: int = 25):
    await purge.callback(ctx, amount, None, True)

@bot.hybrid_command(description='Show this channel’s settings and permissions.')
async def channelinfo(ctx):
    channel = ctx.channel
    e = embed('channel / ' + channel.name[:240])
    e.add_field(name='ID', value=str(channel.id))
    e.add_field(name='Created', value=discord.utils.format_dt(channel.created_at, 'D'))
    e.add_field(name='Slowmode', value=f'{getattr(channel, "slowmode_delay", 0)}s')
    e.add_field(name='BTD lock', value='On' if bot.store.db.execute('SELECT 1 FROM channel_locks WHERE channel=?', (channel.id,)).fetchone() else 'Off')
    await ctx.send(embed=e)

@bot.hybrid_command(description='Show bot uptime, latency and command count.')
async def botinfo(ctx):
    e = embed('BTD / system')
    e.add_field(name='Uptime', value=f'{int((time.monotonic()-bot.started_at)//60)} minutes')
    e.add_field(name='Latency', value=f'{round(bot.latency*1000)} ms')
    e.add_field(name='Commands', value=str(len(list(bot.walk_commands()))))
    e.add_field(name='Library', value='discord.py ' + discord.__version__)
    await ctx.send(embed=e)

SECURITY_DEFAULTS = {'antinuke': False, 'spam': True, 'mentions': True, 'invites': False, 'links': False, 'limit': 5}
security_windows = OrderedDict()
security_processed = OrderedDict()
security_busy = set()
INVITE_PATTERN = re.compile(r'(?:discord\.gg/|discord(?:app)?\.com/invite/)', re.I)
LINK_PATTERN = re.compile(r'https?://|www\.', re.I)

def security_settings(guild_id):
    row = bot.store.db.execute('SELECT value FROM runtime_settings WHERE key=?', (f'security:{guild_id}',)).fetchone()
    try:
        return {**SECURITY_DEFAULTS, **(json.loads(row[0]) if row else {})}
    except (ValueError, TypeError):
        return dict(SECURITY_DEFAULTS)

def trusted(guild, user_id):
    return user_id in (guild.owner_id, bot.user.id) or bool(bot.store.db.execute('SELECT 1 FROM security_trust WHERE guild=? AND user=?', (guild.id, user_id)).fetchone())

def seen_once(key):
    if key in security_processed:
        return False
    security_processed[key] = time.monotonic()
    while len(security_processed) > 5000:
        security_processed.popitem(last=False)
    return True

def hit_window(key, seconds, value=None):
    now = time.monotonic()
    history = [(at, item) for at, item in security_windows.get(key, []) if now-at < seconds]
    history.append((now, value))
    security_windows[key] = history[-100:]
    security_windows.move_to_end(key)
    while len(security_windows) > 5000:
        security_windows.popitem(last=False)
    return history

@bot.event
async def on_message(message):
    if message.author.bot or not seen_once(('message', message.id)):
        return
    if not message.guild:
        await bot.process_commands(message)
        return
    settings = security_settings(message.guild.id)
    member = message.author
    if isinstance(member, discord.Member) and not trusted(message.guild, member.id) and not member.guild_permissions.manage_messages:
        history = hit_window(('chat', message.guild.id, member.id), 8, message.content.casefold().strip())
        repeat = sum(text == history[-1][1] for _, text in history) if message.content.strip() else 0
        reason = None
        if settings['mentions'] and (len(set(m.id for m in message.mentions)) >= 8 or message.mention_everyone):
            reason = 'mass mentions'
        elif settings['spam'] and (len(history) >= 10 or repeat >= 5):
            reason = 'message spam'
        elif settings['invites'] and INVITE_PATTERN.search(message.content):
            reason = 'invite filter'
        elif settings['links'] and LINK_PATTERN.search(message.content):
            reason = 'link filter'
        if reason:
            try:
                await message.delete()
            except discord.HTTPException:
                pass
            # Avoid piling up bot replies when someone keeps spamming.
            notices = hit_window(('notice', message.guild.id, member.id), 20)
            if len(notices) == 1:
                try:
                    await message.channel.send(f'{member.display_name[:80]}: {reason}. Ease up a little.', delete_after=8)
                except discord.HTTPException:
                    pass
                await audit(message.guild, 'Chat protection', f'{member} • {reason} • {message.channel.mention}')
            if reason in ('message spam', 'mass mentions') and len(notices) >= 3 and message.guild.me.guild_permissions.moderate_members and member.top_role < message.guild.me.top_role and not member.guild_permissions.administrator:
                try:
                    await member.timeout(timedelta(minutes=2), reason=f'BTD: repeated {reason}')
                except discord.HTTPException:
                    pass
            return
    await bot.process_commands(message)

async def security_owner(ctx):
    if not ctx.guild or ctx.author.id != ctx.guild.owner_id:
        raise commands.BadArgument('Only the server owner can change security settings.')
    return True

@bot.hybrid_command(description='Security settings: on/off, spam, mentions, invites, links, or limit.')
@commands.check(security_owner)
async def security(ctx, feature: str = 'show', value: str = None):
    settings = security_settings(ctx.guild.id)
    feature = feature.lower()
    if feature in ('on', 'off'):
        settings['antinuke'] = feature == 'on'
    elif feature in settings:
        if value is None:
            raise commands.BadArgument(f'Use security {feature} on/off' if feature != 'limit' else 'Use security limit 5')
        if feature == 'limit':
            if not value.isdecimal() or not 3 <= int(value) <= 20:
                raise commands.BadArgument('Choose an anti-nuke limit from 3 to 20 actions per 15 seconds.')
            settings[feature] = int(value)
        else:
            if value.lower() not in ('on', 'off', 'true', 'false'):
                raise commands.BadArgument('Use on or off.')
            settings[feature] = value.lower() in ('on', 'true')
    elif feature != 'show':
        raise commands.BadArgument('Use on, off, spam, mentions, invites, links, limit, or show.')
    if settings['antinuke']:
        perms = ctx.guild.me.guild_permissions
        if not perms.view_audit_log or not perms.manage_roles:
            raise commands.BadArgument('Anti-nuke needs View Audit Log and Manage Roles. Put BTD above your staff roles.')
    save_runtime(f'security:{ctx.guild.id}', settings)
    e = embed('BTD / security', '\n'.join(f'**{key}**: {"on" if val is True else "off" if val is False else val}' for key, val in settings.items()))
    e.set_footer(text='Anti-nuke limit uses a 15-second window. Owners and trusted users are exempt.')
    await ctx.send(embed=e, ephemeral=bool(ctx.interaction), delete_after=60)

@bot.hybrid_command(description='Server owner: trust a user for chat and anti-nuke protection.')
@commands.check(security_owner)
async def trust(ctx, member: discord.Member):
    with bot.store.db:
        bot.store.db.execute('INSERT OR IGNORE INTO security_trust VALUES (?,?)', (ctx.guild.id, member.id))
    await ctx.send(f'{member} is trusted.', ephemeral=bool(ctx.interaction), delete_after=8)
    await audit(ctx.guild, 'Security trust added', f'{member} • by {ctx.author}')

@bot.hybrid_command(description='Server owner: remove a user from the trusted list.')
@commands.check(security_owner)
async def untrust(ctx, member: discord.Member):
    with bot.store.db:
        bot.store.db.execute('DELETE FROM security_trust WHERE guild=? AND user=?', (ctx.guild.id, member.id))
    await ctx.send(f'Trust removed for {member}.', ephemeral=bool(ctx.interaction), delete_after=8)
    await audit(ctx.guild, 'Security trust removed', f'{member} • by {ctx.author}')

@bot.hybrid_command(description='List explicitly trusted security users.')
@commands.check(security_owner)
async def trustedlist(ctx):
    rows = bot.store.db.execute('SELECT user FROM security_trust WHERE guild=? LIMIT 50', (ctx.guild.id,)).fetchall()
    await ctx.send(embed=embed('trusted users', '\n'.join(f'<@{row[0]}>' for row in rows) or 'Only the server owner and BTD.'), ephemeral=bool(ctx.interaction), delete_after=60)

@bot.event
async def on_audit_log_entry_create(entry):
    guild = entry.guild
    if not seen_once(('audit', entry.id)) or not entry.user_id or trusted(guild, entry.user_id):
        return
    settings = security_settings(guild.id)
    if not settings['antinuke']:
        return
    watched = {discord.AuditLogAction.channel_delete, discord.AuditLogAction.role_delete,
               discord.AuditLogAction.ban, discord.AuditLogAction.kick,
               discord.AuditLogAction.webhook_create, discord.AuditLogAction.channel_create,
               discord.AuditLogAction.role_create, discord.AuditLogAction.bot_add}
    dangerous_grant = entry.action == discord.AuditLogAction.role_update and getattr(entry.after, 'permissions', None) is not None and getattr(entry.before, 'permissions', None) is not None and dangerous_role(SimpleRole(entry.after.permissions)) and not dangerous_role(SimpleRole(entry.before.permissions))
    if entry.action not in watched and not dangerous_grant:
        return
    history = hit_window(('nuke', guild.id, entry.user_id), 15)
    if len(history) < settings['limit'] and not dangerous_grant:
        return
    key = (guild.id, entry.user_id)
    if key in security_busy:
        return
    security_busy.add(key)
    try:
        member = guild.get_member(entry.user_id)
        if member is None:
            try:
                member = await guild.fetch_member(entry.user_id)
            except discord.HTTPException:
                return
        if member.top_role >= guild.me.top_role or not guild.me.guild_permissions.manage_roles:
            await audit(guild, 'Anti-nuke blocked by hierarchy', f'{member} • move BTD above staff roles')
            return
        roles = [role for role in member.roles if not role.is_default() and not role.managed and role < guild.me.top_role and dangerous_role(role)]
        if roles:
            with bot.store.db:
                bot.store.db.execute('INSERT OR IGNORE INTO quarantine VALUES (?,?,?)', (guild.id, member.id, json.dumps([r.id for r in roles])))
            await member.remove_roles(*roles, reason='BTD anti-nuke: dangerous staff activity')
            if dangerous_grant and isinstance(entry.target, discord.Role) and entry.target < guild.me.top_role and not entry.target.managed:
                await entry.target.edit(permissions=entry.before.permissions, reason='BTD anti-nuke: undo dangerous permission grant')
            await audit(guild, 'Anti-nuke / staff roles removed', f'{member} ({member.id}) • {entry.action.name} • {len(history)} actions in 15s\nServer owner: review, then use release @member if appropriate.')
    except discord.HTTPException:
        await audit(guild, 'Anti-nuke action failed', f'User {entry.user_id} • check permissions and hierarchy')
    finally:
        security_busy.discard(key)

@bot.event
async def on_member_update(before, after):
    if trusted(after.guild, after.id) or not security_settings(after.guild.id)['antinuke']:
        return
    if not bot.store.db.execute('SELECT 1 FROM quarantine WHERE guild=? AND member=?', (after.guild.id, after.id)).fetchone():
        return
    roles = [r for r in after.roles if not r.is_default() and not r.managed and dangerous_role(r) and r < after.guild.me.top_role]
    if roles:
        try:
            await after.remove_roles(*roles, reason='BTD anti-nuke quarantine hold')
        except discord.HTTPException:
            await audit(after.guild, 'Quarantine hold failed', f'{after} • check BTD role order')

class SimpleRole:
    def __init__(self, permissions):
        self.permissions = permissions

@bot.hybrid_command(description='Server owner: restore roles removed by anti-nuke after review.')
@commands.check(security_owner)
@commands.bot_has_permissions(manage_roles=True)
async def release(ctx, member: discord.Member):
    row = bot.store.db.execute('SELECT roles FROM quarantine WHERE guild=? AND member=?', (ctx.guild.id, member.id)).fetchone()
    if not row:
        raise commands.BadArgument('No saved anti-nuke roles for that member.')
    roles = [ctx.guild.get_role(role_id) for role_id in json.loads(row[0])]
    roles = [r for r in roles if r and not r.managed and not r.is_default()]
    if any(r >= ctx.me.top_role for r in roles):
        raise commands.BadArgument('Move BTD above the saved roles first.')
    await member.add_roles(*roles, reason=f'BTD security release by {ctx.author}')
    with bot.store.db:
        bot.store.db.execute('DELETE FROM quarantine WHERE guild=? AND member=?', (ctx.guild.id, member.id))
    security_windows.pop(('nuke', ctx.guild.id, member.id), None)
    await ctx.send(f'Restored {len(roles)} saved roles for {member}.', ephemeral=bool(ctx.interaction), delete_after=8)
    await audit(ctx.guild, 'Anti-nuke roles restored', f'{member} • by {ctx.author}')

EXAMPLES = {
    'fm style': 'fm style compact · fm style cover',
    'fm taste': 'fm taste @friend month', 'fm track': 'fm track · fm track Drake | Passionfruit',
    'fm artist': 'fm artist · fm artist Drake', 'fm album': 'fm album · fm album Drake | Views',
    'activity': 'activity listening your playlist', 'check': 'check',
    'purge': 'purge 25 · purge 50 @member · purge 50 @member true',
    'delete': 'delafter MESSAGE_ID 30', 'cleanup': 'cleanup 8 true',
    'warn': 'warn @member repeated spam', 'warnings': 'warnings @member',
    'unwarn': 'unwarn 12', 'slowmode': 'slowmode 10',
    'statuscycle': 'statuscycle discord.gg/banish | Sayo is Dada',
    'fm set': 'fm set username', 'roleall': 'roleall @role · roleall @role true',
    'embed': 'embed "my title" description goes here',
}

CATEGORIES = {
    'General': ['btd', 'tp', 'ping', 'serverinfo', 'userinfo', 'avatar', 'banner', 'servericon', 'membercount', 'roleinfo', 'channelinfo', 'botinfo'],
    'Music': ['fm', 'fm set', 'fm remove', 'fm style', 'fm profile', 'fm loved', 'np'],
    'Music stats': ['fm taste', 'fm artist', 'fm track', 'fm album', 'toptracks', 'topartists', 'topalbums', 'recent'],
    'Community': ['rep', 'reps', 'leaderboard', 'embedcolor', 'embed', 'quote', 'dadaman', 'sam'],
    'Fun': ['coinflip', 'roll', 'choose', '8ball'],
    'Moderation': ['purge', 'delete', 'cleanup', 'kick', 'ban', 'unban', 'timeout', 'untimeout', 'nick', 'say', 'announce', 'snipe', 'editsnipe', 'clearsnipe', 'snipepublic', 'cleanbots'],
    'Cases': ['warn', 'warnings', 'unwarn'],
    'Channels': ['slowmode', 'lock', 'unlock', 'pendingdeletes', 'canceldelete'],
    'Roles': ['autorole', 'autoroleoff', 'roleall'],
    'Security': ['security', 'trust', 'untrust', 'trustedlist', 'release'],
    'Owner': ['check', 'ownerstats', 'activity', 'synccommands', 'fmtest', 'clearcache', 'logchannel', 'logoff', 'status', 'statuscycle'],
}

async def visible_commands(ctx):
    visible = set()
    for command in bot.walk_commands():
        try:
            if not command.hidden and await command.can_run(ctx):
                visible.add(command.qualified_name)
        except commands.CommandError:
            pass
    return visible

class MenuSelect(discord.ui.Select):
    def __init__(self, categories):
        super().__init__(placeholder='Choose a command category', options=[discord.SelectOption(label=c) for c in categories])
    async def callback(self, interaction):
        await interaction.response.edit_message(embed=self.view.page(self.values[0]))

class HelpPanel(discord.ui.View):
    def __init__(self, ctx, visible, lifetime):
        super().__init__(timeout=lifetime)
        self.ctx, self.visible = ctx, visible
        categories = [c for c, names in CATEGORIES.items() if any(n in visible for n in names)]
        self.add_item(MenuSelect(categories))
    async def interaction_check(self, interaction):
        if interaction.user.id != self.ctx.author.id:
            await interaction.response.send_message('Open your own menu with /help.', ephemeral=True)
            return False
        return True
    def page(self, category=None):
        prefix = str(bot.command_prefix)
        e = embed('BTD / ' + (category or 'commands'),
                  f'Prefix `{prefix}` · slash commands supported\nUse `{prefix}help command` for usage and permissions.')
        if category:
            lines = []
            for name in CATEGORIES[category]:
                if name not in self.visible:
                    continue
                cmd = bot.get_command(name)
                description = cmd.description or cmd.help or 'Use command help for arguments.'
                lines.append(f'`{prefix}{name} {cmd.signature}`\n{description[:70]}')
            e.description += '\n\n' + '\n\n'.join(lines)
        else:
            for name, commands_ in CATEGORIES.items():
                count = sum(c in self.visible for c in commands_)
                if count:
                    e.add_field(name=name, value=f'{count} commands', inline=True)
        e.set_footer(text=f'Only commands you can use • closes after {int(self.timeout)}s')
        return e
    @discord.ui.button(label='Close', style=discord.ButtonStyle.secondary)
    async def close_panel(self, interaction, button):
        await interaction.response.defer()
        await interaction.delete_original_response()
        self.stop()

@bot.hybrid_command(description='Browse commands or get detailed usage for one command.')
@commands.cooldown(1, 3, commands.BucketType.user)
async def help(ctx, *, command: str = None):
    is_owner = await bot.is_owner(ctx.author)
    lifetime = 60 if is_owner else 120
    visible = await visible_commands(ctx)
    if command:
        cmd = bot.get_command(command.strip())
        if not cmd or cmd.qualified_name not in visible:
            raise commands.BadArgument('That command is unavailable to you. Use help to browse.')
        e = embed('BTD / ' + cmd.qualified_name, cmd.description or cmd.help or 'Command usage')
        e.add_field(name='Usage', value=f'`{bot.command_prefix}{cmd.qualified_name} {cmd.signature}`', inline=False)
        if cmd.qualified_name in EXAMPLES:
            e.add_field(name='Examples', value=EXAMPLES[cmd.qualified_name], inline=False)
        if cmd.aliases:
            e.add_field(name='Prefix aliases', value=', '.join(cmd.aliases))
        e.set_footer(text='Arguments in [] are optional • <> are required')
        view = None
    else:
        view = HelpPanel(ctx, visible, lifetime)
        e = view.page()
    if is_owner and not ctx.interaction:
        try:
            await ctx.author.send(embed=e, view=view, delete_after=lifetime)
            await ctx.send(f'Owner menu sent to DMs. Deletes after {lifetime}s.', delete_after=8)
        except discord.Forbidden:
            await ctx.send('Use /help for your private owner menu, or enable DMs.', delete_after=8)
        await remove_request(ctx)
    else:
        await ctx.send(embed=e, view=view, ephemeral=bool(ctx.interaction), delete_after=lifetime)

async def dashboard_embed(ctx):
    e = embed('BTD / control panel', f'{ctx.guild.name} · `{bot.command_prefix}` prefix')
    seconds, requests = cleanup_settings(ctx.guild.id)
    row = bot.store.db.execute('SELECT role FROM autoroles WHERE guild=?', (ctx.guild.id,)).fetchone()
    snipe = bot.store.db.execute('SELECT public FROM snipe_settings WHERE guild=?', (ctx.guild.id,)).fetchone()
    pending = bot.store.db.execute('SELECT COUNT(*) FROM scheduled_deletes WHERE guild=?', (ctx.guild.id,)).fetchone()[0]
    e.add_field(name='Connection', value=f'{round(bot.latency * 1000)} ms · uptime {int((time.monotonic()-bot.started_at)//60)}m')
    e.add_field(name='Cleanup', value=f'Confirmations: {seconds}s\nCommand messages: {"on" if requests else "off"}\nPending deletes: {pending}')
    e.add_field(name='Autorole', value=f'<@&{row[0]}>' if row else 'Off')
    channel = bot.store.channel(ctx.guild.id)
    active_warnings = bot.store.db.execute('SELECT COUNT(*) FROM warnings WHERE guild=? AND active=1', (ctx.guild.id,)).fetchone()[0]
    security_state = security_settings(ctx.guild.id)
    e.add_field(name='Protection', value=f'Anti-nuke: {"on" if security_state["antinuke"] else "off"} · spam: {"on" if security_state["spam"] else "off"} · mentions: {"on" if security_state["mentions"] else "off"}')
    e.add_field(name='Cases', value=f'{active_warnings} active warnings')
    e.add_field(name='Logging', value=f'<#{channel}>' if channel else 'Off')
    e.add_field(name='Snipes', value='Public' if snipe and snipe[0] else 'Staff only')
    e.add_field(name='Last.fm', value='API key configured' if os.getenv('LASTFM_API_KEY', '').strip() else 'API key missing')
    perms = ctx.channel.permissions_for(ctx.me)
    missing = [n.replace('_', ' ') for n in ('send_messages', 'embed_links', 'manage_messages', 'read_message_history', 'manage_roles', 'view_audit_log', 'moderate_members', 'kick_members', 'ban_members') if not getattr(perms, n)]
    e.add_field(name='Permissions in this channel', value=', '.join(missing) if missing else 'All checked permissions available', inline=False)
    e.add_field(name='Configure', value=f'`{bot.command_prefix}cleanup 8 true` · `{bot.command_prefix}autorole @role`\n`{bot.command_prefix}snipepublic false` · `{bot.command_prefix}help command`', inline=False)
    e.set_footer(text='Private panel • refresh for current settings • deletes after 60s')
    return e

class DashboardPanel(discord.ui.View):
    def __init__(self, ctx):
        super().__init__(timeout=60)
        self.ctx = ctx
    async def interaction_check(self, interaction):
        if interaction.user.id != self.ctx.author.id:
            await interaction.response.send_message('Open your own /dashboard.', ephemeral=True)
            return False
        return True
    @discord.ui.button(label='Commands', style=discord.ButtonStyle.secondary)
    async def commands_menu(self, interaction, button):
        visible = await visible_commands(self.ctx)
        view = HelpPanel(self.ctx, visible, 60)
        await interaction.response.send_message(embed=view.page(), view=view, ephemeral=True, delete_after=60)
    @discord.ui.button(label='Protection', style=discord.ButtonStyle.secondary)
    async def protection_menu(self, interaction, button):
        settings = security_settings(self.ctx.guild.id)
        e = embed('BTD / protection', '\n'.join(f'**{key}**: {"on" if val is True else "off" if val is False else val}' for key, val in settings.items()))
        e.add_field(name='Owner controls', value=f'`{bot.command_prefix}an on` · `{bot.command_prefix}sec invites on` · `{bot.command_prefix}trust @user`', inline=False)
        e.set_footer(text='Anti-nuke needs View Audit Log, Manage Roles and BTD above staff roles.')
        await interaction.response.send_message(embed=e, ephemeral=True, delete_after=60)
    @discord.ui.button(label='Pending deletes', style=discord.ButtonStyle.secondary)
    async def deletion_menu(self, interaction, button):
        if not interaction.permissions.manage_messages:
            return await interaction.response.send_message('Manage Messages is required.', ephemeral=True)
        rows = bot.store.db.execute('SELECT message, due FROM scheduled_deletes WHERE guild=? AND channel=? ORDER BY due LIMIT 10', (self.ctx.guild.id, self.ctx.channel.id)).fetchall()
        e = embed('pending deletions / this channel', '\n'.join(f'`{message}` · <t:{int(due)}:R>' for message, due in rows) or 'None scheduled.')
        await interaction.response.send_message(embed=e, ephemeral=True, delete_after=30)
    @discord.ui.button(label='Refresh', style=discord.ButtonStyle.secondary)
    async def refresh(self, interaction, button):
        await interaction.response.edit_message(embed=await dashboard_embed(self.ctx))
    @discord.ui.button(label='Close', style=discord.ButtonStyle.secondary)
    async def close_panel(self, interaction, button):
        await interaction.response.defer()
        await interaction.delete_original_response()
        self.stop()

@bot.hybrid_command(description='Private server settings and bot health dashboard.')
@commands.has_permissions(manage_guild=True)
async def dashboard(ctx):
    e, view = await dashboard_embed(ctx), DashboardPanel(ctx)
    if ctx.interaction:
        await ctx.send(embed=e, view=view, ephemeral=True, delete_after=60)
    else:
        try:
            await ctx.author.send(embed=e, view=view, delete_after=60)
            await ctx.send('Dashboard sent to DMs. Deletes after 60s.', delete_after=8)
        except discord.Forbidden:
            await ctx.send('Use /dashboard for the private panel.', delete_after=8)
        await remove_request(ctx)

@bot.event
async def on_command_error(ctx, error):
    if getattr(ctx, '_btd_error_sent', False):
        return
    ctx._btd_error_sent = True
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
        text = f'Missing `{error.param.name}`. Use `{bot.command_prefix}help {ctx.command.qualified_name}`.'
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
    await ctx.send(embed=embed('BTD / command error', text), delete_after=10, ephemeral=bool(ctx.interaction))

async def health_card(ctx):
    ready = bot.is_ready() and not bot.is_closed()
    database_ok = True
    try:
        bot.store.db.execute('SELECT 1').fetchone()
    except sqlite3.Error:
        database_ok = False
    lastfm_state = '🟡 not configured'
    if os.getenv('LASTFM_API_KEY','').strip():
        try:
            await lastfm_request('user.getInfo', bot.store.username(ctx.author.id) or 'rj')
            lastfm_state = '🟢 responding'
        except commands.BadArgument:
            lastfm_state = '🔴 unavailable'
    state=security_settings(ctx.guild.id)
    e=discord.Embed(title='BTD / system status', description='● ONLINE' if ready else '● RECONNECTING', color=0x173D2B if ready and database_ok else 0xC59D39, timestamp=discord.utils.utcnow())
    if bot.user:
        e.set_thumbnail(url=bot.user.display_avatar.url)
    e.add_field(name='Gateway',value=('🟢 connected' if ready else '🔴 disconnected')+f'\n{round(bot.latency*1000)} ms' if m_finite(bot.latency) else '🟡 reconnecting')
    elapsed=int(time.monotonic()-bot.started_at)
    e.add_field(name='Uptime',value=f'{elapsed//86400}d {(elapsed%86400)//3600}h {(elapsed%3600)//60}m')
    e.add_field(name='Database',value='🟢 healthy' if database_ok else '🔴 unavailable')
    e.add_field(name='Last.fm',value=lastfm_state)
    e.add_field(name='Commands',value=f'{len(list(bot.walk_commands()))} loaded')
    e.add_field(name='Anti-nuke',value='🟢 armed' if state['antinuke'] else '⚪ disabled')
    e.add_field(name='Chat protection',value=('🟢 spam filter' if state['spam'] else '⚪ spam off')+'\n'+('🟢 mention filter' if state['mentions'] else '⚪ mentions off'))
    e.add_field(name='Tasks',value=('🟢 timed deletion worker' if deletion_worker.is_running() else '🟡 worker stopped')+'\n'+('🟢 status cycle' if status_cycle.is_running() else '⚪ static presence'))
    e.set_footer(text='BTD • owner control / public status • snapshot at the time shown')
    return e

def m_finite(value):
    return value == value and value not in (float('inf'),float('-inf'))

class StatusPanel(discord.ui.View):
    def __init__(self,ctx):
        super().__init__(timeout=300)
        self.ctx=ctx
        self.last_refresh=0
    @discord.ui.button(label='Refresh status',style=discord.ButtonStyle.success)
    async def refresh(self,interaction,button):
        if not await bot.is_owner(interaction.user):
            return await interaction.response.send_message('Only the BTD owner can run this check.',ephemeral=True)
        if time.monotonic()-self.last_refresh<10:
            return await interaction.response.send_message('Give it 10 seconds.',ephemeral=True)
        self.last_refresh=time.monotonic()
        await interaction.response.defer()
        await interaction.edit_original_response(embed=await health_card(self.ctx))

@bot.hybrid_command(description='Owner-only command that posts a public BTD system dashboard.')
@commands.is_owner()
@commands.cooldown(1,15,commands.BucketType.user)
async def check(ctx):
    await ctx.send(embed=await health_card(ctx),view=StatusPanel(ctx))
    await remove_request(ctx)

@bot.hybrid_command(description='Owner: private service diagnostics and counts.')
@commands.is_owner()
async def ownerstats(ctx):
    e=embed('BTD / owner diagnostics')
    for title,table in [('Warning cases','warnings'),('Pending deletions','scheduled_deletes'),('Linked profiles','lastfm'),('Quarantines','quarantine')]:
        e.add_field(name=title,value=str(bot.store.db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]))
    e.add_field(name='Music cache',value=f'{len(fm_cache)} entries · {len(fm_pending)} requests in flight')
    e.add_field(name='Runtime',value=f'discord.py {discord.__version__}')
    await private_owner_send(ctx,e)

async def private_owner_send(ctx,e):
    if ctx.interaction:
        await ctx.send(embed=e,ephemeral=True,delete_after=60)
    else:
        try:
            await ctx.author.send(embed=e,delete_after=60)
            await ctx.send('Owner panel sent to DMs. Deletes after 60s.',delete_after=8)
        except discord.Forbidden:
            await ctx.send('Use the slash command for a private panel.',delete_after=8)
        await remove_request(ctx)

@bot.hybrid_command(description='Owner: set a saved playing, listening, or watching activity.')
@commands.is_owner()
async def activity(ctx,kind: str,*,text: str):
    kinds={'playing':discord.ActivityType.playing,'listening':discord.ActivityType.listening,'watching':discord.ActivityType.watching}
    if kind.lower() not in kinds or not 1<=len(text)<=128:
        raise commands.BadArgument('Use activity playing/listening/watching text, up to 128 characters.')
    status_cycle.cancel()
    save_runtime('presence',{'mode':'single','texts':[text],'kind':kind.lower()})
    await bot.change_presence(activity=discord.Activity(type=kinds[kind.lower()],name=text))
    await private_owner_send(ctx,embed('presence updated',kind.lower()+' '+text))

@bot.hybrid_command(description='Owner: resync registered slash commands.')
@commands.is_owner()
@commands.cooldown(1,60,commands.BucketType.user)
async def synccommands(ctx):
    synced=await bot.tree.sync()
    await private_owner_send(ctx,embed('commands synced',f'{len(synced)} slash entries registered.'))

@bot.hybrid_command(description='Owner: test the configured Last.fm key without exposing credentials.')
@commands.is_owner()
async def fmtest(ctx):
    fm_cache.clear()
    try:
        await lastfm_request('user.getInfo',bot.store.username(ctx.author.id) or 'rj')
        e=embed('Last.fm / diagnostic','🟢 API request succeeded. Key accepted.')
    except commands.BadArgument as error:
        e=embed('Last.fm / diagnostic',str(error))
    await private_owner_send(ctx,e)

@bot.hybrid_command(description='Owner: clear music cache or recent snipe cache.')
@commands.is_owner()
async def clearcache(ctx,kind: str='music'):
    if kind.lower()=='music':
        fm_cache.clear()
    elif kind.lower()=='snipes':
        deleted_snipes.clear();edited_snipes.clear()
    else:
        raise commands.BadArgument('Choose music or snipes.')
    await private_owner_send(ctx,embed('cache cleared',kind.lower()))

ALIASES = {
    'check': ['health', 'chk', 'sys'], 'ownerstats': ['ostats', 'diagnostics'],
    'activity': ['act'], 'synccommands': ['sync'], 'fmtest': ['lastfmtest'], 'clearcache': ['ccache'],
    'help': ['h', 'cmds', 'commands'], 'dashboard': ['dash', 'panel', 'settings'],
    'purge': ['p', 'clear', 'prune'], 'cleanbots': ['cb', 'botclear'],
    'delete': ['d', 'del', 'delafter', 'deleteafter'], 'cleanup': ['autodel', 'cleanupsettings'],
    'pendingdeletes': ['pending', 'deletions'], 'canceldelete': ['canceldel', 'undel'],
    'kick': ['k'], 'ban': ['b'], 'unban': ['ub'], 'timeout': ['mute', 'tm', 'to'],
    'untimeout': ['unmute', 'untm', 'uto'], 'nick': ['nickname', 'setnick'],
    'warn': ['w'], 'warnings': ['warns', 'ws'], 'unwarn': ['delwarn', 'uw'],
    'slowmode': ['sm', 'slow'], 'lock': ['lc'], 'unlock': ['ul'],
    'security': ['sec', 'an', 'antinuke'], 'trust': ['whitelist', 'wl'],
    'untrust': ['unwhitelist', 'uwl'], 'trustedlist': ['trusted', 'wlist'],
    'release': ['unquarantine', 'uq'], 'avatar': ['av', 'pfp'], 'banner': ['bn'],
    'serverinfo': ['si', 'guildinfo'], 'userinfo': ['ui', 'whois'],
    'servericon': ['icon'], 'membercount': ['mc', 'members'], 'roleinfo': ['ri'],
    'channelinfo': ['ci'], 'botinfo': ['bi', 'uptime'], 'ping': ['latency'],
    'np': ['nowplaying'], 'recent': ['recents', 'recenttracks'],
    'toptracks': ['tt'], 'topartists': ['ta'], 'topalbums': ['tal'],
    'reps': ['repcount'], 'leaderboard': ['lb', 'toprep'], 'coinflip': ['coin', 'flip'],
    'roll': ['dice'], 'choose': ['pick'], '8ball': ['8b'],
    'snipe': ['s'], 'editsnipe': ['es'], 'clearsnipe': ['cs'],
    'autorole': ['ar'], 'autoroleoff': ['aroff'], 'roleall': ['ra'],
    'statuscycle': ['sc', 'cycle'], 'logchannel': ['logs', 'setlogs'],
    'embedcolor': ['color', 'colour'], 'embed': ['emb'], 'say': ['echo'],
}
for command_name, aliases in ALIASES.items():
    command = bot.get_command(command_name)
    if command:
        for alias in aliases:
            if alias not in bot.all_commands:
                command.aliases.append(alias)
                bot.all_commands[alias] = command

if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s: %(message)s')
    token = os.getenv('DISCORD_TOKEN', '').strip()
    if not token:
        raise SystemExit('Set DISCORD_TOKEN in .env or your host environment.')
    bot.run(token)
