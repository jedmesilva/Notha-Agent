---
name: NOTHA Listing Flow Bug — trigger message ignored
description: Root cause and fix for the "always responds to penultimate message" bug in the product listing flow.
---

## The Bug
`_start_listing_flow()` received `text` (the user's trigger message, which already contained the product description e.g. "quero vender um iPhone 13 Pro 256GB") but **completely ignored it**. Every flow always started at step="product" and asked "O que você quer vender?" — forcing the user to repeat info they just gave. This created a systematic one-step lag that felt like the bot was always answering the previous message.

**Why:** The `args` passed to `list_product` in `_deterministic_route` were `{}` (empty), so no description was ever threaded through.

## The Fix (three changes)

1. **`_deterministic_route`** — pass `objective` (Phase-0 extraction) as `description` in the `list_product` args:
   ```python
   "args": {"description": objective},  # was {}
   ```

2. **`_execute_tool` for `list_product`** — forward it to `_start_listing_flow`:
   ```python
   description=args.get("description", ""),
   ```

3. **`_start_listing_flow`** — if a meaningful product description is found after stripping selling-intent phrases, run the "product" step handler immediately, advance the DB state to the next step (brand_model / photos_upload), and return the first real question — skipping "O que você quer vender?" entirely.

## Second Bug Fixed
`_step_photos_text` in `listing_flow.py`:
```python
if not ready.get("ready", True):   # BUG — default True means extraction failure → premature skip
if not ready.get("ready", False):  # FIX — extraction failure → stay in photos step
```

**Why:** If the LLM returned `{}` (empty/failed extraction), the old default `True` made the condition `not True = False`, causing the photos step to be silently skipped as if the user had said "done".
