# btd bot

**banish the doubt.**

this is the bot that runs [discord.gg/banish](https://discord.gg/banish). i built it because i wanted one bot that did everything my server needed without paying for five different ones. moderation, levels, reminders, warnings, logs, last.fm, rep, and some fun stuff on top.

prefix is `,` and `,help` shows you everything.

## running it on a vps (docker)

```bash
git clone https://github.com/1ayoff/BTD-bot-Updated.git
cd BTD-bot-Updated
cp .env.example .env
nano .env                       # put your DISCORD_TOKEN in here
docker compose up -d --build
docker compose logs -f          # you should see "Logged in as ..."
```

to update it by hand: `git pull && docker compose up -d --build`

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

made by sayo. if you're reading this, i love you fr. i love everyone 🖤
