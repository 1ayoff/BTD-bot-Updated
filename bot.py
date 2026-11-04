"""BTD: moderation, logging, reputation and Last.fm. Python 3.11+."""
import asyncio
import logging
import os
from pathlib import Path
import sqlite3
from datetime import timedelta

import aiohttp
import discord
from discord.ext import commands
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / '.env')
COLOR = 0x173D2B
log = logging.getLogger('btd')

class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS config (guild INTEGER PRIMARY KEY, channel INTEGER);
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
    async def close(self):
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

@bot.event
async def on_ready():
    log.info('BTD connected as %s to %s servers', bot.user, len(bot.guilds))

@bot.command()
async def btd(ctx):
    await ctx.send('banish the doubt.')

@bot.command()
async def tp(ctx):
    await ctx.send('lock in')

@bot.command()
async def ping(ctx):
    await ctx.send(f'{round(bot.latency * 1000)} ms')

@bot.command()
@commands.is_owner()
async def status(ctx, *, text: str):
    if len(text) > 128:
        raise commands.BadArgument('Keep the status under 129 characters.')
    await bot.change_presence(activity=discord.Game(text))
    await ctx.send('Status updated.')

@bot.command()
@commands.is_owner()
async def logchannel(ctx, channel: discord.TextChannel):
    perms = channel.permissions_for(ctx.me)
    if not (perms.view_channel and perms.send_messages and perms.embed_links):
        raise commands.BadArgument('I need View Channel, Send Messages and Embed Links there.')
    bot.store.set_channel(ctx.guild.id, channel.id)
    await ctx.send(f'Logging to {channel.mention}.')

@bot.command()
@commands.is_owner()
async def logoff(ctx):
    bot.store.set_channel(ctx.guild.id, None)
    await ctx.send('Logging disabled.')

@bot.command()
@commands.has_permissions(manage_messages=True)
@commands.bot_has_permissions(send_messages=True, embed_links=True)
async def say(ctx, *, message: str):
    if len(message) > 4000:
        raise commands.BadArgument('Keep the message under 4,001 characters.')
    await ctx.send(embed=embed(description=message))

@bot.command()
@commands.has_permissions(administrator=True)
@commands.bot_has_permissions(mention_everyone=True, embed_links=True)
async def announce(ctx, *, message: str):
    if len(message) > 4000:
        raise commands.BadArgument('Keep the announcement under 4,001 characters.')
    await ctx.send('@everyone', embed=embed('btd', message),
                   allowed_mentions=discord.AllowedMentions(everyone=True))

@bot.command()
@commands.has_permissions(manage_messages=True)
@commands.bot_has_permissions(manage_messages=True, read_message_history=True)
async def purge(ctx, amount: int):
    if not 1 <= amount <= 100:
        raise commands.BadArgument('Choose 1–100 messages.')
    # Keep the invoking message out of the count.
    deleted = await ctx.channel.purge(limit=amount, before=ctx.message)
    await ctx.send(f'Deleted {len(deleted)} messages.', delete_after=5)
    await audit(ctx.guild, 'Messages purged', f'{ctx.author} • {ctx.channel.mention} • {len(deleted)} messages')

@bot.command()
@commands.has_permissions(kick_members=True)
@commands.bot_has_permissions(kick_members=True)
async def kick(ctx, member: discord.Member, *, reason='No reason provided'):
    target_check(ctx, member)
    await member.kick(reason=f'{ctx.author}: {reason}'[:512])
    await ctx.send(f'Kicked {member}.')
    await audit(ctx.guild, 'Kick', f'{member} ({member.id}) • by {ctx.author}\n{reason}')

@bot.command()
@commands.has_permissions(ban_members=True)
@commands.bot_has_permissions(ban_members=True)
async def ban(ctx, member: discord.Member, *, reason='No reason provided'):
    target_check(ctx, member)
    await member.ban(reason=f'{ctx.author}: {reason}'[:512], delete_message_seconds=0)
    await ctx.send(f'Banned {member}.')
    await audit(ctx.guild, 'Ban', f'{member} ({member.id}) • by {ctx.author}\n{reason}')

@bot.command()
@commands.has_permissions(ban_members=True)
@commands.bot_has_permissions(ban_members=True)
async def unban(ctx, user_id: int, *, reason='No reason provided'):
    await ctx.guild.unban(discord.Object(id=user_id), reason=f'{ctx.author}: {reason}'[:512])
    await ctx.send(f'Unbanned {user_id}.')
    await audit(ctx.guild, 'Unban', f'{user_id} • by {ctx.author}\n{reason}')

@bot.command()
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

@bot.command()
@commands.has_permissions(moderate_members=True)
@commands.bot_has_permissions(moderate_members=True)
async def untimeout(ctx, member: discord.Member):
    target_check(ctx, member)
    await member.timeout(None, reason=f'Removed by {ctx.author}')
    await ctx.send(f'Removed timeout from {member}.')
    await audit(ctx.guild, 'Timeout removed', f'{member} • by {ctx.author}')

@bot.command()
@commands.has_permissions(manage_nicknames=True)
@commands.bot_has_permissions(manage_nicknames=True)
async def nick(ctx, member: discord.Member, *, nickname=None):
    target_check(ctx, member)
    if nickname and len(nickname) > 32:
        raise commands.BadArgument('Nicknames can have up to 32 characters.')
    await member.edit(nick=nickname, reason=f'Changed by {ctx.author}')
    await ctx.send(f'Nickname updated for {member}.')
    await audit(ctx.guild, 'Nickname changed', f'{member} • by {ctx.author} • {nickname or "reset"}')

@bot.command()
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

@bot.command()
async def userinfo(ctx, member: discord.Member = None):
    m = member or ctx.author
    e = embed(str(m))
    e.set_thumbnail(url=m.display_avatar.url)
    e.add_field(name='Account created', value=discord.utils.format_dt(m.created_at, 'D'))
    e.add_field(name='Joined', value=discord.utils.format_dt(m.joined_at, 'D') if m.joined_at else 'Unknown')
    e.add_field(name='Role', value=m.top_role.mention)
    e.set_footer(text=str(m.id))
    await ctx.send(embed=e)

@bot.command()
async def rep(ctx, member: discord.Member = None):
    if member is None:
        return await ctx.send(f'You have {bot.store.total(ctx.guild.id, ctx.author.id)} rep. Give rep with `,rep @user`.')
    if member.id == ctx.author.id or member.bot:
        raise commands.BadArgument('Give rep to another person.')
    wait = bot.store.give_rep(ctx.guild.id, ctx.author.id, member.id, discord.utils.utcnow().timestamp())
    if wait:
        return await ctx.send(f'You can give rep again in {max(1, int(wait // 3600) + 1)} hours.')
    await ctx.send(f'+1 rep for {member.mention} • {bot.store.total(ctx.guild.id, member.id)} total')

@bot.command()
async def reps(ctx, member: discord.Member = None):
    m = member or ctx.author
    await ctx.send(f'{m.display_name} • {bot.store.total(ctx.guild.id, m.id)} rep')

@bot.command()
async def leaderboard(ctx):
    rows = bot.store.db.execute('SELECT receiver, COUNT(*) AS n FROM reputation WHERE guild=? GROUP BY receiver ORDER BY n DESC, receiver LIMIT 10', (ctx.guild.id,)).fetchall()
    await ctx.send(embed=embed('rep', '\n'.join(f'{i}. <@{u}> • {n}' for i, (u, n) in enumerate(rows, 1)) or 'No rep yet.'))

async def lastfm_request(method, username, **extra):
    key = os.getenv('LASTFM_API_KEY')
    if not key:
        raise commands.BadArgument('Last.fm needs LASTFM_API_KEY in the bot’s environment first.')
    params = {'method': method, 'user': username, 'api_key': key, 'format': 'json', **extra}
    try:
        async with bot.http_session.get('https://ws.audioscrobbler.com/2.0/', params=params) as r:
            if r.status != 200:
                raise commands.BadArgument('Last.fm is unavailable right now. Try again later.')
            data = await r.json(content_type=None)
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
        raise commands.BadArgument('Could not reach Last.fm. Try again later.') from None
    if 'error' in data:
        raise commands.BadArgument('Last.fm could not find that account or complete the request.')
    return data

@bot.group(invoke_without_command=True, aliases=['lastfm'])
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
    e = embed('now playing' if playing else 'last played',
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
    # IDs only: do not retain deleted message bodies or attachments.
    await audit(guild, 'Message deleted', f'Channel <#{payload.channel_id}> • message {payload.message_id}\nAuthor: {msg.author if msg else "not cached"}')

@bot.event
async def on_message_edit(before, after):
    if after.guild and not after.author.bot and before.content != after.content:
        if after.channel.id != bot.store.channel(after.guild.id):
            await audit(after.guild, 'Message edited', f'{after.author} • {after.jump_url}')

@bot.command()
async def help(ctx):
    e = embed('btd', 'Prefix: `' + str(bot.command_prefix) + '`')
    e.add_field(name='general', value='`btd` · `tp` · `ping` · `serverinfo` · `userinfo [@user]`', inline=False)
    e.add_field(name='music', value='`fm set username` · `fm [@user]` · `fm remove`', inline=False)
    e.add_field(name='rep', value='`rep @user` · `reps [@user]` · `leaderboard`\nOne rep per person every 24 hours.', inline=False)
    e.add_field(name='moderation', value='`purge 1–100` · `kick @user [reason]` · `ban @user [reason]`\n`unban user_id [reason]` · `timeout @user minutes [reason]`\n`untimeout @user` · `nick @user [nickname]`\n`say message` · `announce message`', inline=False)
    if await bot.is_owner(ctx.author):
        e.add_field(name='owner', value='`logchannel #channel` · `logoff` · `status text`', inline=False)
    await ctx.send(embed=e)

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
        # Do not print exception strings that may include credential-bearing URLs.
        log.error('Command %s failed (%s)', ctx.command, type(original).__name__)
        text = 'Something went wrong. Check the bot console.'
    await ctx.send(text)

if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s: %(message)s')
    token = os.getenv('DISCORD_TOKEN', '').strip()
    if not token:
        raise SystemExit('Set DISCORD_TOKEN in .env or your host environment.')
    bot.run(token)
