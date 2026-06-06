import asyncio
import io
import os

import discord
import httpx
from discord import app_commands

from app.log import setup as _setup_logging
from version import VERSION

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
        n: Numeric value to format, or None if unavailable.

    Returns:
        Comma-formatted string with up to two decimal places and trailing
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


_MAX_PRICE = 1e18


def _parse_price(s: str) -> float:
    """Parse a user-supplied price string into a float.

    Args:
        s: Price string, optionally suffixed with k/m/b (e.g. "110k", "1.5m").

    Returns:
        The parsed price as a float.

    Raises:
        ValueError: If the string cannot be parsed or exceeds the maximum.
    """
    s = s.strip().lower().replace(",", "")
    suffixes = {"k": 1_000, "m": 1_000_000, "b": 1_000_000_000}
    for suffix, mult in suffixes.items():
        if s.endswith(suffix):
            result = float(s[:-1]) * mult
            break
    else:
        result = float(s)
    if result > _MAX_PRICE:
        raise ValueError("Price too large.")
    return result


def _channel_guard(interaction: discord.Interaction) -> bool:
    """Return True if the interaction originated in an allowed channel."""
    return interaction.channel_id in ALLOWED


async def _api(method: str, path: str, **kwargs) -> dict | list | None:
    """Send an HTTP request to the internal API and return parsed JSON.

    Args:
        method: HTTP method (e.g. "GET", "POST", "DELETE").
        path: URL path relative to the API base URL.
        **kwargs: Additional keyword arguments forwarded to httpx.

    Returns:
        Parsed JSON response as a dict or list, or None for empty responses.

    Raises:
        httpx.HTTPStatusError: If the response status indicates an error.
    """
    r = await http.request(method, path, **kwargs)
    r.raise_for_status()
    if not r.content:
        return None
    return r.json()


def _http_error_detail(exc: httpx.HTTPStatusError) -> str:
    """Extract a user-facing error message from an HTTP error response.

    Args:
        exc: The HTTP status error.

    Returns:
        Detail string from the JSON body, or a generic fallback.
    """
    if exc.response.status_code < 500:
        try:
            return exc.response.json().get("detail", "Something went wrong.")
        except Exception:
            pass
    return "Something went wrong. Please try again."


def _price_embed(data: dict) -> discord.Embed:
    """Build a price embed from a /price/{tag} response.

    Args:
        data: JSON response from the /price/{tag} endpoint.

    Returns:
        A discord.Embed showing buy/sell prices or lowest BIN.
    """
    embed = discord.Embed(title=data["name"], color=0xFFB6C1)
    if data["source"] == "bazaar":
        embed.add_field(name="Buy", value=_coin(data["buy"]), inline=True)
        embed.add_field(name="Sell", value=_coin(data["sell"]), inline=True)
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
            val += f"\n↑ Alert @ {_coin(item['target_above'])}"
        if item["target_below"] is not None:
            val += f"\n↓ Alert @ {_coin(item['target_below'])}"
        embed.add_field(name=item["name"], value=val, inline=False)
    return embed


def _alert_text(hit: dict) -> str:
    """Format an alert notification message.

    Args:
        hit: Triggered alert payload from the API.

    Returns:
        Formatted alert string for Discord.
    """
    name = hit["name"]
    target = _coin(hit["target"])
    if hit["direction"] == "above":
        current = _coin(hit["buy"])
        return f"**{name}** is above your target of {target} — now {current}"
    else:
        current = _coin(hit["sell"] if hit["source"] == "bazaar" else hit["buy"])
        return f"**{name}** is below your target of {target} — now {current}"


async def _item_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice]:
    """Autocomplete from the full item catalogue.

    Args:
        interaction: The Discord interaction context.
        current: Partial string typed so far.

    Returns:
        Up to 25 matching Choice objects.
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


async def _watchlist_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice]:
    """Autocomplete from the user's watchlist items.

    Args:
        interaction: The Discord interaction context.
        current: Partial string typed so far.

    Returns:
        Up to 25 matching Choice objects.
    """
    try:
        wl = await _api("GET", f"/watchlist/{interaction.user.id}") or {}
        q = current.lower()
        return [
            app_commands.Choice(name=i["name"][:100], value=i["tag"][:100])
            for i in wl.get("items", [])
            if not q or q in i["name"].lower() or q in i["tag"].lower()
        ][:25]
    except Exception:
        return []


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
    date_dirs = (
        sorted(d for d in os.listdir(base) if os.path.isdir(os.path.join(base, d)))
        if os.path.isdir(base)
        else []
    )
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
    embed = discord.Embed(
        title="Command Guide", 
        color=0xFFB6C1
    )
    embed.add_field(
        name="/price <item>", 
        value="Look up current price.", 
        inline=False
    )
    embed.add_field(
        name="/watchlist", 
        value="Show your watchlist.", 
        inline=False
    )
    embed.add_field(
        name="/watch <item>", 
        value="Add an item to your watchlist.", 
        inline=False
    )
    embed.add_field(
        name="/unwatch <item>",
        value="Remove an item and all its alerts.",
        inline=False,
    )
    embed.add_field(
        name="/alert <item> <price>",
        value="Set a price alert (e.g. 110k, 1.5m, 2b).",
        inline=False,
    )
    embed.add_field(
        name="/unalert <item>",
        value="Remove all alerts for an item.",
        inline=False,
    )
    embed.add_field(
        name="/unalert <item> <price>",
        value="Remove a specific price alert.",
        inline=False,
    )
    embed.set_footer(text=f"made with ❤️ in Singapore • v{VERSION}")
    await interaction.response.send_message(embed=embed)


@tree.command(name="price", description="Look up an item's current price.")
@app_commands.describe(item="Item tag or name.")
async def cmd_price(interaction: discord.Interaction, item: str) -> None:
    """Look up the current price of a single item.

    Args:
        interaction: The Discord interaction context.
        item: Item tag supplied by the user.
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
        log.warning("/price %s failed for %s — HTTP %s: %s", item, interaction.user.id, exc.response.status_code, exc.response.text[:200])
        await interaction.followup.send(_http_error_detail(exc), ephemeral=True)
    except Exception as exc:
        log.warning("/price %s failed for %s — %s: %s", item, interaction.user.id, type(exc).__name__, exc)
        await interaction.followup.send("Service unavailable.", ephemeral=True)


@cmd_price.autocomplete("item")
async def price_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice]:
    """Autocomplete for /price item argument."""
    return await _item_autocomplete(interaction, current)


@tree.command(name="watchlist", description="Show your watchlist.")
async def cmd_watchlist(interaction: discord.Interaction) -> None:
    """Display the user's watchlist with live prices.

    Args:
        interaction: The Discord interaction context.
    """
    if not _channel_guard(interaction):
        await interaction.response.send_message(
            "Not allowed in this channel.", ephemeral=True
        )
        return
    uid = interaction.user.id
    uname = interaction.user.display_name
    log.info("/watchlist — %s (%s)", uname, uid)
    await interaction.response.defer(ephemeral=True)
    try:
        wl = await _api("GET", f"/watchlist/{uid}")
        await interaction.followup.send(
            embed=_watchlist_embed(wl, uname), ephemeral=True
        )
    except httpx.HTTPStatusError as exc:
        log.warning("/watchlist failed for %s — HTTP %s: %s", uid, exc.response.status_code, exc.response.text[:200])
        await interaction.followup.send(_http_error_detail(exc), ephemeral=True)
    except Exception as exc:
        log.warning("/watchlist failed for %s — %s: %s", uid, type(exc).__name__, exc)
        await interaction.followup.send("Service unavailable.", ephemeral=True)


@tree.command(name="watch", description="Add an item to your watchlist.")
@app_commands.describe(item="Item tag or name.")
async def cmd_watch(interaction: discord.Interaction, item: str) -> None:
    """Add an item to the user's watchlist.

    Args:
        interaction: The Discord interaction context.
        item: Item tag to add.
    """
    if not _channel_guard(interaction):
        await interaction.response.send_message(
            "Not allowed in this channel.", ephemeral=True
        )
        return
    uid = interaction.user.id
    uname = interaction.user.display_name
    log.info("/watch %s — %s (%s)", item, uname, uid)
    await interaction.response.defer(ephemeral=True)
    try:
        wl = await _api("POST", f"/watch/{uid}/{item}")
        added = next(
            (i for i in wl["items"] if i["tag"].upper() == item.upper()), None
        )
        name = added["name"] if added else item
        await interaction.followup.send(
            content=f"**{name}** added to your watchlist.",
            embed=_watchlist_embed(wl, uname),
            ephemeral=True,
        )
    except httpx.HTTPStatusError as exc:
        log.warning("/watch %s failed for %s — HTTP %s: %s", item, uid, exc.response.status_code, exc.response.text[:200])
        await interaction.followup.send(_http_error_detail(exc), ephemeral=True)
    except Exception as exc:
        log.warning("/watch %s failed for %s — %s: %s", item, uid, type(exc).__name__, exc)
        await interaction.followup.send("Service unavailable.", ephemeral=True)


@cmd_watch.autocomplete("item")
async def watch_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice]:
    """Autocomplete for /watch item argument."""
    return await _item_autocomplete(interaction, current)


@tree.command(
    name="unwatch",
    description="Remove an item and all its alerts from your watchlist.",
)
@app_commands.describe(item="Item tag or name.")
async def cmd_unwatch(interaction: discord.Interaction, item: str) -> None:
    """Remove an item and all its alerts from the user's watchlist.

    Args:
        interaction: The Discord interaction context.
        item: Item tag to remove.
    """
    if not _channel_guard(interaction):
        await interaction.response.send_message(
            "Not allowed in this channel.", ephemeral=True
        )
        return
    uid = interaction.user.id
    uname = interaction.user.display_name
    log.info("/unwatch %s — %s (%s)", item, uname, uid)
    await interaction.response.defer(ephemeral=True)
    try:
        wl = await _api("DELETE", f"/watch/{uid}/{item}")
        await interaction.followup.send(
            content="Removed from your watchlist.",
            embed=_watchlist_embed(wl, uname),
            ephemeral=True,
        )
    except httpx.HTTPStatusError as exc:
        log.warning("/unwatch %s failed for %s — HTTP %s: %s", item, uid, exc.response.status_code, exc.response.text[:200])
        await interaction.followup.send(_http_error_detail(exc), ephemeral=True)
    except Exception as exc:
        log.warning("/unwatch %s failed for %s — %s: %s", item, uid, type(exc).__name__, exc)
        await interaction.followup.send("Service unavailable.", ephemeral=True)


@cmd_unwatch.autocomplete("item")
async def unwatch_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice]:
    """Autocomplete for /unwatch item argument."""
    return await _watchlist_autocomplete(interaction, current)


@tree.command(name="alert", description="Set a price alert for an item.")
@app_commands.describe(
    item="Item tag or name.",
    price="Target price (e.g. 110k, 1.5m, 2b).",
)
async def cmd_alert(
    interaction: discord.Interaction, item: str, price: str
) -> None:
    """Set a price alert on an item.

    Args:
        interaction: The Discord interaction context.
        item: Item tag to alert on.
        price: Target price string.
    """
    if not _channel_guard(interaction):
        await interaction.response.send_message(
            "Not allowed in this channel.", ephemeral=True
        )
        return
    try:
        price_val = _parse_price(price)
    except ValueError:
        await interaction.response.send_message("Invalid price.", ephemeral=True)
        return
    if price_val <= 0:
        await interaction.response.send_message(
            "Price must be positive.", ephemeral=True
        )
        return
    uid = interaction.user.id
    uname = interaction.user.display_name
    log.info("/alert %s @ %s — %s (%s)", item, price_val, uname, uid)
    await interaction.response.defer(ephemeral=True)
    try:
        wl = await _api(
            "POST",
            f"/alert/{uid}/{item}",
            params={"price": price_val, "channel_id": interaction.channel_id},
        )
        item_data = next(
            (i for i in wl["items"] if i["tag"].upper() == item.upper()), None
        )
        name = item_data["name"] if item_data else item
        await interaction.followup.send(
            content=f"Alert set for **{name}** at **{_coin(price_val)}**.",
            embed=_watchlist_embed(wl, uname),
            ephemeral=True,
        )
    except httpx.HTTPStatusError as exc:
        log.warning("/alert %s failed for %s — HTTP %s: %s", item, uid, exc.response.status_code, exc.response.text[:200])
        await interaction.followup.send(_http_error_detail(exc), ephemeral=True)
    except Exception as exc:
        log.warning("/alert %s failed for %s — %s: %s", item, uid, type(exc).__name__, exc)
        await interaction.followup.send("Service unavailable.", ephemeral=True)


@cmd_alert.autocomplete("item")
async def alert_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice]:
    """Autocomplete for /alert item argument."""
    return await _item_autocomplete(interaction, current)


@tree.command(name="unalert", description="Remove price alerts from an item.")
@app_commands.describe(
    item="Item tag or name.",
    price="Specific alert price to remove (optional — omit to remove all).",
)
async def cmd_unalert(
    interaction: discord.Interaction,
    item: str,
    price: str | None = None,
) -> None:
    """Remove one or all price alerts from a watched item.

    Args:
        interaction: The Discord interaction context.
        item: Item tag to clear alerts on.
        price: Specific target price to remove; omit to clear all alerts.
    """
    if not _channel_guard(interaction):
        await interaction.response.send_message(
            "Not allowed in this channel.", ephemeral=True
        )
        return
    price_val: float | None = None
    if price is not None:
        try:
            price_val = _parse_price(price)
        except ValueError:
            await interaction.response.send_message("Invalid price.", ephemeral=True)
            return
        if price_val <= 0:
            await interaction.response.send_message(
                "Price must be positive.", ephemeral=True
            )
            return
    uid = interaction.user.id
    uname = interaction.user.display_name
    suffix = f" @ {price_val}" if price_val is not None else ""
    log.info("/unalert %s%s — %s (%s)", item, suffix, uname, uid)
    await interaction.response.defer(ephemeral=True)
    try:
        params = {"price": price_val} if price_val is not None else {}
        wl = await _api("DELETE", f"/alert/{uid}/{item}", params=params)
        msg = (
            f"Alert at **{_coin(price_val)}** removed."
            if price_val is not None
            else "All alerts cleared."
        )
        await interaction.followup.send(
            content=msg,
            embed=_watchlist_embed(wl, uname),
            ephemeral=True,
        )
    except httpx.HTTPStatusError as exc:
        log.warning("/unalert %s failed for %s — HTTP %s: %s", item, uid, exc.response.status_code, exc.response.text[:200])
        await interaction.followup.send(_http_error_detail(exc), ephemeral=True)
    except Exception as exc:
        log.warning("/unalert %s failed for %s — %s: %s", item, uid, type(exc).__name__, exc)
        await interaction.followup.send("Service unavailable.", ephemeral=True)


@cmd_unalert.autocomplete("item")
async def unalert_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice]:
    """Autocomplete for /unalert item argument."""
    return await _watchlist_autocomplete(interaction, current)


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
        except httpx.HTTPStatusError as exc:
            log.warning("alert poll error — HTTP %s: %s", exc.response.status_code, exc.response.text[:200])
            await asyncio.sleep(ALERT_POLL_SECONDS)
            continue
        except Exception as exc:
            log.warning("alert poll error — %s: %s", type(exc).__name__, exc)
            await asyncio.sleep(ALERT_POLL_SECONDS)
            continue
        for hit in hits:
            clear_path = (
                f"/notifications/{hit['discord_id']}/{hit['tag']}/{hit['direction']}"
            )
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
                await ch.send(f"{user.mention} {_alert_text(hit)}")
                await _api("DELETE", clear_path)
                log.info(
                    "alert delivered: %s → %s (%s %s)",
                    hit["name"],
                    hit["discord_id"],
                    hit["direction"],
                    hit["target"],
                )
            except (discord.NotFound, discord.Forbidden) as exc:
                log.warning(
                    "alert cleared (permanent error): %s → %s — %s: %s",
                    hit["name"],
                    hit["discord_id"],
                    type(exc).__name__,
                    exc,
                )
                try:
                    await _api("DELETE", clear_path)
                except Exception:
                    pass
            except Exception as exc:
                log.warning(
                    "alert delivery failed: %s → %s — %s: %s",
                    hit["name"],
                    hit["discord_id"],
                    type(exc).__name__,
                    exc,
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
