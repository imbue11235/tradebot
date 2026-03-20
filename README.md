# 🤖 TRADEBOT

A professional news-sentiment day trader bot using Alpaca Markets + FinBERT NLP.

## Features

- **News/Sentiment strategy** — Scores financial headlines with FinBERT (BERT trained on financial text)
- **Confidence-scaled position sizing** — Bigger bets on stronger signals
- **Full fee accounting** — SEC fees, FINRA TAF, commissions, FX conversion all factored in
- **Hard budget cap** — Never exceeds your configured limit
- **Long-only** — No shorting, ever
- **Stop-loss + take-profit** — Automatic position management
- **Daily loss circuit breaker** — Halts if the day goes badly
- **Telegram reports** — Trade alerts + 12-hour digest
- **Paper trading first** — Test safely before going live
- **CSV trade log** — Every trade recorded for review

---

## Quick Start

### 1. Prerequisites

- Python 3.11+
- A Linux/macOS machine (or WSL on Windows) that can run 24/7
- Alpaca account (free): https://alpaca.markets
- Telegram bot (free): https://t.me/BotFather

### 2. Install

```bash
git clone <your-repo>
cd tradebot

# Create virtual environment
python3 -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt
```

> **Note:** FinBERT (~440MB) will download automatically on first run.

### 3. Get your API keys

**Alpaca:**
1. Go to https://app.alpaca.markets → Paper Trading → API Keys
2. Generate a new key pair
3. Copy `API Key` and `Secret Key`

**Telegram bot:**
1. Message @BotFather on Telegram: `/newbot`
2. Follow prompts, copy the bot token
3. Start a chat with your new bot, then go to:
   `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates`
4. Send any message to your bot, then refresh the URL
5. Find `"chat":{"id":...}` — that number is your `chat_id`

**NewsAPI (optional, free tier 100 req/day):**
- https://newsapi.org/register

**Finnhub (optional, free tier):**
- https://finnhub.io/register

### 4. Configure

Edit `config.yaml`:

```yaml
alpaca:
  api_key: "PKxxxxxxxxxxxxxxxx"
  api_secret: "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
  paper_trading: true   # Keep true until you're confident!

budget:
  max_total_usd: 10000  # Your budget cap

telegram:
  bot_token: "123456789:ABCdefGHIjklMNOpqrSTUvwxYZ"
  chat_id: "987654321"
```

### 5. Validate your config

```bash
python main.py --dry-run
```

### 6. Check account status

```bash
python main.py --status
```

### 7. Run

```bash
# Foreground (Ctrl+C to stop)
python main.py

# Background with nohup (logs to nohup.out)
nohup python main.py &

# Or as a systemd service (recommended for 24/7)
# See below
```

---

## Running 24/7 with systemd (Linux)

```bash
# Copy service file
sudo cp tradebot.service /etc/systemd/system/

# Edit it — replace YOUR_USER with your username
sudo nano /etc/systemd/system/tradebot.service

# Enable and start
sudo systemctl daemon-reload
sudo systemctl enable tradebot
sudo systemctl start tradebot

# View live logs
journalctl -u tradebot -f

# Stop
sudo systemctl stop tradebot
```

---

## Going Live

When you're happy with paper trading results:

1. Open `config.yaml`
2. Change `paper_trading: true` → `paper_trading: false`
3. Replace paper API keys with **live** API keys from Alpaca
4. Restart the bot

**⚠️ Warning:** Live trading uses real money. Start with a small budget cap. Past paper performance does not guarantee live results.

---

## Files

```
tradebot/
├── main.py                  # Entry point
├── config.yaml              # All configuration
├── requirements.txt         # Python dependencies
├── tradebot.service         # systemd service file
├── core/
│   ├── broker.py            # Alpaca API wrapper
│   ├── engine.py            # Main trading loop
│   ├── fees.py              # Fee calculator
│   ├── risk.py              # Stop-loss, take-profit, daily halt
│   └── sizer.py             # Confidence-scaled position sizing
├── strategies/
│   └── sentiment.py         # FinBERT news sentiment engine
├── reporting/
│   └── telegram.py          # Telegram alerts + reports
├── utils/
│   ├── config.py            # Config loader
│   └── logger.py            # Logging setup + CSV trade log
└── logs/
    ├── tradebot_YYYYMMDD.log  # Daily log files
    └── trades.csv             # All executed trades
```

---

## Telegram Messages

You'll receive:

| Message | When |
|---------|------|
| 🤖 STARTED | Bot boots up |
| 🟢 TRADE EXECUTED | Every buy order |
| 💰/📉 POSITION CLOSED | Every sell (stop/profit/EOD) |
| 📊 STATUS REPORT | Every 12 hours |
| 🛑 HALTED | If daily loss limit hit |

---

## Strategy Details

### Signal Generation
1. Alpaca News API is polled every 60 seconds for articles on your watchlist
2. Each article is scored by FinBERT (3 classes: positive/negative/neutral)
3. Score = P(positive) - P(negative) ∈ [-1, 1]
4. Recent articles weighted higher (recency decay)
5. Only BUY signals processed (score > threshold)

### Position Sizing
| Confidence | Budget % per trade |
|------------|-------------------|
| 75–100% | Up to 10% |
| 55–75% | Up to 5% |
| 40–55% | Up to 2% |
| < 40% | No trade |

Plus: never exceeds 1/8 of budget per position, never trades if fees > 50% of expected gain.

### Exit Rules
- **Stop-loss:** -2.5% → close immediately
- **Take-profit:** +4.0% → close immediately
- **EOD:** Close all positions 10 minutes before market close

### Circuit Breakers
- Daily loss > 5% → all trading halted until next day
- Max 8 open positions at once
- No trades in first 15 min or last 10 min of session

---

## Disclaimer

This bot is for educational and research purposes. Trading stocks involves significant financial risk. Past performance is not indicative of future results. You are solely responsible for any trades placed by this software. Always start with paper trading.
