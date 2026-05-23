# Automated Polymarket vs Deribit Options Comparison

This automation continuously compares Polymarket prediction market probabilities (BTC and ETH) with Deribit options-implied probabilities, sends Slack alerts when spreads exceed a threshold, and supports on-demand scans via a Slack slash command.

## Architecture

```
┌──────────────┐   POST /slack/commands   ┌──────────────────────┐
│  Slack User  │ ──────────────────────▶  │  Flask app (Railway)  │
│  /polymarket │ ◀────────── results ──── │                      │
│   -scan      │                          │  ┌──────────────┐    │
└──────────────┘                          │  │  Background   │    │
                                          │  │  Scheduler    │    │
       ┌──────────────┐                   │  │  (15 min)     │    │
       │ Slack Channel │ ◀── webhook ──── │  └──────┬───────┘    │
       │  #arb-alerts  │                  │         │            │
       └──────────────┘                   │         ▼            │
                                          │  ┌──────────────┐    │
                                          │  │ Scan Pipeline │    │
                                          │  │ 1. Polymarket │    │
                                          │  │ 2. Deribit    │    │
                                          │  │ 3. SVI + MC   │    │
                                          │  │ 4. Compare    │    │
                                          │  └──────────────┘    │
                                          └──────────────────────┘
```

## Quick Start

### 1. Create a Slack App

1. Go to [api.slack.com/apps](https://api.slack.com/apps) → **Create New App** → **From scratch**
2. Name it `Polymarket Scanner`, pick your workspace

#### Enable Incoming Webhooks
1. **Incoming Webhooks** → toggle **On**
2. **Add New Webhook to Workspace** → pick a channel (e.g. `#arb-alerts`)
3. Copy the webhook URL → this is your `SLACK_WEBHOOK_URL`

#### Enable Slash Commands
1. **Slash Commands** → **Create New Command**
   - Command: `/option`
   - Request URL: `https://<your-railway-app>.railway.app/slack/commands`
   - Short Description: `Polymarket vs Deribit scanner: start/stop, status, scan`
   - Usage Hint: `[start | stop | status | btc | eth | all | expiry <days> | threshold <number> | help]`
2. Install the app to your workspace

#### Get Signing Secret
1. **Basic Information** → **App Credentials** → copy **Signing Secret**
2. This is your `SLACK_SIGNING_SECRET`

### 2. Deploy to Railway

1. Push this repo to GitHub
2. Go to [railway.app](https://railway.app) → **New Project** → **Deploy from GitHub**
3. Select your repo
4. (Optional) **Alert deduplication** — set `DATABASE_URL` to the path of a SQLite `.db` file. The app records which Polymarket URLs have already been alerted on so you don’t get duplicate Slack alerts. History is pruned after 90 days. **The app never wipes the database when the bot stops.** On Railway, the container filesystem is ephemeral (restarts/deploys lose the file), so you **must** add a **Volume**, mount it (e.g. `/data`), and set `DATABASE_URL=/data/alerts.db` so the DB persists. To confirm the DB is in use: check Railway logs for `Alert dedupe: N URLs in history` at the start of each scan, or call `GET /health` and check `alert_dedupe: true` and `alert_history_count` (number of rows).

5. Add environment variables (Settings → Variables):

| Variable | Value | Required |
|----------|-------|----------|
| `SLACK_WEBHOOK_URL` | `https://hooks.slack.com/services/...` | Yes |
| `SLACK_SIGNING_SECRET` | Your Slack signing secret | Yes |
| `SCAN_INTERVAL_MINUTES` | `15` (default) | No |
| `ALERT_THRESHOLD_PCT` | `5.0` (default) | No |
| `SCAN_CURRENCIES` | `BTC,ETH` (default) | No |
| `MAX_EXPIRY_DAYS` | `90` (default) — only scan events expiring within N days | No |
| `DATABASE_URL` | Path to SQLite file, e.g. `alerts.db` or `/data/alerts.db` | No (required for dedupe) |
| `ALERT_RETENTION_DAYS` | `90` (default) — keep alert history for N days | No |

6. Railway auto-detects Python + deploys. The service starts at `https://<app>.railway.app`
7. Update your Slack slash command Request URL to `https://<app>.railway.app/slack/commands`

### 3. Verify

- Visit `https://<app>.railway.app/health` — should return JSON with status
- Type `/option` in Slack — should start a scan and post results
- Type `/option threshold 3` — uses 3% threshold instead of default

## Usage

### Slash Command

| Command | Description |
|--------|-------------|
| `/option start` | Start the continuous run (scheduler). Scans run every N minutes for all currencies and post alerts when spread exceeds threshold. |
| `/option stop` | Stop the continuous run. No scheduled scans until you send `start` again. |
| `/option status` | Show whether the scheduler is running, active currencies, and the last scan result. |
| `/option btc` | One-off scan for **BTC only** (default threshold). |
| `/option eth` | One-off scan for **ETH only** (default threshold). |
| `/option all` | One-off scan for **all currencies** (default threshold). |
| `/option btc threshold 3` | One-off scan for BTC with 3% threshold. |
| `/option expiry 30` | One-off scan, events expiring within 30 days. |
| `/option btc expiry 14 threshold 3` | BTC only, events ≤ 14 days, 3% threshold. |
| `/option threshold 5` | One-off scan, all currencies, 5% threshold. |
| `/option help` | Show all available commands and usage. |
| `/option` | One-off scan, all currencies, default threshold & expiry. |

For one-off scans, the bot will:
1. Immediately respond: "Scan started — results will be posted when ready"
2. Run the full pipeline in the background (takes 1–3 minutes per expiry)
3. Post results to the channel when done

### Alert deduplication (database)

If `DATABASE_URL` is set to a SQLite file path (e.g. `alerts.db` or on Railway `/data/alerts.db` on a Volume), the app records every Polymarket URL it has already sent an alert for. It only sends a Slack alert when the opportunity’s Polymarket URL is **new** (not in the database), so you don’t get repeated alerts for the same event. Records are pruned after `ALERT_RETENTION_DAYS` (default 90). **Stopping the bot does not wipe the database** — only pruning deletes old rows. Without a database (or if the DB file is on ephemeral storage and the container restarts), every flagged opportunity triggers an alert on every scan.

### Automated Alerts

The scanner runs automatically every `SCAN_INTERVAL_MINUTES` (default: 15). When a spread exceeds the threshold (and the URL is new, if DB is configured), it posts an alert like:

```
🚨 2 Alert(s) — Polymarket vs Model
Expiry: 2026-03-11  |  Spot: $68,611  |  Threshold: 5%

🔴  $68,000–$70,000  (between)
      PM: 27.5%  Model: 36.9%  Spread: -9.4%

🔴  $72,000–$74,000  (between)
      PM: 11.0%  Model: 4.7%  Spread: +6.3%
```

### Health Check

```
GET /health
```

Returns JSON with last run status, interval, and threshold.

## Local Development

```bash
# Install dependencies
pip install -r requirements.txt

# Set environment variables
cp .env.example .env
# Edit .env with your Slack webhook URL and signing secret

# Run locally
export $(cat .env | xargs) && python app.py
```

The server starts on `http://localhost:8080`. For Slack slash commands to work locally, use [ngrok](https://ngrok.com) to tunnel:

```bash
ngrok http 8080
# Then update your Slack slash command URL to the ngrok URL
```

## How It Works

### Pipeline Steps

1. **Polymarket**: For each currency (BTC, ETH), fetches prediction markets from the Gamma API, extracts strikes, probabilities, and question types (above/below/between)
2. **Deribit**: Fetches option instruments, builds SVI volatility smiles across 6 expiries
3. **Local Vol Surface**: Converts implied vol to local vol via Dupire formula
4. **Monte Carlo**: Simulates 200,000 price paths to compute end-of-period probability distribution
5. **Compare**: For each Polymarket quote, computes the model probability matching the question type:
   - "above $X" → P(S_T ≥ X)
   - "below $X" → P(S_T < X)
   - "between $X–$Y" → P(X ≤ S_T < Y)
6. **Alert**: If |spread| > threshold, sends Slack alert

### Files

| File | Purpose |
|------|---------|
| `app.py` | Flask server, slash command handler, background scheduler |
| `alert_db.py` | SQLite alert history for deduplication (no duplicate Slack alerts) |
| `slack_alerts.py` | Slack webhook messaging with Block Kit formatting |
| `fetch_polymarket_prob.py` | Polymarket Gamma API client |
| `find_deribit_arbitrage.py` | Deribit option price fetcher |
| `old_scripts/x_price_option_all_expiries.py` | Monte Carlo model, SVI fitting, comparison logic |
| `Procfile` | Railway process definition |
| `railway.toml` | Railway build/deploy config |

## Troubleshooting

- **No Slack alerts**: Check `SLACK_WEBHOOK_URL` is set correctly. Visit `/health` to verify the service is running.
- **Slash command not working**: Ensure the Request URL matches your Railway app URL + `/slack/commands`. Check `SLACK_SIGNING_SECRET`.
- **Scan takes too long**: Each expiry requires ~100 Deribit API calls for SVI fitting + Monte Carlo simulation. Near-term expiries process faster.
- **Process killed on Railway**: If the Railway instance runs out of memory, reduce `N_PATHS_HIT` in the model config or upgrade the Railway plan.
- **Duplicate Slack alerts**: Set `DATABASE_URL` to a path for a SQLite file. On Railway you must use a **Volume** and a path on it (e.g. `DATABASE_URL=/data/alerts.db`); otherwise the file is lost on every deploy/restart and the app will alert every time. Check logs for `Alert dedupe: N URLs in history` and `GET /health` for `alert_dedupe` and `alert_history_count` to confirm the DB is in use.
