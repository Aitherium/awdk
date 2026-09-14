"""Inventory lookup tool for Wildroot Alchemy."""

from __future__ import annotations

# Sample inventory — in production this would query the database
_INVENTORY = {
    "lavender-calm-tincture": {"in_stock": True, "quantity": 24, "restock_date": None},
    "fire-cider": {"in_stock": True, "quantity": 8, "restock_date": None},
    "elderberry-syrup": {"in_stock": False, "quantity": 0, "restock_date": "2026-06-01"},
    "rose-garden-salve": {"in_stock": True, "quantity": 15, "restock_date": None},
    "mushroom-focus-blend": {"in_stock": True, "quantity": 3, "restock_date": None},
}


def check_inventory(product_slug: str) -> str:
    """Check inventory status for a product.

    Args:
        product_slug: The product slug (e.g. 'fire-cider', 'elderberry-syrup').

    Returns:
        Inventory status as formatted text.
    """
    slug = product_slug.lower().strip()
    inv = _INVENTORY.get(slug)

    if inv is None:
        return f"Product '{product_slug}' not found in inventory."

    if inv["in_stock"]:
        status = f"In stock ({inv['quantity']} available)"
        if inv["quantity"] <= 5:
            status += " — low stock, order soon!"
    else:
        status = "Out of stock"
        if inv["restock_date"]:
            status += f" — expected restock: {inv['restock_date']}"

    return f"{product_slug}: {status}"
