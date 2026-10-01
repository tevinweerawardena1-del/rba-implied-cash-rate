"""Run with:  python -m pytest tests -q"""
import sys
from datetime import date, timedelta
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


def test_month_end_curves_max_four_and_rolling():
    # Month ends May-Sep 2026 plus a mid-October close.
    days = [date(2026, 5, 29), date(2026, 6, 30), date(2026, 7, 31), date(2026, 8, 31),
            date(2026, 9, 30), date(2026, 10, 15)]
    fh = {d: {date(2026, 11, 1): 4.6} for d in days}
    out = month_end_curves({d: fh[d] for d in days[:5]})
    assert [c["as_at"] for c in out] == ["2026-06-30", "2026-07-31", "2026-08-31", "2026-09-30"]
    out = month_end_curves(fh)                      # a month later: June rolls off
    assert [c["as_at"] for c in out] == ["2026-07-31", "2026-08-31", "2026-09-30", "2026-10-15"]


def test_align_respects_staleness():
    base = Series({date(2026, 9, 1): 1.0, date(2026, 9, 30): 2.0})
    other = Series({date(2026, 8, 1): 5.0, date(2026, 9, 29): 6.0})
    rows = align(base, {"o": other}, date(2026, 1, 1))
    assert rows[0]["o"] is None and rows[1]["o"] == 6.0


BOND_SAMPLE = "\n".join([
    "F16  INDICATIVE MID RATES OF AUSTRALIAN GOVERNMENT SECURITIES,,,,",
    "Title,Treasury Bond 167,Treasury Bond 150,Treasury Indexed Bond 20,Treasury Bond 168",
    "Description,Treasury Bond 4.25% 21-Apr-2027,Treasury Bond 1.75% 21-Jun-2051,"
    "Treasury Indexed Bond 2.5% 20-Sep-2030,Treasury Bond 3.25% 21-Jun-2039",
    "Issue date,10/01/2015,12/06/2020,20/09/2009,01/01/2018",
    "Publication date,25-Sep-2052,25-Sep-2052,25-Sep-2052,25-Sep-2052",
    "Series ID,A,B,C,D",
    "30/09/2025,3.40,4.90,1.9,4.70",
    "30/08/2026,4.40,5.30,2.0,5.10",
    "30/09/2026,4.50,5.40,2.1,5.20",
])


def test_bond_table_and_yield_curves():
    from sources import parse_bond_table
    from build import yield_curves
    bonds = parse_bond_table(BOND_SAMPLE)
    assert {b["name"] for b in bonds} == {"Treasury Bond 4.25% 21-Apr-2027", "Treasury Bond 1.75% 21-Jun-2051",
                                         "Treasury Bond 3.25% 21-Jun-2039"}
    mats = {b["maturity"]: b["name"] for b in bonds}
    assert date(2051, 6, 21) in mats
    curves = yield_curves(bonds)
    assert [c["label"] for c in curves] == ["Latest", "1 month earlier", "1 year earlier"]
    latest = curves[0]["points"]
    assert [p["maturity"] for p in latest] == ["2027-04-21", "2039-06-21", "2051-06-21"]
    assert latest[-1]["years"] == pytest.approx(24.72, abs=0.01) and latest[-1]["yield"] == 5.40


SCHEDULE_HTML = """
<h2>Monetary Policy Board Meeting Dates</h2>
<h3>2027</h3><ul><li>8&ndash;9 February</li><li>22–23 March</li><li>3–4 May</li><li>21–22 June</li>
<li>9–10 August</li><li>27–28 September</li><li>1–2 November</li><li>13–14 December</li></ul>
<h3>2025</h3><ul><li>17–18 February</li><li>31 March–1 April</li><li>19–20 May</li><li>7–8 July</li>
<li>11–12 August</li><li>29–30 September</li><li>3–4 November</li><li>8–9 December</li></ul>
<p>Updated 12 March 2026. Payments System Board: 5–6 March 2026</p>
"""


def test_parse_rba_schedule():
    from sources import parse_rba_schedule
    s = parse_rba_schedule(SCHEDULE_HTML)
    assert sorted(s) == [2025, 2027]                     # stray 2026 range ignored (<6 dates)
    assert s[2027][0] == date(2027, 2, 9) and s[2027][-1] == date(2027, 12, 14)
    assert date(2025, 4, 1) in s[2025]                    # cross-month range
    assert all(d.weekday() == 1 for ds in s.values() for d in ds)


def test_merge_meetings_prefers_published_years():
    from build import merge_meetings
    cfg = [date(2026, 11, 3), date(2027, 2, 9), date(2027, 3, 23)]
    scraped = {2027: [date(2027, 2, 9), date(2027, 3, 30)], 2028: [date(2028, 2, 8)]}
    assert merge_meetings(cfg, scraped) == [date(2026, 11, 3), date(2027, 2, 9),
                                            date(2027, 3, 30), date(2028, 2, 8)]
    assert merge_meetings(cfg, None) == cfg


def test_health_check():
    from build import health_check
    today = date(2026, 10, 10)
    last = {"ASX futures": date(2026, 10, 9), "RBA cash rate & bank bills (F1)": date(2026, 9, 1),
            "RBA bond yields (F2)": date(2026, 10, 1), "RBA bond lines (F16)": date(2026, 10, 1),
            "AUD/USD": date(2026, 10, 8), "US 2-year Treasury": None}
    meetings = [date(2027, 12, 14)]
    h = health_check(today, last, meetings, date(2028, 2, 1))
    assert any("F1" in w for w in h["warnings"]) and any("Treasury" in w for w in h["warnings"])
    assert not any("ASX" in w for w in h["warnings"])
    assert h["notes"] and "Feb 2028" in h["notes"][0]          # expected gap -> note only
    h = health_check(today, last, [date(2027, 6, 22)], date(2028, 2, 1))
    assert any("config/rba_meetings.json" in w for w in h["warnings"])


def test_everything_rolls_forward_in_time():
    """Simulate looking at the page in April 2027: the path, next-three-meeting
    history, probabilities and month-end curves must all start from then."""
    from build import outcome_probabilities, month_end_curves
    meetings = [date(2026, 11, 3), date(2026, 12, 8), date(2027, 2, 9), date(2027, 3, 23),
                date(2027, 5, 4), date(2027, 6, 22), date(2027, 8, 10), date(2027, 9, 28),
                date(2027, 11, 2), date(2027, 12, 14), date(2028, 2, 8), date(2028, 3, 21)]
    fh = {}
    for as_at in (date(2027, 1, 29), date(2027, 2, 26), date(2027, 3, 31), date(2027, 4, 15)):
        fh[as_at] = flat(month_start_of(as_at), 17, 4.85)
    target = Series({date(2027, 3, 24): 4.85})
    ibocr = Series({date(2027, 3, 24): 4.85})
    meeting_rows, horizon_rows = daily_analytics(fh, target, ibocr, meetings)
    latest = [r for r in meeting_rows if r["as_at"] == "2027-04-15"]
    assert latest[0]["decision_date"] == "2027-05-04"           # next meeting after April
    assert all(r["decision_date"] > "2027-04-15" for r in latest)
    res = implied_path(fh[date(2027, 4, 15)], 4.85, date(2027, 4, 15), 0.0, meetings)
    outs = outcome_probabilities([{"decision_date": r.decision_date, "implied_rate": r.implied_rate} for r in res], 4.85)
    assert [o["decision_date"] for o in outs][:2] == ["2027-05-04", "2027-06-22"]
    assert [c["as_at"] for c in month_end_curves(fh)] == ["2027-01-29", "2027-02-26", "2027-03-31", "2027-04-15"]
    assert horizon_rows[-1]["next_meeting"] == "2027-05-04"


def month_start_of(d):
    return d.replace(day=1)


def test_forward_rate_and_series():
    from build import forward_rate, forwards_series
    assert forward_rate(4.0, 1, 4.0, 2) == pytest.approx(4.0)
    assert forward_rate(4.0, 1, 5.0, 2) == pytest.approx(6.0096, abs=1e-3)
    d = date(2026, 9, 23)
    bonds = [{"maturity": date(2026 + n, 9, 23) + timedelta(days=round(0.25 * n)),
              "series": Series({d: 4.0 + 0.1 * n})} for n in (1, 2, 3, 4, 5, 7, 10, 12)]
    rows = forwards_series(bonds, date(2026, 1, 1))
    assert len(rows) == 1 and rows[0]["f1y1y"] > 4.1 and rows[0]["f5y5y"] > rows[0]["f1y1y"]


def test_real_cash_rate():
    from build import real_cash_rate
    target = Series({date(2026, 6, 1): 4.35, date(2026, 9, 30): 4.60})
    head = Series({date(2026, 6, 1): 3.8})
    tm = Series({date(2026, 6, 1): 3.2})
    rows = real_cash_rate(target, head, tm, date(2026, 1, 1))
    assert rows == [{"date": "2026-06-30", "cash_rate": 4.35, "cpi_ye": 3.8, "trimmed_mean_ye": 3.2,
                     "real_headline": 0.55, "real_trimmed_mean": 1.15}]


def test_pricing_scorecard():
    from build import pricing_scorecard
    target = Series({date(2026, 9, 28): 4.35, date(2026, 9, 29): 4.35, date(2026, 9, 30): 4.60})
    rows = [{"as_at": "2026-06-30", "decision_date": "2026-09-29", "change_bp": 5.0},
            {"as_at": "2026-08-29", "decision_date": "2026-09-29", "change_bp": 10.0},
            {"as_at": "2026-09-22", "decision_date": "2026-09-29", "change_bp": 20.0},
            {"as_at": "2026-09-28", "decision_date": "2026-09-29", "change_bp": 23.0},
            {"as_at": "2026-09-29", "decision_date": "2026-09-29", "change_bp": 25.0}]   # after the decision
    sc = pricing_scorecard(rows, target, [date(2026, 9, 29), date(2026, 11, 3)])
    assert len(sc["rows"]) == 1
    r = sc["rows"][0]
    assert r["actual_bp"] == 25 and r["priced_bp"] == {"3 months": 5.0, "1 month": 10.0, "1 week": 20.0, "1 day": 23.0}
    assert r["surprise_bp"] == 2.0 and sc["summary"]["1 day"]["mean_abs_miss_bp"] == 2.0
