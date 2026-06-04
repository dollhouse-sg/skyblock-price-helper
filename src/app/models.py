from pydantic import BaseModel


class ItemChoice(BaseModel):
    """A search result returned from the items endpoint.

    Attributes:
        tag: Unique item identifier used in API calls.
        name: Human-readable display name.
        source: Price source, either "bazaar" or "auction".
    """

    tag: str
    name: str
    source: str


class ItemPrice(BaseModel):
    """Current price data for a single item.

    Attributes:
        tag: Unique item identifier.
        name: Human-readable display name.
        source: Price source, either "bazaar" or "auction".
        buy: Insta-buy price (bazaar) or lowest BIN (auction).
        sell: Insta-sell price (bazaar only); None for auctions.
        status: "ok" when prices are available, "unknown" otherwise.
    """

    tag: str
    name: str
    source: str
    buy: float | None
    sell: float | None
    status: str


class WatchedItem(BaseModel):
    """A single item on a user's watchlist, enriched with live prices.

    Attributes:
        tag: Unique item identifier.
        name: Human-readable display name.
        source: Price source, either "bazaar" or "auction".
        target_above: Alert target for an "above" price crossing; None if unset.
        target_below: Alert target for a "below" price crossing; None if unset.
        buy: Current buy price.
        sell: Current sell price.
        status: "ok" or "unknown".
    """

    tag: str
    name: str
    source: str
    target_above: float | None
    target_below: float | None
    buy: float | None
    sell: float | None
    status: str


class Watchlist(BaseModel):
    """A user's full watchlist with live prices attached.

    Attributes:
        discord_id: The owning Discord user's snowflake ID.
        items: Ordered list of watched items.
        action: Human-readable description of the change that produced this
            response (e.g. "added", "removed", "alert_set", "alert_cleared").
            None for plain fetch responses.
    """

    discord_id: int
    items: list[WatchedItem]
    action: str | None = None


class Triggered(BaseModel):
    """Payload returned when a price alert has crossed its target.

    Attributes:
        discord_id: The Discord user whose alert fired.
        tag: Item identifier.
        name: Human-readable display name.
        source: Price source, either "bazaar" or "auction".
        buy: Current buy price at the moment the alert fired.
        sell: Current sell price at the moment the alert fired.
        direction: The direction ("above" or "below") that triggered.
        channel_id: Discord channel to post the alert notification in.
        target: The target price that was crossed.
    """

    discord_id: int
    tag: str
    name: str
    source: str
    buy: float | None
    sell: float | None
    direction: str
    channel_id: int
    target: float
