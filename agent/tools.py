"""Homework 1: the remaining commerce-agent tools.

The three lecture tools (`search_help_center`, `get_order`, `issue_refund`)
are implemented in agent/agent.py and are worked examples of the pattern:
check permissions first, go through agent/db.py for data, and return a
structured dict, never a prose error. The homework tools follow the same
pattern. agent/agent.py wraps the five required functions as SDK tools.
The additional store-policy review and current-date tools define their SDK
wrappers here and are registered by role in agent/agent.py.

Result convention (see agent/auth.py):
  - Success: a dict with "ok": True plus the payload fields named in each
    docstring.
  - Failure: {"ok": False, "error": <code>, "reason": <human-readable str>}.

Run the contract tests with: uv run pytest tests/test_hw_holes.py -k hw1
They are marked xfail and flip to passing as you implement each function.
"""

from __future__ import annotations

from typing import Any

from agents import RunContextWrapper, function_tool
from pydantic import StrictInt

from agent import db
from agent.auth import AuthContext, can_cancel_order, permission_denied
from agent.config import load_facts
from agent.helpcenter import load_policy_docs
from agent.killswitch import kill_switch
from observability.instrument import record_tool_result

MAX_SEARCH_LIMIT = 25
DEFAULT_ORDER_LIMIT = 20
STORE_REVIEW_PLATFORM_POLICY_IDS = frozenset({
    "cw-returns", "cw-restocking-fees", "cw-store-overrides",
})


@function_tool(failure_error_function=None)
def get_current_date(wrapper: RunContextWrapper[AuthContext]) -> dict[str, Any]:
    """Get today's ISO date (YYYY-MM-DD) in Cartwheel's course world.

    Use for questions about today, elapsed days, or time remaining in a return
    window. The date comes from the configured database's world_asof clock,
    which anchors the course's orders and refund rules, not the wall clock.
    Available to all roles, with no arguments or side effects. Missing or
    invalid clock configuration raises an execution error.
    """
    with db.connection() as conn:
        current_date = db.world_asof(conn).isoformat()
    result = {"ok": True, "current_date": current_date, "source": "world_asof"}
    record_tool_result(wrapper.context, result)
    return result


def get_policy(ctx: AuthContext, policy_id: str) -> dict[str, Any]:
    """Fetch one policy doc by its exact id. Risk tier: read.

    Every role may read every policy doc (the corpus is public help-center
    content), so this tool needs no permission check.

    Args:
        ctx: The caller's auth context. Unused here, but every tool takes it.
        policy_id: An exact policy id, e.g. "cw-returns" or
            "store-juniper-home-goods-policy". Matching is exact and
            case-sensitive; ids are the `policy_id` front-matter field of the
            files in data/policies/.

    Returns:
        On success: {"ok": True, "policy_id": str, "title": str,
        "audience": str, "body": str} where body is the markdown body of the
        doc without the front matter.
        If no doc has that id: {"ok": False, "error": "not_found",
        "reason": ...} naming the id that was requested.

    Implementation notes:
        agent.helpcenter.load_policy_docs() returns every parsed doc.
    """
    for doc in load_policy_docs():
        if doc.policy_id == policy_id:
            return {
                "ok": True,
                "policy_id": doc.policy_id,
                "title": doc.title,
                "audience": doc.audience,
                "body": doc.body,
            }
    return {
        "ok": False,
        "error": "not_found",
        "reason": f"no policy with id '{policy_id}'",
    }


def get_store_policies_logic(
    ctx: AuthContext, store_id: int | None = None,
) -> dict[str, Any]:
    """Review one store's return and restocking configuration (SPEC TOOL-10).

    Merchants default to their authenticated store and cannot select another.
    Support must specify a positive store ID. Reject other roles or invalid
    identities before accessing data. Invalid selectors return invalid_argument;
    an authorized lookup of an unknown store returns not_found.

    Success includes the store, configured_overrides, has_store_overrides,
    effective_rules, and citable policies sorted by policy_id, plus count.
    No override page is needed when the store uses platform defaults. Missing
    required documents return inconsistent_data; execution failures propagate.
    This tool never writes to the database or policy files.
    """
    if (
        ctx.role not in ("merchant", "support")
        or type(ctx.user_id) is not int
        or ctx.user_id <= 0
    ):
        return permission_denied(
            "Store policy review requires an authenticated merchant or support user."
        )
    if ctx.role == "merchant" and (type(ctx.store_id) is not int or ctx.store_id <= 0):
        return permission_denied("The merchant has no valid authenticated store.")
    if store_id is not None and (type(store_id) is not int or store_id <= 0):
        return {
            "ok": False, "error": "invalid_argument",
            "reason": "store_id must be a positive integer.",
        }
    target_store_id = (
        ctx.store_id if ctx.role == "merchant" and store_id is None else store_id
    )
    if target_store_id is None:
        return {
            "ok": False, "error": "invalid_argument",
            "reason": "Support must specify a store_id to review.",
        }
    if ctx.role == "merchant" and target_store_id != ctx.store_id:
        return permission_denied("Merchants may review only their authenticated store.")

    with db.connection() as conn:
        store = db.get_store(conn, target_store_id)
    if store is None:
        return {"ok": False, "error": "not_found", "reason": f"no store #{target_store_id}"}

    has_overrides = (
        store.return_window_days_override is not None or store.restocking_fee_opt_in
    )
    store_policy_id = f"store-{store.slug}-policy"
    docs = {doc.policy_id: doc for doc in load_policy_docs()}
    required_ids = set(STORE_REVIEW_PLATFORM_POLICY_IDS)
    if has_overrides:
        required_ids.add(store_policy_id)
    missing_ids = sorted(required_ids - docs.keys())
    if missing_ids:
        return {
            "ok": False, "error": "inconsistent_data",
            "reason": f"Required policy documents are missing: {', '.join(missing_ids)}.",
        }
    if store_policy_id in docs:
        required_ids.add(store_policy_id)
    policies = [
        {
            "policy_id": policy_id,
            "title": docs[policy_id].title,
            "audience": docs[policy_id].audience,
            "body": docs[policy_id].body,
            "scope": "store" if policy_id == store_policy_id else "platform",
        }
        for policy_id in sorted(required_ids)
    ]
    facts = load_facts()
    return {
        "ok": True,
        "store": {"store_id": store.id, "name": store.name, "slug": store.slug},
        "has_store_overrides": has_overrides,
        "configured_overrides": {
            "return_window_days": store.return_window_days_override,
            "restocking_fee_opt_in": store.restocking_fee_opt_in,
        },
        "effective_rules": {
            "return_window_days": (
                store.return_window_days_override
                if store.return_window_days_override is not None
                else facts["return_window_days"]
            ),
            "restocking_fee": {
                "enabled": store.restocking_fee_opt_in,
                "maximum_percent": (
                    facts["restocking_fee_max_percent"]
                    if store.restocking_fee_opt_in else 0
                ),
                "opened_items_only": facts["restocking_fee_opened_items_only"],
            },
        },
        "policies": policies,
        "count": len(policies),
    }


@function_tool(failure_error_function=None)
def get_store_policies(
    wrapper: RunContextWrapper[AuthContext], store_id: StrictInt | None = None,
) -> dict[str, Any]:
    """Review a store's configured policy overrides and effective rules.

    Returns return-window and restocking settings with full source documents
    and policy IDs to cite. Merchants may review only their own store; omit
    store_id or pass null to select it. Support must specify a store_id.
    Stores without overrides return platform defaults. Read-only.

    Args:
        store_id: Positive store identifier; optional for merchants only.
    """
    result = get_store_policies_logic(wrapper.context, store_id)
    record_tool_result(wrapper.context, result)
    return result


def search_products(
    ctx: AuthContext,
    query: str,
    store: str | None = None,
    max_price_usd: float | None = None,
    limit: int = 5,
) -> dict[str, Any]:
    """Search the product catalog. Risk tier: read.

    Every role may search products. Matching is deterministic keyword
    matching, not semantic search: a product matches when every whitespace
    token of `query` appears case-insensitively as a substring of the
    product's title or description.

    Args:
        ctx: The caller's auth context.
        query: Free-text query. Must be non-empty after stripping whitespace;
            otherwise return {"ok": False, "error": "invalid_argument",
            "reason": ...}.
        store: Optional store filter. Matched with
            agent.db.get_store_by_name (case-insensitive name or slug). If
            given and no store matches, return {"ok": False, "error":
            "not_found", "reason": ...} naming the store string.
        max_price_usd: Optional inclusive price ceiling. If given and not
            strictly positive, return an "invalid_argument" error.
        limit: Maximum products to return. Clamp to the range
            [1, MAX_SEARCH_LIMIT]; do not error on out-of-range values.

    Returns:
        {"ok": True, "products": [...], "count": <len(products)>} where each
        product is {"product_id": int, "store_id": int, "title": str,
        "price_usd": float}. Sort matches by price_usd ascending, then by
        product_id ascending, and truncate to `limit`. No matches is still a
        success: {"ok": True, "products": [], "count": 0}.

    Implementation notes:
        agent.db.list_products(conn, store_id) gives the candidate set.
        Use `with db.connection() as conn:` to close the database automatically.
    """
    tokens = query.lower().split()
    if not tokens:
        return {"ok": False, "error": "invalid_argument", "reason": "query must not be empty"}
    if max_price_usd is not None and not max_price_usd > 0:
        return {
            "ok": False,
            "error": "invalid_argument",
            "reason": "max_price_usd must be strictly positive",
        }

    with db.connection() as conn:
        store_id = None
        if store is not None:
            matched_store = db.get_store_by_name(conn, store)
            if matched_store is None:
                return {"ok": False, "error": "not_found", "reason": f"no store named '{store}'"}
            store_id = matched_store.id
        candidates = db.list_products(conn, store_id)

    matches = []
    for product in candidates:
        text = f"{product.title} {product.description}".lower()
        if not all(token in text for token in tokens):
            continue
        if max_price_usd is not None and product.price_usd > max_price_usd:
            continue
        matches.append(product)

    matches.sort(key=lambda product: (product.price_cents, product.id))
    limit = max(1, min(limit, MAX_SEARCH_LIMIT))
    products = [
        {
            "product_id": product.id,
            "store_id": product.store_id,
            "title": product.title,
            "price_usd": product.price_usd,
        }
        for product in matches[:limit]
    ]
    return {"ok": True, "products": products, "count": len(products)}


def list_my_orders(ctx: AuthContext) -> dict[str, Any]:
    """List recent orders in the caller's own scope. Risk tier: read.

    Role behavior, straight from the access matrix in SPEC.md:
        - shopper: the caller's own orders.
        - merchant: the caller's store's orders (ctx.store_id).
        - support: support staff have no orders of their own and look up
          specific orders with get_order instead, so return {"ok": False,
          "error": "invalid_argument", "reason": ...} saying exactly that.

    Returns:
        For shopper and merchant: {"ok": True, "orders": [...],
        "count": <len(orders)>} where each order is
        agent.db.Order.to_public_dict() and the list holds at most
        DEFAULT_ORDER_LIMIT orders, newest first (agent.db.list_orders_for_user
        and list_orders_for_store already sort and limit this way).

    Implementation notes:
        No permission check is needed beyond the role dispatch, because the
        scope is baked into which query you run. That is the point of the
        tool: the model cannot ask for someone else's orders through it.
    """
    if ctx.role == "support":
        return {
            "ok": False,
            "error": "invalid_argument",
            "reason": "Support staff have no orders of their own; use get_order to look up a specific order.",
        }

    with db.connection() as conn:
        if ctx.role == "shopper":
            orders = db.list_orders_for_user(conn, ctx.user_id, limit=DEFAULT_ORDER_LIMIT)
        elif ctx.role == "merchant":
            orders = db.list_orders_for_store(conn, ctx.store_id, limit=DEFAULT_ORDER_LIMIT)
        else:
            return permission_denied(f"role '{ctx.role}' may not list orders")

    public_orders = [order.to_public_dict() for order in orders]
    return {"ok": True, "orders": public_orders, "count": len(public_orders)}


def cancel_order(ctx: AuthContext, order_id: int, reason: str) -> dict[str, Any]:
    """Cancel an order. Risk tier: write.

    This is the homework's write tool, and it must enforce two independent
    rules in this order:

    1. The access matrix (scope): use agent.auth.can_cancel_order. Shoppers
       may cancel only their own orders, merchants only their own store's
       orders, support any order. On failure return
       agent.auth.permission_denied(...) with a reason naming the role and
       the order id. Scope is checked before the status rule so that an
       out-of-scope caller learns nothing about the order's state.
    2. The pre-shipment rule (facts.yaml `cancel_cutoff`): only orders whose
       status is exactly "placed" can be cancelled, for every role. If the
       order is in scope but its status is not "placed", return
       {"ok": False, "error": "not_eligible", "reason": ...} that names the
       current status and states that orders can be cancelled only before
       shipment.

    Args:
        ctx: The caller's auth context.
        order_id: The order to cancel.
        reason: Free-text reason from the user; not validated.

    Returns:
        If no order has this id: {"ok": False, "error": "not_found",
        "reason": ...}.
        On success: {"ok": True, "order_id": order_id, "status": "cancelled"}
        after persisting the new status with agent.db.set_order_status.

    Implementation notes:
        Fetch with agent.db.get_order. Note the argument order of
        can_cancel_order(ctx, order_user_id, order_store_id).

    The Module 4 kill switch is checked first (before the scope and
    status rules and before your code), so that a paused write tool touches
    nothing. It is provided; the default ("off") returns None and falls
    through to your implementation.
    """
    paused = kill_switch("cancel_order")
    if paused is not None:
        return {"ok": False, "error": "paused", "reason": paused}
    with db.connection() as conn:
        order = db.get_order(conn, order_id)
        if order is None:
            return {"ok": False, "error": "not_found", "reason": f"no order #{order_id}"}
        if not can_cancel_order(ctx, order.user_id, order.store_id):
            return permission_denied(
                f"role '{ctx.role}' may not cancel order #{order_id}"
            )
        if order.status != "placed":
            return {
                "ok": False,
                "error": "not_eligible",
                "reason": (
                    f"order #{order_id} has status '{order.status}'; "
                    "orders can be cancelled only before shipment"
                ),
            }
        db.set_order_status(conn, order_id, "cancelled")
        return {"ok": True, "order_id": order_id, "status": "cancelled"}


def find_order(ctx: AuthContext, query: str) -> dict[str, Any]:
    """Search the caller's orders by product name. Risk tier: read.

    Takes a natural-language query (e.g., "earmuffs I bought last week")
    and searches the authenticated user's orders for products whose name
    matches. Use fuzzy string matching (e.g., thefuzz.fuzz.partial_ratio
    or case-insensitive substring matching) to find orders whose product name is close to the
    query.

    Access rules: a shopper searches only the shopper's own orders, a
    merchant searches orders from the merchant's store, and support staff
    can search any orders. Use agent.db.list_order_search_candidates with
    user_id=ctx.user_id for shoppers, store_id=ctx.store_id for merchants,
    or all_orders=True only for support. Derive the scope from ctx, never
    from the query; reject unsupported roles or missing required identity.
    Use agent.db.list_products to map product IDs to product titles.

    The helper returns the complete authorised scope, newest first with
    order ID descending as the tie-breaker. Match product names first,
    preserve that order, then return at most five matches. Do not search
    only the 20 most recent orders. Convert matches with to_public_dict().

    Args:
        ctx: The caller's auth context.
        query: A natural-language description of the product.

    Returns:
        {"ok": True, "orders": [...]} with a list of matching orders
        (at most 5), each as the dict returned by agent.db. If no orders
        match, return {"ok": True, "orders": []}.
    """
    if ctx.role not in ("shopper", "merchant", "support"):
        return permission_denied(f"role '{ctx.role}' may not search orders")
    if ctx.user_id is None or (ctx.role == "merchant" and ctx.store_id is None):
        return permission_denied(f"role '{ctx.role}' is missing the required identity")

    query = query.strip().casefold()
    if not query:
        return {"ok": True, "orders": []}

    with db.connection() as conn:
        if ctx.role == "shopper":
            candidates = db.list_order_search_candidates(conn, user_id=ctx.user_id)
        elif ctx.role == "merchant":
            candidates = db.list_order_search_candidates(conn, store_id=ctx.store_id)
        else:
            candidates = db.list_order_search_candidates(conn, all_orders=True)
        titles = {
            product.id: product.title.strip().casefold()
            for product in db.list_products(conn)
        }

    matches = []
    for order in candidates:
        title = titles.get(order.product_id, "")
        if title and (query in title or title in query):
            matches.append(order.to_public_dict())
            if len(matches) == 5:
                break
    return {"ok": True, "orders": matches}
