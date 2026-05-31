import asyncio
import io
import os

import discord
import httpx
from discord import app_commands

from app.log import setup as _setup_logging

log = _setup_logging("bot")

API = os.environ["API_URL"]
ALLOWED = {int(c) for c in os.environ["ALLOWED_CHANNELS"].split(",") if c.strip()}
ALERT_POLL_SECONDS = int(os.environ.get("ALERT_POLL_SECONDS", "60"))
HTTP_TIMEOUT = float(os.environ.get("HTTP_TIMEOUT", "30"))
OWNER_ID = int(os.environ.get("OWNER_ID", "0"))

intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)
http = httpx.AsyncClient(base_url=API, timeout=HTTP_TIMEOUT)
# Guards against on_ready spawning a second alert_loop task on reconnect.
_alert_loop_started = False


def _coin(n: float | None) -> str:
    """Format a coin value as a compact, human-readable string.

    Args:
        n: The numeric value to format, or None if unavailable.

    Returns:
        A comma-formatted string with up to two decimal places and trailing
        zeros stripped, or "unknown" if n is None.
    """
    if n is None:
        return "unknown"
    val = f"{n:,.2f}"
    if val.endswith(".00"):
        return val[:-3]
    if val.endswith("0") and "." in val:
        return val[:-1]
    return val


_MAX_PRICE = 9_223_372_036_854_775_807  # Postgres BIGINT max


def _parse_price(s: str) -> int:
    s = s.strip().lower().replace(",", "")
    suffixes = {"k": 1_000, "m": 1_000_000, "b": 1_000_000_000}
    for suffix, mult in suffixes.items():
        if s.endswith(suffix):
            result = round(float(s[:-1]) * mult)
            break
    else:
        result = round(float(s))
    if result > _MAX_PRICE:
        raise ValueError("Price too large.")
    return result


def _channel_guard(interaction: discord.Interaction) -> bool:
    """Return True if the interaction originated in an allowed channel."""
    return interaction.channel_id in ALLOWED


async def _api(method: str, path: str, **kwargs) -> dict | list | None:
    """Send an HTTP request to the internal API and return the parsed JSON body.

    Args:
        method: HTTP method (e.g. "GET", "POST", "DELETE").
        path: URL path relative to the API base URL.
        **kwargs: Additional keyword arguments forwarded to httpx.

    Returns:
        Parsed JSON response as a dict or list, or None for empty responses
        (e.g. 204 No Content).

    Raises:
        httpx.HTTPStatusError: If the response status indicates an error.
    """
    r = await http.request(method, path, **kwargs)
    r.raise_for_status()
    if not r.content:
        return None
    return r.json()


def _action_status(action: str | None) -> str | None:
    """Convert a watchlist action token into a human-readable status line.

    Tokens are unit-separator (U+001F) delimited fields. Supported kinds:
    ``added``, ``removed``, and ``alert_set``.

    Args:
        action: Action token from the API, e.g. ``"added\\x1fSugar Cane"``
            or ``"alert_set\\x1fSugar Cane\\x1f5000"``.

    Returns:
        A short human-readable string, or None if action is absent or
        unrecognised.
    """
    if not action:
        return None
    parts = action.split("\x1f", 3)
    kind = parts[0]
    if kind == "added" and len(parts) >= 2:
        return f"**{parts[1]}** added to watchlist."
    if kind == "removed" and len(parts) >= 2:
        return f"**{parts[1]}** removed from watchlist."
    if kind == "alert_set" and len(parts) >= 3:
        try:
            price_str = _coin(float(parts[2]))
        except ValueError:
            price_str = parts[2]
        return f"Alert set for **{parts[1]}** at **{price_str}**."
    return None




def _price_embed(data: dict) -> discord.Embed:
    """Build a price embed from a /price/{tag} response.

    Args:
        data: JSON response from the /price/{tag} endpoint.

    Returns:
        A discord.Embed showing buy/sell prices or lowest BIN.
    """
    embed = discord.Embed(title=data["name"], color=0xFFB6C1)
    if data["source"] == "bazaar":
        embed.add_field(name="Buy", value=_coin(data["sell"]), inline=True)
        embed.add_field(name="Sell", value=_coin(data["buy"]), inline=True)
    else:
        embed.add_field(name="LBIN", value=_coin(data["buy"]), inline=True)
    return embed


def _watchlist_embed(wl: dict, user_name: str) -> discord.Embed:
    """Build a watchlist embed with live prices and alert details.

    Args:
        wl: JSON response from a watchlist endpoint.
        user_name: Display name of the Discord user.

    Returns:
        A discord.Embed listing all watched items.
    """
    embed = discord.Embed(title=f"{user_name}'s Watchlist", color=0xFFB6C1)
    if not wl["items"]:
        embed.description = "No items."
        return embed
    for item in wl["items"]:
        if item["source"] == "bazaar":
            val = f"{_coin(item['sell'])} — {_coin(item['buy'])}"
        else:
            val = _coin(item["buy"])
        if item["target_above"] is not None:
            val += f"\nAlert @ {_coin(item['target_above'])}"
        if item["target_below"] is not None:
            val += f"\nAlert @ {_coin(item['target_below'])}"
        embed.add_field(name=item["name"], value=val, inline=False)
    return embed


def _alert_embed(hit: dict) -> discord.Embed:
    """Build an alert embed for a triggered price notification.

    Args:
        hit: A Triggered payload returned by /notifications/triggered.

    Returns:
        A discord.Embed showing the item, target, and current prices.
    """
    direction_label = "Above" if hit["direction"] == "above" else "Below"
    embed = discord.Embed(title=hit["name"], color=0xFFB6C1)
    embed.add_field(name="Alert", value=direction_label, inline=True)
    embed.add_field(name="Target", value=_coin(hit["target"]), inline=True)
    if hit["source"] == "bazaar":
        embed.add_field(name="Buy", value=_coin(hit["sell"]), inline=True)
        embed.add_field(name="Sell", value=_coin(hit["buy"]), inline=True)
    else:
        embed.add_field(name="LBIN", value=_coin(hit["buy"]), inline=True)
    return embed


@tree.command(name="logs", description="Send recent log files.")
async def cmd_logs(interaction: discord.Interaction) -> None:
    """Send the last 200 lines of each log file as attachments.

    Restricted to the bot owner and only works in DMs.

    Args:
        interaction: The Discord interaction context.
    """
    if interaction.user.id != OWNER_ID:
        await interaction.response.send_message("Not authorised.", ephemeral=True)
        return
    if interaction.guild is not None:
        await interaction.response.send_message("DM only.", ephemeral=True)
        return
    await interaction.response.defer()
    base = os.environ.get("LOG_DIR", "logs")
    date_dirs = sorted(
        d for d in os.listdir(base) if os.path.isdir(os.path.join(base, d))
    ) if os.path.isdir(base) else []
    log_dir = os.path.join(base, date_dirs[-1]) if date_dirs else base
    files = []
    for name in ("bot.log", "api.log"):
        path = os.path.join(log_dir, name)
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8") as f:
            tail = "".join(f.readlines()[-200:])
        files.append(discord.File(io.BytesIO(tail.encode()), filename=name))
    if files:
        log.info("/logs requested by owner (%s)", interaction.user.id)
        await interaction.followup.send(
            content=f"`{os.path.basename(log_dir)}`",
            files=files,
        )
    else:
        await interaction.followup.send("No logs found.")


@tree.command(name="help", description="Show available commands.")
async def cmd_help(interaction: discord.Interaction) -> None:
    """Show a summary of all available commands.

    Args:
        interaction: The Discord interaction context.
    """
    log.info("/help — %s (%s)", interaction.user.display_name, interaction.user.id)
    embed = discord.Embed(title="Commands", color=0xFFB6C1)
    embed.add_field(name="/price <item>", value="Get the current price.", inline=False)
    embed.add_field(name="/watch", value="Show your watchlist.", inline=False)
    embed.add_field(name="/watch <item>", value="Add or remove from watchlist.", inline=False)
    embed.add_field(name="/watch <item> <price>", value="Set a price alert.", inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@tree.command(name="price", description="Look up an item's current price.")
@app_commands.describe(item="Item tag or name.")
async def cmd_price(interaction: discord.Interaction, item: str) -> None:
    """Look up the current price of a single item.

    Args:
        interaction: The Discord interaction context.
        item: Item tag supplied by the user (autocompleted).
    """
    if not _channel_guard(interaction):
        await interaction.response.send_message(
            "Not allowed in this channel.", ephemeral=True
        )
        return
    log.info(
        "/price %s — %s (%s)",
        item,
        interaction.user.display_name,
        interaction.user.id,
    )
    await interaction.response.defer()
    try:
        data = await _api("GET", f"/price/{item}")
        await interaction.followup.send(embed=_price_embed(data))
    except httpx.HTTPStatusError as exc:
        log.warning("/price %s failed for %s — %s", item, interaction.user.id, exc)
        if exc.response.status_code < 500:
            try:
                detail = exc.response.json().get("detail", "Something went wrong.")
            except Exception:
                detail = "Something went wrong."
        else:
            detail = "Something went wrong. Please try again."
        await interaction.followup.send(detail, ephemeral=True)
    except Exception:
        await interaction.followup.send("Service unavailable.", ephemeral=True)


@cmd_price.autocomplete("item")
async def price_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice]:
    """Autocomplete handler for the /price item argument.

    Args:
        interaction: The Discord interaction context.
        current: The partial string typed so far.

    Returns:
        Up to 25 matching app_commands.Choice objects.
    """
    return await _autocomplete(current)


@tree.command(name="watch", description="Manage watchlist.")
@app_commands.describe(
    item="Item tag or name.",
    price="Alert price target (e.g. 110k, 1.5m, 2b).",
)
async def cmd_watch(
    interaction: discord.Interaction,
    item: str | None = None,
    price: str | None = None,
) -> None:
    """Show, add/remove, or set a price alert on the watchlist.

    Args:
        interaction: The Discord interaction context.
        item: Item tag to add, remove, or alert on; omit to show the list.
        price: Target price for an alert.
    """
    if not _channel_guard(interaction):
        await interaction.response.send_message(
            "Not allowed in this channel.", ephemeral=True
        )
        return
    if price is not None and item is None:
        await interaction.response.send_message(
            "Specify an item.", ephemeral=True
        )
        return
    price_int: int | None = None
    if price is not None:
        try:
            price_int = _parse_price(price)
        except ValueError:
            await interaction.response.send_message(
                "Invalid price. Use a number like `110k`, `1.5m`, or `2b`.",
                ephemeral=True,
            )
            return
        if price_int <= 0:
            await interaction.response.send_message(
                "Price must be positive.", ephemeral=True
            )
            return
    await interaction.response.defer()
    uid = interaction.user.id
    uname = interaction.user.display_name
    try:
        if item is None:
            log.info("/watch show — %s (%s)", uname, uid)
            wl = await _api("GET", f"/watch/{uid}")
            await interaction.followup.send(embed=_watchlist_embed(wl, uname))
            return
        if price_int is not None:
            log.info("/watch alert %s @ %s — %s (%s)", item, price_int, uname, uid)
            wl = await _api(
                "POST",
                f"/watch/{uid}/{item}/notify",
                params={
                    "price": price_int,
                    "channel_id": interaction.channel_id,
                },
            )
        else:
            log.info("/watch toggle %s — %s (%s)", item, uname, uid)
            wl = await _api("POST", f"/watch/{uid}/{item}/toggle")
        status = _action_status(wl.get("action"))
        await interaction.followup.send(
            content=status, embed=_watchlist_embed(wl, uname), ephemeral=True
        )
    except httpx.HTTPStatusError as exc:
        log.warning("/watch %s failed for %s — %s", item, uid, exc)
        if exc.response.status_code < 500:
            try:
                detail = exc.response.json().get("detail", "Something went wrong.")
            except Exception:
                detail = "Something went wrong."
        else:
            detail = "Something went wrong. Please try again."
        await interaction.followup.send(detail, ephemeral=True)
    except Exception:
        await interaction.followup.send("Service unavailable.", ephemeral=True)


@cmd_watch.autocomplete("item")
async def watch_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice]:
    """Autocomplete handler for the /watch item argument.

    Args:
        interaction: The Discord interaction context.
        current: The partial string typed so far.

    Returns:
        Up to 25 matching app_commands.Choice objects.
    """
    return await _autocomplete(current)


async def _autocomplete(current: str) -> list[app_commands.Choice]:
    """Fetch item name suggestions from the API for autocomplete fields.

    Args:
        current: The partial string typed so far.

    Returns:
        Up to 25 matching app_commands.Choice objects, or an empty list on error.
    """
    try:
        choices = (
            await _api("GET", "/items", params={"query": current, "limit": 25}) or []
        )
        return [
            app_commands.Choice(name=c["name"][:100], value=c["tag"][:100])
            for c in choices
        ]
    except Exception:
        return []


async def alert_loop() -> None:
    """Poll for triggered price alerts and deliver them to Discord.

    Runs indefinitely, sleeping ALERT_POLL_SECONDS between each poll. Each
    alert is cleared only after the Discord message is confirmed sent, so a
    delivery failure will be retried on the next poll cycle.
    """
    await client.wait_until_ready()
    while not client.is_closed():
        try:
            hits = await _api("GET", "/notifications/triggered") or []
        except Exception as exc:
            log.warning("alert poll error — %s", type(exc).__name__)
            await asyncio.sleep(ALERT_POLL_SECONDS)
            continue
        for hit in hits:
            clear_path = f"/notifications/{hit['discord_id']}/{hit['tag']}/{hit['direction']}"
            try:
                ch = client.get_channel(hit["channel_id"])
                if ch is None:
                    ch = await client.fetch_channel(hit["channel_id"])
                if not isinstance(ch, discord.abc.Messageable):
                    log.warning(
                        "alert skipped: channel %s is not messageable",
                        hit["channel_id"],
                    )
                    await _api("DELETE", clear_path)
                    continue
                user = await client.fetch_user(hit["discord_id"])
                await ch.send(content=user.mention, embed=_alert_embed(hit))
                await _api("DELETE", clear_path)
                log.info(
                    "alert delivered: %s → %s (%s %s)",
                    hit["name"], hit["discord_id"], hit["direction"], hit["target"],
                )
            except (discord.NotFound, discord.Forbidden) as exc:
                log.warning(
                    "alert cleared (permanent error): %s → %s — %s",
                    hit["name"], hit["discord_id"], type(exc).__name__,
                )
                try:
                    await _api("DELETE", clear_path)
                except Exception:
                    pass
            except Exception as exc:
                log.warning(
                    "alert delivery failed: %s → %s — %s",
                    hit["name"], hit["discord_id"], type(exc).__name__,
                )
        await asyncio.sleep(ALERT_POLL_SECONDS)


@client.event
async def on_message(message: discord.Message) -> None:
    """Log all incoming messages — DMs, guild messages, and bot mentions."""
    if message.author.bot:
        return
    if isinstance(message.channel, discord.DMChannel):
        log.info(
            "DM from %s (%s): %s",
            message.author.display_name,
            message.author.id,
            message.content,
        )
    elif client.user in message.mentions:
        log.info(
            "mention in #%s (%s) by %s (%s): %s",
            message.channel.name,
            message.channel.id,
            message.author.display_name,
            message.author.id,
            message.content,
        )


@client.event
async def on_ready() -> None:
    """Sync slash commands and start the alert polling loop on login."""
    global _alert_loop_started
    await tree.sync()
    if not _alert_loop_started:
        _alert_loop_started = True
        asyncio.create_task(alert_loop())
    log.info("logged in as %s", client.user)


client.run(os.environ["DISCORD_TOKEN"])
