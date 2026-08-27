#!/usr/bin/env python
"""Server entry point (systemd timer / cron): crawl -> render -> post to X.

On a persistent host the OI store (liqmap.oistore) survives between runs, so the
bias C2 term fills in over ~24h with no special infra. Unlike the GitHub Actions
deployment, no catch-up gate is needed — systemd's Persistent=true handles missed
runs after downtime.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from liqmap import distribute  # noqa: E402
from liqmap.clusters import build_caption, build_liquidation_map  # noqa: E402
from liqmap.oistore import update_and_get_24h  # noqa: E402
from liqmap.render import render_png  # noqa: E402
from liqmap.sources.hyperliquid import fetch_snapshot  # noqa: E402
from liqmap.sources.sentiment import fetch_fear_greed  # noqa: E402


def main() -> None:
    snap = fetch_snapshot(max_addresses=0, refresh=True)  # always a fresh full crawl
    oi_24h = update_and_get_24h(snap.open_interest)
    m = build_liquidation_map(snap, fng=fetch_fear_greed(), use_llm=True, oi_24h_ago=oi_24h)

    out = Path("out") / "liqmap_btc_live.png"
    render_png(m, out)
    caption = build_caption(m)

    print(
        f"[liqmap] {snap.as_of} price=${m.current_price:,.0f} bias={m.bias_score}({m.bias_state}) "
        f"oi_24h={'set' if oi_24h else 'cold'} smart={snap.smart_money_net}"
    )
    distribute.post_x(out, caption, live=True)
    distribute.post_discord(out, caption, live=True)

    # ---- BTC吸収力モニター (absorb card) --------------------------------
    # Reuses the SAME snapshot crawled above — no second scan; only two cheap
    # extra API calls (L2 book + market ctx). Bias comes straight from the
    # cascade card's evaluation so both cards in one slot always agree.
    # Isolated so an absorb failure can never take down the cascade post.
    try:
        from dataclasses import asdict

        from liqmap.absorb import (
            bias_ctx,
            build_absorb_caption,
            build_context as build_absorb_context,
            fetch_book,
            render_absorb_png,
        )

        market, depth = fetch_book()
        actx = build_absorb_context(
            asdict(snap), market, depth,
            bias=bias_ctx(m.bias_score, m.bias_state, m.bias_side, m.bias_label),
        )
        absorb_out = Path("out") / "liqmap_btc_absorb.png"
        render_absorb_png(actx, absorb_out)
        acap = build_absorb_caption(actx)
        print(f"[absorb] {actx['headline']['text']} / 下={actx['verdict_dn']['text']} "
              f"/ 上={actx['verdict_up']['text']}")
        distribute.post_x(absorb_out, acap, live=True)
        distribute.post_discord(absorb_out, acap, live=True)
    except Exception as e:
        print(f"[absorb] failed (cascade post unaffected): {type(e).__name__}: {e}")


if __name__ == "__main__":
    main()
