#!/usr/bin/env python
"""Render the latest cached live snapshot and (optionally) post it to X.

Separate from generate.py so we post the ALREADY-crawled snapshot without
re-crawling. Posting requires the explicit --post-x flag (live send).

    python scripts/post_live.py                 # render + preview caption (no post)
    python scripts/post_live.py --post-x        # render + actually post to X
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from liqmap import distribute  # noqa: E402
from liqmap.clusters import build_caption, build_liquidation_map  # noqa: E402
from liqmap.render import render_png  # noqa: E402
from liqmap.sources.hyperliquid import Position, Snapshot  # noqa: E402
from liqmap.sources.sentiment import fetch_fear_greed  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--snapshot", default="out/cache/snapshot_0_7.json")
    ap.add_argument("--out", default="out/liqmap_btc_live.png")
    ap.add_argument("--post-x", action="store_true")
    ap.add_argument("--post-discord", action="store_true")
    ap.add_argument("--llm", action="store_true", help="LLM commentary (Gemini->OpenAI->Grok, template fallback)")
    args = ap.parse_args()

    d = json.loads(Path(args.snapshot).read_text(encoding="utf-8"))
    d["positions"] = [Position(**p) for p in d["positions"]]
    snap = Snapshot(**d)
    print(f"snapshot: scanned={snap.scanned:,}  btc_positions={snap.btc_count}  price=${snap.price:,.0f}  as_of={snap.as_of}")

    m = build_liquidation_map(snap, fng=fetch_fear_greed(), use_llm=args.llm)
    path = render_png(m, args.out)
    cap = build_caption(m)

    print(f"PNG -> {path}")
    print("key levels:", [(k.price, k.label, round(k.notional / 1e6, 1)) for k in m.key_levels])
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
