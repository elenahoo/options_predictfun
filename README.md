# Automated Predict.fun vs Deribit Options Comparison

This automation continuously compares Predict.fun Exchange daily crypto price prediction market probabilities with Deribit options-implied probabilities, sends Slack alerts when spreads exceed a threshold, and supports on-demand scans via a Slack slash command.

## Supported Markets

Only **daily price** binary events for these two tokens:

| Token | Predict.fun Market | Example |
|-------|-----------------|---------|
| BTC | `btc-daily-price` | "BTC above $78528.03 on May 4, 10:00 UTC?" |
| ETH | `eth-daily-price` | "ETH above $2303.90 on May 4, 06:00 UTC?" |

Each market is a binary YES/NO event: "Will {TOKEN} be above ${STRIKE} at {DEADLINE}?"

## Architecture

```
┌──────────────┐   POST /slack/commands   ┌──────────────────────┐
│  Slack User  │ ──────────────────────▶  │  Flask app (Railway)  │
│   /option    │ ◀────────── results ──── │                      │
│              │                          │  ┌──────────────┐    │
└──────────────┘                          │  │  Background   │    │
                                          │  │  Scheduler    │    │
       ┌──────────────┐                   │  │  (15 min)     │    │
       │ Slack Channel │ ◀── webhook ──── │  └──────┬───────┘    │
       │  #arb-alerts  │                  │         │            │
       └──────────────┘                   │         ▼            │
                                          │  ┌──────────────┐    │
                                          │  │ Scan Pipeline │    │
                                          │  │ 1. Predict.fun  │    │
                                          │  │ 2. Deribit    │    │
                                          │  │ 3. SVI surface│    │
                                          │  │ 4. Compare    │    │
                                          │  └──────────────┘    │
                                          └──────────────────────┘
```

## Quick Start

### 1. Create a Slack App

1. Go to [api.slack.com/apps](https://api.slack.com/apps) → **Create New App** → **From scratch**
2. Name it `Predict.fun Scanner`, pick your workspace

#### Enable Incoming Webhooks
1. **Incoming Webhooks** → toggle **On**
2. **Add New Webhook to Workspace** → pick a channel (e.g. `#predictfun-arb-alerts`)
3. Copy the webhook URL → this is your `SLACK_WEBHOOK_URL`

#### Enable Slash Commands
1. **Slash Commands** → **Create New Command**
   - Command: `/option`
   - Request URL: `https://<your-railway-app>.railway.app/slack/commands`
   - Short Description: `Scan Predict.fun vs Deribit for arbitrage`
   - Usage Hint: `[start|stop|status|btc|eth|all|threshold N|help]`
2. Install the app to your workspace

#### Get Signing Secret
1. **Basic Information** → **App Credentials** → copy **Signing Secret**
2. This is your `SLACK_SIGNING_SECRET`

### 2. Deploy to Railway

1. Push this repo to GitHub
2. Go to [railway.app](https://railway.app) → **New Project** → **Deploy from GitHub**
3. Select your repo

#### Create a Volume (required for persistent data)

4. In the Railway dashboard, click the service → **Settings** → **Volumes** → **Add Volume**
   - Mount path: `/data`
   - This stores the SQLite database for alert deduplication and trade positions
   - Without a volume, the DB is lost on every redeploy

#### Set environment variables

5. Go to **Settings** → **Variables** and add:

Minimum variables for scanning and Slack alerts:

| Variable | Value |
|----------|-------|
| `SLACK_WEBHOOK_URL` | `https://hooks.slack.com/services/...` |
| `SLACK_SIGNING_SECRET` | Your Slack signing secret |
| `DATABASE_URL` | `/data/scanner.db` |
| `SCAN_CURRENCIES` | `BTC,ETH` |
| `MAX_EXPIRY_DAYS` | `2` |
| `ALERT_THRESHOLD_PCT` | `5.0` |
| `SCAN_INTERVAL_MINUTES` | `15` |

Additional variables for live trading:

| Variable | Value |
|----------|-------|
| `TRADE_ENABLED` | `true` to place orders; `false` for alerts only |
| `PREDICTFUN_API_KEY` | Predict.fun mainnet API key for the `x-api-key` header |
| `PREDICTFUN_PRIVATE_KEY` | EOA private key, or Privy exported key when using a Predict Account |
| `PREDICTFUN_JWT` | Optional pre-generated JWT; otherwise the bot signs the auth message |
| `PREDICTFUN_PREDICT_ACCOUNT` | Optional Predict Account deposit address from the web app |
| `TRADE_SIZE` | `10` target shares per trade |
| `MIN_ORDER_DOLLARS` | `5.0` |
| `MIN_ORDER_SIZE` | `5` |
| `MAX_ORDER_DOLLARS` | `10.0` hard cap on buy notional per trade |
| `TRADE_MIN_BALANCE_USDT` | `10` |

Optional overrides:

| Variable | Default |
|----------|---------|
| `PREDICTFUN_CHAIN_ID` | `56` (BNB mainnet) |
| `PREDICTFUN_API_BASE` | `https://api.predict.fun` |
| `PREDICTFUN_REQUEST_TIMEOUT` | `60` |
| `PREDICTFUN_CONNECT_TIMEOUT` | `10` |
| `PREDICTFUN_READ_TIMEOUT` | `60` |
| `PREDICTFUN_MAX_RETRIES` | `3` |
| `PREDICTFUN_RETRY_BACKOFF` | `0.8` |
| `PREDICTFUN_PUBLIC_GET_TIMEOUT` | `20` |
| `PREDICTFUN_PUBLIC_GET_RETRIES` | `3` |
| `PREDICTFUN_PUBLIC_GET_BACKOFF` | `0.8` |
| `PREDICTFUN_FEE_RATE` | `0.02` |
| `PREDICTFUN_MIN_TAKER_FEE` | `0.01` |
| `POSITION_CHECK_INTERVAL_SECONDS` | `30` |
| `ALERT_RETENTION_DAYS` | `90` |

#### Trading Wallet

Trading can use either a direct EOA or the Predict Account smart wallet created
by the Predict.fun web app. For EOA mode, set `PREDICTFUN_PRIVATE_KEY` to the
wallet that holds USDT on BNB Chain. For Predict Account mode, set
`PREDICTFUN_PRIVATE_KEY` to the exported Privy key and
`PREDICTFUN_PREDICT_ACCOUNT` to the Predict Account deposit address.

Before live trading, fund the account with USDT and BNB gas on BNB Chain, then
run `python scripts/approve_predictfun.py` to set the Predict.fun SDK approvals.

See `.env.example` for the full list of configurable variables.

6. Railway auto-detects Python + deploys. The service starts at `https://<app>.railway.app`
7. Update your Slack slash command Request URL to `https://<app>.railway.app/slack/commands`

### 3. Verify

- Visit `https://<app>.railway.app/health` — should return JSON with status
- Type `/option` in Slack — should start a scan and post results
- Type `/option btc threshold 3` — scans BTC with 3% threshold

## Usage

### Slash Command

```
/option                        # Scan all currencies with default 5% threshold
/option btc                    # Scan BTC only
/option eth threshold 3        # Scan ETH with 3% threshold
/option start                  # Start continuous scheduler
/option start threshold 5      # Start scheduler with 5% threshold
/option stop                   # Stop continuous scheduler
/option status                 # Show scheduler status and last run
/option help                   # Show help
```

The bot will:
1. Immediately respond: "Scan started — results will be posted when ready"
2. Run the full pipeline in the background (takes 1-3 minutes per expiry)
3. Post results to the channel when done

### Automated Alerts

The scanner runs automatically every `SCAN_INTERVAL_MINUTES` (default: 15). When a spread exceeds the threshold, it posts an alert like:

```
🚨 2 Alert(s) — BTC Predict.fun vs Model
Expiry: 2026-05-04 10:00 UTC  |  Spot: $78,528  |  Threshold: 5%

🔴  > $78,528.03  (above)
      PM: 55.1%  Model: 62.3%  Spread: -7.2%
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

1. **Predict.fun**: Fetches active daily price markets from the Predict.fun REST API (`GET /v1/search` and `GET /v1/categories` with a `GET /v1/markets` fallback), filters by ticker (BTC, ETH), fetches market/category details and orderbooks to get YES probability and the event baseline strike
2. **Deribit**: Fetches option instruments and requires a Deribit expiry on the same calendar date as the Predict.fun daily event
3. **SVI Surface**: Fits Deribit implied-volatility smiles and preserves the actual Deribit expiry timestamp
4. **Terminal Digital Probability**: Computes the risk-neutral terminal probability from the Deribit call surface, i.e. P(S_T >= baseline strike)
5. **Compare**: For each Predict.fun quote, compares the event baseline strike against the Deribit up probability and its complement for down
6. **Alert**: If |spread| > threshold, sends Slack alert
7. **Trade**: If `TRADE_ENABLED=true`, buys the underpriced Predict.fun side and immediately places a GTC sell order at the Deribit target probability. The buy order is capped by `MAX_ORDER_DOLLARS`, which defaults to `$10.00`.

### Predict.fun API

The scanner uses these Predict.fun endpoints:

- `GET /v1/markets` — list visible/open markets (mainnet requires `x-api-key`)
- `GET /v1/search` — narrow BTC/ETH daily candidates before broad scans
- `GET /v1/categories/{slug}` — category details and category markets
- `GET /v1/markets/{id}` — full market details
- `GET /v1/markets/{id}/orderbook` — current YES-side bids and asks
- `GET /v1/auth/message`, `POST /v1/auth`, and `POST /v1/orders` — live trading

API docs: https://docs.predict.fun/developers/predict-rest-api

### Files

| File | Purpose |
|------|---------|
| `app.py` | Flask server, slash command handler, background scheduler |
| `slack_alerts.py` | Slack webhook messaging with Block Kit formatting |
| `fetch_predictfun_prob.py` | Predict.fun REST API client (market discovery + quote extraction) |
| `find_deribit_arbitrage.py` | Deribit option price fetcher + spot price |
| `trade_executor.py` | Predict.fun CLOB buy/sell execution (REST + EIP-712 signing) |
| `position_monitor.py` | Background daemon monitoring open sell orders |
| `alert_db.py` | SQLite alert deduplication database |
| `trade_db.py` | SQLite trade position tracking database |
| `old_scripts/x_price_option_all_expiries.py` | Deribit SVI fitting, terminal probability, and diagnostic sweep logic |
| `Procfile` | Railway process definition |
| `railway.toml` | Railway build/deploy config (with Volume mount) |

## Troubleshooting

- **No Slack alerts**: Check `SLACK_WEBHOOK_URL` is set correctly. Visit `/health` to verify the service is running.
- **Slash command not working**: Ensure the Request URL matches your Railway app URL + `/slack/commands`. Check `SLACK_SIGNING_SECRET`.
- **No Predict.fun markets found**: Predict.fun daily price markets refresh throughout the day. Check that `SCAN_CURRENCIES` includes the tokens you want and that `MAX_EXPIRY_DAYS` is at least 2.
- **Scan takes too long**: Each expiry still requires Deribit ticker calls for SVI fitting, but the live target probability is read directly from the fitted terminal distribution rather than a Monte Carlo path simulation.
