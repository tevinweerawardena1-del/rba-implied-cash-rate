"""
Daily job: scrape ASX 30 Day Interbank Cash Rate Futures, fetch the RBA cash
rate target, calculate the implied cash rate path and write the data files the
web page reads (docs/data/).

Usage:
    python scripts/update.py             # normal daily run
    python scripts/update.py --backfill  # rebuild history from 2025 onwards
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import re
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from calc import implied_path, results_as_dicts, month_start  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "docs" / "data"
LATEST = DATA / "latest.json"
MEETING_HISTORY = DATA / "meeting_history.csv"
FUTURES_HISTORY = DATA / "futures_history.csv"
MEETINGS_FILE = ROOT / "config" / "rba_meetings.json"

ASX_URL = ("https://www.asx.com.au/markets/trade-our-derivatives-market/"
           "derivatives-market-prices/short-term-derivatives")
RBA_F11_URL = "https://www.rba.gov.au/statistics/tables/csv/f1.1-data.csv"
# Backup / history source: an MIT-licensed public scrape of the same ASX page.
BACKUP_LATEST_URL = ("https://raw.githubusercontent.com/aaronw22/"
                     "ois-curve-tracker/master/latest-data/scraped_cash_rate_latest.csv")
BACKUP_HISTORY_URL = ("https://raw.githubusercontent.com/aaronw22/"
                      "ois-curve-tracker/master/combined-data/all_data.csv")
BACKFILL_FROM = date(2025, 1, 1)

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0 Safari/537.36")


# --------------------------------------------------------------------------
# ASX futures
# --------------------------------------------------------------------------

def parse_asx_table(df: pd.DataFrame) -> tuple[dict[date, float], date] | None:
    """Turn the ASX price table into {contract month: implied yield} and the
    settlement date. Returns None if this isn't the cash rate futures table."""
    cols = {c.lower().strip(): c for c in map(str, df.columns)}
    exp_col = next((cols[c] for c in cols if "expiry" in c), None)
    set_col = next((cols[c] for c in cols if "previous settlement" in c), None)
    if exp_col is None or set_col is None:
        return None

    futures: dict[date, float] = {}
    as_of_dates = []
    for _, row in df.iterrows():
        exp = re.search(r"([A-Za-z]{3})\s*'?(\d{2})\b", str(row[exp_col]))
        cell = str(row[set_col])
        px = re.search(r"\d{2}\.\d+", cell)
        if not exp or not px:
            continue
        month = datetime.strptime(f"01 {exp.group(1).title()} {exp.group(2)}",
                                  "%d %b %y").date()
        futures[month] = round(100 - float(px.group()), 4)
        a = re.search(r"(\d{1,2}/\d{1,2}/\d{2,4})", cell)
        if a:
            fmt = "%d/%m/%y" if len(a.group(1).split("/")[-1]) == 2 else "%d/%m/%Y"
            as_of_dates.append(datetime.strptime(a.group(1), fmt).date())

    if len(futures) < 6 or not as_of_dates:
        return None
    months = sorted(futures)
    # IB contracts are monthly and consecutive; other tables (e.g. 90 day bank
    # bills) are quarterly, so this check picks out the right table.
    if any((b.year - a.year) * 12 + b.month - a.month != 1
           for a, b in zip(months, months[1:])):
        return None
    if not all(-1 < y < 20 for y in futures.values()):
        return None
    return futures, max(as_of_dates)


def scrape_asx(attempts: int = 4) -> tuple[dict[date, float], date]:
    from selenium import webdriver
    from selenium.common.exceptions import TimeoutException, WebDriverException
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait

    opts = Options()
    for a in ["--headless=new", "--disable-gpu", "--window-size=1920,1200",
              "--no-sandbox", "--disable-dev-shm-usage", f"--user-agent={UA}"]:
        opts.add_argument(a)
    driver = webdriver.Chrome(options=opts)
    try:
        for i in range(1, attempts + 1):
            print(f"ASX scrape attempt {i}")
            try:
                driver.get(ASX_URL)
                WebDriverWait(driver, 60).until(
                    lambda d: d.find_elements(By.CSS_SELECTOR, "table tbody tr"))
                time.sleep(3)
                for table in pd.read_html(io.StringIO(driver.page_source)):
                    parsed = parse_asx_table(table)
                    if parsed:
                        return parsed
            except (TimeoutException, WebDriverException, ValueError) as e:
                print(f"  failed: {type(e).__name__}: {e}")
            time.sleep(10)
    finally:
        driver.quit()
    raise RuntimeError("Could not read the ASX cash rate futures table")


def fetch_backup_latest() -> tuple[dict[date, float], date]:
    df = pd.read_csv(io.StringIO(http_get(BACKUP_LATEST_URL)))
    futures = {pd.Timestamp(d).date(): float(r)
               for d, r in zip(df["date"], df["cash_rate"])}
    return futures, pd.Timestamp(df["scrape_date"].max()).date()


# --------------------------------------------------------------------------
# RBA cash rate target and interbank overnight cash rate (table F1.1)
# --------------------------------------------------------------------------

def http_get(url: str) -> str:
    r = requests.get(url, headers={"User-Agent": UA}, timeout=60)
    r.raise_for_status()
    for enc in ("utf-8-sig", "cp1252"):
        try:
            return r.content.decode(enc)
        except UnicodeDecodeError:
            continue
    return r.text


def parse_rba_date(s: str) -> date | None:
    s = s.strip()
    for fmt in ("%d-%b-%Y", "%d/%m/%Y", "%Y-%m-%d", "%d-%b-%y", "%d/%m/%y",
                "%d %b %Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def parse_rba_f11(text: str) -> tuple[dict[date, float], dict[date, float]]:
    rows = list(csv.reader(io.StringIO(text)))
    title = next(r for r in rows if r and r[0].strip().lower() == "title")
    sid_i = next(i for i, r in enumerate(rows)
                 if r and r[0].strip().lower() == "series id")

    def col(pred):
        return next((i for i, t in enumerate(title) if pred(t.strip().lower())), None)

    t_col = col(lambda t: t == "cash rate target")
    i_col = col(lambda t: t == "interbank overnight cash rate")
    print(f"RBA F1.1 columns: target={t_col}, ibocr={i_col}")
    if t_col is None:
        raise ValueError("Cash Rate Target column not found in RBA F1.1")

    target, ibocr = {}, {}
    data_rows = [r for r in rows[sid_i + 1:] if r and r[0].strip()]
    print(f"RBA F1.1 sample rows: {data_rows[:1]} ... {data_rows[-1:]}")
    for r in data_rows:
        d = parse_rba_date(r[0])
        if d is None:
            continue
        for c, store in ((t_col, target), (i_col, ibocr)):
            if c is not None and c < len(r) and r[c].strip():
                try:
                    store[d] = float(r[c])
                except ValueError:
                    pass
    return target, ibocr


def value_on(series: dict[date, float], d: date) -> tuple[date, float] | None:
    """Last observation on or before d."""
    keys = [k for k in series if k <= d]
    if not keys:
        return None
    k = max(keys)
    return k, series[k]


def rate_inputs(target_s, ibocr_s, as_at: date):
    """Target, the date it's known to, and the IBOCR-target spread at as_at."""
    t = value_on(target_s, as_at)
    if t is None:
        raise ValueError(f"No cash rate target on or before {as_at}")
    target_as_of, target = t
    spread = 0.0
    i = value_on(ibocr_s, as_at)
    if i is not None:
        tgt_then = value_on(target_s, i[0])
        if tgt_then is not None:
            spread = round(i[1] - tgt_then[1], 4)
    return target, target_as_of, spread


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------

def load_meetings() -> list[date]:
    cfg = json.loads(MEETINGS_FILE.read_text())
    return sorted(date.fromisoformat(d) for d in cfg["decision_dates"])


def history_rows(as_at: date, futures, results):
    m_rows = [{"as_at": as_at.isoformat(),
               "decision_date": r["decision_date"],
               "implied_rate": r["implied_rate"],
               "change_bp": r["change_bp"],
               "cumulative_bp": r["cumulative_bp"]} for r in results]
    f_rows = [{"as_at": as_at.isoformat(),
               "contract_month": m.isoformat(),
               "implied_yield": y} for m, y in sorted(futures.items())]
    return m_rows, f_rows


def upsert_csv(path: Path, rows: list[dict], key="as_at"):
    new = pd.DataFrame(rows)
    if new.empty:
        return
    if path.exists():
        old = pd.read_csv(path, dtype=str)
        old = old[~old[key].isin(new[key].astype(str).unique())]
        new = pd.concat([old, new.astype(str)], ignore_index=True)
    new = new.sort_values(list(new.columns[:2]))
    new.to_csv(path, index=False)


def write_latest(as_at, futures, target, target_as_of, spread, results,
                 source, notes, meetings):
    last = max(futures)
    upcoming = [d.isoformat() for d in meetings if d > as_at]
    payload = {
        "as_at": as_at.isoformat(),
        "generated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"),
        "source": source,
        "cash_rate_target": target,
        "target_as_of": target_as_of.isoformat(),
        "ibocr_spread_bp": round(spread * 100, 2),
        "futures": [{"month": m.isoformat(), "implied_yield": y,
                     "target_equivalent": round(y - spread, 4)}
                    for m, y in sorted(futures.items())],
        "meetings": results,
        "meetings_beyond_futures": [d for d in upcoming
                                    if month_start(date.fromisoformat(d)) > last],
        "notes": notes,
    }
    LATEST.write_text(json.dumps(payload, indent=2))


def run_daily():
    meetings = load_meetings()
    notes = []

    # 1. Futures: scrape ASX; fall back to the public backup scrape.
    source = "ASX website"
    try:
        futures, as_at = scrape_asx()
    except Exception as e:
        print(f"ASX scrape failed ({e}); trying backup source")
        futures, as_at = fetch_backup_latest()
        source = "backup scrape (aaronw22/ois-curve-tracker)"
        notes.append("ASX page could not be read today; used a backup scrape "
                     "rounded to 2 decimal places.")
    print(f"Futures as at {as_at}: {len(futures)} contracts")

    if LATEST.exists():
        prev = json.loads(LATEST.read_text())
        if prev.get("as_at", "") > as_at.isoformat():
            print("Scraped data is older than what's already published; stopping.")
            return

    # 2. RBA cash rate target and IBOCR.
    try:
        target_s, ibocr_s = parse_rba_f11(http_get(RBA_F11_URL))
        target, target_as_of, spread = rate_inputs(target_s, ibocr_s, as_at)
    except Exception as e:
        if not LATEST.exists():
            raise
        prev = json.loads(LATEST.read_text())
        target = prev["cash_rate_target"]
        target_as_of = date.fromisoformat(prev["target_as_of"])
        spread = prev["ibocr_spread_bp"] / 100
        notes.append(f"RBA data unavailable ({type(e).__name__}); "
                     "used the previous day's cash rate target.")
    print(f"Target {target}% (to {target_as_of}), spread {spread*100:.1f}bp")

    # 3. Calculate and write.
    results = results_as_dicts(
        implied_path(futures, target, target_as_of, spread, meetings))
    write_latest(as_at, futures, target, target_as_of, spread, results,
                 source, notes, meetings)
    m_rows, f_rows = history_rows(as_at, futures, results)
    upsert_csv(MEETING_HISTORY, m_rows)
    upsert_csv(FUTURES_HISTORY, f_rows)
    for r in results:
        print(f"  {r['decision_date']}: {r['implied_rate']:.3f}%  "
              f"({r['change_bp']:+.1f}bp, {r['method']})")


def run_backfill():
    """Rebuild meeting_history.csv and futures_history.csv from the public
    daily scrape (2dp, from 2025) using the RBA target and spread on each day."""
    meetings = load_meetings()
    hist = pd.read_csv(io.StringIO(http_get(BACKUP_HISTORY_URL)))
    hist["scrape_date"] = pd.to_datetime(hist["scrape_date"]).dt.date
    hist["date"] = pd.to_datetime(hist["date"]).dt.date
    hist = hist[(hist["scrape_date"] >= BACKFILL_FROM)
                & hist["cash_rate"].notna()]
    hist = hist[pd.to_datetime(hist["scrape_date"]).dt.weekday < 5]

    target_s, ibocr_s = parse_rba_f11(http_get(RBA_F11_URL))
    all_m, all_f = [], []
    for as_at, g in hist.groupby("scrape_date"):
        futures = {m: float(r) for m, r in zip(g["date"], g["cash_rate"])
                   if m >= month_start(as_at)}
        if len(futures) < 6:
            continue
        target, target_as_of, spread = rate_inputs(target_s, ibocr_s, as_at)
        results = results_as_dicts(implied_path(
            futures, target, target_as_of, spread, meetings))
        m_rows, f_rows = history_rows(as_at, futures, results)
        all_m += m_rows
        all_f += f_rows
    for path in (MEETING_HISTORY, FUTURES_HISTORY):
        if path.exists():
            path.unlink()
    upsert_csv(MEETING_HISTORY, all_m)
    upsert_csv(FUTURES_HISTORY, all_f)
    print(f"Backfilled {len({r['as_at'] for r in all_m})} days")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--backfill", action="store_true")
    args = ap.parse_args()
    DATA.mkdir(parents=True, exist_ok=True)
    if args.backfill:
        try:
            run_backfill()
        except Exception:
            import traceback
            print("Backfill failed; continuing with today's update only:")
            traceback.print_exc(file=sys.stdout)
    run_daily()
