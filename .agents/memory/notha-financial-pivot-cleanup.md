---
name: NOTHA physical-goods to financial-platform pivot cleanup
description: What to check when auditing NOTHA (or similarly pivoted domains) for leftover old-domain references after a business-model change
---

NOTHA pivoted from a physical-goods negotiation bot to a WhatsApp lending/investment platform. The DB schema (`schema.sql`) was already rewritten for the new domain, but old-domain concepts survived in Python code as dangling references to tables that no longer exist: `seller_profile`, `buyer_profile`, `courier_profile` (pickup/delivery/pix fields for a marketplace role model). These only surface as runtime `UndefinedTableError`/`AttributeError`, not at import time, because `py_compile` and app boot don't touch those code paths until a specific tool/flow is exercised.

**Why:** Old-domain repository methods and orchestrator context-building code kept calling `SELECT * FROM seller_profile` etc. even after those tables were dropped from the schema. `py_compile` passing and `/health` returning 200 gave false confidence — the bug only appeared when the `/test` chat endpoint actually ran a full turn through `orchestrator._gather`.

**How to apply:** After any domain pivot or big schema rewrite, don't just grep for deleted table names in dropped files — also exercise real conversational/business flows (not just health checks) to catch stale queries. Also check leftover `db/migrations/*.sql` files for old-domain table renames/columns; if they were never applied (verify against live schema) they're dead and should be deleted, not left "for reference."

Separately, found and fixed a pre-existing (unrelated to the pivot) bug: `ConversationAgent.chat_with_tools()` returns a `(reply, tool_calls)` tuple, but `orchestrator.handle_message()` called it as if it returned a plain string, causing `AttributeError: 'tuple' object has no attribute 'lower'` downstream in `_maybe_set_turn_state`. Always check return-type contracts when tracing "works until phase N" bugs in multi-phase pipelines.
