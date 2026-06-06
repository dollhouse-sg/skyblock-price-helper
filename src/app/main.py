import asyncio
from contextlib import asynccontextmanager
from typing import Annotated, AsyncGenerator

from fastapi import FastAPI, HTTPException, Path, Query

from app import logic, models, postgres
from app.log import setup as _setup_logging

log = _setup_logging("api")

_DiscordId = Annotated[int, Path(gt=0)]
_Tag = Annotated[str, Path(min_length=1, max_length=100)]
_Direction = Annotated[str, Path(pattern="^(above|below)$")]


def _price_reference(source: str, direction: str) -> str:
    return "sell" if source == "bazaar" and direction == "below" else "buy"


async def _retry_pool(retries: int = 10, delay: float = 3.0) -> None:
    """Attempt to connect to Postgres, retrying on failure.

    Args:
        retries: Maximum number of connection attempts.
        delay: Seconds to wait between attempts.

    Raises:
        RuntimeError: If all attempts are exhausted without a successful connection.
    """
    for attempt in range(retries):
        try:
            await postgres.get_pool()
            return
        except Exception as exc:
            log.warning(
                "postgres connection attempt %d/%d failed — %s: %s",
                attempt + 1,
                retries,
                type(exc).__name__,
                exc,
            )
            await asyncio.sleep(delay)
    raise RuntimeError("Could not connect to Postgres.")


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncGenerator[None, None]:
    """Manage startup and shutdown of shared resources.

    On startup: establishes the Postgres connection pool and pre-warms the
    item list cache. On shutdown: closes the pool and the outbound HTTP client.
    """
    await _retry_pool()
    await logic.get_items()
    log.info("startup complete")
    yield
    pool = await postgres.get_pool()
    await pool.close()
    await logic.close_client()
    log.info("shutdown complete")


app = FastAPI(
    lifespan=lifespan,
    title="skyblock-bot",
    version="0.1.0",
)


@app.get("/items", response_model=list[models.ItemChoice])
async def items(
    query: Annotated[str, Query(max_length=200)] = "",
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
) -> list[models.ItemChoice]:
    """Search for items by name or tag.

    Args:
        query: Substring to match against item names and tags.
        limit: Maximum number of results (default 25).

    Returns:
        Matching ItemChoice objects.
    """
    return await logic.search_items(query, limit)


@app.get("/price/{tag}", response_model=models.ItemPrice)
async def price(tag: _Tag) -> models.ItemPrice:
    """Return the current price for a single item.

    Args:
        tag: The unique item identifier.

    Returns:
        Live price data.

    Raises:
        HTTPException: 404 if the tag is not recognised.
    """
    try:
        return await logic.get_price(tag)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.get("/watchlist/{discord_id}", response_model=models.Watchlist)
async def get_watchlist(discord_id: _DiscordId) -> models.Watchlist:
    """Return a user's full watchlist with live prices.

    Args:
        discord_id: The Discord user's snowflake ID.

    Returns:
        The user's Watchlist.
    """
    rows = await postgres.fetch_watchlist(discord_id)
    return models.Watchlist(discord_id=discord_id, items=await _enrich(rows))


@app.post("/watch/{discord_id}/{tag}", response_model=models.Watchlist)
async def add_watch(discord_id: _DiscordId, tag: _Tag) -> models.Watchlist:
    """Add an item to the watchlist.

    Args:
        discord_id: The Discord user's snowflake ID.
        tag: The unique item identifier.

    Returns:
        The updated Watchlist.

    Raises:
        HTTPException: 404 if the tag is unknown; 400 if already present or
            the watchlist limit is reached.
    """
    item = await logic.resolve_tag(tag)
    if item is None:
        raise HTTPException(404, f"Unknown item: {tag}")
    source = logic.item_source(item)
    name = logic.clean_name(item.get("name") or tag) or tag
    try:
        await postgres.add_item(discord_id, tag, name, source)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    rows = await postgres.fetch_watchlist(discord_id)
    return models.Watchlist(discord_id=discord_id, items=await _enrich(rows))


@app.delete("/watch/{discord_id}/{tag}", response_model=models.Watchlist)
async def remove_watch(discord_id: _DiscordId, tag: _Tag) -> models.Watchlist:
    """Remove an item and all its alerts from the watchlist.

    Args:
        discord_id: The Discord user's snowflake ID.
        tag: The unique item identifier.

    Returns:
        The updated Watchlist.

    Raises:
        HTTPException: 404 if the item is not in the watchlist.
    """
    try:
        await postgres.remove_item(discord_id, tag)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    rows = await postgres.fetch_watchlist(discord_id)
    return models.Watchlist(discord_id=discord_id, items=await _enrich(rows))


@app.post("/alert/{discord_id}/{tag}", response_model=models.Watchlist)
async def add_alert(
    discord_id: _DiscordId,
    tag: _Tag,
    price: Annotated[float, Query(gt=0, le=1e18)],
    channel_id: Annotated[int, Query(gt=0)],
) -> models.Watchlist:
    """Set a price alert on an item, adding it to the watchlist if needed.

    Direction (above/below) is determined automatically from the current price.

    Args:
        discord_id: The Discord user's snowflake ID.
        tag: The unique item identifier.
        price: Target price that triggers the alert.
        channel_id: Discord channel for the alert notification.

    Returns:
        The updated Watchlist.

    Raises:
        HTTPException: 404 if the tag is unknown; 400 for validation errors.
    """
    item = await logic.resolve_tag(tag)
    if item is None:
        raise HTTPException(404, f"Unknown item: {tag}")
    source = logic.item_source(item)
    name = logic.clean_name(item.get("name") or tag) or tag
    current_price = await logic.get_price(tag)
    if current_price.buy is None:
        raise HTTPException(400, "Cannot set alert: price is currently unknown.")
    if source == "bazaar":
        if current_price.sell is None:
            raise HTTPException(400, "Cannot set alert: price is currently unknown.")
        low, high = current_price.sell, current_price.buy
        if low <= price <= high:
            raise HTTPException(
                400,
                f"Target must be below {low:,.2f} or above {high:,.2f}.",
            )
        direction = "above" if price > high else "below"
    else:
        direction = "above" if price > current_price.buy else "below"
        if direction == "below" and current_price.buy <= price:
            raise HTTPException(400, "Price has already crossed that target.")
    try:
        await postgres.set_alert(
            discord_id, tag, name, source, price, channel_id, direction
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    rows = await postgres.fetch_watchlist(discord_id)
    return models.Watchlist(discord_id=discord_id, items=await _enrich(rows))


@app.delete("/alert/{discord_id}/{tag}", response_model=models.Watchlist)
async def remove_alert(
    discord_id: _DiscordId,
    tag: _Tag,
    price: Annotated[float | None, Query(gt=0, le=1e18)] = None,
) -> models.Watchlist:
    """Remove price alerts from a watched item.

    Args:
        discord_id: The Discord user's snowflake ID.
        tag: The unique item identifier.
        price: Specific alert price to remove; omit to clear all alerts.

    Returns:
        The updated Watchlist.

    Raises:
        HTTPException: 404 if the item is not in the watchlist or no alert
            matched the given price.
    """
    try:
        found = await postgres.clear_alerts(discord_id, tag, price)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    if price is not None and not found:
        raise HTTPException(404, "No alert found at that price.")
    rows = await postgres.fetch_watchlist(discord_id)
    return models.Watchlist(discord_id=discord_id, items=await _enrich(rows))


@app.get("/notifications/triggered", response_model=list[models.Triggered])
async def triggered() -> list[models.Triggered]:
    """Return all alerts whose price condition is currently met.

    Alerts are not cleared here. The bot calls
    DELETE /notifications/{discord_id}/{tag}/{direction} after each
    notification is successfully delivered, so a failed delivery is
    automatically retried on the next poll cycle.

    Returns:
        Triggered payloads for alerts that have crossed their target.
    """
    rows = await postgres.fetch_alerts()
    if not rows:
        return []

    async def _fetch_safe(tag: str) -> models.ItemPrice | None:
        try:
            p = await logic.fetch_price_fresh(tag)
            return None if p.status == "unknown" else p
        except Exception as exc:
            log.warning("alert price fetch failed: %s — %s: %s", tag, type(exc).__name__, exc)
            return None

    prices = await asyncio.gather(*[_fetch_safe(row["tag"]) for row in rows])
    results: list[models.Triggered] = []
    for row, p in zip(rows, prices):
        if p is None:
            continue
        ref = _price_reference(p.source, row["direction"])
        current = p.buy if ref == "buy" else p.sell
        if current is None:
            continue
        fired = (row["direction"] == "above" and current >= row["target"]) or (
            row["direction"] == "below" and current <= row["target"]
        )
        if fired:
            results.append(
                models.Triggered(
                    discord_id=row["discord_id"],
                    tag=row["tag"],
                    name=row["name"],
                    source=p.source,
                    buy=p.buy,
                    sell=p.sell,
                    direction=row["direction"],
                    channel_id=row["channel_id"],
                    target=row["target"],
                )
            )
    if results:
        log.info(
            "%d alert(s) fired: %s",
            len(results),
            ", ".join(f"{r.name} → {r.discord_id}" for r in results),
        )
    return results


@app.delete("/notifications/{discord_id}/{tag}/{direction}", status_code=204)
async def clear_notification(
    discord_id: _DiscordId, tag: _Tag, direction: _Direction
) -> None:
    """Clear a fired alert after the bot has successfully delivered it.

    Args:
        discord_id: The Discord user's snowflake ID.
        tag: Unique item identifier.
        direction: "above" or "below".
    """
    await postgres.clear_alert(discord_id, tag, direction)
    log.info("alert cleared (%s): %s → %s", direction, tag, discord_id)


async def _enrich(rows: list) -> list[models.WatchedItem]:
    """Attach live prices to a list of watchlist database rows.

    Fetches all prices concurrently. Items whose price cannot be fetched are
    included with buy/sell set to None and status "unknown".

    Args:
        rows: Raw database rows from watched_items.

    Returns:
        WatchedItem objects with live price data attached.
    """

    async def _fetch_one(row) -> models.WatchedItem:
        try:
            p = await logic.get_price(row["tag"])
            buy, sell, status, source = p.buy, p.sell, p.status, p.source
        except Exception as exc:
            log.warning("watchlist price fetch failed: %s — %s: %s", row["tag"], type(exc).__name__, exc)
            buy = sell = None
            status = "unknown"
            source = row["source"]
        return models.WatchedItem(
            tag=row["tag"],
            name=row["name"],
            source=source,
            target_above=row["target_above"],
            target_below=row["target_below"],
            buy=buy,
            sell=sell,
            status=status,
        )

    return list(await asyncio.gather(*[_fetch_one(row) for row in rows]))
