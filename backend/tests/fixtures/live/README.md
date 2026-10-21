# Real seller pages (trimmed)

These fixtures come from public pages captured by the independent audit of 2026-10-21. The
captures are in the audit evidence archive under `baseline/captures/`.

They let parser tests run against real markup rather than markup written to suit the parser.

## How they were made

`make_fixtures.py` built them from the original captures. It does four things:

- **Keeps** only what the parsers read:
  - album header and photo blocks;
  - album links and pagination;
  - the password-lock panel;
  - Weidian's embedded item data.
- **Drops** scripts, styles and navigation.
- **Redacts** phone numbers.
- **Shortens** inline `data:` placeholder images.

Nothing else is edited. Product titles, links, prices and image URLs are as the sellers published them.

## Files

| File | Source page |
|---|---|
| `yupoo_madebykungfu_album_259067099.html` | madebykungfu.x.yupoo.com album 259067099 |
| `yupoo_aristide_album_224673345.html` | aristide.x.yupoo.com album 224673345 |
| `yupoo_stitches_album_250109320.html` | stitches-reps-2.x.yupoo.com album 250109320 |
| `yupoo_*_listing.html` | `/albums?page=1` of madebykungfu, aristide, stitches-reps-2 and ethanhomme |
| `weidian_item_7707075268.html` | weidian.com item 7707075268 |

The ethanhomme catalogue is password-protected. Its page shows only the lock panel.

These pages are a sample, not a crawl. Sellers change their layouts, so a passing test means the
parser handles these pages. It does not show the parser handles every seller.
