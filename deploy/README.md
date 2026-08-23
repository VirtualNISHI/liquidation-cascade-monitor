# Deploy (VPS / systemd) — server migration from GitHub Actions

Runs the liquidation-cascade monitor on the same server as the other bots, so it
shares a persistent disk (OI history fills with no Actions-cache eviction) and
fires on a reliable systemd timer (no GitHub cron skips).

Project path assumed: `/srv/liquidation-cascade-monitor/`. Adjust paths in the
unit/cron files if different.

## 1. Initial setup

```bash
# Dedicated user
sudo useradd -r -m -d /srv/liquidation-cascade-monitor -s /bin/bash liqmap

# Clone (public repo)
sudo -u liqmap git clone https://github.com/VirtualNISHI/liquidation-cascade-monitor /srv/liquidation-cascade-monitor
cd /srv/liquidation-cascade-monitor

# venv + deps  (server has Python 3.10; 3.10+ is fine)
sudo -u liqmap python3 -m venv .venv
sudo -u liqmap .venv/bin/pip install -r requirements.txt
sudo .venv/bin/python -m playwright install-deps chromium       # system libs (needs root)
sudo -u liqmap .venv/bin/python -m playwright install chromium  # browser (as the bot user)

# log/output dirs — systemd `append:` needs data/ to already exist
sudo -u liqmap mkdir -p data out

# Japanese font for rendering (if not already present)
sudo apt-get update && sudo apt-get install -y fonts-noto-cjk

# .env — X + Discord + LLM keys (reuse the same values your other bots use)
sudo -u liqmap cp .env.example .env
sudo -u liqmap $EDITOR .env   # fill X_API_KEY/SECRET/ACCESS_TOKEN/ACCESS_SECRET, DISCORD_WEBHOOK_URL,
                              # GEMINI_API_KEY, OPENAI_API_KEY, XAI_API_KEY

# Smoke test WITHOUT posting (renders to out/, prints bias)
sudo -u liqmap PYTHONUTF8=1 .venv/bin/python scripts/generate.py --source live --max-addresses 4000 --llm
```

## 2. systemd timer (recommended)

```bash
sudo cp deploy/liqmap-post.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now liqmap-post.timer

systemctl list-timers | grep liqmap
# First live post now (optional):
sudo systemctl start liqmap-post.service
journalctl -u liqmap-post.service -n 50 --no-pager
tail -n 50 /srv/liquidation-cascade-monitor/data/post.log
```

(Or use cron instead: `sudo -u liqmap crontab deploy/liqmap.cron`.)

## 3. Decommission GitHub Actions (avoid double-posting)

Once the server posts successfully, disable the GitHub schedule so it doesn't
post in parallel:

```bash
gh workflow disable post-liquidation-map -R VirtualNISHI/liquidation-cascade-monitor
```

## Notes
- `OnCalendar` uses the server timezone; the file assumes UTC (00/2 → 00,02,04,06,08,10,12,14,16,18,20,22 UTC = 12/day).
- Bias C2 (OI velocity) activates after ~24h once `out/cache/oi_history.json` accumulates.
- To update: `sudo -u liqmap git -C /srv/liquidation-cascade-monitor pull` (timer picks it up next run).
