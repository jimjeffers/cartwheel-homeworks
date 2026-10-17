# Cartwheel support agent: specification

The specification is the source of intended behavior for the Cartwheel
support agent. The application does not read the Markdown file at runtime.
Developers translate its requirements into model instructions, tool code,
authorization checks, and tests. Scenario generation later uses the same
requirements to decide which situations the agent must encounter.

## How the specification enters the application

| Specification content | Implementation location | Reason |
| --- | --- | --- |
| Supported and refused requests | `SYSTEM_PROMPT_TEMPLATE` in `agent/agent.py` | The model must decide whether to answer, use a tool, or refuse. |
| Guidance about tool choice and policy citations | `SYSTEM_PROMPT_TEMPLATE` in `agent/agent.py` | The model chooses the next tool and writes the response. |
| Role permissions | `agent/auth.py` and each tool function | Authorization must remain correct even when the model makes a poor decision. |
| Refund eligibility and approval threshold | `seed/eligibility.py`, `facts.yaml`, and the refund tool | Deterministic code can enforce the rule exactly. |
| Escalation requirements | The system prompt and `escalate_to_human` | The model chooses escalation, while code creates the ticket. |
| Expected behavior in evaluation scenarios | `scenarios/*.jsonl` | A scenario cites the requirement or deterministic rule used to judge the run. |

The system prompt is therefore one implementation of part of the
specification. Copying the entire specification into the prompt would be
insufficient, because a prompt cannot enforce access control or validate a
refund.

## 1. Purpose

**PURPOSE-1.** The agent is Cartwheel's support assistant. It answers shopper, merchant, and
support staff questions about orders, returns, refunds, products, and platform
policy. It acts through tools, cites policy documents for every policy claim,
and escalates risky or unclear cases to a human.

## 2. Scope

**SCOPE-1.** The agent supports:

- Order status lookups.
- Returns and refunds, within the access matrix and the eligibility rules.
- Product and policy questions, answered from the help center.
- Escalation to a human for anything above its authority.

**SCOPE-2.** The agent refuses:

- Legal advice.
- Payment-card changes or any payment-credential handling.
- Anything outside Cartwheel (general web questions, other companies).

## 3. Roles and permissions

**AUTH-1.** The harness enforces the following matrix in the tool layer. The model never sees rows
outside the caller's role. Authorization is not a prompt.

| Capability | Shopper | Merchant | Support |
| --- | --- | --- | --- |
| View own orders | yes | no | any order |
| View store's orders | no | own store only | any store |
| Review store policy configuration | no | own store only | any store |
| Search products / policies | yes | yes | yes |
| Read the current course date | yes | yes | yes |
| Issue refund | own orders, <= threshold | own store's orders, <= threshold | any, <= threshold |
| Cancel order | own, pre-shipment | own store's | any |
| Above-threshold refund | queued for human | queued for human | queued for human |

The threshold is `refund_auto_approve_threshold_usd` in `facts.yaml` ($100).

The store policy configuration restriction applies to `get_store_policies`.
Published help-center policies, including store policy pages, remain public through
`get_policy` and `search_help_center` for every role. Access to the review tool does
not determine access to those published documents.

## 4. Tools

Successful results contain `ok: true` and the result fields. Expected failures contain `ok: false`, an `error` code, and a human-readable `reason`. Unexpected execution failures raise exceptions.

| ID | Tool | Inputs | Side effects | Risk |
| --- | --- | --- | --- | --- |
| TOOL-1 | `search_help_center` | query | none | read |
| TOOL-2 | `get_policy` | policy identifier | none | read |
| TOOL-3 | `search_products` | query, optional store and price ceiling, result limit | none | read |
| TOOL-4 | `get_order` | order identifier | none | read |
| TOOL-5 | `list_my_orders` | none | none | read |
| TOOL-6 | `find_order` | natural-language product description | none | read |
| TOOL-7 | `issue_refund` | order identifier, amount, reason | creates a refund record; marks the order refunded only for an automatically approved refund | write |
| TOOL-8 | `cancel_order` | order identifier, reason | marks an eligible order cancelled | write |
| TOOL-9 | `escalate_to_human` | summary, context | creates a support ticket | write |
| TOOL-10 | `get_store_policies` | optional store identifier | none | read |
| TOOL-11 | `get_current_date` | none | none | read |

### Success and failure contracts

| Tool | On success | On failure |
| --- | --- | --- |
| `search_help_center` | `results` containing policy identifiers, titles, snippets, and retrieval scores. | `invalid_argument` for an empty or whitespace-only query; execution exception if retrieval fails. |
| `get_policy` | `policy_id`, `title`, `audience`, and the full `body` of the requested policy. | `not_found` for an unknown policy identifier. |
| `search_products` | `products` and `count`, filtered and sorted by price, then product identifier. Each product includes its identifier, store identifier, title, and price. The result limit is clamped to 1 through 25. No matches yields an empty list and count zero. | `invalid_argument` for an empty query or a nonpositive price ceiling; `not_found` for an unknown store. |
| `get_order` | An authorized `order` record, including dates, status, store name, and refund eligibility. | `not_found` for an unknown order; `permission_denied` for an order outside the caller's scope. |
| `list_my_orders` | `orders` and `count` for the shopper's own orders or the merchant's store, newest first, with at most 20 records. No orders yields an empty list and count zero. | `invalid_argument` for a support caller; execution exception if the database query fails. |
| `find_order` | Up to five fuzzy product-name matches in `orders`, scoped to the shopper, merchant store, or authorized support caller. No matches yields an empty list. | Execution exception if search or database access fails. |
| `issue_refund` | `refund_id`, `order_id`, `amount_usd`, and `status`. Status is `auto_approved` at or below the threshold and `queued_for_approval` above it. | `invalid_argument` for a nonpositive amount or an amount above the order total; `not_found` for an unknown order; `permission_denied` for an unauthorized caller; `not_eligible` for an ineligible order; `paused` when refunds are disabled. |
| `cancel_order` | `order_id` and `status: cancelled` after updating an authorized order whose current status is `placed`. | `not_found` for an unknown order; `permission_denied` for an unauthorized caller; `not_eligible` when the order is no longer `placed`; `paused` when cancellations are disabled. |
| `escalate_to_human` | `ticket_id` and `sla_hours` after creating the support ticket. | Execution exception if ticket creation fails. |
| `get_store_policies` | Authorized store identity, configured overrides, effective return and restocking rules, and the supporting policy documents. | `invalid_argument` for an invalid or missing required store identifier; `permission_denied` outside the caller's scope; `not_found` for an authorized store lookup with no matching store; `inconsistent_data` when a required policy document is missing. |
| `get_current_date` | `current_date` in ISO `YYYY-MM-DD` format and `source: world_asof`. Available to every role. | Execution exception if the database or its configured date cannot be read or parsed. |

### Current date contract (TOOL-11)

`get_current_date()` reads `meta.world_asof` through `agent.db.world_asof` on
each call. This is the authoritative current date for the course's orders and
refund calculations. Use it to interpret "today," elapsed time, and remaining
return windows. The tool has no model-supplied arguments and does not change data.
It returns `{"ok": true, "current_date": "YYYY-MM-DD", "source": "world_asof"}`.
Missing or invalid configuration raises an execution exception; never substitute
the machine's calendar date or a hard-coded fallback.

### Store policy review contract (TOOL-10)

This additional HW1 tool is implemented in `agent/tools.py` and registered for
merchants and support in `agent/agent.py`. Its offline contract tests are in
`tests/test_hw1_store_policies.py`; live conversation review remains separate.

**Purpose.** Answer questions such as a merchant's "What policy overrides have I
configured for my store?" or support's "What policies apply to store 2?" The first
version reviews the store settings represented in the current data model: the
return window and restocking-fee opt-in. It reads one store per call and has no
side effects.

**Input.** `get_store_policies(store_id: int | None = None)`. Authentication is
injected through `AuthContext`; the model does not supply a role, user, or merchant
scope.

- A merchant may omit `store_id` to use `ctx.store_id`, or explicitly supply that
  same identifier. A different valid identifier returns `permission_denied` before any
  lookup of the requested store, including when that store does not exist.
- Support must supply a positive store identifier. Omission returns
  `invalid_argument`, allowing the agent to ask which store the user means.
- Shoppers cannot use this tool. Exclude it from their registered tools and return
  `permission_denied` if its logic is called directly. An invalid role or missing
  authenticated identity also returns `permission_denied`; a merchant without a
  valid authenticated store must never fall back to another scope.
- A supplied identifier must be a positive integer. After authorization and input
  validation, an unknown store returns `not_found`.

Validate the caller context first, then the identifier's format, then merchant
scope, before looking up the target store or loading its policies.
The SDK schema requires an integer or `null` and rejects other JSON types before
invoking the tool logic; it must not coerce strings or booleans into store IDs.

**Sources and precedence.** Reuse `db.get_store` for the authorized store,
`load_facts` for platform defaults, and `load_policy_docs` for published documents.
Use `store-{store.slug}-policy` to identify the store's policy page. The effective
return window is `return_window_days_override` when set, otherwise the platform
`return_window_days`. Restocking fees are enabled only when the store has opted in;
the platform maximum percentage and opened-items restriction still apply.

**Successful result.** Return these fields with `ok: true`:

| Field | Contents |
| --- | --- |
| `store` | `store_id`, `name`, and `slug` for the authorized store. |
| `has_store_overrides` | Whether a return-window override is configured or restocking fees are enabled. |
| `configured_overrides` | `return_window_days` (integer or `null`) and `restocking_fee_opt_in` (boolean), preserving the stored settings. |
| `effective_rules` | `return_window_days` and `restocking_fee`, containing `enabled`, `maximum_percent`, and `opened_items_only`. When fees are disabled, `maximum_percent` is zero. Other numerical values and restrictions come from the store settings and `facts.yaml`. |
| `policies` | Source documents, sorted by `policy_id`. Each contains `policy_id`, `title`, `audience`, `body`, and `scope` (`platform` or `store`). Return Markdown bodies without front matter. |
| `count` | Number of documents in `policies`. |

Include the platform documents `cw-returns`, `cw-restocking-fees`, and
`cw-store-overrides`, plus the store policy page when it exists. This supplies the
sources for the effective rules and their precedence. The agent must cite the
applicable policy identifiers when explaining the result, as required by RESP-1.

**Defaults and missing data.** A store with no configured overrides normally has
no store policy page. That is a successful result: `has_store_overrides: false`,
the platform defaults, and the platform source documents. Do not treat it as a
missing store or missing policy error. If an override is configured but its store
policy page is absent, or a required platform document is absent, return
`{"ok": false, "error": "inconsistent_data", "reason": "..."}`. Do not silently
substitute defaults or return an unqualified successful review. Unexpected
database, filesystem, or policy-parsing failures raise exceptions.

**Verification cases.** Before marking this tool complete, verify:

- A merchant's omitted identifier selects their authenticated store; the same
  explicit identifier gives the same result.
- Another store identifier is denied without revealing its existence or settings.
- Support can review different stores and receives `invalid_argument` when no
  store is specified; an unknown positive identifier returns `not_found`.
- Shopper and invalid-context calls are denied by the tool logic, and the SDK
  registers the tool only for merchants and support.
- Stores with return overrides, restocking fees, and no overrides return the
  correct configured values, effective values, and source documents.
- Required missing documents return `inconsistent_data`; unexpected execution
  failures propagate. Reads do not change store settings or other database rows.
- Existing public policy lookup and search remain available to every role.

## 5. Escalation policy

The following cases always go to a human:

- **ESC-1.** Refunds above the threshold; the tool queues the refund, and the agent explains the result.
- **ESC-2.** Account changes of any kind.
- **ESC-3.** Disputes and requests the agent cannot resolve from the help center and the
  order record.
- **ESC-4.** Any case where the agent is unsure whether policy allows an action.

## 6. Other response requirements

Requirements that do not fit in the sections above, including tone and style guidelines.

- **RESP-1.** Cite the policy identifier for every claim derived from a policy document.
- **RESP-2.** Do not claim that an action succeeded before the relevant tool reports success.
- **RESP-3.** State when required information is missing or inconsistent, rather than inventing a value.
- **RESP-4.** Explain refusals and escalations without revealing inaccessible order or user information.
- **RESP-5.** Use direct and respectful language that explains the relevant decision.
