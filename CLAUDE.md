# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Language in use
使用中文对话

## What this is

熊家食堂 ("Bear Family Canteen") — a small Chinese-language Flask app for a household to plan a week of meals. Three roles open from the landing page: 大熊 (default eater), 大猫 (admin, redirects to login), and 其他亲友 (guest, prompts for a name). An eater picks a future week on a calendar, then fills one of four modes per weekday (Mon–Sat): 熊山洞开小灶 / 爱心便当 / 出去觅食 / 特殊事件. Each submission is persisted to SQLite *and* dumped to a human-readable Chinese .txt under `orders_txt/` so the cook (大猫) can print or read it offline.

## Commands

```powershell
pip install -r requirements.txt
python init_db.py    # DESTRUCTIVE: drop_all + create_all + seed demo data
python app.py        # dev server on http://127.0.0.1:5000 (debug=True)
```

No test suite, linter, or build step exists. The SQLite file `canteen.db` is auto-created under `instance/` on first run; uploads go to `static/uploads/`; generated order text files go to `orders_txt/`.

## Architecture

**Single-file Flask app.** Everything lives in `app.py` (~440 lines): models, helpers, public API, and admin views. Templates are Jinja in `templates/`; there is no JS build step — pages pull Bootstrap 5 + Bootstrap Icons from a CDN and inline their own JS.

**Data model** (`app.py:27-54`):
- `Category` is a self-joining tree via `parent_id` (0 = root, not NULL). Order is via `sort_order`. The category endpoint in `get_dishes` rebuilds the tree in Python — there is no ORM relationship.
- `Dish.extra_options` is a JSON string (list of `{name, type: 'radio'|'checkbox', options: [...]}`), parsed in `get_dishes` and edited in `dish_form.html` via repeated form fields named `opt_name[]`, `opt_type[]`, `opt_options[]`.
- `Order` has one row per (`customer_name`, week). `order_data` is a JSON string of `{days: [...]}` with one entry per weekday. Resubmitting overwrites the same row (`submit_order` at `app.py:179`).

**Order lifecycle.** `submit_order` calls `is_order_editable` (`app.py:77`) to enforce the **Sunday 10:00 Australia/Sydney** cutoff before the target week, then upserts the `Order` row, then calls `generate_txt` (`app.py:84`) which renders the Chinese summary and saves it to `orders_txt/{customer}_{YYYYMMDD}一周点单.txt`. The txt filename is stored back on the row so the admin orders page can offer a download link.

**Admin auth is in-memory.** `_admin_password_hash` is a module-level global (`app.py:220`), seeded with `Vantage2020@`. Password resets via `set_admin_hash` are lost on process restart — there is no persistence layer for the admin credential. The "forgot password" flow checks three hardcoded answers in `SECURITY_QUESTIONS`.

**Image handling.** `save_uploaded_image` (`app.py:59`) optionally crops via client-supplied `crop_x/y/width/height`, then thumbnails to 300×300 with PIL and saves under `static/uploads/`. The stored `image_url` is a path like `static/uploads/...` relative to the CWD, and delete handlers call `os.remove(dish.image_url)` directly — so the app must be run from the project root or those deletes will silently fail.

**Frontend flow.** `index.html` builds a custom month calendar in JS (no library), gates "selectable" weeks to those whose Sunday is ≥ next Sunday, then redirects to `/order?customer=...&week_sunday=YYYY-MM-DD`. `order.html` reads URL params, fetches `/api/dishes`, and renders six day-cards on the left with a tabbed dish picker on the right. The `currentFocusSlot` pattern (`order.html:85, 256, 405`) means a dish click only fills the input that was last focused.

**Dish availability rules** (`order.html:231-249`) are enforced client-side only:
- 爱心便当 requires `bento_compatible` AND `preprocess_days ≤ daySinceSunday` (Mon=1, Sat=6).
- 热炒 requires `preprocess_days - 1 ≤ daySinceSunday`.
The server does no validation of mode/availability on submit.

## Known inconsistencies to watch for

These look like real bugs rather than intentional design — surface them rather than silently mirroring whichever side you happen to touch first:

- **`week_sunday` vs `week_monday` mismatch.** `index.html` and `order.html` send `week_sunday` in URLs and request bodies, but `/api/order/check_existing` and `/api/order/submit` (`app.py:159, 170`) parse `data['week_monday']` and store `Order.week_monday`. As written, the existing-order check and submission will KeyError on the current frontend payloads.
- **Missing `/order` route.** `index.html` redirects to `/order?...` and `templates/order.html` exists, but `app.py` defines no `@app.route('/order')` — the link 404s today.
- **`init_db.py` is destructive.** It calls `db.drop_all()` unconditionally; running it after orders exist will wipe them. There is no migration tooling (no Alembic).

## Conventions

- All user-facing strings are Simplified Chinese; keep that consistent when adding routes/templates.
- Times in DB are UTC naive (`datetime.utcnow` default); convert through `pytz.timezone('Australia/Sydney')` for any user-visible display or deadline math — see `is_order_editable` and the `submitted_at` formatting in `generate_txt`.
- Categories use `parent_id=0` for roots, not NULL — preserve this when adding/querying.
