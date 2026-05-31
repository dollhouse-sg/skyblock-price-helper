import asyncio
import os

import asyncpg

_pool: asyncpg.Pool | None = None
_pool_lock = asyncio.Lock()

WATCHLIST_LIMIT = int(os.environ.get("WATCHLIST_LIMIT", "10"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    discord_id BIGINT PRIMARY KEY
);
CREATE TABLE IF NOT EXISTS watched_items (
    discord_id       BIGINT REFERENCES users(discord_id) ON DELETE CASCADE,
    tag              TEXT   NOT NULL,
    name             TEXT   NOT NULL,
    source           TEXT   NOT NULL,
    target_above     FLOAT8,
    channel_id_above BIGINT,
    target_below     FLOAT8,
    channel_id_below BIGINT,
    PRIMARY KEY (discord_id, tag)
);
ALTER TABLE watched_items
    ADD COLUMN IF NOT EXISTS target_above     FLOAT8,
    ADD COLUMN IF NOT EXISTS channel_id_above BIGINT,
    ADD COLUMN IF NOT EXISTS target_below     FLOAT8,
    ADD COLUMN IF NOT EXISTS channel_id_below BIGINT;
ALTER TABLE watched_items
    ALTER COLUMN target_above TYPE FLOAT8,
    ALTER COLUMN target_below TYPE FLOAT8;
"""


async def get_pool() -> asyncpg.Pool:
    """Return the shared connection pool, creating it on first call.

    Also applies the database schema on first connection.

    Returns:
        The live asyncpg.Pool instance.
    """
    global _pool
    if _pool is not None:
        return _pool
    async with _pool_lock:
        if _pool is None:
            user = os.environ["POSTGRES_USER"]
            password = os.environ["POSTGRES_PASSWORD"]
            host = os.environ["POSTGRES_HOST"]
            port = os.environ["POSTGRES_PORT"]
            db = os.environ["POSTGRES_DB"]
            dsn = f"postgresql://{user}:{password}@{host}:{port}/{db}"
            pool = await asyncpg.create_pool(dsn)
            async with pool.acquire() as conn:
                await conn.execute(SCHEMA)
            _pool = pool
    return _pool


async def fetch_watchlist(discord_id: int) -> list[asyncpg.Record]:
    """Fetch all watched items for a user.

    Args:
        discord_id: The Discord user's snowflake ID.

    Returns:
        A list of database rows from watched_items.
    """
    pool = await get_pool()
    return await pool.fetch(
        "SELECT tag, name, source, target_above, target_below"
        " FROM watched_items WHERE discord_id=$1",
        discord_id,
    )


async def toggle_item(discord_id: int, tag: str, name: str, source: str) -> bool:
    """Add an item to the watchlist, or remove it if already present.

    Args:
        discord_id: The Discord user's snowflake ID.
        tag: Unique item identifier.
        name: Human-readable display name.
        source: Price source ("bazaar" or "auction").

    Returns:
        True if the item was added, False if it was removed.

    Raises:
        ValueError: If the item has active alerts (must be cleared first).
        ValueError: If the watchlist is already at its item limit.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO users(discord_id) VALUES($1) ON CONFLICT DO NOTHING",
            discord_id,
        )
        async with conn.transaction():
            existing = await conn.fetchrow(
                "SELECT target_above, target_below"
                " FROM watched_items WHERE discord_id=$1 AND tag=$2",
                discord_id,
                tag,
            )
            if existing:
                if (
                    existing["target_above"] is not None
                    or existing["target_below"] is not None
                ):
                    raise ValueError(
                        "Remove all alerts on this item before removing"
                        " it from your watchlist."
                    )
                await conn.execute(
                    "DELETE FROM watched_items WHERE discord_id=$1 AND tag=$2",
                    discord_id,
                    tag,
                )
                return False
            count = await conn.fetchval(
                "SELECT COUNT(*) FROM watched_items WHERE discord_id=$1", discord_id
            )
            if count >= WATCHLIST_LIMIT:
                raise ValueError(f"Watchlist is full ({WATCHLIST_LIMIT} items max).")
            await conn.execute(
                "INSERT INTO watched_items(discord_id,tag,name,source)"
                " VALUES($1,$2,$3,$4)",
                discord_id,
                tag,
                name,
                source,
            )
            return True


async def set_notify(
    discord_id: int,
    tag: str,
    name: str,
    source: str,
    price: float,
    channel_id: int,
    direction: str,
) -> None:
    """Set or overwrite a price alert for one direction on a watched item.

    Adds the item to the watchlist first if it is not already present.
    Setting an alert for one direction leaves the other direction untouched.

    Args:
        discord_id: The Discord user's snowflake ID.
        tag: Unique item identifier.
        name: Human-readable display name.
        source: Price source ("bazaar" or "auction").
        price: Target price that triggers the alert.
        channel_id: Discord channel to post the fired alert in.
        direction: "above" or "below".

    Raises:
        ValueError: If the watchlist is full and the item is not yet in it.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO users(discord_id) VALUES($1) ON CONFLICT DO NOTHING",
            discord_id,
        )
        async with conn.transaction():
            existing = await conn.fetchrow(
                "SELECT 1 FROM watched_items WHERE discord_id=$1 AND tag=$2",
                discord_id,
                tag,
            )
            if not existing:
                count = await conn.fetchval(
                    "SELECT COUNT(*) FROM watched_items WHERE discord_id=$1", discord_id
                )
                if count >= WATCHLIST_LIMIT:
                    raise ValueError(
                        f"Watchlist is full ({WATCHLIST_LIMIT} items max)."
                    )
                await conn.execute(
                    "INSERT INTO watched_items(discord_id,tag,name,source)"
                    " VALUES($1,$2,$3,$4) ON CONFLICT DO NOTHING",
                    discord_id,
                    tag,
                    name,
                    source,
                )
            if direction == "above":
                await conn.execute(
                    """UPDATE watched_items
                       SET target_above=$3, channel_id_above=$4
                       WHERE discord_id=$1 AND tag=$2""",
                    discord_id,
                    tag,
                    price,
                    channel_id,
                )
            else:
                await conn.execute(
                    """UPDATE watched_items
                       SET target_below=$3, channel_id_below=$4
                       WHERE discord_id=$1 AND tag=$2""",
                    discord_id,
                    tag,
                    price,
                    channel_id,
                )


async def fetch_alerts() -> list[asyncpg.Record]:
    """Fetch one record per active price alert across all users.

    Returns two rows per item that has both directions set.

    Returns:
        Records with columns: discord_id, tag, name, source, direction,
        target, channel_id.
    """
    pool = await get_pool()
    return await pool.fetch(
        """
        SELECT discord_id, tag, name, source,
               'above'         AS direction,
               target_above    AS target,
               channel_id_above AS channel_id
        FROM watched_items WHERE target_above IS NOT NULL
        UNION ALL
        SELECT discord_id, tag, name, source,
               'below'         AS direction,
               target_below    AS target,
               channel_id_below AS channel_id
        FROM watched_items WHERE target_below IS NOT NULL
        """
    )


async def clear_alert(discord_id: int, tag: str, direction: str) -> None:
    """Clear one directional alert from a watched item.

    Args:
        discord_id: The Discord user's snowflake ID.
        tag: Unique item identifier.
        direction: "above" or "below" — only that slot is cleared.
    """
    pool = await get_pool()
    if direction == "above":
        await pool.execute(
            """UPDATE watched_items
               SET target_above=NULL, channel_id_above=NULL
               WHERE discord_id=$1 AND tag=$2""",
            discord_id,
            tag,
        )
    else:
        await pool.execute(
            """UPDATE watched_items
               SET target_below=NULL, channel_id_below=NULL
               WHERE discord_id=$1 AND tag=$2""",
            discord_id,
            tag,
        )
