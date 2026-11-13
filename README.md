# btd bot

**banish the doubt.**

this is the bot that runs [discord.gg/banish](https://discord.gg/banish). i built it because i wanted one bot that did everything my server needed without paying for five different ones. moderation, levels, reminders, warnings, logs, last.fm, rep, and some fun stuff on top.

prefix is `,` and `,help` pulls up a menu with everything.

## what it does

- **moderation** - kick, ban, timeout, warn (they get a dm), purge, lock/unlock, slowmode. it checks role hierarchy so mods can't touch people above them
- **logs** - joins, leaves, deleted and edited messages, and every mod action goes to your log channel
- **levels** - xp for talking (with a cooldown so spamming doesn't work), `,rank` and a leaderboard
- **rep** - give someone +1 once a day, `,replb` for the top
- **last.fm** - `,fm` shows what you're playing with your scrobble count
- **utility** - reminders that survive restarts, afk with auto replies, `,snipe`, `,whois` with buttons
- **fun** - 8ball, dice, coinflip, polls, `,choose`
- **setup** - welcome messages and autorole per server

everything's saved in sqlite so nothing resets when the bot restarts.

## running it on a vps (docker)

```bash
git clone https://github.com/1ayoff/BTD-bot-Updated.git
cd BTD-bot-Updated
cp .env.example .env
nano .env                       # put your DISCORD_TOKEN in here
docker compose up -d --build
docker compose logs -f          # you should see "Logged in as ..."
```

the container runs as a non-root user with a read-only filesystem and no extra privileges, so even if something goes wrong it can't do much.

to update it by hand: `git pull && docker compose up -d --build`

### settings (.env)

| variable | what it's for |
| --- | --- |
| `DISCORD_TOKEN` | required, your bot token |
| `OWNER_ID` | your discord id so `,status` works |
| `PREFIX` | if you want something other than `,` |
| `LASTFM_API_KEY` | turns on `,fm` ([get one here](https://www.last.fm/api/account/create)) |
| `HEARTBEAT_URL` / `HEARTBEAT_TOKEN` | optional, pings a status page every minute |

### auto-deploy

i got tired of ssh-ing in every time i changed something, so `deploy/autodeploy.sh` checks github every minute and redeploys by itself when there's a new commit. one-time setup:

```bash
chmod +x deploy/autodeploy.sh
./deploy/autodeploy.sh          # test run, shouldn't ask for a password
(crontab -l 2>/dev/null; echo "* * * * * $PWD/deploy/autodeploy.sh >> $HOME/btd-deploy.log 2>&1") | crontab -
```

`tail -f ~/btd-deploy.log` to watch it work. to turn it off, `crontab -e` and delete the line.

## discord developer portal

under **Bot**, turn on **Message Content Intent** and **Server Members Intent**. if you skip these, commands and welcome messages just won't work.

also put the bot's role **above** any role you want it to manage (kick, ban, timeout, nick, role, autorole). discord won't let it touch roles higher than its own.

## first time in a new server

```
,setlog #mod-logs
,setwelcome #welcome
,autorole Member
,settings
```

## running it locally

```bash
pip install -r requirements.txt
python bot.py
```

---

made by sayo. love everyone 🖤
