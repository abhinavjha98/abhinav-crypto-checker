"""
Top-1000 Crypto Watchlist Screener
==================================

Fetches the top N coins by market cap from CoinGecko (prices in INR),
applies the hard filters, scores the survivors and writes:

  docs/data/coins.json        every coin + metrics + reject reason (used by the dashboard)
  docs/data/watchlist.csv     final watchlist with the default filters (Excel-ready)
  docs/data/changes.json      coins that entered / left the watchlist since the last run
  docs/data/history/<date>.json   daily snapshot of the watchlist

Rules (defaults in config.json):
  1. market_cap_rank <= 1000
  2. current price <= Rs 500
  3. max historical rise (ATL -> ATH) <= 2000 %
       max_rise % = (ATH / ATL - 1) * 100
  4. 24h volume >= min_volume_24h
  5. not a stablecoin / wrapped / bridged / liquid-staking token

Standard library only - no pip install needed.
Optional: set env COINGECKO_API_KEY to a free CoinGecko "Demo" key for reliable rate limits.
"""

import csv
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "docs" / "data"
HISTORY_DIR = DATA_DIR / "history"
API_BASE = "https://api.coingecko.com/api/v3"
PER_PAGE = 250

# Fallback detection if a category request fails
STABLE_SYMBOLS = {"usdt", "usdc", "dai", "busd", "tusd", "usdp", "fdusd", "pyusd", "usde",
                  "usdd", "frax", "gusd", "lusd", "eurc", "eurs", "usds", "rlusd", "usd0", "usdy"}
WRAPPED_PREFIXES = ("wrapped ", "bridged ", "staked ", "liquid staked ")


# --------------------------------------------------------------------------- #
# Config / HTTP
# --------------------------------------------------------------------------- #
def load_config():
    with open(ROOT / "config.json", encoding="utf-8") as f:
        return json.load(f)


def api_get(path, params, cfg, retries=6):
    url = f"{API_BASE}{path}?{urllib.parse.urlencode(params)}"
    headers = {"accept": "application/json", "user-agent": "crypto-watchlist-screener/1.0"}
    key = os.environ.get("COINGECKO_API_KEY", "").strip()
    if key:
        headers["x-cg-demo-api-key"] = key

    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            time.sleep(cfg.get("request_delay_seconds", 6))
            return data
        except urllib.error.HTTPError as e:
            if e.code == 429 or e.code >= 500:
                wait = 30 * attempt
                print(f"  HTTP {e.code} - waiting {wait}s (attempt {attempt}/{retries})")
                time.sleep(wait)
                continue
            raise
        except (urllib.error.URLError, TimeoutError) as e:
            wait = 10 * attempt
            print(f"  Network error {e} - waiting {wait}s")
            time.sleep(wait)
    raise RuntimeError(f"Failed after {retries} attempts: {url}")


def fetch_markets(cfg, category=None, max_coins=None):
    max_coins = max_coins or cfg["top_n"]
    pages = (max_coins + PER_PAGE - 1) // PER_PAGE
    coins = []
    for page in range(1, pages + 1):
        params = {
            "vs_currency": cfg["vs_currency"],
            "order": "market_cap_desc",
            "per_page": PER_PAGE,
            "page": page,
            "price_change_percentage": "24h,7d,30d",
            "sparkline": "false",
        }
        if category:
            params["category"] = category
        batch = api_get("/coins/markets", params, cfg)
        coins.extend(batch)
        label = f"category '{category}'" if category else "top coins"
        print(f"  {label}: page {page}/{pages} -> {len(batch)} coins")
        if len(batch) < PER_PAGE:
            break
    return coins[:max_coins]


def fetch_excluded_ids(cfg):
    """coin_id -> category slug, for stablecoins / wrapped tokens etc."""
    excluded = {}
    for cat in cfg.get("exclude_categories", []):
        try:
            for c in fetch_markets(cfg, category=cat, max_coins=500):
                excluded.setdefault(c["id"], cat)
        except Exception as e:  # keep going; name/symbol fallback still applies
            print(f"  ! could not fetch category {cat}: {e}")
    return excluded


# --------------------------------------------------------------------------- #
# Metrics, filters, score
# --------------------------------------------------------------------------- #
def num(v):
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def days_since(iso):
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - dt).days
    except ValueError:
        return None


def clamp(x, lo=0.0, hi=1.0):
    return max(lo, min(hi, x))


def token_type(coin, excluded):
    if coin["id"] in excluded:
        return excluded[coin["id"]]
    name = (coin.get("name") or "").lower()
    if (coin.get("symbol") or "").lower() in STABLE_SYMBOLS:
        return "stablecoins"
    if name.startswith(WRAPPED_PREFIXES):
        return "wrapped-tokens"
    return None


def opportunity_score(c, cfg):
    """0-100 ranking heuristic (NOT a buy signal). Returns (total, breakdown)."""
    b = {}
    top_n = cfg["top_n"]

    # Market-cap quality (20): higher rank = better
    b["market_cap"] = 20 * clamp(1 - (c["rank"] - 1) / top_n)

    # Liquidity (20): volume / market cap, 10%+ gets full marks
    b["liquidity"] = 20 * clamp((c["liquidity_ratio"] or 0) / 0.10)

    # Distance from ATH (15): sweet spot 50-85% below ATH
    d = -(c["from_ath_pct"] or 0)
    if 50 <= d <= 85:
        b["ath_distance"] = 15
    elif 30 <= d < 50 or 85 < d <= 95:
        b["ath_distance"] = 10
    elif 10 <= d < 30:
        b["ath_distance"] = 5
    else:
        b["ath_distance"] = 2

    # 30-day momentum (15): -20% -> 0, +30% -> full
    m30 = c["change_30d"]
    b["momentum_30d"] = 15 * clamp((m30 + 20) / 50) if m30 is not None else 5

    # Historical upside condition (15): the less it has ever risen, the better
    b["historical_upside"] = 15 * clamp(1 - c["max_rise_pct"] / cfg["max_rise_pct"])

    # Price stability (10): calm 24h move is better
    m24 = c["change_24h"]
    b["stability"] = 10 * (1 - clamp(abs(m24) / 20)) if m24 is not None else 5

    # Data quality (5): longer price history + complete fields
    age = c["history_days"] or 0
    complete = all(c[k] is not None for k in ("change_24h", "change_7d", "change_30d"))
    b["data_quality"] = 3 * clamp(age / 730) + (2 if complete else 0)

    b = {k: round(v, 1) for k, v in b.items()}
    return round(sum(b.values())), b


def status_for(score):
    if score >= 80:
        return "Strong Watch"
    if score >= 65:
        return "Watch"
    if score >= 50:
        return "Monitor"
    return "Weak"


def build_rows(raw, excluded, cfg):
    rows = []
    for i, coin in enumerate(raw, start=1):
        price, ath, atl = num(coin.get("current_price")), num(coin.get("ath")), num(coin.get("atl"))
        mcap, vol = num(coin.get("market_cap")), num(coin.get("total_volume"))
        rank = coin.get("market_cap_rank") or i

        max_rise = ((ath / atl) - 1) * 100 if ath and atl and atl > 0 else None
        from_ath = ((price / ath) - 1) * 100 if price is not None and ath else None
        ath_age, atl_age = days_since(coin.get("ath_date")), days_since(coin.get("atl_date"))
        history_days = max([a for a in (ath_age, atl_age) if a is not None], default=None)

        r = {
            "id": coin["id"],
            "symbol": (coin.get("symbol") or "").upper(),
            "name": coin.get("name"),
            "image": coin.get("image"),
            "rank": rank,
            "price": price,
            "market_cap": mcap,
            "volume_24h": vol,
            "ath": ath,
            "ath_date": (coin.get("ath_date") or "")[:10],
            "atl": atl,
            "atl_date": (coin.get("atl_date") or "")[:10],
            "max_rise_pct": round(max_rise, 2) if max_rise is not None else None,
            "from_ath_pct": round(from_ath, 2) if from_ath is not None else None,
            "liquidity_ratio": round(vol / mcap, 4) if vol and mcap else None,
            "change_24h": num(coin.get("price_change_percentage_24h_in_currency")),
            "change_7d": num(coin.get("price_change_percentage_7d_in_currency")),
            "change_30d": num(coin.get("price_change_percentage_30d_in_currency")),
            "history_days": history_days,
            "token_type": token_type(coin, excluded),
            "young": history_days is not None and history_days < 365,
        }

        # Default-filter decision (first failing rule wins)
        if price is None or mcap is None or max_rise is None:
            reason = "missing data"
        elif rank > cfg["top_n"]:
            reason = "outside top N"
        elif price > cfg["max_price"]:
            reason = "price too high"
        elif max_rise > cfg["max_rise_pct"]:
            reason = "max rise too high"
        elif (vol or 0) < cfg["min_volume_24h"]:
            reason = "low volume"
        elif r["token_type"]:
            reason = "excluded type"
        else:
            reason = None
        r["reject_reason"] = reason

        if max_rise is not None and from_ath is not None:
            r["score"], r["score_breakdown"] = opportunity_score(r, cfg)
        else:
            r["score"], r["score_breakdown"] = 0, {}
        r["status"] = status_for(r["score"])
        rows.append(r)
    return rows


# --------------------------------------------------------------------------- #
# Output
# --------------------------------------------------------------------------- #
CSV_COLUMNS = ["rank", "name", "symbol", "price", "ath", "ath_date", "atl", "atl_date",
               "max_rise_pct", "from_ath_pct", "change_24h", "change_7d", "change_30d",
               "volume_24h", "market_cap", "liquidity_ratio", "score", "status"]


def write_outputs(rows, cfg, generated_at):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)

    watchlist = sorted((r for r in rows if r["reject_reason"] is None),
                       key=lambda r: r["score"], reverse=True)

    funnel = {"total": len(rows)}
    for reason in ("missing data", "outside top N", "price too high", "max rise too high",
                   "low volume", "excluded type"):
        funnel[reason] = sum(1 for r in rows if r["reject_reason"] == reason)
    funnel["watchlist"] = len(watchlist)

    # Compare with previous snapshot
    today = generated_at[:10]
    previous = sorted(p for p in HISTORY_DIR.glob("*.json") if p.stem != today)
    prev_ids = set()
    if previous:
        prev_ids = set(json.loads(previous[-1].read_text(encoding="utf-8"))["ids"])
    cur_ids = {r["id"] for r in watchlist}
    by_id = {r["id"]: r for r in rows}
    changes = {
        "compared_to": previous[-1].stem if previous else None,
        "entered": [{"id": i, "name": by_id[i]["name"], "score": by_id[i]["score"]}
                    for i in sorted(cur_ids - prev_ids, key=lambda i: -by_id[i]["score"])] if previous else [],
        "left": [{"id": i, "name": by_id[i]["name"] if i in by_id else i,
                  "reason": by_id[i]["reject_reason"] if i in by_id else "dropped out of top N"}
                 for i in sorted(prev_ids - cur_ids)],
    }

    meta = {
        "generated_at": generated_at,
        "currency": cfg["vs_currency"],
        "defaults": {k: cfg[k] for k in ("top_n", "max_price", "max_rise_pct", "min_volume_24h")},
        "funnel": funnel,
    }

    (DATA_DIR / "coins.json").write_text(
        json.dumps({"meta": meta, "changes": changes, "coins": rows}, separators=(",", ":")),
        encoding="utf-8")
    (DATA_DIR / "changes.json").write_text(json.dumps(changes, indent=2), encoding="utf-8")
    (HISTORY_DIR / f"{today}.json").write_text(
        json.dumps({"date": today, "ids": [r["id"] for r in watchlist],
                    "scores": {r["id"]: r["score"] for r in watchlist}}, indent=1),
        encoding="utf-8")

    with open(DATA_DIR / "watchlist.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        w.writeheader()
        w.writerows(watchlist)

    # Prune old snapshots
    keep = cfg.get("history_days_to_keep", 90)
    for old in sorted(HISTORY_DIR.glob("*.json"))[:-keep]:
        old.unlink()

    return watchlist, funnel, changes


def main():
    cfg = load_config()
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"Fetching top {cfg['top_n']} coins in {cfg['vs_currency'].upper()} ...")
    raw = fetch_markets(cfg)
    if len(raw) < cfg["top_n"] * 0.9:
        sys.exit(f"Only got {len(raw)} coins - aborting so we don't publish partial data.")

    print("Fetching stablecoin / wrapped token lists ...")
    excluded = fetch_excluded_ids(cfg)

    rows = build_rows(raw, excluded, cfg)
    watchlist, funnel, changes = write_outputs(rows, cfg, generated_at)

    print("\nFunnel:")
    for k, v in funnel.items():
        print(f"  {k:<20} {v}")
    print(f"\nNew in watchlist: {len(changes['entered'])}   Left: {len(changes['left'])}")
    print("\nTop 15:")
    for r in watchlist[:15]:
        print(f"  #{r['rank']:<5} {r['name'][:22]:<22} Rs {r['price']:<12,.4f} "
              f"rise {r['max_rise_pct']:>8,.0f}%  from ATH {r['from_ath_pct']:>6.1f}%  "
              f"score {r['score']}")


if __name__ == "__main__":
    main()
