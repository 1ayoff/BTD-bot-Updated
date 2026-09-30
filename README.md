# btd bot

Discord moderation, quiet green embeds, Last.fm and persistent reputation.

## Run

Use Python 3.11 or 3.12.

```sh
python -m pip install -r requirements.txt
python bot.py
```

Keep your token in `.env` beside bot.py or in the host's secret environment:

```dotenv
DISCORD_TOKEN=your_new_bot_token
OWNER_ID=your_discord_user_id
LASTFM_API_KEY=your_lastfm_api_key
BOT_PREFIX=,
BTD_DATA_DIR=./data
```

OWNER_ID is your numeric Discord user ID (Developer Mode → Copy User ID).
Without it, owner checks use the application's owner/team information. Set it explicitly for commands restricted to you.
Get a Last.fm API key at https://www.last.fm/api/account/create.
Last.fm commands need the key; moderation and reputation work without it.

In Discord's Developer Portal, enable **Message Content Intent** and **Server Members Intent** on the Bot page.
Invite with the bot scope. Grant View Channels, Send Messages, Embed Links, Read Message History,
Manage Messages, Kick Members, Ban Members, Moderate Members and Manage Nicknames as needed.
Grant Mention Everyone only if you want `,announce` to ping everyone.
Move the bot role above the members it should moderate. Administrator is not required.

## Commands

- `,help`, `,btd`, `,tp`, `,ping`, `,serverinfo`, `,userinfo [@user]`
- `,say message`, `,announce message`
- `,purge 1–100`, `,kick @user [reason]`, `,ban @user [reason]`, `,unban user_id [reason]`
- `,timeout @user minutes [reason]`, `,untimeout @user`, `,nick @user [nickname]`
- `,rep @user`, `,reps [@user]`, `,leaderboard`
- `,fm set username`, `,fm [@user]`, `,fm remove` (alias: `,lastfm`)
- Owner only: `,logchannel #channel`, `,logoff`, `,status text`

Rep is scoped to each server. One rep per giver per 24 hours; no self-rep or bot rep.
Linked Last.fm accounts are shared across servers the bot serves.
Moderation checks enforce user permissions, bot permissions and role hierarchy.
Logs cover bot moderation, member joins/leaves, edits and deletions observed while connected.
Deleted message bodies and attachments are not saved. Uncached deletions have no author.
Logging is disabled until the owner picks a channel. Restrict that channel to trusted staff.
This is an original implementation of these features, not a complete Bleed bot clone.

## Keep it online

Run `python bot.py` as a persistent background worker/container on a host with outbound Discord access.
Use the included Dockerfile if your host supports Docker. Configure restart-on-failure.
Mount a persistent volume at `/data` and set `BTD_DATA_DIR=/data`.
SQLite holds rep, Last.fm links and the log channel setting; back up the volume.
A static site or a request-only serverless function cannot run this Gateway bot continuously.
This chat's runtime is not a permanent host. No live hosting account is configured in these files.
Never put `.env` in Git, container images or shared archives. Rotate tokens exposed in old source files.
