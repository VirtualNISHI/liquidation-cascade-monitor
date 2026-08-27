"""板 vs 清算（吸収力） — cumulative order-book depth vs cumulative liquidations.

Method (after @SakaneBTC「板と清算のどちらが強いのか」2026-08-27): on
Hyperliquid BOTH sides are exactly knowable — the L2 book (liquidity) and every
position's liquidation price (from the address crawl). Cumulate both outward
from the current price in $500 rows:

    above: shorts liquidate on the way UP  -> forced BUYS eat the ASK book
    below: longs liquidate on the way DOWN -> forced SELLS eat the BID book

The white bar is the difference, drawn at the OUTER tip of the larger side:
the uncovered inner part of a depth bar is exactly the notional the cascade
would eat; the white part is the surplus absorption left. If cumulative
liquidations ever exceed cumulative depth, the white bar flips to the
liquidation side — the price where that first happens is the headline number.

Depth granularity: the l2Book endpoint returns 20 levels per side, so we merge
nSigFigs=3 ($100 buckets, covers ~±2.5%) near the mid with nSigFigs=2 ($1,000
buckets, covers ~±25%) beyond. Cumulative values are exact at every bucket
boundary; $500 rows inside a far $1,000 bucket take half that bucket.

The bias monitor strip reuses the canonical 偏りスコア logic (liqmap.bias)
with LIVE market inputs; evaluate() is called directly so nothing is appended
to the forward-only bias log (that series belongs to the regular pipeline).
"""

from __future__ import annotations

import asyncio
import math
from datetime import datetime, timezone
from pathlib import Path

import httpx
from jinja2 import Environment, FileSystemLoader, select_autoescape

from .bias import Inputs, WATCH_SCORE, evaluate
from .sources.hyperliquid import INFO_URL, fetch_market

ROW_STEP = 500
RANGE_PCT = 0.10
CARD_W = 1200

# Cluster tags on the liquidation side (cascade-card vocabulary).
TAG_MIN_NOTIONAL = 15e6   # a $500 bucket must hold at least this to earn a tag
TAG_MAX = 4               # at most this many tags
TAG_MIN_SPACING = 2       # rows apart (same side) so labels don't stack

_TEMPLATES = Path(__file__).parent / "templates"


# ----- formatting ----------------------------------------------------------
def _fmt_m(x: float) -> str:
    if x >= 1e9:
        return f"${x / 1e9:.2f}B"
    return f"${x / 1e6:,.1f}M"


def _fmt_m0(x: float) -> str:
    return f"${x / 1e6:,.0f}M"


def _fmt_price(x: float) -> str:
    return f"${x:,.0f}"


# ----- order book ----------------------------------------------------------
def _l2(client: httpx.Client, n_sig_figs: int) -> tuple[list, list]:
    r = client.post(INFO_URL, json={"type": "l2Book", "coin": "BTC", "nSigFigs": n_sig_figs})
    r.raise_for_status()
    bids, asks = r.json()["levels"]
    to_pairs = lambda side: [(float(l["px"]), float(l["px"]) * float(l["sz"])) for l in side]  # noqa: E731
    return to_pairs(bids), to_pairs(asks)


class DepthCurves:
    """Cumulative-notional-from-mid lookups on the merged fine+coarse book."""

    def __init__(self, fine_bids, fine_asks, coarse_bids, coarse_asks):
        # Bid buckets are floored (bucket px covers [px, px+width)); ask buckets
        # are ceiled (covers (px-width, px]). Fine covers a complete range down/up
        # to a clean $1,000 boundary; coarse strictly beyond it.
        self.fb = {px: n for px, n in fine_bids}
        self.fa = {px: n for px, n in fine_asks}
        self.cb = {px: n for px, n in coarse_bids}
        self.ca = {px: n for px, n in coarse_asks}
        self.bid_boundary = math.ceil(min(self.fb) / 1000) * 1000 if self.fb else 0
        self.ask_boundary = math.floor(max(self.fa) / 1000) * 1000 if self.fa else 0
        self._fine_bid_total = sum(n for px, n in self.fb.items() if px >= self.bid_boundary)
        self._fine_ask_total = sum(n for px, n in self.fa.items() if px <= self.ask_boundary)

    def cum_bid(self, p: float) -> float:
        """Notional of resting bids at prices >= p (p is a $500 multiple)."""
        if p >= self.bid_boundary:
            return sum(n for px, n in self.fb.items() if px >= p)
        total = self._fine_bid_total
        b = math.floor(p / 1000) * 1000
        full_from = b + 1000 if p != b else b
        total += sum(n for px, n in self.cb.items() if full_from <= px < self.bid_boundary)
        if p != b:  # row splits a $1,000 bucket -> take half of it
            total += 0.5 * self.cb.get(b, 0.0)
        return total

    def cum_ask(self, p: float) -> float:
        """Notional of resting asks at prices <= p (p is a $500 multiple)."""
        if p <= self.ask_boundary:
            return sum(n for px, n in self.fa.items() if px <= p)
        total = self._fine_ask_total
        a = math.ceil(p / 1000) * 1000
        full_to = a - 1000 if p != a else a
        total += sum(n for px, n in self.ca.items() if self.ask_boundary < px <= full_to)
        if p != a:
            total += 0.5 * self.ca.get(a, 0.0)
        return total


def fetch_book() -> tuple[dict, DepthCurves]:
    with httpx.Client(timeout=30, headers={"Content-Type": "application/json"}) as c:
        market = fetch_market(c)
        fine_b, fine_a = _l2(c, 3)
        coarse_b, coarse_a = _l2(c, 2)
    return market, DepthCurves(fine_b, fine_a, coarse_b, coarse_a)


# ----- bias monitor --------------------------------------------------------
def bias_ctx(score: int, state: str | None, side: str | None, label: str | None) -> dict:
    """Template context for the 偏りモニター strip. Also used by run.py to
    reuse the bias the cascade card just computed (same run = same numbers)."""
    return {
        "score": f"{score:+d}",
        "side": side or "neutral",
        "state": state or "静観",
        "label": label or "中立",
        "gauge_pct": round(max(0.0, min(100.0, (score + 100) / 2.0)), 1),
    }


def compute_bias(snapshot: dict, market: dict, oi_24h_ago: float | None) -> dict:
    """Canonical 偏りスコア on LIVE market inputs + crawled positions.

    Calls evaluate() directly (NOT build_liquidation_map) so the forward-only
    bias log is untouched. Cluster list uses $1,000 buckets to match the
    cascade card's band structure the gate was calibrated on.
    """
    mark = market["mark"]
    longs = [(p["liq_px"], p["notional"]) for p in snapshot["positions"]
             if p["side"] == "long" and 0 < p["liq_px"] < mark]
    shorts = [(p["liq_px"], p["notional"]) for p in snapshot["positions"]
              if p["side"] == "short" and p["liq_px"] > mark]
    buckets: dict[tuple[float, str], float] = {}
    for px, n in longs:
        buckets[(math.floor(px / 1000) * 1000, "long")] = buckets.get((math.floor(px / 1000) * 1000, "long"), 0) + n
    for px, n in shorts:
        buckets[(math.floor(px / 1000) * 1000, "short")] = buckets.get((math.floor(px / 1000) * 1000, "short"), 0) + n
    bias = evaluate(Inputs(
        price=mark,
        price_24h_ago=market["prev_day"] or mark,
        funding_8h=market["funding"] * 8,
        oi_now=market["open_interest"],
        oi_24h_ago=oi_24h_ago,
        long_cluster_total=sum(n for _, n in longs),
        short_cluster_total=sum(n for _, n in shorts),
        clusters=[(px, side, n) for (px, side), n in buckets.items()],
        smart_money_net=snapshot.get("smart_money_net"),
    ))
    score, side = bias["score"], bias["side"]
    if side == "neutral":
        label = "中立"
    elif abs(score) >= WATCH_SCORE:
        label = "ロング過熱・下落カスケード警戒" if side == "long" else "ショート過熱・上踏み警戒"
    else:
        label = "中立〜やや下値リスク優勢" if side == "long" else "中立〜やや上値リスク優勢"
    return bias_ctx(score, bias["state"], side, label)


# ----- assembly ------------------------------------------------------------
def build_context(snapshot: dict, market: dict, depth: DepthCurves,
                  bias: dict | None = None) -> dict:
    mark = market["mark"]
    longs = [(p["liq_px"], p["notional"]) for p in snapshot["positions"]
             if p["side"] == "long" and 0 < p["liq_px"] < mark]
    shorts = [(p["liq_px"], p["notional"]) for p in snapshot["positions"]
              if p["side"] == "short" and p["liq_px"] > mark]

    def cum_long(p):  # longs liquidated if price falls TO p
        return sum(n for px, n in longs if px >= p)

    def cum_short(p):  # shorts liquidated if price rises TO p
        return sum(n for px, n in shorts if px <= p)

    row0 = math.floor(mark / ROW_STEP) * ROW_STEP
    up = []
    p = row0 + ROW_STEP
    while p <= mark * (1 + RANGE_PCT):
        up.append(p)
        p += ROW_STEP
    dn = []
    p = row0 - ROW_STEP
    while p >= mark * (1 - RANGE_PCT):
        dn.append(p)
        p -= ROW_STEP

    def make(price, zone):
        if zone == "up":
            d, l = depth.cum_ask(price), cum_short(price)
        else:  # "dn" and the current row both lean on the bid book
            d, l = depth.cum_bid(price), cum_long(price)
        return {"price": price, "zone": zone, "depth": d, "liq": l}

    raw = [make(p, "up") for p in reversed(up)]
    raw.append(make(row0, "cur"))
    raw += [make(p, "dn") for p in dn]

    axis_max = max(max(r["depth"], r["liq"]) for r in raw) * 1.02

    def crossover(rows):
        for r in rows:
            if r["liq"] > r["depth"]:
                return r["price"]
        return None

    x_up = crossover([r for r in reversed(raw) if r["zone"] == "up"])   # scan outward
    x_dn = crossover([r for r in raw if r["zone"] == "dn"])

    # ----- per-bucket liq increments -> cascade-style cluster tags -----
    incr: dict[float, float] = {}
    for r in raw:
        if r["zone"] == "up":
            incr[r["price"]] = r["liq"] - cum_short(r["price"] - ROW_STEP)
        elif r["zone"] == "dn":
            incr[r["price"]] = r["liq"] - cum_long(r["price"] + ROW_STEP)
    candidates = sorted(
        ((px, n) for px, n in incr.items() if n >= TAG_MIN_NOTIONAL),
        key=lambda t: -t[1],
    )
    picked: list[tuple[float, float]] = []
    for px, n in candidates:
        if len(picked) >= TAG_MAX:
            break
        same_side = [q for q, _ in picked if (q > mark) == (px > mark)]
        if all(abs(px - q) >= TAG_MIN_SPACING * ROW_STEP for q in same_side):
            picked.append((px, n))
    tags: dict[float, str] = {}
    if picked:
        max_px = picked[0][0]
        tags[max_px] = "最大クラスター"
        below = [(px, n) for px, n in picked if px < mark and px != max_px]
        if below:
            trig_px = min(below, key=lambda t: mark - t[0])[0]
            tags[trig_px] = "一次トリガー"
        for px, n in picked:
            if px not in tags:
                tags[px] = "主要クラスター"
    tag_label = {px: f"{name} {_fmt_m0(incr[px])}" for px, name in tags.items()}

    rows = []
    for r in raw:
        d_w = r["depth"] / axis_max * 100
        l_w = r["liq"] / axis_max * 100
        diff = abs(d_w - l_w)
        white_side = "left" if r["depth"] >= r["liq"] else "right"
        rows.append({
            "zone": r["zone"],
            "price_label": _fmt_price(r["price"]),
            "pct_label": f"{(r['price'] - mark) / mark * 100:+.1f}%",
            "depth_label": _fmt_m(r["depth"]),
            "liq_label": _fmt_m(r["liq"]),
            "depth_w": round(d_w, 2),
            "liq_w": round(l_w, 2),
            "white_side": white_side,
            "white_w": round(diff, 2),
            "white_pos": round((100 - d_w) if white_side == "left" else (l_w - diff), 2),
            "tag": tag_label.get(r["price"]),
        })

    def verdict(x):
        if x is None:
            return {"ok": True, "text": f"±{RANGE_PCT * 100:.0f}%以内なし ＝ 板が吸収できる"}
        pct = (x - mark) / mark * 100
        return {"ok": False, "text": f"{_fmt_price(x)}（{pct:+.1f}%）で清算が板を超過"}

    # Headline pill next to the title: the one-glance verdict.
    breaches = [x for x in (x_dn, x_up) if x is not None]
    if not breaches:
        headline = {"text": "いまは板が優勢", "warn": False}
    else:
        nearest = min(breaches, key=lambda x: abs(x - mark))
        headline = {
            "text": f"決壊ライン {_fmt_price(nearest)}（{(nearest - mark) / mark * 100:+.1f}%）",
            "warn": True,
        }

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return {
        "rows": rows,
        "range_label": f"±{RANGE_PCT * 100:.0f}%",
        "step_label": f"${ROW_STEP}刻み",
        "verdict_dn": verdict(x_dn),
        "verdict_up": verdict(x_up),
        "book_as_of": now,
        "liq_as_of": snapshot["as_of"],
        "mark_label": _fmt_price(mark),
        "headline": headline,
        "bias": bias,
    }


# ----- caption -------------------------------------------------------------
def build_absorb_caption(ctx: dict) -> str:
    """Deterministic X/Discord caption. Weighted-length safe (~230 of 280)."""
    def short(v: dict) -> str:
        return f"±{RANGE_PCT * 100:.0f}%内 決壊なし" if v["ok"] else v["text"]

    head = ctx["headline"]
    lines = [
        f"⚖️ BTC吸収力モニター  {ctx['mark_label']}",
        f"板（壁）vs 清算（燃料）→ {'⚠️ ' if head['warn'] else ''}{head['text']}",
        "",
        f"\U0001f53b下: {short(ctx['verdict_dn'])}",
        f"\U0001f53a上: {short(ctx['verdict_up'])}",
    ]
    for r in ctx["rows"]:
        tag = r.get("tag") or ""
        if tag.startswith(("一次トリガー", "最大クラスター")):
            name, size = tag.rsplit(" ", 1)
            lines.append(f"・{name} {r['price_label']}（{size}）")
    lines += ["", "※投資助言ではありません", "#BTC #Hyperliquid #清算"]
    return "\n".join(lines)


# ----- screenshot ----------------------------------------------------------
async def render_absorb_png_async(ctx: dict, out_path: str | Path, scale: int = 2) -> Path:
    from playwright.async_api import async_playwright

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    env = Environment(
        loader=FileSystemLoader(str(_TEMPLATES)),
        autoescape=select_autoescape(["html", "j2"]),
    )
    html = env.get_template("absorb.html.j2").render(**ctx)

    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page(
            viewport={"width": CARD_W, "height": 1600}, device_scale_factor=scale
        )
        await page.set_content(html, wait_until="networkidle")
        await page.evaluate("async () => { await document.fonts.ready; }")
        await page.wait_for_timeout(150)
        card = await page.query_selector("#card")
        await card.screenshot(path=str(out_path))
        await browser.close()
    return out_path


def render_absorb_png(ctx: dict, out_path: str | Path, scale: int = 2) -> Path:
    return asyncio.run(render_absorb_png_async(ctx, out_path, scale))
