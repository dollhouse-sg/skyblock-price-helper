import asyncio
import logging
import os
import re
import time

import httpx

from app.models import ItemChoice, ItemPrice

log = logging.getLogger("api.logic")

_client: httpx.AsyncClient | None = None

_items: list[dict] = []
_items_loaded_at: float = 0.0
_items_lock = asyncio.Lock()

_price_cache: dict[str, tuple[float, ItemPrice]] = {}
_price_locks: dict[str, asyncio.Lock] = {}

_ITEM_CACHE_TTL = float(os.environ.get("ITEM_CACHE_TTL", "3600"))
_PRICE_CACHE_TTL = float(os.environ.get("PRICE_CACHE_TTL", "300"))
_HTTP_TIMEOUT = float(os.environ.get("HTTP_TIMEOUT", "30"))


def _get_client() -> httpx.AsyncClient:
    """Return the shared HTTP client, creating it on first call.

    Returns:
        A configured httpx.AsyncClient pointed at the SkyApi base URL.
    """
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            base_url=os.environ["SKYAPI_BASE"], timeout=_HTTP_TIMEOUT
        )
    return _client


async def close_client() -> None:
    """Close the shared HTTP client and release its resources."""
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


def clean_name(name: str) -> str:
    """Strip Minecraft color formatting codes from an item name.

    Args:
        name: Raw item name that may contain § color codes.

    Returns:
        The name with all §x sequences removed and whitespace stripped.
    """
    if not name:
        return ""
    return re.sub(r"§[0-9a-fk-or]", "", name, flags=re.IGNORECASE).strip()


def item_source(item: dict) -> str:
    """Determine the price source for an item dict from the SkyApi items list.

    Args:
        item: A raw item dict as returned by GET /api/items.

    Returns:
        "bazaar" for bazaar items, "auction" for everything else.
    """
    flags = item.get("flags")
    if isinstance(flags, str):
        flags_list = [flags]
    elif isinstance(flags, (list, tuple)):
        flags_list = [str(f) for f in flags]
    else:
        flags_list = []
    if (
        any(f.upper() == "BAZAAR" for f in flags_list)
        or item.get("bazaar") is True
        or item.get("category") == "bazaar"
    ):
        return "bazaar"
    return "auction"


async def get_items() -> list[dict]:
    """Return the full item list, refreshing from SkyApi at most once per hour.

    A stale in-memory cache is kept when the refresh request fails.

    Returns:
        List of raw item dicts from GET /api/items.
    """
    global _items, _items_loaded_at
    if time.monotonic() - _items_loaded_at < _ITEM_CACHE_TTL and _items:
        return _items
    async with _items_lock:
        if time.monotonic() - _items_loaded_at < _ITEM_CACHE_TTL and _items:
            return _items
        try:
            r = await _get_client().get("/api/items")
            r.raise_for_status()
            _items = r.json()
            _items_loaded_at = time.monotonic()
            log.info("item list refreshed (%d items)", len(_items))
        except httpx.HTTPStatusError as exc:
            log.warning(
                "item list refresh failed — HTTP %s %s: %s",
                exc.response.status_code,
                exc.request.url,
                exc.response.text[:200],
            )
        except Exception as exc:
            log.warning("item list refresh failed — %s: %s", type(exc).__name__, exc)
    return _items


async def search_items(query: str, limit: int = 25) -> list[ItemChoice]:
    """Search items by name or tag prefix.

    Args:
        query: Substring to match against item names and tags.
        limit: Maximum number of results to return.

    Returns:
        Up to limit matching ItemChoice objects.
    """
    items = await get_items()
    q = query.lower()
    matches = []
    for i in items:
        raw_name = i.get("name") or ""
        tag = i.get("tag") or ""
        name = clean_name(raw_name)
        if not name or name.lower() in ("null", "none"):
            continue
        if q in name.lower() or q in tag.lower():
            if tag:
                matches.append((tag, name, item_source(i)))
    return [
        ItemChoice(tag=tag, name=name, source=source)
        for tag, name, source in matches[:limit]
    ]


async def resolve_tag(tag: str) -> dict | None:
    """Look up a single item by its exact tag (case-insensitive).

    Args:
        tag: The unique item identifier to look up.

    Returns:
        The raw item dict, or None if the tag is not found.
    """
    items = await get_items()
    target = tag.upper()
    return next((i for i in items if (i.get("tag") or "").upper() == target), None)


async def get_price(tag: str) -> ItemPrice:
    """Return the current price for an item, using an in-memory cache.

    Args:
        tag: The unique item identifier.

    Returns:
        An ItemPrice with live or cached price data.

    Raises:
        ValueError: If tag is not found in the item list.
    """
    entry = _price_cache.get(tag)
    if entry is not None and time.monotonic() - entry[0] < _PRICE_CACHE_TTL:
        return entry[1]
    if tag not in _price_locks:
        _price_locks[tag] = asyncio.Lock()
    async with _price_locks[tag]:
        entry = _price_cache.get(tag)
        if entry is not None and time.monotonic() - entry[0] < _PRICE_CACHE_TTL:
            return entry[1]
        price = await fetch_price_fresh(tag)
        if price.status != "unknown":
            _price_cache[tag] = (time.monotonic(), price)
        return price


async def fetch_price_fresh(tag: str) -> ItemPrice:
    """Fetch live price data from the SkyApi for a single item.

    Args:
        tag: The unique item identifier.

    Returns:
        An ItemPrice populated from the live API response, or with
        buy/sell set to None and status "unknown" if the request fails.

    Raises:
        ValueError: If tag is not found in the item list.
    """
    item = await resolve_tag(tag)
    if item is None:
        raise ValueError(f"Unknown item: {tag}")
    source = item_source(item)
    name = clean_name(item.get("name") or tag) or tag
    try:
        r = await _get_client().get(f"/api/item/price/{tag}/current")
        r.raise_for_status()
        data = r.json()
        is_ah = data.get("isAh", True)
        source = "auction" if is_ah else "bazaar"
        buy_val = data.get("buy")
        sell_val = None if is_ah else data.get("sell")
        return ItemPrice(
            tag=tag,
            name=name,
            source=source,
            buy=buy_val,
            sell=sell_val,
            status="ok",
        )
    except httpx.HTTPStatusError as exc:
        log.warning(
            "price fetch failed: %s — HTTP %s %s: %s",
            tag,
            exc.response.status_code,
            exc.request.url,
            exc.response.text[:200],
        )
        return ItemPrice(
            tag=tag, name=name, source=source, buy=None, sell=None, status="unknown"
        )
    except Exception as exc:
        log.warning("price fetch failed: %s — %s: %s", tag, type(exc).__name__, exc)
        return ItemPrice(
            tag=tag, name=name, source=source, buy=None, sell=None, status="unknown"
        )
