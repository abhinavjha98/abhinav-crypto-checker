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
  3. biggest real rise <= 2000 %  (a low followed by a LATER high; see compute_rises)
  4. 24h volume >= min_volume_24h
  5. not a stablecoin / wrapped / bridged / liquid-staking token

Standard library only - no pip install needed.
Optional: set env COINGECKO_API_KEY to a free CoinGecko "Demo" key for reliable rate limits.
"""

import csv
import json
from concurrent.futures import ThreadPoolExecutor
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


EXCLUDED_CACHE = ROOT / "cache" / "excluded.json"


def fetch_excluded_ids(cfg):
    """coin_id -> category slug, for stablecoins / wrapped tokens etc.

    These lists barely change, so they are fetched once a day (cache/excluded.json)
    to keep hourly runs inside the free CoinGecko monthly call limit."""
    try:
        cached = json.loads(EXCLUDED_CACHE.read_text(encoding="utf-8"))
        if (days_since(cached["fetched_at"]) or 0) < 1 and cached.get("categories") == cfg.get("exclude_categories", []):
            print(f"  using today's saved lists ({len(cached['ids'])} coins)")
            return cached["ids"]
    except (FileNotFoundError, ValueError, KeyError):
        pass
    excluded, complete = {}, True
    for cat in cfg.get("exclude_categories", []):
        try:
            for c in fetch_markets(cfg, category=cat, max_coins=500):
                excluded.setdefault(c["id"], cat)
        except Exception as e:  # keep going; name/symbol fallback still applies
            print(f"  ! could not fetch category {cat}: {e}")
            complete = False
    if complete:
        EXCLUDED_CACHE.parent.mkdir(parents=True, exist_ok=True)
        EXCLUDED_CACHE.write_text(json.dumps({"fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                                              "categories": cfg.get("exclude_categories", []), "ids": excluded},
                                             separators=(",", ":"), sort_keys=True), encoding="utf-8")
    return excluded


# --------------------------------------------------------------------------- #
# Biggest real rise
# --------------------------------------------------------------------------- #
# The rule "remove coins that already rose more than 2000%" needs the biggest
# rise that actually happened: a low followed by a LATER high.
#
#  * Lowest price (ATL) came before highest (ATH): ATH / ATL is exact.
#  * ATH came first and the coin fell to its ATL afterwards: ATH / ATL never
#    happened. We need price history to find the low that came before the peak.
#    - Binance daily candles (free, full history since the coin's Binance listing)
#    - else CoinGecko daily prices (free plan: last 365 days only -> "limited")
#
# Each coin's history is downloaded once and kept in cache/rise_cache.json.
# Every run then extends it with the latest price, ATH and ATL.

BINANCE_API = "https://data-api.binance.vision/api/v3"
RISE_CACHE = ROOT / "cache" / "rise_cache.json"


def http_json(url, timeout=30):
    req = urllib.request.Request(url, headers={"user-agent": "crypto-watchlist-screener/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def ms_to_date(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


def run_up(points, st):
    """Walk (date, low, high) points in date order, tracking the biggest low -> later-high rise."""
    for d, lo, hi in sorted(points, key=lambda p: p[0]):
        if st.get("min") and hi / st["min"] - 1 > st.get("rise", 0):
            st.update(rise=hi / st["min"] - 1, low=st["min"], low_date=st["min_date"], high=hi, high_date=d)
        if lo and (not st.get("min") or lo < st["min"]):
            st["min"], st["min_date"] = lo, d
    return st


def binance_history(symbol):
    points, start = [], 0
    while True:
        batch = http_json(f"{BINANCE_API}/klines?symbol={urllib.parse.quote(symbol)}&interval=1d&startTime={start}&limit=1000")
        points += [(ms_to_date(k[0]), float(k[3]), float(k[2])) for k in batch]
        if start == 0 and batch:
            # Listing-day open and low are often fake prints far below the real price; trust only the close
            k = batch[0]
            points[0] = (points[0][0], float(k[4]), float(k[4]))
        if len(batch) < 1000:
            return points
        start = batch[-1][0] + 1
        time.sleep(0.2)


def coingecko_history(coin_id, cfg):
    data = api_get(f"/coins/{coin_id}/market_chart",
                   {"vs_currency": cfg["vs_currency"], "days": 365, "interval": "daily"}, cfg)
    return [(ms_to_date(ms), p, p) for ms, p in data.get("prices", []) if p]


def tether_price(raw):
    return next((num(c.get("current_price")) for c in raw if c["id"] == "tether"), None)


def binance_pairs(raw):
    """coin_id -> Binance USDT pair, only where it is the same coin (price within 5%)."""
    usdt = tether_price(raw)
    try:
        binance_prices = {t["symbol"]: float(t["price"]) for t in http_json(f"{BINANCE_API}/ticker/price")}
    except Exception as e:
        print(f"  ! Binance not reachable ({e}); using CoinGecko only")
        return {}
    pairs = {}
    for c in raw:
        sym = (c.get("symbol") or "").upper() + "USDT"
        bp, price = binance_prices.get(sym), num(c.get("current_price"))
        if bp and usdt and price and abs(bp * usdt / price - 1) < 0.05:
            pairs[c["id"]] = sym
    return pairs


def compute_rises(raw, cfg, pairs):
    """coin_id -> {rise_pct, low, low_date, high, high_date, basis, since}. Prices in vs_currency."""
    try:
        cache = json.loads(RISE_CACHE.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        cache = {}

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    usdt = tether_price(raw)

    # Coins that need history, most relevant first (affordable coins, then by rank)
    need = [c for c in raw
            if num(c.get("ath")) and num(c.get("atl")) and (c.get("atl_date") or "") > (c.get("ath_date") or "")]
    need.sort(key=lambda c: ((num(c.get("current_price")) or 1e18) > cfg["max_price"], c.get("market_cap_rank") or 1e9))
    budget = cfg.get("history_fetches_per_run", 150)
    fetched = {"binance": 0, "coingecko": 0}

    # Download Binance histories in parallel (each request is slow, Binance allows plenty per minute)
    todo = {c["id"]: pairs[c["id"]] for c in need if c["id"] not in cache and c["id"] in pairs}
    binance_pts = {}
    if todo:
        def grab(item):
            try:
                return item[0], binance_history(item[1])
            except Exception as e:
                print(f"  ! Binance history failed for {item[1]}: {e}")
                return item[0], None
        with ThreadPoolExecutor(max_workers=8) as pool:
            binance_pts = dict(pool.map(grab, todo.items()))

    for c in need:
        cid = c["id"]
        if cid in cache:
            continue
        ath, atl = num(c.get("ath")), num(c.get("atl"))
        ath_d, atl_d = c["ath_date"][:10], c["atl_date"][:10]
        try:
            if binance_pts.get(cid):
                pts = binance_pts[cid]
                if pts:
                    st = run_up(pts, {"ccy": "usdt", "src": "binance", "since": pts[0][0]})
                    st["basis"] = "history" if st["since"] <= ath_d else "limited"
                    st["upto"] = today
                    cache[cid] = st
                    fetched["binance"] += 1
                    continue
            if fetched["coingecko"] < budget:
                pts = coingecko_history(cid, cfg)
                if pts:
                    since = pts[0][0]
                    young = len(pts) < 360           # coin is younger than a year: this IS its full history
                    extra = [(d, v, v) for d, v in ((ath_d, ath), (atl_d, atl)) if d >= since]
                    st = run_up(pts + extra, {"ccy": cfg["vs_currency"], "src": "coingecko", "since": since})
                    st["basis"] = "history" if young or since <= ath_d else "limited"
                    st["upto"] = today
                    cache[cid] = st
                    fetched["coingecko"] += 1
        except Exception as e:
            print(f"  ! history failed for {cid}: {e}")

    print(f"  history downloaded: Binance {fetched['binance']}, CoinGecko {fetched['coingecko']} "
          f"(cached total {len(cache)})")

    rises = {}
    for c in raw:
        price, ath, atl = num(c.get("current_price")), num(c.get("ath")), num(c.get("atl"))
        if not (ath and atl and atl > 0):
            continue
        ath_d, atl_d = (c.get("ath_date") or "")[:10], (c.get("atl_date") or "")[:10]
        if atl_d <= ath_d:
            rises[c["id"]] = {"rise_pct": (ath / atl - 1) * 100, "low": atl, "low_date": atl_d,
                              "high": ath, "high_date": ath_d, "basis": "exact", "since": None}
            continue

        st = cache.get(c["id"])
        if st is None:
            # Not fetched yet: the rise from ATL up to today's price is real, use it until history arrives
            rises[c["id"]] = {"rise_pct": max(0.0, (price / atl - 1) * 100) if price else 0.0,
                              "low": atl, "low_date": atl_d, "high": price, "high_date": today,
                              "basis": "pending", "since": None}
            continue

        # Extend cached history with what happened since it was last updated
        f = 1.0
        if st["ccy"] == "usdt":
            if not usdt:
                continue
            f = 1 / usdt
        upto = st.get("upto", st["since"])
        pts = [(d, v * f, v * f) for d, v in ((ath_d, ath), (atl_d, atl)) if d > upto]
        if price:
            pts.append((today, price * f, price * f))
        run_up(pts, st)
        st["upto"] = today

        to_local = 1 / f
        rises[c["id"]] = {"rise_pct": st.get("rise", 0) * 100,
                          "low": st.get("low", 0) * to_local, "low_date": st.get("low_date"),
                          "high": st.get("high", 0) * to_local, "high_date": st.get("high_date"),
                          "basis": st["basis"], "since": st["since"]}

    RISE_CACHE.parent.mkdir(parents=True, exist_ok=True)
    RISE_CACHE.write_text(json.dumps(cache, separators=(",", ":"), sort_keys=True), encoding="utf-8")
    return rises


# --------------------------------------------------------------------------- #
# Start price (first price the coin ever traded at)
# --------------------------------------------------------------------------- #
# A start price never changes, so each coin is looked up once and kept in cache/start_cache.json.
#  * On Binance: close of its first daily candle, converted from USD at that day's exchange rate.
#    If the coin traded elsewhere before Binance listed it, this is its Binance listing price.
#  * Not on Binance but under a year old: its first CoinGecko daily price (the free plan's
#    365 days cover its whole life).
#  * Older coins not on Binance: close of the first daily candle on Gate.io, converted the same way.
#  * Anything else: unknown (full history needs a paid CoinGecko plan).

START_CACHE = ROOT / "cache" / "start_cache.json"
FX_API = "https://api.frankfurter.app"
START_RECHECK_DAYS = 30
GATE_API = "https://api.gateio.ws/api/v4"


def fx_rates(ccy, since):
    """Sorted [(date, units of ccy per USD)] from `since` onward (working days only)."""
    if ccy == "usd":
        return [(since, 1.0)]
    data = http_json(f"{FX_API}/{since}..?from=USD&to={ccy.upper()}")
    return sorted((d, r[ccy.upper()]) for d, r in data.get("rates", {}).items())


def rate_on(rates, day):
    """Rate on `day`, or the last one before it (no rates on weekends / holidays)."""
    best = rates[0][1] if rates else None
    for d, r in rates:
        if d > day:
            break
        best = r
    return best


def binance_first_close(symbol):
    k = http_json(f"{BINANCE_API}/klines?symbol={urllib.parse.quote(symbol)}&interval=1d&startTime=0&limit=1")
    # Listing-day open and low are often fake prints; trust only the close
    return (ms_to_date(k[0][0]), float(k[0][4])) if k else None


def gate_pairs(raw, skip):
    """coin_id -> Gate USDT pair for coins not in `skip`, same coin only (price within 5%)."""
    usdt = tether_price(raw)
    try:
        listed = {p["id"]: p for p in http_json(f"{GATE_API}/spot/currency_pairs")}
        last = {t["currency_pair"]: num(t.get("last")) for t in http_json(f"{GATE_API}/spot/tickers")}
    except Exception as e:
        print(f"  ! Gate.io not reachable ({e})")
        return {}
    out = {}
    for c in raw:
        pair = (c.get("symbol") or "").upper() + "_USDT"
        info, gp, price = listed.get(pair), last.get(pair), num(c.get("current_price"))
        if c["id"] in skip or not (info and gp and usdt and price) or abs(gp * usdt / price - 1) >= 0.05:
            continue
        out[c["id"]] = pair
    return out


def gate_first_close(pair):
    # At most 1000 daily candles per request. Take the latest 1000; while a page is full, older
    # candles may exist, so step back 1000 days at a time. (Gate's listing dates are unreliable.)
    url = f"{GATE_API}/spot/candlesticks?currency_pair={urllib.parse.quote(pair)}&interval=1d"
    k = http_json(url + "&limit=1000")
    first = k[0] if k else None
    while k and len(k) >= 1000:
        end = int(k[0][0]) - 86400
        k = http_json(url + f"&from={end - 999 * 86400}&to={end}")
        if k:
            first = k[0]
    # [time, quote volume, close, high, low, open, ...]; trust only the close, as on Binance
    return (ms_to_date(int(first[0]) * 1000), float(first[2])) if first else None


def compute_starts(raw, cfg, pairs):
    """coin_id -> {"price", "date", "src"} for every coin whose start price is known."""
    try:
        cache = json.loads(START_CACHE.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        cache = {}
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    have = lambda cid: bool(cache.get(cid, {}).get("price"))

    def maybe_young(c):
        """ATH and ATL both within the last year: the coin may be under a year old."""
        ages = [days_since(c.get(k)) for k in ("ath_date", "atl_date")]
        return all(a is not None and a < 365 for a in ages)

    # Exchanges: cheap requests, in parallel. Gate only for older coins (incl. ones CoinGecko
    # found to be over a year old): for young ones CoinGecko gives the true launch price.
    todo = {c["id"]: ("binance", lambda sym=pairs[c["id"]]: binance_first_close(sym))
            for c in raw if c["id"] in pairs and not have(c["id"])}
    rest = [c for c in raw if not have(c["id"]) and c["id"] not in pairs
            and (not maybe_young(c) or cache.get(c["id"], {}).get("checked"))]
    if rest:
        for cid, pair in gate_pairs(rest, pairs).items():
            todo[cid] = ("gate", lambda pair=pair: gate_first_close(pair))
    added = {"binance": 0, "gate": 0}
    if todo:
        def grab(item):
            cid, (src, fetch) = item
            try:
                return cid, src, fetch()
            except Exception as e:
                print(f"  ! {src} start price failed for {cid}: {e}")
                return cid, src, None
        with ThreadPoolExecutor(max_workers=6) as pool:
            firsts = [(cid, src, v) for cid, src, v in pool.map(grab, todo.items()) if v]
        try:
            rates = fx_rates(cfg["vs_currency"], min(v[0] for _, _, v in firsts)) if firsts else []
        except Exception as e:
            print(f"  ! exchange rates not reachable ({e}); exchange start prices wait for the next run")
            rates = []
        for cid, src, (d, usd) in firsts:
            r = rate_on(rates, d)
            if r:
                cache[cid] = {"price": usd * r, "usd": usd, "date": d, "src": src}
                added[src] += 1

    # CoinGecko: only coins that may be under a year old
    def due(cid):
        checked = cache.get(cid, {}).get("checked")
        return not checked or (days_since(checked) or 0) >= START_RECHECK_DAYS

    need = [c for c in raw if not have(c["id"]) and c["id"] not in pairs and maybe_young(c) and due(c["id"])]
    need.sort(key=lambda c: ((num(c.get("current_price")) or 1e18) > cfg["max_price"], c.get("market_cap_rank") or 1e9))
    budget, fetched = cfg.get("start_fetches_per_run", 100), 0
    for c in need[:budget]:
        try:
            pts = coingecko_history(c["id"], cfg)
        except Exception as e:
            print(f"  ! start price failed for {c['id']}: {e}")
            continue
        fetched += 1
        if pts and len(pts) < 360:      # history shorter than the 365-day window: the first point is the start
            cache[c["id"]] = {"price": pts[0][1], "date": pts[0][0], "src": "coingecko"}
        else:
            cache[c["id"]] = {"checked": today}

    known = {cid: v for cid, v in cache.items() if v.get("price")}
    print(f"  start prices: {sum(1 for c in raw if c['id'] in known)} of {len(raw)} known "
          f"(new from Binance {added['binance']}, Gate {added['gate']}, CoinGecko checked {fetched}, {max(0, len(need) - budget)} left for later runs)")
    START_CACHE.parent.mkdir(parents=True, exist_ok=True)
    START_CACHE.write_text(json.dumps(cache, separators=(",", ":"), sort_keys=True), encoding="utf-8")
    return known


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
        if dt.tzinfo is None:           # plain "YYYY-MM-DD"
            dt = dt.replace(tzinfo=timezone.utc)
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


def build_rows(raw, excluded, rises, starts, cfg):
    rows = []
    for i, coin in enumerate(raw, start=1):
        price, ath, atl = num(coin.get("current_price")), num(coin.get("ath")), num(coin.get("atl"))
        mcap, vol = num(coin.get("market_cap")), num(coin.get("total_volume"))
        rank = coin.get("market_cap_rank") or i

        rise = rises.get(coin["id"])
        max_rise = rise["rise_pct"] if rise else None
        from_ath = ((price / ath) - 1) * 100 if price is not None and ath else None
        ath_age, atl_age = days_since(coin.get("ath_date")), days_since(coin.get("atl_date"))
        start = starts.get(coin["id"])
        first_seen = min(d for d in ((coin.get("ath_date") or "")[:10], (coin.get("atl_date") or "")[:10], "9999") if d)
        # An exchange's first day is the true start only if CoinGecko saw no trading (ATH / ATL) before it
        start_basis = None if not start else (
            "listing" if start["src"] != "coingecko" and first_seen < start["date"] else "launch")
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
            "rise_low": rise["low"] if rise else None,
            "rise_low_date": rise["low_date"] if rise else None,
            "rise_high": rise["high"] if rise else None,
            "rise_high_date": rise["high_date"] if rise else None,
            "rise_basis": rise["basis"] if rise else None,
            "rise_since": rise["since"] if rise else None,
            "from_ath_pct": round(from_ath, 2) if from_ath is not None else None,
            "start_price": start["price"] if start else None,
            "start_date": start["date"] if start else None,
            "start_basis": start_basis,
            "start_src": start["src"] if start else None,
            "since_start_pct": round((price / start["price"] - 1) * 100, 2) if start and price is not None else None,
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
CSV_COLUMNS = ["rank", "name", "symbol", "price", "start_price", "start_date", "ath", "ath_date", "atl", "atl_date",
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

    print("Working out each coin's biggest real rise ...")
    pairs = binance_pairs(raw)
    rises = compute_rises(raw, cfg, pairs)

    print("Looking up start prices ...")
    starts = compute_starts(raw, cfg, pairs)

    rows = build_rows(raw, excluded, rises, starts, cfg)
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
