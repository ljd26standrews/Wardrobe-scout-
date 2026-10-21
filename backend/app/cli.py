"""Command-line helpers: `python -m app.cli <command>`."""

from __future__ import annotations

import argparse
import csv
import json
import sys

from .config import get_settings
from .db import SessionLocal
from .logging_setup import configure_logging
from .main import bootstrap


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="wardrobe-scout")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init-db", help="create/upgrade the database and load seed data")
    sc = sub.add_parser("scan", help="scan seller catalogues now")
    sc.add_argument("--seller", help="seller slug (default: all active sellers)")
    sc.add_argument("--mode", choices=["incremental", "full"], default="incremental")
    iu = sub.add_parser("import-url", help="import one product page")
    iu.add_argument("url")
    ic = sub.add_parser("import-csv", help="import products from CSV")
    ic.add_argument("path")
    sub.add_parser("recompute", help="recompute embeddings and duplicate groups")
    ex = sub.add_parser("export", help="export a JSON backup")
    ex.add_argument("path")
    im = sub.add_parser("import-backup", help="merge a JSON backup")
    im.add_argument("path")
    args = ap.parse_args(argv)

    s = get_settings()
    configure_logging(s.log_level, s.log_json)
    bootstrap()
    db = SessionLocal()
    try:
        if args.cmd == "init-db":
            print(f"Database ready at {s.database_url}")
        elif args.cmd == "scan":
            from .ingestion.scan import scan_seller
            from .models import Seller

            q = db.query(Seller).filter(Seller.status == "active", Seller.base_url.isnot(None))
            if args.seller:
                q = db.query(Seller).filter(Seller.slug == args.seller)
            for seller in q.order_by(Seller.priority).all():
                run = scan_seller(db, seller, mode=args.mode, progress=lambda m, f=None: print("  ", m))
                print(f"{seller.name}: {run.status} — {run.message}")
        elif args.cmd == "import-url":
            from .ingestion.scan import import_url

            print(json.dumps(import_url(db, args.url), indent=2))
        elif args.cmd == "import-csv":
            from .api.sellers import _bulk

            with open(args.path, encoding="utf-8-sig") as f:
                rows = [{k.strip().lower(): (v or "").strip() for k, v in r.items() if k} for r in csv.DictReader(f)]
            res = _bulk(db, rows, "csv")
            print(f"imported {res['imported']}, failed {res['failed']}")
        elif args.cmd == "recompute":
            from .services.jobs import recompute_all

            print(recompute_all(db, lambda m, f=None: print("  ", m)))
        elif args.cmd == "export":
            from .services.backup import export_all

            with open(args.path, "w", encoding="utf-8") as f:
                json.dump(export_all(db), f, ensure_ascii=False, indent=1)
            print(f"wrote {args.path}")
        elif args.cmd == "import-backup":
            from .services.backup import import_all

            with open(args.path, encoding="utf-8") as f:
                print(import_all(db, json.load(f)))
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
