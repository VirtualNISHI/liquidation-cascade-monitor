#!/usr/bin/env python
"""Render the BTC吸収力モニター card — depth-vs-liquidation absorption.

Liquidations come from the crawled snapshot (cached by default; --crawl does a
fresh full scan), depth from the LIVE Hyperliquid L2 book. Posting is DRY-RUN
unless the explicit --post-* flags are passed (house convention).

    python scripts/absorb_render.py                       # cached snapshot, render only
    python scripts/absorb_render.py --crawl               # fresh full crawl first (~17 min)
    python scripts/absorb_render.py --crawl --post-x      # fresh numbers -> post to X
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from liqmap import distribute  # noqa: E402
from liqmap.absorb import (  # noqa: E402
    build_absorb_caption,
    build_context,
    compute_bias,
    fetch_book,
    render_absorb_png,
)


def oi_24h_lookup(path: str | Path, *, window_h: float = 24.0, tol_h: float = 4.0) -> float | None:
    """READ-ONLY nearest-to-24h-ago lookup on an oistore file (never writes —
    the store is owned by the server pipeline; see liqmap.oistore)."""
    p = Path(path)
    if not p.exists():
        return None
    try:
        hist = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None
    target = time.time() - window_h * 3600
    best_oi, best_dt = None, tol_h * 3600 + 1
    for ts, oi in hist:
        dt = abs(ts - target)
        if dt < best_dt:
            best_dt, best_oi = dt, oi
    return best_oi if best_dt <= tol_h * 3600 else None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--snapshot", default="out/cache/snapshot_0_7.json")
    ap.add_argument("--oi-history", default="out/cache/oi_history.json")
    ap.add_argument("--out", default="out/liqmap_btc_absorb.png")
    ap.add_argument("--crawl", action="store_true", help="fresh full crawl instead of the cached snapshot")
    ap.add_argument("--post-x", action="store_true")
    ap.add_argument("--post-discord", action="store_true")
    args = ap.parse_args()

    if args.crawl:
        from liqmap.sources.hyperliquid import fetch_snapshot
        from dataclasses import asdict
        snap = asdict(fetch_snapshot(max_addresses=0, refresh=True))
    else:
        snap = json.loads(Path(args.snapshot).read_text(encoding="utf-8"))
    print(f"snapshot: {snap['as_of']}  positions={len(snap['positions'])}")

    market, depth = fetch_book()
    print(f"book: mark=${market['mark']:,.0f}  "
          f"fine bids>=${depth.bid_boundary:,.0f} / asks<=${depth.ask_boundary:,.0f}")

    oi_24h = oi_24h_lookup(args.oi_history)
    bias = compute_bias(snap, market, oi_24h)
    print(f"bias: {bias['score']} ({bias['state']}) {bias['label']}  oi_24h={'set' if oi_24h else 'cold'}")

    ctx = build_context(snap, market, depth, bias=bias)
    print(f"headline: {ctx['headline']['text']}")
    print(f"下値: {ctx['verdict_dn']['text']}")
    print(f"上値: {ctx['verdict_up']['text']}")
    for r in ctx["rows"]:
        if r["tag"]:
            print(f"  tag: {r['price_label']} {r['tag']}")

    path = render_absorb_png(ctx, args.out)
    cap = build_absorb_caption(ctx)
    print(f"PNG -> {path}")
    print("--- caption ---")
    print(cap)
    print("---------------")

    if args.post_x:
        tid = distribute.post_x(path, cap, live=True)
        if tid:
            print(f"POSTED (X) -> https://x.com/i/web/status/{tid}")
    if args.post_discord:
        mid = distribute.post_discord(path, cap, live=True)
        if mid:
            print(f"POSTED (Discord) -> message id {mid}")
    if not args.post_x and not args.post_discord:
        print("(preview only — pass --post-x / --post-discord to publish)")


if __name__ == "__main__":
    main()
