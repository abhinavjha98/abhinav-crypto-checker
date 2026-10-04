# Crypto Watchlist Screener

Picks a watchlist out of the **top 1000 cryptocurrencies** (by market cap) using three main rules:

| Rule | Default |
|---|---|
| Market-cap rank | ≤ 1000 |
| Current price | ≤ ₹500 |
| Max historical rise (ATL → ATH) | ≤ 2000% |

Stablecoins, wrapped/bridged tokens and coins with very low 24h volume are removed too. The remaining coins are ranked by a 0–100 **watch score**.

The data refreshes every day automatically through GitHub Actions, and the results are shown on a dashboard hosted free on GitHub Pages.

> **Hinglish summary:** Top 1000 coin ki list CoinGecko se aati hai. Jo coin ₹500 se mehenga hai, ya jo apne lowest (ATL) se highest (ATH) tak 2000% se zyada chadh chuka hai, woh cut ho jaata hai. Baaki coins ki watchlist banti hai, score ke hisaab se sorted. Website par sliders se ₹500 / 2000% badal sakte ho. Roz subah 6 baje data apne aap update hota hai.

## How "max rise" is calculated

```
Max rise % = (ATH ÷ ATL − 1) × 100
```

Example: ATL ₹10 and ATH ₹180 gives 1,700%, so the coin is kept. ATL ₹10 and ATH ₹400 gives 3,900%, so it is removed.

ATH and ATL are CoinGecko's all-time high and all-time low prices in INR.

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

## Project layout

```
screener.py                         fetches data, filters, scores, writes output (Python stdlib only)
config.json                         default rules (₹500, 2000%, min volume, excluded categories)
docs/index.html                     dashboard (GitHub Pages)
docs/data/coins.json                all 1000 coins with metrics (dashboard reads this)
docs/data/watchlist.csv             final watchlist, opens in Excel
docs/data/changes.json              coins that entered / left since the previous day
docs/data/history/YYYY-MM-DD.json   daily snapshots (last 90 days)
.github/workflows/update-watchlist.yml   daily auto-update
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

The site will be at `https://<username>.github.io/<repo-name>/`. After that the workflow runs every day at 06:00 IST and commits fresh data.

## Changing the rules

- **Temporarily:** use the controls on the dashboard (max price, max rise, min volume, min score). Nothing is saved.
- **Permanently:** edit `config.json` and commit. The next run uses the new values for the CSV, the "new / dropped" list and the dashboard defaults.

## Known limitations

- A coin that launched at an extremely low price (for example a ₹0.000001 ATL from launch-day trading) shows a huge max rise and gets removed, even if its real trading history is normal.
- Token price alone says nothing about valuation. A ₹5 coin with a huge supply can be more expensive than a ₹5,000 coin. Check market cap.
- Data comes from CoinGecko's free API and may be a few minutes old.

## Possible next steps

- 1-year and 2-year max rise instead of all-time ATL → ATH
- Telegram / email alert when a coin enters the watchlist with a high score
- Backtesting the rules on the daily snapshots
