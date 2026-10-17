"""Offline search contracts exercised against a temporary course database."""

import pytest

from agent import db, tools
from agent.auth import AuthContext


SHOPPER = AuthContext(user_id=1, role="shopper")


@pytest.fixture
def catalog(world_copy):
    with db.connection() as conn:
        conn.execute(
            "UPDATE products SET title = 'FixtureWidget', "
            "description = 'Blue finish', price_cents = 100000 WHERE store_id = 1"
        )
        conn.executemany(
            "UPDATE products SET title = ?, description = ?, price_cents = ? WHERE id = ?",
            [
                ("FixtureWidget", "Blue finish", 2000, 1),
                ("Blue FixtureWidget", "", 1500, 2),
                ("FixtureWidget", "blue finish", 1500, 3),
                ("FixtureWidget", "Red finish", 100, 4),
                ("Blue FixtureWidget", "", 100, 41),  # Store 2: exclude this bargain.
            ],
        )
        conn.commit()


def test_no_products_within_budget_is_success(catalog):
    result = tools.search_products(
        SHOPPER, "fixturewidget blue", store="Blue Heron Ceramics", max_price_usd=14
    )
    assert result == {"ok": True, "products": [], "count": 0}


def test_all_keywords_store_and_inclusive_price_filter(catalog):
    result = tools.search_products(
        SHOPPER, "  FIXTUREWIDGET   BLUE  ",
        store="BLUE-HERON-CERAMICS", max_price_usd=15,
    )
    assert result == {
        "ok": True,
        "products": [
            {"product_id": 2, "store_id": 1, "title": "Blue FixtureWidget", "price_usd": 15.0},
            {"product_id": 3, "store_id": 1, "title": "FixtureWidget", "price_usd": 15.0},
        ],
        "count": 2,
    }


@pytest.mark.parametrize("limit, expected_ids", [(0, [2]), (2, [2, 3]), (5, [2, 3, 1])])
def test_sort_by_price_then_id_before_applying_limit(catalog, limit, expected_ids):
    result = tools.search_products(
        SHOPPER, "fixturewidget blue", store="Blue Heron Ceramics",
        max_price_usd=20, limit=limit,
    )
    assert result["ok"] is True
    assert [product["product_id"] for product in result["products"]] == expected_ids
    assert result["count"] == len(expected_ids)


def test_limit_is_capped_at_25(catalog):
    result = tools.search_products(
        SHOPPER, "fixturewidget blue", store="Blue Heron Ceramics", limit=100
    )
    assert result["ok"] is True
    assert result["count"] == len(result["products"]) == 25


@pytest.mark.parametrize("ceiling", [0, -1, float("nan")])
def test_price_ceiling_must_be_strictly_positive(world, ceiling):
    result = tools.search_products(SHOPPER, "mug", max_price_usd=ceiling)
    assert result["ok"] is False
    assert result["error"] == "invalid_argument"
    assert result["reason"]
