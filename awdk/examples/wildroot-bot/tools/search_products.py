"""Product catalog search tool for Wildroot Alchemy."""

from __future__ import annotations

# Sample catalog — in production this would query a database or Shopify API
_CATALOG = [
    {"slug": "lavender-calm-tincture", "name": "Lavender Calm Tincture", "price": 24.00,
     "category": "tinctures", "ingredients": ["lavender", "chamomile", "passionflower"],
     "description": "A soothing blend for evening relaxation."},
    {"slug": "fire-cider", "name": "Fire Cider", "price": 18.00,
     "category": "tonics", "ingredients": ["apple cider vinegar", "horseradish", "ginger", "garlic", "turmeric"],
     "description": "Traditional immunity-boosting tonic with a kick."},
    {"slug": "elderberry-syrup", "name": "Elderberry Syrup", "price": 22.00,
     "category": "syrups", "ingredients": ["elderberry", "honey", "cinnamon", "clove"],
     "description": "Classic elderberry syrup for seasonal wellness."},
    {"slug": "rose-garden-salve", "name": "Rose Garden Salve", "price": 16.00,
     "category": "salves", "ingredients": ["rose", "calendula", "beeswax", "coconut oil"],
     "description": "Nourishing skin salve with rose and calendula."},
    {"slug": "mushroom-focus-blend", "name": "Mushroom Focus Blend", "price": 32.00,
     "category": "powders", "ingredients": ["lion's mane", "reishi", "chaga", "cordyceps"],
     "description": "Functional mushroom blend for cognitive support."},
]


def search_products(query: str, category: str = "") -> str:
    """Search the Wildroot Alchemy product catalog.

    Args:
        query: Search term (product name, ingredient, or keyword).
        category: Optional category filter (tinctures, tonics, syrups, salves, powders).

    Returns:
        Matching products as formatted text.
    """
    query_lower = query.lower()
    results = []

    for product in _CATALOG:
        if category and product["category"] != category.lower():
            continue
        # Match on name, description, or ingredients
        searchable = (
            product["name"].lower() + " "
            + product["description"].lower() + " "
            + " ".join(product["ingredients"])
        )
        if query_lower in searchable:
            results.append(product)

    if not results:
        return f"No products found matching '{query}'."

    lines = []
    for p in results:
        lines.append(
            f"- **{p['name']}** (${p['price']:.2f}) — {p['description']} "
            f"[{p['category']}]"
        )
    return "\n".join(lines)
