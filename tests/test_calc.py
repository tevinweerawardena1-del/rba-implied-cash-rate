"""Run with:  python -m pytest tests -q"""
import sys
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from calc import implied_path  # noqa: E402
from update import parse_asx_table, parse_rba_f11, rate_inputs  # noqa: E402


def flat(start: date, n: int, rate: float) -> dict:
    out, d = {}, start
    for _ in range(n):
        out[d] = rate
        d = date(d.year + (d.month == 12), d.month % 12 + 1, 1)
    return out


def test_no_change_priced():
    fut = flat(date(2026, 10, 1), 12, 4.60)
    res = implied_path(fut, 4.60, date(2026, 9, 30), 0.0,
                       [date(2026, 11, 3), date(2026, 12, 8)])
    assert [r.change_bp for r in res] == [0, 0]


def test_formula_recovers_known_hike():
    # Nov 2026 meeting: decision 3 Nov, new rate from 4 Nov -> 3 days old, 27 new.
    # December also has a meeting so November must use the formula.
    nov_avg = (3 * 4.60 + 27 * 4.85) / 30
    fut = {date(2026, 10, 1): 4.60, date(2026, 11, 1): nov_avg,
           date(2026, 12, 1): 4.85, date(2027, 1, 1): 4.85,
           date(2027, 2, 1): 4.85, date(2027, 3, 1): 4.85}
    res = implied_path(fut, 4.60, date(2026, 9, 30), 0.0,
                       [date(2026, 11, 3), date(2026, 12, 8)])
    assert res[0].method == "formula"
    assert res[0].implied_rate == pytest.approx(4.85, abs=1e-9)
    assert res[0].move_fraction == pytest.approx(1.0)
    assert res[1].method == "clean-month"      # January has no meeting
    assert res[1].change_bp == pytest.approx(0, abs=1e-9)


def test_partial_pricing_and_spread():
    # 40% chance of a 25bp hike in Dec; IBOCR trades 1bp under target.
    exp_after = 4.60 + 0.4 * 0.25
    fut = flat(date(2026, 10, 1), 6, 4.59)
    fut[date(2027, 1, 1)] = exp_after - 0.01
    fut[date(2027, 2, 1)] = exp_after - 0.01
    fut[date(2027, 3, 1)] = exp_after - 0.01
    res = implied_path(fut, 4.60, date(2026, 9, 30), -0.01,
                       [date(2026, 11, 3), date(2026, 12, 8)])
    assert res[1].implied_rate == pytest.approx(exp_after)
    assert res[1].move_fraction == pytest.approx(0.4)


def test_meeting_on_last_day_uses_next_month():
    # Effective 1st of month -> the whole month is at the new rate.
    fut = flat(date(2025, 9, 1), 3, 3.60)
    fut[date(2025, 10, 1)] = 3.35
    fut[date(2025, 11, 1)] = 3.35
    res = implied_path(fut, 3.60, date(2025, 9, 1), 0.0, [date(2025, 9, 30)])
    assert res[0].method == "full-month"
    assert res[0].change_bp == pytest.approx(-25)


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
    assert len(fut) == 6


def test_parse_rba_f11_and_spread():
    csv_text = "\n".join([
        "F1.1  INTEREST RATES AND YIELDS - MONEY MARKET,,,",
        "Title,Cash Rate Target,Change in the Cash Rate Target,Interbank Overnight Cash Rate",
        "Description,x,x,x", "Frequency,Daily,Daily,Daily",
        "Series ID,FIRMMCRTD,FIRMMCCRT,FIRMMCRID",
        "26-Sep-2026,4.35,0,4.34",
        "29-Sep-2026,4.35,0,4.34",
        "30-Sep-2026,4.60,0.25,",
    ])
    target, ibocr = parse_rba_f11(csv_text)
    t, t_asof, spread = rate_inputs(target, ibocr, date(2026, 9, 30))
    assert t == 4.60 and t_asof == date(2026, 9, 30)
    assert spread == pytest.approx(-0.01)
