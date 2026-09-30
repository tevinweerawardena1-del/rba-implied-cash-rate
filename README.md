# RBA Implied Cash Rate

A self-updating web page showing the market-implied path of the RBA cash rate target, calculated every weekday from ASX 30 Day Interbank Cash Rate Futures.

**Live page:** `https://<your-username>.github.io/rba-implied-cash-rate/`

## What it shows

- The expected cash rate target after each RBA meeting covered by the futures strip (about 17 months).
- The priced change and probability of a 25bp move at each meeting.
- How pricing for the next three meetings has moved day by day.
- Downloadable CSVs (open straight in Excel): `docs/data/meeting_history.csv` and `docs/data/futures_history.csv`.

## How the calculation works

1. **Futures give monthly averages.** Each IB contract settles on the average interbank overnight cash rate (IBOCR) for its month, so `100 − price` is the expected monthly average.
2. **Convert to target terms.** The IBOCR trades a few bp away from the cash rate target. Today's spread (IBOCR − target, from RBA table F1.1) is subtracted from every contract.
3. **Back out each meeting.** A new target applies from the day after the decision.
   - If the month after the meeting has no meeting, that month's contract *is* the post-meeting rate ("clean-month").
   - Otherwise: `r_after = (average × D − r_before × d1) / (D − d1)`, where `D` is days in the month and `d1` the days before the change ("formula").
   - If fewer than 7 days of the month fall after the change and there's no clean month to use, the path stops (the division would magnify rounding noise).
4. **Chain forward.** Each meeting's result is the next meeting's `r_before`.

Probability of a move = priced change ÷ 25bp.

## Data sources

| Data | Source |
|---|---|
| IB futures prices (previous settlement) | [ASX short-term derivatives prices](https://www.asx.com.au/markets/trade-our-derivatives-market/derivatives-market-prices/short-term-derivatives), read with headless Chrome |
| Cash rate target and IBOCR | [RBA statistical table F1.1](https://www.rba.gov.au/statistics/tables/) (`f1.1-data.csv`) |
| RBA meeting dates | `config/rba_meetings.json` (from the RBA's published schedule) |
| Backup and pre-launch history (from 2025) | [aaronw22/ois-curve-tracker](https://github.com/aaronw22/ois-curve-tracker) (MIT licence), a public daily scrape of the same ASX page, rounded to 2dp |

## Repo layout

```
scripts/calc.py        the maths (no network; unit-tested)
scripts/update.py      fetches data, runs calc, writes docs/data/
config/rba_meetings.json
docs/index.html        the web page (GitHub Pages serves /docs)
docs/data/             generated data: latest.json + history CSVs
tests/test_calc.py
.github/workflows/update.yml   runs weekdays 9pm Sydney (and 7am retry)
```

## Maintenance

- **Add each new year's RBA meeting dates** to `config/rba_meetings.json` when the RBA publishes them (usually in the second half of the year). Without them the path stops at the last listed meeting.
- If a run fails, check the **Actions** tab. The ASX page layout can change; the parser is `parse_asx_table()` in `scripts/update.py`. When the ASX scrape fails, the job falls back to the backup source and shows a notice on the page.
- GitHub disables scheduled workflows in repos with no activity for 60 days. The daily data commits should keep it active, but if the schedule stops, re-enable it from the Actions tab.

## Run locally

```bash
pip install -r requirements.txt
python -m pytest tests -q
python scripts/update.py            # needs Chrome installed
cd docs && python -m http.server    # then open http://localhost:8000
```

Indicative only; not financial advice.
