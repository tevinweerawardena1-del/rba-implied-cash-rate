"""
Daily job: collect market data, rebuild every chart series, and write the files
the web page reads (docs/data/).

    python scripts/update.py             # normal daily run
    python scripts/update.py --backfill  # also (re)load futures history from 2025

Each run:
  1. adds today's ASX futures strip to docs/data/futures_history.csv
  2. downloads RBA tables F1 and F2 and FRED series (full history each time)
  3. recalculates everything from that raw data and writes the outputs
"""

from __future__ import annotations

import argparse
import json
import sys
import traceback
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sources as src  # noqa: E402
from build import (daily_analytics, decisions, month_end_curves,  # noqa: E402
                   outcome_probabilities, rate_inputs, align, yield_curves,
                   add_front_end, merge_meetings, health_check)
from calc import implied_path, results_as_dicts, month_start  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "docs" / "data"
MEETINGS_FILE = ROOT / "config" / "rba_meetings.json"
FUTURES_HISTORY = DATA / "futures_history.csv"

BACKFILL_FROM = date(2025, 1, 1)
DECISIONS_FROM = date(2016, 1, 1)
MONEY_FROM = date(2023, 1, 1)
BONDS_FROM = date(2016, 1, 1)
FX_FROM = date(2018, 1, 1)


def load_meetings() -> list[date]:
    cfg = json.loads(MEETINGS_FILE.read_text())
    return sorted(date.fromisoformat(d) for d in cfg["decision_dates"])


def load_futures_history() -> dict[date, dict[date, float]]:
    if not FUTURES_HISTORY.exists():
        return {}
    df = pd.read_csv(FUTURES_HISTORY)
    out: dict[date, dict[date, float]] = {}
    for a, m, y in zip(df["as_at"], df["contract_month"], df["implied_yield"]):
        out.setdefault(date.fromisoformat(a), {})[date.fromisoformat(m)] = float(y)
    return out


def save_futures_history(fh: dict[date, dict[date, float]]):
    rows = [(a.isoformat(), m.isoformat(), y)
            for a in sorted(fh) for m, y in sorted(fh[a].items())]
    pd.DataFrame(rows, columns=["as_at", "contract_month", "implied_yield"]) \
        .to_csv(FUTURES_HISTORY, index=False)


def r(x, dp=4):
    return None if x is None else round(x, dp)


def try_step(label: str, status: dict, fn):
    try:
        out = fn()
        status[label] = "ok"
        return out
    except Exception as e:
        print(f"{label} failed: {type(e).__name__}: {e}")
        traceback.print_exc(file=sys.stdout)
        status[label] = f"failed: {type(e).__name__}"
        return None


def run(backfill: bool):
    DATA.mkdir(parents=True, exist_ok=True)
    notes: list[str] = []
    status: dict[str, str] = {}

    # ---- 0. RBA meeting schedule (config + RBA's published dates) ----------
    scraped = try_step("RBA meeting schedule", status, src.fetch_rba_schedule)
    meetings = merge_meetings(load_meetings(), scraped)
    (DATA / "rba_meetings.json").write_text(json.dumps(
        {"source": "config/rba_meetings.json + RBA board meeting schedule page",
         "years_from_rba": sorted(scraped) if scraped else [],
         "decision_dates": [d.isoformat() for d in meetings]}, indent=2))

    # ---- 1. Futures ------------------------------------------------------
    fh = load_futures_history()
    if backfill or not fh:
        hist = try_step("futures backfill", status,
                        lambda: src.fetch_backup_history(BACKFILL_FROM))
        if hist:
            for d, f in hist.items():
                fh.setdefault(d, f)      # never overwrite days we scraped ourselves
            print(f"Futures history: {len(fh)} days")

    source = "ASX website"
    got = try_step("ASX futures", status, src.scrape_asx)
    if got is None:
        got = try_step("futures backup", status, src.fetch_backup_latest)
        source = "backup scrape (aaronw22/ois-curve-tracker)"
        if got:
            notes.append("The ASX page couldn't be read today, so a backup "
                         "scrape (rounded to 2 decimal places) was used.")
    if got:
        fut, as_at = got
        print(f"Futures as at {as_at}: {len(fut)} contracts")
        fh[as_at] = fut
    if not fh:
        raise RuntimeError("No futures data at all")
    save_futures_history(fh)
    as_at = max(fh)

    # ---- 2. RBA and FRED -------------------------------------------------
    f1 = try_step("RBA F1", status, lambda: src.fetch_rba("f1")) or {}
    target = src.pick_exact(f1, "Cash Rate Target")
    ibocr = src.pick_exact(f1, "Interbank Overnight Cash Rate")
    if not target:
        f11 = try_step("RBA F1.1", status, lambda: src.fetch_rba("f1.1")) or {}
        target = src.pick_exact(f11, "Cash Rate Target")
        ibocr = src.pick_exact(f11, "Interbank Overnight Cash Rate")
        notes.append("Daily RBA data unavailable; used the monthly table.")
    if not target:
        raise RuntimeError("No cash rate target available from the RBA")
    bab = {k: (src.pick(f1, f"{k}-month", "bab") or src.pick(f1, "bank accepted", f"{k} month")
               or src.pick(f1, f"{k} month", "bab"))
           for k in (1, 3, 6)}

    f2 = try_step("RBA F2", status, lambda: src.fetch_rba("f2")) or {}

    def acgb(tenor):
        for must in (("australian government", tenor), ("commonwealth", tenor), (tenor,)):
            s = src.pick(f2, *must, exclude=("indexed", "treasury corp", "nsw", "tcorp"))
            if s:
                return s
        return src.Series()
    acgb2, acgb10 = acgb("2 year"), acgb("10 year")

    bond_list = try_step("RBA F16", status, src.fetch_bonds) or []

    audusd = try_step("FRED AUD/USD", status, lambda: src.fetch_fred("DEXUSAL"))
    if not audusd:
        audusd = try_step("RBA AUD/USD", status, src.fetch_rba_audusd)
        if audusd:
            status.pop("FRED AUD/USD", None)
    ust2 = try_step("FRED US 2y", status, lambda: src.fetch_fred("DGS2"))
    if not ust2:
        ust2 = try_step("US Treasury 2y", status, lambda: src.fetch_treasury_2y(FX_FROM.year))
        if ust2:
            status.pop("FRED US 2y", None)
    audusd, ust2 = audusd or src.Series(), ust2 or src.Series()

    # ---- 3. Analytics ----------------------------------------------------
    meeting_rows, horizon_rows = daily_analytics(fh, target, ibocr, meetings)
    pd.DataFrame(meeting_rows).to_csv(DATA / "meeting_history.csv", index=False)
    pd.DataFrame(horizon_rows).to_csv(DATA / "implied_horizons.csv", index=False)

    tgt, tgt_asof, spread = rate_inputs(target, ibocr, as_at)
    fut = fh[as_at]
    results = results_as_dicts(implied_path(fut, tgt, tgt_asof, spread, meetings))
    last_month = max(fut)
    latest = {
        "as_at": as_at.isoformat(),
        "generated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"),
        "source": source,
        "cash_rate_target": tgt,
        "target_as_of": tgt_asof.isoformat(),
        "ibocr_spread_bp": round(spread * 100, 2),
        "futures": [{"month": m.isoformat(), "implied_yield": y,
                     "target_equivalent": round(y - spread, 4)}
                    for m, y in sorted(fut.items())],
        "meetings": results,
        "meetings_beyond_futures": [d.isoformat() for d in meetings
                                    if d > as_at and month_start(d) > last_month],
        "notes": notes,
    }
    (DATA / "latest.json").write_text(json.dumps(latest, indent=2))
    for x in results:
        print(f"  {x['decision_date']}: {x['implied_rate']:.3f}%  "
              f"({x['change_bp']:+.1f}bp, {x['method']})")

    # Market series
    money = [{"date": row["date"], "bab1": r(row["base"]), "bab3": r(row["b3"]),
              "bab6": r(row["b6"]),
              "spread_bp": None if row["b6"] is None else r((row["b6"] - row["base"]) * 100, 2)}
             for row in align(bab[1], {"b3": bab[3], "b6": bab[6]}, MONEY_FROM)]
    bonds = [{"date": row["date"], "acgb2": r(row["base"]), "acgb10": r(row["y10"]),
              "spread": None if row["y10"] is None else r(row["y10"] - row["base"])}
             for row in align(acgb2, {"y10": acgb10}, BONDS_FROM)]
    fx = [{"date": row["date"], "audusd": r(row["base"]), "ust2": r(row["ust2"]),
           "acgb2": r(row["acgb2"]),
           "diff": None if None in (row["ust2"], row["acgb2"]) else r(row["ust2"] - row["acgb2"])}
          for row in align(audusd, {"ust2": ust2, "acgb2": acgb2}, FX_FROM)]
    for name, rows in (("money_market", money), ("bonds", bonds), ("fx_differential", fx)):
        pd.DataFrame(rows).to_csv(DATA / f"{name}.csv", index=False)

    curves_by_maturity = add_front_end(yield_curves(bond_list), fh)
    pd.DataFrame([{"curve": c["label"], "as_at": c["as_at"], "source": "ACGB", **p}
                  for c in curves_by_maturity for p in c["points"]]
                 + [{"curve": c["label"], "as_at": c["as_at"], **p}
                    for c in curves_by_maturity for p in c.get("front_end", [])]) \
        .to_csv(DATA / "yield_curve.csv", index=False)

    # ---- 4. Health ---------------------------------------------------------
    bond_last = max((b["series"].last_date for b in bond_list if b["series"]), default=None)
    last_dates = {"ASX futures": as_at,
                  "RBA cash rate & bank bills (F1)": target.last_date,
                  "RBA bond yields (F2)": acgb2.last_date,
                  "RBA bond lines (F16)": bond_last,
                  "AUD/USD": audusd.last_date,
                  "US 2-year Treasury": ust2.last_date}
    today = datetime.now(ZoneInfo("Australia/Sydney")).date()
    health = health_check(today, last_dates, meetings, last_month)
    health["failed_steps"] = [k for k, v in status.items() if v != "ok"]
    health["last_dates"] = {k: (v.isoformat() if v else None) for k, v in last_dates.items()}
    (DATA / "health.json").write_text(json.dumps(health, indent=2))
    print("Health:", json.dumps(health))

    charts = {
        "as_at": as_at.isoformat(),
        "health": health,
        "asof": {"futures": as_at.isoformat(),
                 "f1": target.last_date.isoformat() if target.last_date else None,
                 "money": money[-1]["date"] if money else None,
                 "bonds": bonds[-1]["date"] if bonds else None,
                 "ycurve": curves_by_maturity[0]["as_at"] if curves_by_maturity else None,
                 "fx": fx[-1]["date"] if fx else None},
        "generated_utc": latest["generated_utc"],
        "status": status,
        "decisions": decisions(target, DECISIONS_FROM),
        "horizons": horizon_rows,
        "curves": month_end_curves(fh),
        "outcomes": outcome_probabilities(results, tgt),
        "money_market": money,
        "bonds": bonds,
        "yield_curves": curves_by_maturity,
        "fx": fx,
    }
    (DATA / "charts.json").write_text(json.dumps(charts, separators=(",", ":")))
    print("Status:", json.dumps(status))
    print(f"Wrote charts: horizons={len(horizon_rows)} money={len(money)} "
          f"bonds={len(bonds)} fx={len(fx)}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--backfill", action="store_true")
    run(ap.parse_args().backfill)
