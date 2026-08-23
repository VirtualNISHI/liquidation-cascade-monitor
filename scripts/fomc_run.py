#!/usr/bin/env python
"""FOMC special run — 4 timed posts around the FOMC decision.

Targets (UTC, computed for the run date; = JST +9):
  P1  17:50  full crawl                       FOMC直前
  P2  18:01  reuse P1 clusters + fresh price   決定直後
  P3  18:31  full crawl                       会見開始
  P4  19:01  full crawl                       会見後

P1→P2 are only 11 min apart, shorter than a ~17 min full crawl, so P2 reuses
the snapshot P1 just crawled and only refreshes the live market (price / funding
/ OI). Liquidation clusters are position-derived and barely move in 11 min; what
moves on the decision is price — exactly what P2 re-stamps.

Posting is OFF unless --live is passed (matches distribute.post_x convention).
    python scripts/fomc_run.py --smoke        # validate reuse+render now, no post
    python scripts/fomc_run.py                # scheduled run, DRY (no post)
    python scripts/fomc_run.py --live         # scheduled run, posts to X
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import httpx  # noqa: E402

from liqmap import distribute  # noqa: E402
from liqmap.clusters import build_caption, build_liquidation_map  # noqa: E402
from liqmap.oistore import update_and_get_24h  # noqa: E402
from liqmap.render import render_png  # noqa: E402
from liqmap.sources.hyperliquid import (  # noqa: E402
    Position,
    Snapshot,
    fetch_market,
    fetch_snapshot,
)
from liqmap.sources.sentiment import fetch_fear_greed  # noqa: E402

SNAP_CACHE = Path("out/cache/snapshot_0_7.json")
OUT_PNG = Path("out") / "liqmap_btc_live.png"
CRAWL_LEAD = 22 * 60  # start a full crawl this many seconds before its post time

LIVE = False  # flipped on by --live


def log(msg: str) -> None:
    print(f"[fomc {datetime.now(timezone.utc):%H:%M:%S}] {msg}", flush=True)


def target_utc(hh: int, mm: int) -> datetime:
    now = datetime.now(timezone.utc)
    t = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if t < now - timedelta(hours=6):  # already well past -> mean tomorrow
        t += timedelta(days=1)
    return t


def sleep_until(t: datetime, what: str) -> None:
    while True:
        rem = (t - datetime.now(timezone.utc)).total_seconds()
        if rem <= 0:
            return
        if rem > 90:
            log(f"waiting {rem / 60:.1f} min until {what} ({t:%H:%M} UTC)")
        time.sleep(min(rem, 60))


def load_cached_snapshot() -> Snapshot:
    d = json.loads(SNAP_CACHE.read_text(encoding="utf-8"))
    d["positions"] = [Position(**p) for p in d["positions"]]
    return Snapshot(**d)


def refresh_market(snap: Snapshot) -> Snapshot:
    """Overwrite a snapshot's market fields with a fresh single-call quote."""
    with httpx.Client(timeout=30, headers={"Content-Type": "application/json"}) as c:
        mk = fetch_market(c)
    snap.price = mk["mark"]
    snap.oracle_px = mk["oracle"]
    snap.open_interest = mk["open_interest"]
    snap.funding = mk["funding"]
    snap.price_24h_ago = mk["prev_day"]
    snap.as_of = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return snap


def render_and_post(snap: Snapshot, label: str, target: datetime | None) -> None:
    oi24 = update_and_get_24h(snap.open_interest)
    m = build_liquidation_map(snap, fng=fetch_fear_greed(), use_llm=True, oi_24h_ago=oi24)
    path = render_png(m, OUT_PNG)
    cap = build_caption(m)
    log(f"{label}: price=${m.current_price:,.0f} bias={m.bias_score}({m.bias_state}) "
        f"oi_24h={'set' if oi24 else 'cold'} -> {path}")
    if target is not None:
        sleep_until(target, f"{label} post time")
    if LIVE:
        tid = distribute.post_x(Path(path), cap, live=True)
        log(f"{label}: POSTED tid={tid}  https://x.com/i/web/status/{tid}")
    else:
        log(f"{label}: DRY-RUN — not posted. caption:\n{cap}\n----")


def do_full(target: datetime, label: str) -> None:
    sleep_until(target - timedelta(seconds=CRAWL_LEAD), f"{label} crawl start")
    log(f"{label}: starting full crawl")
    snap = fetch_snapshot(max_addresses=0, refresh=True)
    render_and_post(snap, label, target)


def do_reuse(target: datetime, label: str) -> None:
    sleep_until(target - timedelta(seconds=90), f"{label} price refresh")
    log(f"{label}: reusing latest crawled clusters, refreshing live price")
    snap = refresh_market(load_cached_snapshot())
    render_and_post(snap, label, target)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true", help="actually post to X")
    ap.add_argument("--smoke", action="store_true",
                    help="validate the reuse+render path against the cached snapshot now (no post, no schedule)")
    args = ap.parse_args()

    global LIVE
    LIVE = args.live

    if args.smoke:
        LIVE = False
        log("SMOKE: refresh price on cached snapshot -> render -> caption (no post)")
        snap = refresh_market(load_cached_snapshot())
        render_and_post(snap, "SMOKE", None)
        return

    p1 = target_utc(17, 50)
    p2 = target_utc(18, 1)
    p3 = target_utc(18, 31)
    p4 = target_utc(19, 1)
    log(f"targets UTC: P1 {p1:%H:%M} P2 {p2:%H:%M} P3 {p3:%H:%M} P4 {p4:%H:%M}  LIVE={LIVE}")

    for fn, t, label in [
        (do_full, p1, "P1 FOMC直前"),
        (do_reuse, p2, "P2 決定直後"),
        (do_full, p3, "P3 会見開始"),
        (do_full, p4, "P4 会見後"),
    ]:
        try:
            fn(t, label)
        except Exception as e:  # one failed slot must not kill the rest
            log(f"{label}: ERROR {type(e).__name__}: {e}")

    log("FOMC run complete")


if __name__ == "__main__":
    main()
