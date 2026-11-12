# BTD Bot

banish the doubt. A Discord bot with moderation, levels, reminders, warnings, logs and fun commands. The prefix is `,` and `,help` lists every command.

## Run on the VPS (Docker)

```bash
git clone https://github.com/1ayoff/BTD-bot-Updated.git
cd BTD-bot-Updated
cp .env.example .env
nano .env                       # paste your DISCORD_TOKEN
docker compose up -d --build
docker compose logs -f          # should say "Logged in as ..."
```

To update after you push changes, run `git pull && docker compose up -d --build`.

### Auto-deploy (optional)

`deploy/autodeploy.sh` checks GitHub every minute and redeploys when there are new commits. Set it up once:

```bash
chmod +x deploy/autodeploy.sh
./deploy/autodeploy.sh                     # test run - should finish without asking for a password
(crontab -l 2>/dev/null; echo "* * * * * $PWD/deploy/autodeploy.sh >> $HOME/btd-deploy.log 2>&1") | crontab -
```

Watch deploys with `tail -f ~/btd-deploy.log`. To turn it off, run `crontab -e` and delete the line.

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
