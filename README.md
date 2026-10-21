# Wardrobe Scout

A personal, local-first app for discovering, researching and deciding on clothing from Yupoo catalogues, Weidian listings and independent sellers. It collects real listings, separates evidence from marketing, learns your taste from quick decisions, finds the same or better pieces at other sellers, and helps you build a cohesive wardrobe.

- **Discover** — one piece at a time (or a grid), with swipeable photos, CNY/GBP and UK landed cost, a quality assessment with confidence, why it suits you, its three strengths and three unknowns, links, and comparable listings. Love / Save / Maybe / Dislike (with reasons) / Skip / Compare / Purchased, all with keyboard shortcuts and undo.
- **Browse** — search and filter everything collected (brand, seller, price, claimed material, evidence status, size, season, category, source).
- **Compare** — cross-seller matches, clearly separated into *confirmed identical*, *same purchase link*, *same photographs (unconfirmed)* and *visually similar*, with a verdict on whether a higher price is justified.
- **Saved** — Loved / Shortlist / Maybe / Wishlist with priorities and a landed-cost total against your budget.
- **My Wardrobe** — what you own (with photos), gaps against seasonal essentials, redundancy warnings, outfit combinations (including saved candidates), inspiration and anti-inspiration photos.
- **Sellers** — add, pause, prioritise and scan sellers. A status dashboard shows exactly how many products were genuinely collected from each seller and how its last scan went. Also: link / manual / CSV / JSON / bookmarklet imports, watched links, jobs and community research leads.
- **Insights** — what has been learned (and how confident that is), effective ranking weights, value ceilings, explanations for top picks, and the full decision history with undo.
- **Settings** — taste profile, ranking weights, exploration rate, exchange rate, landed-cost assumptions, optional AI with a spending cap, refresh schedule, backup and restore.

---

## Quick start (macOS / Linux)

Requirements: **Python 3.11+** and **Node.js 20+**. On macOS: `brew install python@3.12 node`.

```bash
cd wardrobe-scout
./scripts/setup.sh          # creates backend/.venv, installs everything, builds the UI, creates .env
./scripts/start.sh          # backend on :8000, app on :3000
```

Open **http://localhost:3000**. By default the app is reachable **only from this computer**.

**On your iPhone:** add `WS_LAN=1` and `WS_ACCESS_CODE=<a long passphrase>` to `.env`, then restart. `start.sh` prints an address such as `http://192.168.1.20:3000`; open it on the same Wi-Fi and enter the access code once. In Safari choose *Share → Add to Home Screen* to get an app-like, full-screen experience.

Optional extras during setup:

```bash
WITH_CLIP=1 ./scripts/setup.sh      # CLIP image embeddings (much better visual similarity, ~2 GB incl. PyTorch)
WITH_BROWSER=1 ./scripts/setup.sh   # headless Chromium for JS-only pages (then set WS_ENABLE_PLAYWRIGHT=true)
```

For development with auto-reload: `./scripts/dev.sh`.

### Windows

```powershell
cd wardrobe-scout\backend
py -3.12 -m venv .venv
.venv\Scripts\pip install -r requirements-dev.txt
.venv\Scripts\python -m app.cli init-db
cd ..\frontend
npm install
npm run build
cd ..
copy .env.example .env
powershell -ExecutionPolicy Bypass -File scripts\start-windows.ps1
```

### First steps in the app

1. **Sellers → Scan all.** This scans the five starting catalogues politely. Each seller's card then shows the real outcome: products collected, requests made, and any error or block message.
2. If a site restricts automated access, use **Sellers → Import products**: paste links, use the **bookmarklet** while browsing yourself, or import CSV/JSON.
3. **Discover** — make quick decisions. Give reasons when you dislike something; they matter (see below).
4. **My Wardrobe** — add a few pieces you own (photos help), plus inspiration photos.

---

## Configuration

Everything lives in `.env` at the repo root (copied from `.env.example`) and in **Settings** inside the app.

| Variable | Default | Purpose |
|---|---|---|
| `WS_DATABASE_URL` | SQLite in `backend/data/` | Use `postgresql+psycopg://…` for PostgreSQL (then `pip install psycopg[binary]`) |
| `WS_DATA_DIR` | `backend/data` | Database, downloaded images, uploads, backups |
| `WS_REQUEST_DELAY_SECONDS` | `2.0` | Minimum delay between requests to the same host |
| `WS_MAX_PAGES_PER_SCAN` | `40` | Listing pages per scan |
| `WS_RESPECT_ROBOTS_TXT` | `true` | Honour robots.txt (keep this on) |
| `WS_ENABLE_SCHEDULER` | `true` | Hourly check that refreshes sellers whose interval has elapsed |
| `WS_EMBEDDING_BACKEND` | `auto` | `auto` / `clip` / `lite` |
| `WS_ENABLE_PLAYWRIGHT` | `false` | Render JS-only pages (Weidian) in headless Chromium |
| `ANTHROPIC_API_KEY` | – | Optional AI features (also enable them in Settings → AI) |
| `WS_LAN` | `0` | `1` makes the app reachable from other devices on your network (requires `WS_ACCESS_CODE`) |
| `WS_ACCESS_CODE` | – | Passphrase other devices enter once (stored as a cookie) |
| `WS_API_TOKEN` | generated | Per-install API key. Leave unset: it is generated in `<data dir>/api_token` (mode 600) |
| `WS_ALLOW_PRIVATE_URLS` | `false` | Allow fetching LAN/localhost addresses (only for local test servers) |
| `WS_MAX_DOWNLOAD_BYTES` | 25 MB | Size cap for any downloaded page or image |
| `BACKEND_URL` | `http://127.0.0.1:8000` | Where the Next.js server proxies `/api` (read at runtime) |
| `NEXT_PUBLIC_BACKEND_PUBLIC_URL` | `http://localhost:8000` | Backend address the bookmarklet posts to (rebuild the UI after changing it) |

API keys are only read from the environment and are never stored in the database or in backups.

### Security model

- **Every `/api` call needs a per-install key.** Without one, any website open in your browser could read or change your data through the localhost API. The key is generated on first start, the app's own server adds it to requests, and you never handle it except inside the bookmarklet.
- **Both servers listen on `127.0.0.1` only.** Sharing on your network is opt-in (`WS_LAN=1`) and always requires an access code.
- **Server-side fetches refuse private, loopback and cloud-metadata addresses**, including via redirects. Downloads are size-capped.
- **The bookmarklet contains your key.** Treat it like a password and don't share it. It only works against your own computer's backend.

---

## How collection works

| Source | Method | Status |
|---|---|---|
| **Yupoo** catalogues | Server-rendered HTML: category discovery, `/albums?page=N` pagination, album pages (title, description, all photos, Weidian/Taobao links, item codes, prices) | Automatic |
| **Shopify** shops | The official public `/products.json` feed | Automatic |
| Other shop websites | schema.org JSON-LD / OpenGraph on product pages; sitemap discovery | Automatic per page |
| **Weidian** items | Embedded data in the item page when present. Optional Playwright rendering for JS-only pages | Per link (best effort) |
| Weidian shop listings | Served by authenticated JavaScript APIs, so **not traversed** | Add item links instead |
| Anything else | Paste link · manual form with photo upload · CSV/JSON · **bookmarklet** | Always works |

**Politeness and limits.** Scans run one at a time. They honour robots.txt, wait between requests, and retry only transient errors. On HTTP 401/403/429 or a verification/CAPTCHA page they **stop and report "blocked"** — nothing is ever bypassed. Image hosts that check the Referer header get the album page as Referer, exactly as your browser sends it.

**Incremental refresh.** A scan stops after a run of already-known albums. It re-fetches an album only when its listing title changed or it hasn't been seen for a week. Products are marked *new* until you open them. Price, size and availability changes are recorded with timestamps, and a *full* rescan marks albums that have disappeared as unavailable. Nothing is deleted.

**Provenance.** Every product records its source URL, first/last seen times and how it entered the system (`scraped`, `manual`, `csv`, `json`, `bookmarklet`, `seed`). Every extracted claim (composition, GSM, hardware, "1:1 accurate") is stored as a *seller claim* with its source URL and capture time.

**Translation.** Chinese text is translated with a curated garment, material and colour glossary. Unknown characters are left in place rather than guessed. With AI enabled, full translations are used and cached.

**The bookmarklet** (Sellers → Import → Bookmarklet) sends the page you are viewing, as your browser rendered it, to your local backend. It never logs in, solves CAPTCHAs or reads other tabs. In Chrome, drag the button to the bookmarks bar. In Safari on iPhone, create any bookmark, then edit its address and paste the code shown.

---

## Quality: evidence, not price

Each product gets separate ratings for **design compatibility, demonstrated construction, material evidence, seller reliability and value**, each with a confidence.

| Evidence type | Weight | Notes |
|---|---|---|
| Verified evidence | 1.0 | Lab test, clear label photos plus multiple QC sets, etc. |
| Your own note | 0.8 | After you own the item |
| Independent buyer report | 0.6 | ×0.35 if marked affiliate/promotional |
| Seller claim | 0.12 | Shown, but never sufficient on its own |
| AI photo observation | 0.08 | Never accepted for fibre content or fabric weight |

A dimension with no independent source is shown as **Unverified** (or *Seller claim only*), never as good or bad. Price never raises a quality score. Value compares price with the category median in your catalogue, adjusts for evidence, and drops when the same photographs are listed more cheaply elsewhere. Add evidence from any product page or seller card. Community search (Sellers → ⋯ → Search community reviews) finds Reddit posts as **leads**; you decide which become evidence and whether they are positive or negative, and promotional language is flagged automatically.

## The recommender

A transparent hybrid score:

`score = Σ weight × gate × (component − 0.5)` over **written preferences, learned attributes, visual similarity, description similarity, quality evidence, value, wardrobe fit and seller credibility**. You can adjust the weights in Settings and inspect them in Insights.

- **Learning strength** `s = n / (n + k)` (k = 15 by default). Learned components are scaled by `s`, and written preferences by `1 − 0.6s`. With few decisions the app says so and leans on your written profile.
- **Reason-aware rejections.** *Colour* only down-weights that colour. *Silhouette* teaches fit and visual shape. *Logo* teaches branding visibility. *Fabric* teaches materials. *Poor construction* makes quality stricter and lowers that seller. *Too expensive for the quality* only sets a value ceiling for the category. *Already own something similar* only signals redundancy. *This specific item* hides near-identical listings only. A dislike without a reason is a mild general negative. **Skip teaches nothing.**
- **Seed examples.** Your four starting items count as positive style examples. The ¥510 Aristide white T-shirt is recorded with *too expensive for the quality*, which sets a T-shirt value ceiling and does **not** teach a dislike of white or heavyweight tees (this is covered by a test).
- **Exploration.** About 18% of feed slots (adjustable) go to under-explored categories, brands and sellers, and are labelled as exploration picks.
- **Near-duplicates** of items you rejected (shared photos or near-identical images) are suppressed.
- **Visual signals** come from your approvals and rejections, your uploaded inspiration and anti-inspiration photos, and your wardrobe photos (weighted by how much you love each piece).

**Image embeddings.** With CLIP installed (`WITH_CLIP=1`, OpenCLIP ViT-B-32 / LAION-2B) visual similarity understands garments semantically. Without it, a lightweight built-in model captures colour, tone, texture and rough shape. Switching models triggers an automatic re-embed.

## Cross-seller comparison

Matches are tiered, and the UI never blurs the tiers:

| Tier | Evidence | Affects value & grouping? |
|---|---|---|
| Confirmed identical | your confirmation | yes |
| Same purchase link | both listings point at the same Weidian/Taobao item | yes |
| Several identical photographs | at least **two** distinct photos match (perceptual hash ≤ 10 bits and embedding cosine ≥ 0.97) | yes. Labelled "likely the same source photos — not proof of the same batch" |
| One similar photograph | a single photo matches | no (hint only) |
| Visually similar | embedding + title similarity | no |

The per-photo rule was chosen on a synthetic benchmark (crop / resize / watermark / recompression of re-posted photos versus different products). It cut false photo matches by about 85% while finding more genuine re-posts than the previous rule. Re-tune it on real labelled pairs. You can confirm or reject any suggestion, and your decision is never overridden.

## Optional AI (Anthropic Claude)

Off by default. With `ANTHROPIC_API_KEY` set and **Settings → AI** enabled, it provides:

- full translations
- photo observations (stored as *AI visual assessment* evidence with caveats; never used for fibre content or durability)
- research summaries of collected leads, with citations

Every call is cached, rate-limited and checked against a monthly spending cap (default $5, estimated from token usage). The default model is `claude-opus-5-5` and can be changed in Settings.

## Backup and migration

- **Settings → Export backup**: one JSON file with your settings, sellers, watched links, decisions, saved lists, wardrobe and inspiration (photos included), and evidence. **Import backup** merges it and is safe to run twice.
- **Download database**: a consistent SQLite snapshot.
- CLI: `backend/.venv/bin/python -m app.cli export backup.json` / `import-backup backup.json`.
- **PostgreSQL**: set `WS_DATABASE_URL`. Alembic migrations run automatically on start (`backend/alembic/`).

## Command line

```bash
cd backend
.venv/bin/python -m app.cli init-db
.venv/bin/python -m app.cli scan --seller madebykungfu --mode incremental
.venv/bin/python -m app.cli import-url "https://weidian.com/item.html?itemID=7707075268"
.venv/bin/python -m app.cli import-csv products.csv
.venv/bin/python -m app.cli recompute
```

## Tests

```bash
cd backend && .venv/bin/pytest            # 54 tests: parsing, ingestion, dedup, scoring, feedback, API, AI guardrails
cd backend && .venv/bin/ruff check .
cd frontend && npm run typecheck && npm run lint
./scripts/e2e.sh                          # Playwright, desktop + iPhone viewport
```

`e2e.sh` starts a **synthetic** demo catalogue (`backend/scripts/demo_catalogue_server.py`; every title is prefixed `[DEMO]` and the images are drawn shapes) on a throwaway database in `.e2e-data/`. It scans that catalogue through the real ingestion pipeline and drives the UI. Your real data is never touched. Run `npx playwright install chromium` once in `frontend/` first.

## Architecture

```
backend/   FastAPI · SQLAlchemy 2 · Alembic · SQLite→PostgreSQL · APScheduler · structlog
  app/ingestion/   polite HTTP client, Yupoo / Shopify / generic / Weidian adapters, parsing, translation, scans
  app/services/    recommender, quality, dedup, embeddings, wardrobe, landed cost, AI, community, jobs, backup
  app/api/         REST endpoints (OpenAPI docs at http://localhost:8000/docs)
frontend/  Next.js 15 (App Router) · TypeScript · Tailwind · shadcn-style Radix components · SWR · framer-motion
  src/app/api/[...path]/route.ts   runtime proxy to the backend (single origin, good for iPhone use)
scripts/   setup.sh · start.sh · dev.sh · e2e.sh · start-windows.ps1
```

## Known limitations (please read)

- **Not yet verified against the live seller sites.** This was built in a sandbox whose network policy blocked yupoo.com, weidian.com and reddit.com. The Yupoo parser follows Yupoo's public page structure and uses several fallback selectors. It is tested against faithful synthetic pages and a full local scan, but its first run against the real catalogues happens on your computer. If Yupoo's layout differs, the seller card says **"Page layout not recognised"** instead of silently reporting zero products. The parser is `backend/app/ingestion/yupoo.py`.
- **The four seed items are unverified.** They come from your notes, with historical prices and no photos. They are labelled *Seed · unverified* and are watched: once a Weidian page is readable on your machine (statically or with Playwright), the record is replaced with the real data.
- **Weidian** pages are mostly rendered by JavaScript. Static extraction may find nothing. Then enable Playwright, use the bookmarklet, or enter the item manually. Weidian shop-wide listing traversal isn't implemented, because it relies on authenticated APIs.
- **Seller reputation starts "unassessed" for all five sellers.** I couldn't reach community sources from the build environment, and popularity isn't treated as evidence. Run community searches from your machine and accept the leads you trust.
- **Without CLIP**, visual similarity is approximate (colour, tone and shape). Install CLIP for real garment understanding.
- The **landed-cost** figure is an estimate from editable assumptions (shipping rates, agent fee, VAT 20%, 12% duty above £135). Check current UK rules and your agent's prices.
- **Outfit suggestions** use colour-harmony and layering rules, not a learned fashion model.
- Duplicate grouping by **shared photos** is a strong hint but not proof of the same batch. The UI says so, and only your confirmation (or a shared purchase link) makes a match "confirmed". The photo-matching thresholds were tuned on synthetic images and still need checking on real seller photos.
- Some catalogues may contain replicas of branded goods. Check your local rules on importing them; UK Border Force can seize counterfeit goods.

## Troubleshooting

- **"Backend not reachable"** — start the backend (`./scripts/start.sh`) or check `BACKEND_URL`.
- **A seller shows "Blocked"** — the site restricted automated access. Use the bookmarklet or imports, and don't raise request rates.
- **Images missing** — some image hosts reject direct fetches. The product page offers "Re-check listing", and photos you upload always work.
- **iPhone can't connect** — set `WS_LAN=1` and `WS_ACCESS_CODE` in `.env` and restart. Both devices must be on the same network, and your computer's firewall must allow port 3000.
- **"missing or invalid API token"** — you called the backend directly. Use the app (port 3000), or send the key from `backend/data/api_token` as the `X-WS-Token` header.
- **Bookmarklet says 401** — re-copy it from Sellers → Import → Bookmarklet; the key changes if the data folder is recreated.
