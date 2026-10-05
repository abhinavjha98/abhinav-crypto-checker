# Crypto Watchlist Screener

Picks a watchlist out of the **top 1000 cryptocurrencies** (by market cap) using three main rules:

| Rule | Default |
|---|---|
| Market-cap rank | ≤ 1000 |
| Current price | ≤ ₹500 |
| Biggest jump that ever happened (low → later high) | ≤ 2000% |

Stablecoins, wrapped/bridged tokens and coins with very low 24h volume are removed too. The remaining coins are ranked by a 0–100 **watch score**.

A GitHub Actions job fetches fresh data from CoinGecko **every 3 hours** and saves it to the repo. The dashboard (free on GitHub Pages) shows that saved data and says how many minutes old it is. The page itself never calls CoinGecko.

> **Hinglish summary:** Top 1000 coin ki list CoinGecko se aati hai. Jo coin ₹500 se mehenga hai, ya jo apne lowest (ATL) se highest (ATH) tak 2000% se zyada chadh chuka hai, woh cut ho jaata hai. Baaki coins ki watchlist banti hai, score ke hisaab se sorted. Website par sliders se ₹500 / 2000% badal sakte ho. Data har 3 ghante mein apne aap update hota hai.

## How "biggest jump" is calculated

The biggest jump is the largest rise that **actually happened**: a low price followed by a *later* high price.

```
Biggest jump % = (later high ÷ earlier low − 1) × 100
```

Example: a low of ₹10 followed by a high of ₹180 gives 1,700%, so the coin is kept. A low of ₹10 followed by a high of ₹400 gives 3,900%, so it is removed.

The simple formula ATH ÷ ATL is only correct when the all-time low came *before* the all-time high. For about half the coins the all-time high came first and the coin crashed afterwards. For those coins the screener looks at daily price history:

| Case | Source | Label on the site |
|---|---|---|
| All-time low came before the all-time high | ATH ÷ ATL (exact) | none |
| Coin trades on Binance | Binance daily candles since its Binance listing (free, no key) | "Checked on daily prices since …" |
| Not on Binance | CoinGecko daily prices; the free plan only gives the last 365 days | ⚠ if the peak is older than that |
| History not downloaded yet | Rise from all-time low to today (a real rise, used until history arrives) | ⚠ |

Each coin's history is downloaded once and stored in `cache/rise_cache.json`. Every later run only adds the newest prices, so runs stay fast. The dashboard has a "Hide coins whose older price history is missing" option under More filters.

## Watch score (0–100)

| Factor | Points | Idea |
|---|---|---|
| Market-cap rank | 20 | Higher rank is more established |
| Liquidity | 20 | 24h volume ÷ market cap (10%+ gets full marks) |
| Distance from ATH | 15 | Best when 50–85% below ATH |
| 30-day momentum | 15 | −20% scores 0, +30% scores full |
| Low historical rise | 15 | The less it has ever risen, the higher |
| 24h stability | 10 | Calmer 24h move scores higher |
| Data quality | 5 | Longer price history, complete data |

80+ = Strong Watch · 65–79 = Watch · 50–64 = Monitor · below 50 = Weak

This score is a ranking heuristic. It is **not** a buy signal or a price prediction.

## My lists (your own watchlists)

The screener watchlist above stays as it is. Next to it, anyone can build their own lists:

- Tap ☆ on any coin card or table row to add it to a list. If no list exists yet, "My watchlist" is created automatically.
- "+ New list" makes another list, for example "Long term". Open a list and use Rename or Delete list to manage it.
- In a coin's detail panel, tick which lists the coin belongs to.
- The filters don't remove coins from your own lists. The status pill still shows whether a coin would pass them.
- "Download list for Excel" exports whichever list is open.

Lists are saved in the browser (`localStorage`) because the site has no server. They stay on that one browser and device. To move a list to another phone or computer, open it and tap "Copy link to open on another device". Opening that link adds the list there.

## Project layout

```
screener.py                         fetches data, filters, scores, writes output (Python stdlib only)
config.json                         default rules (₹500, 2000%, min volume, excluded categories)
docs/index.html                     dashboard (GitHub Pages)
docs/data/coins.json                all 1000 coins with metrics (dashboard reads this)
docs/data/watchlist.csv             final watchlist, opens in Excel
docs/data/changes.json              coins that entered / left since the previous day
docs/data/history/YYYY-MM-DD.json   daily snapshots (last 90 days)
.github/workflows/update-watchlist.yml   auto-update every 3 hours
cache/rise_cache.json               stored price-history results per coin
```

## Run it locally

Needs Python 3.9+. No packages to install.

```bash
python screener.py
python -m http.server 8000 -d docs
```

Open http://localhost:8000. A full run takes about 2–4 minutes because the free CoinGecko API is rate limited.

## Deploy on GitHub

1. Create a new repository and push this folder to it.
2. **Settings → Pages**: Source = *Deploy from a branch*, Branch = `main`, Folder = `/docs`. Save.
3. **Settings → Actions → General → Workflow permissions**: choose *Read and write permissions*. Save.
4. Recommended: get a free CoinGecko Demo API key at https://www.coingecko.com/en/api/pricing and add it under **Settings → Secrets and variables → Actions** as `COINGECKO_API_KEY`. Without a key the public API often rate-limits GitHub's servers.
5. **Actions → Update watchlist → Run workflow** to run it the first time.

The site will be at `https://<username>.github.io/<repo-name>/`. After that the workflow runs every 3 hours (at minute 7) and commits fresh data. GitHub sometimes starts scheduled runs a few minutes late.

## Changing the rules

- **Temporarily:** use the controls on the dashboard (max price, max rise, min volume, min score). Nothing is saved.
- **Permanently:** edit `config.json` and commit. The next run uses the new values for the CSV, the "new / dropped" list and the dashboard defaults.

## Known limitations

- A coin that launched at an extremely low price (for example a ₹0.000001 first trade) can show a huge jump and get removed, even if its real trading history is normal.
- Coins that are not on Binance and peaked more than a year ago only have one year of free history, marked ⚠.
- Token price alone says nothing about valuation. A ₹5 coin with a huge supply can be more expensive than a ₹5,000 coin. Check market cap.
- Data comes from CoinGecko's free API and may be a few minutes old.

## Possible next steps

- Telegram / email alert when a coin enters the watchlist with a high score
- Backtesting the rules on the daily snapshots
