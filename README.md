# AUS Rates Monitor

A self-updating web page tracking Australian rates markets, rebuilt every weekday from free public data:

- **Cash rate expectations** (ASX 30 Day Interbank Cash Rate Futures): the implied RBA path meeting by meeting, target-rate probabilities, implied change at 6 and 12 months and at the end of the strip, next-meeting pricing, futures curves at each month end, and how pricing has moved.
- **Money markets** (RBA table F1): the 6-month minus 1-month bank bill spread, and 1/3/6-month BBSW.
- **Government bonds** (RBA table F2): the 2-year ACGB yield and the 2s10s curve.
- **Policy:** the Taylor rule vs the actual cash rate, and the real cash rate (cash rate minus headline and trimmed mean inflation, RBA table G1).
- **Curve analytics** (RBA F16/F2): the ACGB yield curve, 1y1y / 2y1y / 5y5y forward rates, and the 2s5s10s butterfly.
- **Global & FX** (FRED + RBA F2): US 2-year and 10-year Treasury yields, and AUD/USD alongside the 2-year ACGB minus US 2-year Treasury differential.

**Live page:** https://tevinweerawardena1-del.github.io/rba-implied-cash-rate/

Every chart's data can be downloaded as CSV (it opens straight in Excel) from `docs/data/`.

## How the implied cash rate is calculated

1. **Futures give monthly averages.** Each IB contract settles on the average interbank overnight cash rate (IBOCR) for its month, so `100 − price` is the expected monthly average.
2. **Convert to target terms.** The IBOCR trades a few bp away from the cash rate target. The spread (IBOCR − target, RBA table F1) is subtracted from every contract.
3. **Back out each meeting.** A new target applies from the day after the decision.
   - If the month after the meeting has no meeting, that month's contract *is* the post-meeting rate ("clean-month").
   - Otherwise: `r_after = (average × D − r_before × d1) / (D − d1)`, where `D` is days in the month and `d1` the days before the change ("formula").
   - If fewer than 7 days of the month fall after the change and there's no clean month to use, the path stops (the division would magnify rounding noise).
4. **Chain forward.** Each meeting's result is the next meeting's `r_before`.

Probability of a move = priced change ÷ 25bp. Target-rate probabilities split each implied rate between the two nearest 25bp outcomes.

The forward-horizon chart uses the contract 6 and 12 months ahead of each pricing date, plus the furthest listed contract. The strip never reaches a full 18 months.

The futures implied curves chart shows a maximum of 4 curves at all times (excluding the current target line): the latest close plus the previous three month ends. Each new month the oldest curve rolls off. The count is `n_months` in `month_end_curves()` in `scripts/build.py`.

## Data sources

| Data | Source |
|---|---|
| IB futures (previous settlement) | [ASX short-term derivatives prices](https://www.asx.com.au/markets/trade-our-derivatives-market/derivatives-market-prices/short-term-derivatives), read with headless Chrome |
| Cash rate target, IBOCR, bank bills (BBSW) | [RBA table F1](https://www.rba.gov.au/statistics/tables/) (`f1-data.csv`, daily) |
| 2- and 10-year ACGB yields | RBA table F2 (`f2-data.csv`, daily) |
| AUD/USD, US 2-year Treasury | [FRED](https://fred.stlouisfed.org/) series `DEXUSAL` and `DGS2` |
| RBA meeting dates | `config/rba_meetings.json` |
| Futures history from 2025, and backup | [aaronw22/ois-curve-tracker](https://github.com/aaronw22/ois-curve-tracker) (MIT licence), a public daily scrape of the same ASX page, rounded to 2dp |

## How it updates

Every run adds that day's futures strip to `docs/data/futures_history.csv`, re-downloads the full RBA and FRED series, then rebuilds every chart from scratch. A source that fails one day doesn't stop the others: its charts keep the last good data and the page shows a notice. Each run's log is saved to `docs/data/run_log.txt`.

The job runs weekdays at 9pm Sydney (with a 7am retry), and whenever code in `scripts/`, `config/` or the workflow changes.

## Repo layout

```
scripts/calc.py      implied path maths (no network; unit-tested)
scripts/build.py     chart series from raw history (no network; unit-tested)
scripts/sources.py   ASX / RBA / FRED fetchers and parsers
scripts/update.py    runs a daily update and writes docs/data/
config/rba_meetings.json
docs/index.html      the web page (GitHub Pages serves /docs)
docs/data/           generated data (JSON + CSV)
tests/test_calc.py
.github/workflows/update.yml
```

## Keeping itself up to date

Everything is recalculated from raw data on every run, so the page rolls forward on its own:

- **RBA meeting dates** are read from the RBA's [board meeting schedule](https://www.rba.gov.au/schedules-events/board-meeting-schedules.html) each run and merged with `config/rba_meetings.json` (the RBA's published list wins for any year it covers). New years appear automatically once the RBA publishes them. The merged list is saved to `docs/data/rba_meetings.json`.
- **Rolling views:** the target rate probabilities, "how pricing has moved" (next three meetings), the futures curves (latest plus the previous three month ends) and the yield curve (latest, 1 month and 1 year earlier) are always relative to the latest data. Long time series default to a rolling window (2, 3 or 10 years).
- **Health checks:** each run records how current every source is (`docs/data/health.json`), and each chart shows its own "data to" date. If a run fails or a source goes stale, a banner appears on the page and the job opens a GitHub issue labelled `data-health`, which GitHub emails to you. The issue updates itself and closes automatically when the data is current again.
- **Tests run before every update**, so if a future library version breaks something, the run stops and raises an issue rather than publishing bad numbers.

## Maintenance

- If you get a `data-health` issue, read `docs/data/run_log.txt`. A source changing its page or file layout is the most likely cause; the parsers are in `scripts/sources.py`.
- If the RBA schedule page can't be read, add the dates to `config/rba_meetings.json` by hand (the health check warns if the futures strip runs more than ~6 months past the last known meeting).
- GitHub disables scheduled workflows in repos with no activity for 60 days. The daily data commits keep it active, but if the schedule ever stops, re-enable it from the Actions tab.

## Run locally

```bash
pip install -r requirements.txt
python -m pytest tests -q
python scripts/update.py            # needs Chrome installed
cd docs && python -m http.server    # then open http://localhost:8000
```

Indicative only; not financial advice.
