"""Character inventory arithmetic — issues #463, #464.

Code owns coin math and the sheet's item list: loot boxes add to them,
purchases pay from them. Coins live on the sheet's ``cp``/``sp``/``ep``/
``gp``/``pp`` columns, items in ``equipment``.
"""

from __future__ import annotations

from typing import Any

#: Coin values in copper, smallest first (2024: 1 ep = 5 sp).
COIN_CP = {"cp": 1, "sp": 10, "ep": 50, "gp": 100, "pp": 1000}


class InventoryError(ValueError):
    """A refused inventory change; ``message`` is DM- or player-facing."""


def wallet(sheet: Any) -> dict[str, int]:
    return {coin: int(getattr(sheet, coin) or 0) for coin in COIN_CP}


def wallet_cp(sheet: Any) -> int:
    return sum(count * COIN_CP[coin] for coin, count in wallet(sheet).items())


def describe_cp(amount: int) -> str:
    """``1234`` -> ``"12 gp, 3 sp, 4 cp"`` (gold, silver, copper only)."""
    gp, rest = divmod(int(amount), 100)
    sp, cp = divmod(rest, 10)
    parts = [f"{n} {c}" for n, c in ((gp, "gp"), (sp, "sp"), (cp, "cp")) if n]
    return ", ".join(parts) or "0 cp"


def pay(sheet: Any, price_cp: int) -> dict[str, int]:
    """Pay ``price_cp`` from the sheet's coins, smallest coins first; returns coins spent.

    When the coins left over are all larger than what is still owed, one
    coin is broken and the change comes back in gold, silver, and copper.
    """
    price_cp = int(price_cp)
    if price_cp < 0:
        raise InventoryError("a price cannot be negative")
    have = wallet(sheet)
    if wallet_cp(sheet) < price_cp:
        raise InventoryError(f"not enough coin: costs {describe_cp(price_cp)}, has {describe_cp(wallet_cp(sheet))}")
    owed = price_cp
    spent = {coin: 0 for coin in COIN_CP}
    for coin, value in COIN_CP.items():
        use = min(have[coin], owed // value)
        have[coin] -= use
        spent[coin] += use
        owed -= use * value
    if owed:
        coin = next(c for c, v in COIN_CP.items() if have[c] and v > owed)
        have[coin] -= 1
        spent[coin] += 1
        change = COIN_CP[coin] - owed
        for back in ("gp", "sp", "cp"):
            count, change = divmod(change, COIN_CP[back])
            have[back] += count
    for coin, count in have.items():
        setattr(sheet, coin, count)
    return spent


def add_items(sheet: Any, items: list[dict[str, Any]], *, source: str) -> None:
    """Add items to the sheet's equipment, stacking identical (name, rarity, kind) entries."""
    equipment = [dict(e) for e in (sheet.equipment or []) if isinstance(e, dict)]
    for item in items:
        rarity, kind = item.get("rarity") or "common", item.get("kind") or "gear"
        match = next(
            (e for e in equipment if e.get("name") == item["name"] and (e.get("rarity") or "common") == rarity
             and (e.get("kind") or "gear") == kind),
            None,
        )
        if match is not None:
            match["quantity"] = int(match.get("quantity") or 1) + int(item.get("quantity") or 1)
            continue
        equipment.append({
            "name": item["name"], "quantity": int(item.get("quantity") or 1), "rarity": rarity,
            "kind": kind, "description": item.get("description") or "", "source": source,
        })
    sheet.equipment = equipment
