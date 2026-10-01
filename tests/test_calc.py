"""Run with:  python -m pytest tests -q"""
import sys
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from calc import implied_path  # noqa: E402
from sources import Series, parse_asx_table, parse_rba_table, parse_fred, pick, pick_exact  # noqa: E402
from build import (rate_inputs, decisions, daily_analytics, outcome_distribution,  # noqa: E402
                   month_end_curves, align, add_months)


def flat(start: date, n: int, rate: float) -> dict:
    out, d = {}, start
    for _ in range(n):
        out[d] = rate
        d = add_months(d, 1)
    return out


# ---- calc ---------------------------------------------------------------

def test_no_change_priced():
    fut = flat(date(2026, 10, 1), 12, 4.60)
    res = implied_path(fut, 4.60, date(2026, 9, 30), 0.0,
                       [date(2026, 11, 3), date(2026, 12, 8)])
    assert [r.change_bp for r in res] == [0, 0]


def test_formula_recovers_known_hike():
    nov_avg = (3 * 4.60 + 27 * 4.85) / 30
    fut = {date(2026, 10, 1): 4.60, date(2026, 11, 1): nov_avg,
           date(2026, 12, 1): 4.85, date(2027, 1, 1): 4.85,
           date(2027, 2, 1): 4.85, date(2027, 3, 1): 4.85}
    res = implied_path(fut, 4.60, date(2026, 9, 30), 0.0,
                       [date(2026, 11, 3), date(2026, 12, 8)])
    assert res[0].method == "formula"
    assert res[0].implied_rate == pytest.approx(4.85, abs=1e-9)
    assert res[1].method == "clean-month"
    assert res[1].change_bp == pytest.approx(0, abs=1e-9)


def test_partial_pricing_and_spread():
    exp_after = 4.60 + 0.4 * 0.25
    fut = flat(date(2026, 10, 1), 6, 4.59)
    for m in (date(2027, 1, 1), date(2027, 2, 1), date(2027, 3, 1)):
        fut[m] = exp_after - 0.01
    res = implied_path(fut, 4.60, date(2026, 9, 30), -0.01,
                       [date(2026, 11, 3), date(2026, 12, 8)])
    assert res[1].implied_rate == pytest.approx(exp_after)
    assert res[1].move_fraction == pytest.approx(0.4)


def test_meeting_on_last_day_uses_full_month():
    fut = flat(date(2025, 9, 1), 3, 3.60)
    fut[date(2025, 10, 1)] = 3.35
    fut[date(2025, 11, 1)] = 3.35
    res = implied_path(fut, 3.60, date(2025, 9, 1), 0.0, [date(2025, 9, 30)])
    assert res[0].method == "full-month"
    assert res[0].change_bp == pytest.approx(-25)


# ---- sources ------------------------------------------------------------

def test_parse_asx_table_picks_monthly_contracts():
    ib = pd.DataFrame({
        "Expiry date": ["Oct 26", "Nov 26", "Dec 26", "Jan 27", "Feb 27", "Mar 27"],
        "Previous settlement": [f"{p} As of 30/09/26" for p in
                                ["95.410", "95.350", "95.310", "95.300", "95.230", "95.190"]],
    })
    bab = ib.copy()
    bab["Expiry date"] = ["Dec 26", "Mar 27", "Jun 27", "Sep 27", "Dec 27", "Mar 28"]
    assert parse_asx_table(bab) is None
    fut, as_at = parse_asx_table(ib)
    assert as_at == date(2026, 9, 30)
    assert fut[date(2026, 10, 1)] == pytest.approx(4.59)


RBA_SAMPLE = "\n".join([
    "F1  INTEREST RATES AND YIELDS - MONEY MARKET,,,,",
    "Title,Cash Rate Target,Interbank Overnight Cash Rate,"
    "EOD 1-month BABs/NCDs,EOD 6-month BABs/NCDs",
    "Description,x,x,x,x", "Frequency,Daily,Daily,Daily,Daily",
    "Series ID,A,B,C,D",
    "26/09/2026,4.35,4.34,4.30,4.80",
    "29-Sep-2026,4.35,4.34,4.31,4.82",
    "2026-09-30,4.60,,4.40,4.95",
])


def test_parse_rba_table_and_pick():
    cols = parse_rba_table(RBA_SAMPLE)
    target = pick_exact(cols, "Cash Rate Target")
    ibocr = pick_exact(cols, "Interbank Overnight Cash Rate")
    assert len(target) == 3 and target.last_date == date(2026, 9, 30)
    t, t_asof, spread = rate_inputs(target, ibocr, date(2026, 9, 30))
    assert t == 4.60 and spread == pytest.approx(-0.01)
    assert pick(cols, "6-month", "bab").on_or_before(date(2026, 9, 30))[1] == 4.95
    assert not pick(cols, "3-month", "bab")


def test_parse_treasury():
    from sources import parse_treasury
    text = 'Date,"1 Mo","2 Yr","10 Yr"\n09/30/2026,4.10,3.95,4.20\n09/29/2026,4.11,,4.21\n'
    assert parse_treasury(text) == {date(2026, 9, 30): 3.95}


def test_parse_fred_skips_missing():
    s = parse_fred("observation_date,DEXUSAL\n2026-09-28,0.6612\n2026-09-29,.\n2026-09-30,0.6650\n")
    assert s == {date(2026, 9, 28): 0.6612, date(2026, 9, 30): 0.665}


# ---- build --------------------------------------------------------------

def test_decisions_dated_day_before_effective():
    target = Series({date(2026, 9, 29): 4.35, date(2026, 9, 30): 4.60})
    assert decisions(target, date(2026, 1, 1)) == [
        {"date": "2026-09-29", "change_bp": 25, "rate": 4.60}]


def test_daily_analytics_horizons():
    fut = flat(date(2026, 10, 1), 17, 4.60)
    fut[date(2027, 4, 1)] = 4.85
    target = Series({date(2026, 9, 30): 4.60})
    ibocr = Series({date(2026, 9, 30): 4.59})
    _, rows = daily_analytics({date(2026, 10, 1): {m: y - 0.01 for m, y in fut.items()}},
                              target, ibocr, [date(2026, 11, 3)])
    row = rows[0]
    assert row["h6_bp"] == pytest.approx(25)
    assert row["h12_bp"] == pytest.approx(0)
    assert row["hend_months"] == 16 and row["hend_bp"] == pytest.approx(0)
    assert row["next_meeting"] == "2026-11-03" and row["days_to_meeting"] == 33


def test_outcome_distribution():
    d = outcome_distribution(4.60, 4.667)
    assert [o["rate"] for o in d] == [4.60, 4.85]
    assert d[1]["prob"] == pytest.approx(0.268, abs=1e-3)
    d = outcome_distribution(4.60, 4.95)          # 1.4 hikes priced
    assert [o["rate"] for o in d] == [4.85, 5.10]
    assert d[0]["prob"] == pytest.approx(0.6)


def test_month_end_curves():
    fh = {date(2026, 7, 31): {date(2026, 8, 1): 4.4}, date(2026, 8, 29): {date(2026, 9, 1): 4.5},
          date(2026, 8, 31): {date(2026, 9, 1): 4.6}, date(2026, 9, 30): {date(2026, 10, 1): 4.7}}
    out = month_end_curves(fh, 2)
    assert [c["as_at"] for c in out] == ["2026-07-31", "2026-08-31", "2026-09-30"]
    assert out[-1]["latest"]


def test_align_respects_staleness():
    base = Series({date(2026, 9, 1): 1.0, date(2026, 9, 30): 2.0})
    other = Series({date(2026, 8, 1): 5.0, date(2026, 9, 29): 6.0})
    rows = align(base, {"o": other}, date(2026, 1, 1))
    assert rows[0]["o"] is None and rows[1]["o"] == 6.0
