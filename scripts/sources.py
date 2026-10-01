"""
Data sources. Each fetcher returns plain Python data; network failures raise.

- ASX 30 Day Interbank Cash Rate Futures (headless Chrome scrape, with a public
  backup scrape as fallback and as the source of pre-launch history)
- RBA statistical tables (CSV): F1 (money market, daily) and F2 (government
  bond yields, daily); F1.1 (monthly) as a fallback for the cash rate target
- FRED (St Louis Fed): AUD/USD exchange rate and the US 2-year Treasury yield
"""

from __future__ import annotations

import bisect
import csv
import io
import re
import time
from datetime import date, datetime

import pandas as pd
import requests

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0 Safari/537.36")

ASX_URL = ("https://www.asx.com.au/markets/trade-our-derivatives-market/"
           "derivatives-market-prices/short-term-derivatives")
RBA_CSV = "https://www.rba.gov.au/statistics/tables/csv/{code}-data.csv"
FRED_CSV = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={sid}"
# MIT-licensed public daily scrape of the same ASX page (2dp).
BACKUP_LATEST_URL = ("https://raw.githubusercontent.com/aaronw22/"
                     "ois-curve-tracker/master/latest-data/scraped_cash_rate_latest.csv")
BACKUP_HISTORY_URL = ("https://raw.githubusercontent.com/aaronw22/"
                      "ois-curve-tracker/master/combined-data/all_data.csv")


class Series:
    """A dated series with fast 'last value on or before' lookups."""

    def __init__(self, data: dict[date, float] | None = None):
        data = data or {}
        self.dates = sorted(data)
        self.values = [data[d] for d in self.dates]

    def __len__(self):
        return len(self.dates)

    def __bool__(self):
        return bool(self.dates)

    def on_or_before(self, d: date) -> tuple[date, float] | None:
        i = bisect.bisect_right(self.dates, d) - 1
        return (self.dates[i], self.values[i]) if i >= 0 else None

    def items(self, start: date | None = None):
        for d, v in zip(self.dates, self.values):
            if start is None or d >= start:
                yield d, v

    @property
    def last_date(self) -> date | None:
        return self.dates[-1] if self.dates else None


def http_get(url: str, tries: int = 3, ua: str = UA) -> str:
    last = None
    for i in range(tries):
        try:
            r = requests.get(url, headers={"User-Agent": ua, "Accept": "*/*"}, timeout=90)
            r.raise_for_status()
            for enc in ("utf-8-sig", "cp1252"):
                try:
                    return r.content.decode(enc)
                except UnicodeDecodeError:
                    continue
            return r.text
        except requests.RequestException as e:
            last = e
            time.sleep(5 * (i + 1))
    raise last


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


def fetch_backup_history(start: date) -> dict[date, dict[date, float]]:
    """{scrape date: {contract month: implied yield}} from the backup scrape."""
    df = pd.read_csv(io.StringIO(http_get(BACKUP_HISTORY_URL)))
    df = df[df["cash_rate"].notna()]
    out: dict[date, dict[date, float]] = {}
    for s, m, r in zip(df["scrape_date"], df["date"], df["cash_rate"]):
        s, m = pd.Timestamp(s).date(), pd.Timestamp(m).date()
        if s < start or s.weekday() >= 5 or m < s.replace(day=1):
            continue
        out.setdefault(s, {})[m] = float(r)
    return {s: f for s, f in out.items() if len(f) >= 6}


# --------------------------------------------------------------------------
# RBA statistical tables
# --------------------------------------------------------------------------

def parse_rba_date(s: str) -> date | None:
    s = s.strip()
    for fmt in ("%d/%m/%Y", "%d-%b-%Y", "%Y-%m-%d", "%d-%b-%y", "%d/%m/%y",
                "%d %b %Y", "%Y/%m/%d", "%b-%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def parse_rba_table(text: str) -> dict[str, dict[date, float]]:
    """Every column of an RBA statistics CSV, keyed by its 'Title' row."""
    rows = list(csv.reader(io.StringIO(text)))
    title = next(r for r in rows if r and r[0].strip().lower() == "title")
    sid_i = next(i for i, r in enumerate(rows)
                 if r and r[0].strip().lower() == "series id")
    cols: dict[str, dict[date, float]] = {
        t.strip(): {} for t in title[1:] if t.strip()}
    idx = [(i, t.strip()) for i, t in enumerate(title) if i and t.strip()]
    for r in rows[sid_i + 1:]:
        if not r or not r[0].strip():
            continue
        d = parse_rba_date(r[0])
        if d is None:
            continue
        for i, t in idx:
            if i < len(r) and r[i].strip():
                try:
                    cols[t][d] = float(r[i])
                except ValueError:
                    pass
    return cols


def fetch_rba(code: str) -> dict[str, dict[date, float]]:
    cols = parse_rba_table(http_get(RBA_CSV.format(code=code)))
    print(f"RBA {code.upper()}: {len(cols)} columns")
    for t, s in cols.items():
        if s:
            print(f"   - {t!r}: {min(s)} to {max(s)}")
    return cols


def pick(cols: dict[str, dict], *must: str, exclude: tuple[str, ...] = ()) -> Series:
    """Find a column whose title contains every word in `must` (case-insensitive)."""
    for t, s in cols.items():
        tl = t.lower()
        if all(m in tl for m in must) and not any(x in tl for x in exclude):
            return Series(s)
    return Series()


def pick_exact(cols: dict[str, dict], title: str) -> Series:
    for t, s in cols.items():
        if t.lower() == title.lower():
            return Series(s)
    return Series()


# --------------------------------------------------------------------------
# FRED
# --------------------------------------------------------------------------

def parse_fred(text: str) -> dict[date, float]:
    out = {}
    reader = csv.reader(io.StringIO(text))
    next(reader, None)
    for r in reader:
        if len(r) < 2:
            continue
        try:
            out[date.fromisoformat(r[0].strip())] = float(r[1])
        except ValueError:
            continue    # FRED marks missing days with "."
    return out


def fetch_fred(sid: str) -> Series:
    last = None
    # FRED sometimes refuses browser-like clients from cloud servers; try a
    # plain client first.
    for ua in ("curl/8.5.0", UA):
        try:
            s = Series(parse_fred(http_get(FRED_CSV.format(sid=sid), tries=2, ua=ua)))
            print(f"FRED {sid}: {len(s)} obs to {s.last_date}")
            return s
        except Exception as e:
            last = e
    raise last


# --------------------------------------------------------------------------
# Fallbacks when FRED is unreachable
# --------------------------------------------------------------------------

TREASURY_CSV = ("https://home.treasury.gov/resource-center/data-chart-center/"
                "interest-rates/daily-treasury-rates.csv/{year}/all"
                "?type=daily_treasury_yield_curve&field_tdr_date_value={year}&page&_format=csv")


def parse_treasury(text: str, column: str = "2 Yr") -> dict[date, float]:
    out = {}
    reader = csv.DictReader(io.StringIO(text))
    for r in reader:
        d, v = (r.get("Date") or "").strip(), (r.get(column) or "").strip()
        if not d or not v:
            continue
        try:
            out[datetime.strptime(d, "%m/%d/%Y").date()] = float(v)
        except ValueError:
            continue
    return out


def fetch_treasury_2y(start_year: int) -> Series:
    """US 2-year Treasury par yield from the US Treasury's yearly CSV files."""
    data: dict[date, float] = {}
    for y in range(start_year, date.today().year + 1):
        data.update(parse_treasury(http_get(TREASURY_CSV.format(year=y), tries=2)))
    s = Series(data)
    if not s:
        raise ValueError("No US Treasury 2-year data parsed")
    print(f"US Treasury 2y: {len(s)} obs to {s.last_date}")
    return s


def fetch_rba_audusd() -> Series:
    """AUD/USD from RBA table F11.1 (daily exchange rates; recent years only)."""
    cols = fetch_rba("f11.1")
    s = pick(cols, "usd")
    if not s:
        raise ValueError("No USD column in RBA F11.1")
    return s
