# Skyblock Price Helper

A Discord bot for Hypixel Skyblock that lets users look up item prices and manage a personal watchlist with automatic price alerts.

## Getting Started

Set the required environment variables in `.env`, then run:

```bash
docker compose up --build -d
```

### Required

| Variable | Description |
|---|---|
| `DISCORD_TOKEN` | Discord bot token |
| `OWNER_ID` | Discord user ID of the bot owner (grants access to `/logs`) |
| `ALLOWED_CHANNELS` | Comma-separated channel IDs where commands are allowed |
| `SKYAPI_BASE` | Base URL for the Coflnet SkyApi |
| `POSTGRES_USER` | Postgres username |
| `POSTGRES_PASSWORD` | Postgres password |
| `POSTGRES_DB` | Postgres database name |
| `POSTGRES_HOST` | Postgres host |
| `POSTGRES_PORT` | Postgres port |
| `API_URL` | Internal URL the bot uses to reach the API container |

### Optional

| Variable | Default | Description |
|---|---|---|
| `ALERT_POLL_SECONDS` | `60` | How often the bot checks for triggered alerts |
| `PRICE_CACHE_TTL` | `300` | Price cache duration in seconds |
| `ITEM_CACHE_TTL` | `3600` | Item list cache duration in seconds |
| `HTTP_TIMEOUT` | `30` | Outbound HTTP request timeout in seconds |
| `WATCHLIST_LIMIT` | `10` | Maximum watchlist items per user |

## Commands

| Command | Description |
|---|---|
| `/price <item>` | Look up the current price of an item |
| `/watch` | Show your watchlist with live prices |
| `/watch <item>` | Add or remove an item from your watchlist |
| `/watch <item> <price>` | Add an item and set a price alert |

## Data

Prices provided by [Coflnet SkyApi](https://sky.coflnet.com).
