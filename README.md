# BTD Bot

banish the doubt. A Discord bot with moderation, levels, reminders, warnings, logs and fun commands. The prefix is `,` and `,help` lists every command.

## Run on the VPS (Docker)

```bash
git clone https://github.com/1ayoff/btd-bot.git
cd btd-bot
cp .env.example .env
nano .env                       # paste your DISCORD_TOKEN
docker compose up -d --build
docker compose logs -f          # should say "Logged in as ..."
```

To update after you push changes, run `git pull && docker compose up -d --build`.

## Discord developer portal settings

Under **Bot**, turn on **Message Content Intent** and **Server Members Intent**. Without both, commands and welcome messages won't work.

The bot's role needs to be **above** any roles it manages (for kick, ban, timeout, nick, role and autorole).

## First-time server setup

```
,setlog #mod-logs
,setwelcome #welcome
,autorole Member
,settings
```

## Run locally

```bash
pip install -r requirements.txt
python bot.py
```
