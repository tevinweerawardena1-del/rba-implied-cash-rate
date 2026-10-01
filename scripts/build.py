"""
Builds every chart series from raw history. Pure functions (no network), so
the whole page can be regenerated from the stored futures history plus the
latest RBA/FRED downloads on every run.
"""

from __future__ import annotations

from datetime import date, timedelta

from calc import implied_path, results_as_dicts, month_start
from sources import Series

HORIZONS = (6, 12)              # months ahead for the "forward expectation" chart
MAX_STALE_DAYS = 7              # how old a matched observation may be when aligning series


def add_months(d: date, n: int) -> date:
    y, m = divmod(d.month - 1 + n, 12)
    return date(d.year + y, m + 1, 1)


def rate_inputs(target: Series, ibocr: Series, as_at: date):
    """Cash rate target, the date it's known to, and the IBOCR-target spread."""
    t = target.on_or_before(as_at)
    if t is None:
        return None
    target_as_of, tgt = t
    spread = 0.0
    i = ibocr.on_or_before(as_at)
    if i is not None:
        tgt_then = target.on_or_before(i[0])
        if tgt_then is not None:
            spread = round(i[1] - tgt_then[1], 4)
    return tgt, target_as_of, spread


def decisions(target: Series, start: date) -> list[dict]:
    """Cash rate target changes, dated by the decision (day before effective)."""
    out, prev = [], None
    for d, v in target.items():
        if prev is not None and abs(v - prev) > 1e-9 and d >= start:
            out.append({"date": (d - timedelta(days=1)).isoformat(),
                        "change_bp": round((v - prev) * 100), "rate": v})
        prev = v
    return out


def daily_analytics(futures_hist: dict[date, dict[date, float]],
                    target: Series, ibocr: Series, meetings: list[date]):
    """Per pricing day: meeting-by-meeting path, horizon changes, next meeting."""
    meeting_rows, horizon_rows = [], []
    for as_at in sorted(futures_hist):
        fut = futures_hist[as_at]
        ri = rate_inputs(target, ibocr, as_at)
        if ri is None:
            continue
        tgt, tgt_asof, spread = ri
        res = results_as_dicts(implied_path(fut, tgt, tgt_asof, spread, meetings))
        for r in res:
            meeting_rows.append({"as_at": as_at.isoformat(), **{
                k: r[k] for k in ("decision_date", "implied_rate", "change_bp",
                                  "cumulative_bp")}})
        row = {"as_at": as_at.isoformat(), "target": tgt,
               "spread_bp": round(spread * 100, 2)}
        base = month_start(as_at)
        for h in HORIZONS:
            y = fut.get(add_months(base, h))
            row[f"h{h}_bp"] = None if y is None else round(((y - spread) - tgt) * 100, 2)
        # Furthest contract on the strip (usually ~16-17 months ahead; the
        # strip never reaches a full 18 months).
        far = max(fut)
        row["hend_bp"] = round(((fut[far] - spread) - tgt) * 100, 2)
        row["hend_months"] = (far.year - base.year) * 12 + far.month - base.month
        if res:
            nxt = res[0]
            row["next_meeting"] = nxt["decision_date"]
            row["next_meeting_bp"] = nxt["change_bp"]
            row["days_to_meeting"] = (date.fromisoformat(nxt["decision_date"]) - as_at).days
        horizon_rows.append(row)
    return meeting_rows, horizon_rows


def month_end_curves(futures_hist: dict[date, dict[date, float]], n_months: int = 3):
    """The futures strip at the last pricing day of each of the previous
    n_months calendar months, plus the latest day: at most n_months + 1 curves
    (4 by default). Each new month the oldest curve rolls off."""
    days = sorted(futures_hist)
    if not days:
        return []
    latest = days[-1]
    picks = []
    for k in range(n_months, 0, -1):
        m_start = add_months(month_start(latest), -k)
        m_end = add_months(m_start, 1)
        in_month = [d for d in days if m_start <= d < m_end]
        if in_month:
            picks.append(in_month[-1])
    picks.append(latest)
    return [{"as_at": d.isoformat(), "latest": d == latest,
             "points": [{"month": m.isoformat(), "yield": y}
                        for m, y in sorted(futures_hist[d].items())]}
            for d in picks]


def outcome_distribution(rate_before: float, implied: float, step: float = 0.25):
    """Split an implied rate into the two nearest 25bp outcomes."""
    k = (implied - rate_before) / step
    lo = int(k // 1)
    p_hi = k - lo
    out = []
    for n, p in ((lo, 1 - p_hi), (lo + 1, p_hi)):
        if p > 0.005:
            out.append({"rate": round(rate_before + n * step, 4), "prob": round(p, 4)})
    return out


def outcome_probabilities(latest_results: list[dict], target: float, n: int = 4):
    """For each of the next n meetings: probabilities of the cash rate target
    level after that meeting, relative to today's target."""
    return [{"decision_date": r["decision_date"],
             "implied_rate": r["implied_rate"],
             "outcomes": outcome_distribution(target, r["implied_rate"])}
            for r in latest_results[:n]]


def align(base: Series, others: dict[str, Series], start: date) -> list[dict]:
    """Rows on base's dates with the latest value of each other series
    (if no older than MAX_STALE_DAYS)."""
    rows = []
    for d, v in base.items(start):
        row = {"date": d.isoformat(), "base": v}
        for k, s in others.items():
            hit = s.on_or_before(d)
            row[k] = hit[1] if hit and (d - hit[0]).days <= MAX_STALE_DAYS else None
        rows.append(row)
    return rows


def yield_curves(bonds: list[dict], lookbacks=((0, "Latest"), (30, "1 month earlier"),
                                               (365, "1 year earlier")),
                 min_years: float = 1 / 12) -> list[dict]:
    """Government bond yield curves by years to maturity, at the latest bond
    date and at earlier dates. Each point is one bond's yield on that day."""
    last = max((b["series"].last_date for b in bonds if b["series"]), default=None)
    if last is None:
        return []
    curves = []
    for days_back, label in lookbacks:
        ref = last - timedelta(days=days_back)
        pts = []
        for b in bonds:
            hit = b["series"].on_or_before(ref)
            if not hit or (ref - hit[0]).days > MAX_STALE_DAYS:
                continue
            years = (b["maturity"] - ref).days / 365.25
            if years < min_years:
                continue
            pts.append({"years": round(years, 3), "yield": hit[1], "name": b["name"],
                        "maturity": b["maturity"].isoformat()})
        if len(pts) >= 3:
            curves.append({"label": label, "as_at": ref.isoformat(),
                           "latest": days_back == 0,
                           "points": sorted(pts, key=lambda p: p["years"])})
    return curves


# --------------------------------------------------------------------------
# Long-run upkeep: meeting schedule and data health
# --------------------------------------------------------------------------

def merge_meetings(config_dates: list[date], scraped: dict[int, list[date]] | None) -> list[date]:
    """Config dates, with any year the RBA has published replaced by the RBA's
    own list (so rescheduled meetings are picked up automatically)."""
    scraped = scraped or {}
    keep = [d for d in config_dates if d.year not in scraped]
    return sorted(set(keep) | {d for ds in scraped.values() for d in ds})


# Calendar days a source may lag today before it's flagged as stale.
STALE_AFTER = {"ASX futures": 6, "RBA cash rate & bank bills (F1)": 8,
               "RBA bond yields (F2)": 28, "RBA bond lines (F16)": 28, "AUD/USD": 10,
               "US 2-year Treasury": 10, "US 10-year Treasury": 10,
               # Quarterly CPI arrives about 4 weeks after each quarter ends.
               "RBA CPI (G1)": 135,
               # Monthly labour force, released ~3 weeks after month end.
               "RBA unemployment (H5)": 60,
               # The Fed Board updates the Kim-Wright series weekly.
               "US term premium (Kim-Wright)": 21}


def health_check(today: date, last_dates: dict[str, date | None], meetings: list[date],
                 last_contract_month: date) -> dict:
    """Warnings (things that need fixing) and notes (expected gaps)."""
    warnings, notes = [], []
    for name, limit in STALE_AFTER.items():
        d = last_dates.get(name)
        if d is None:
            warnings.append(f"{name}: no data")
        elif (today - d).days > limit:
            warnings.append(f"{name}: last data {d.isoformat()} ({(today - d).days} days old)")
    strip_end = add_months(last_contract_month, 1) - timedelta(days=1)
    last_meeting = max(meetings) if meetings else None
    if last_meeting is None or (strip_end - last_meeting).days > 70:
        msg = (f"Futures run to {strip_end:%b %Y} but the last RBA meeting date on file is "
               f"{last_meeting:%d %b %Y}" if last_meeting else "No RBA meeting dates on file")
        # The RBA publishes each year's dates well ahead; a gap of more than ~6 months
        # means the schedule isn't being picked up and needs attention.
        if last_meeting is None or (strip_end - last_meeting).days > 180:
            warnings.append(msg + ". Add dates to config/rba_meetings.json.")
        else:
            notes.append(msg + "; later meetings will appear once the RBA publishes them.")
    return {"checked": today.isoformat(), "warnings": warnings, "notes": notes}


# --------------------------------------------------------------------------
# Forward rates from the government bond curve
# --------------------------------------------------------------------------

def _interp(points: list[tuple[float, float]], t: float) -> float | None:
    """Linear interpolation of yield on years to maturity (no extrapolation)."""
    if not points or t < points[0][0] or t > points[-1][0]:
        return None
    for (t0, y0), (t1, y1) in zip(points, points[1:]):
        if t0 <= t <= t1:
            return y0 if t1 == t0 else y0 + (y1 - y0) * (t - t0) / (t1 - t0)
    return points[-1][1]


def forward_rate(ya: float, ta: float, yb: float, tb: float) -> float:
    """Annually compounded forward rate (%) between ta and tb years."""
    return (((1 + yb / 100) ** tb / (1 + ya / 100) ** ta) ** (1 / (tb - ta)) - 1) * 100


def forwards_series(bonds: list[dict], start: date, min_years: float = 0.25) -> list[dict]:
    """Daily 1y1y, 2y1y and 5y5y forwards. Each day, that day's bond yields are
    interpolated by maturity to get 1, 2, 3, 5 and 10-year rates (treating
    yields as zero rates, a standard approximation), then forwards follow."""
    all_dates = sorted({d for b in bonds for d in b["series"].dates if d >= start})
    rows = []
    for d in all_dates:
        pts = []
        for b in bonds:
            hit = b["series"].on_or_before(d)
            if not hit or hit[0] != d:
                continue
            t = (b["maturity"] - d).days / 365.25
            if t >= min_years:
                pts.append((t, hit[1]))
        pts.sort()
        if len(pts) < 6:
            continue
        y = {n: _interp(pts, n) for n in (1, 2, 3, 5, 10)}
        if None in y.values():
            continue
        rows.append({"date": d.isoformat(),
                     "f1y1y": round(forward_rate(y[1], 1, y[2], 2), 4),
                     "f2y1y": round(forward_rate(y[2], 2, y[3], 3), 4),
                     "f5y5y": round(forward_rate(y[5], 5, y[10], 10), 4)})
    return rows


# --------------------------------------------------------------------------
# Real cash rate
# --------------------------------------------------------------------------

def month_end(d: date) -> date:
    return add_months(d.replace(day=1), 1) - timedelta(days=1)


def real_cash_rate(target: Series, headline: Series, trimmed: Series, start: date) -> list[dict]:
    """Ex-post real cash rate each quarter: cash rate target at the end of the
    quarter minus year-ended inflation for that quarter."""
    rows = []
    for d, h in headline.items(start):
        q_end = month_end(d)
        t = target.on_or_before(q_end)
        if t is None:
            continue
        tm = trimmed.on_or_before(d)
        tm = tm[1] if tm and tm[0] == d else None
        rows.append({"date": q_end.isoformat(), "cash_rate": t[1], "cpi_ye": h,
                     "trimmed_mean_ye": tm,
                     "real_headline": round(t[1] - h, 3),
                     "real_trimmed_mean": None if tm is None else round(t[1] - tm, 3)})
    return rows


# --------------------------------------------------------------------------
# Market pricing scorecard
# --------------------------------------------------------------------------

SCORE_HORIZONS = ((91, "3 months"), (30, "1 month"), (7, "1 week"), (1, "1 day"))


def pricing_scorecard(meeting_rows: list[dict], target: Series, meetings: list[date]) -> dict:
    """For each RBA decision with a known outcome: the change futures priced
    for that meeting at several horizons before it, versus the actual change."""
    by_meeting: dict[str, list[tuple[str, float]]] = {}
    for r in meeting_rows:
        by_meeting.setdefault(r["decision_date"], []).append((r["as_at"], r["change_bp"]))
    for v in by_meeting.values():
        v.sort()
    rows = []
    last_known = target.last_date
    for d in meetings:
        if last_known is None or d + timedelta(days=1) > last_known:
            continue
        before, after = target.on_or_before(d), target.on_or_before(d + timedelta(days=1))
        if not before or not after:
            continue
        hist = by_meeting.get(d.isoformat(), [])
        priced = {}
        for days, label in SCORE_HORIZONS:
            cutoff = (d - timedelta(days=days)).isoformat()
            # Latest close on or before the cutoff (and not more than a week earlier).
            cands = [(a, c) for a, c in hist if a <= cutoff
                     and (date.fromisoformat(cutoff) - date.fromisoformat(a)).days <= 7]
            priced[label] = round(cands[-1][1], 1) if cands else None
        if all(v is None for v in priced.values()):
            continue
        actual = round((after[1] - before[1]) * 100)
        day_before = priced["1 day"]
        rows.append({"decision_date": d.isoformat(), "actual_bp": actual,
                     "rate_after": after[1], "priced_bp": priced,
                     "surprise_bp": None if day_before is None else round(actual - day_before, 1)})
    summary = {}
    for _, label in SCORE_HORIZONS:
        errs = [abs(r["actual_bp"] - r["priced_bp"][label]) for r in rows
                if r["priced_bp"][label] is not None]
        summary[label] = {"mean_abs_miss_bp": round(sum(errs) / len(errs), 1) if errs else None,
                          "n": len(errs)}
    return {"rows": rows, "summary": summary, "horizons": [l for _, l in SCORE_HORIZONS]}


# --------------------------------------------------------------------------
# Taylor rule
# --------------------------------------------------------------------------

def taylor_rate(pi: float, u: float, p: dict) -> float:
    return (p["neutral_real_rate"] + pi
            + p["inflation_gap_weight"] * (pi - p["inflation_target"])
            - p["unemployment_gap_weight"] * (u - p["nairu"]))


def taylor_series(target: Series, trimmed: Series, unemp: Series, p: dict) -> list[dict]:
    """Monthly: actual cash rate target and Taylor rule rate at each month end,
    using the latest inflation (quarterly) and unemployment (monthly) known by then."""
    start = date.fromisoformat(p["start"])
    if not unemp or not trimmed:
        return []
    end = unemp.last_date
    rows, m = [], month_end(start)
    while m <= month_end(end):
        pi, u, i = trimmed.on_or_before(m), unemp.on_or_before(m), target.on_or_before(m)
        if pi and u and i and (m - pi[0]).days <= 200 and (m - u[0]).days <= 62:
            rows.append({"date": m.isoformat(), "cash_rate": i[1],
                         "taylor": round(taylor_rate(pi[1], u[1], p), 3),
                         "inflation": pi[1], "inflation_asof": pi[0].isoformat(),
                         "unemployment": u[1]})
        m = month_end(add_months(m.replace(day=1), 1))
    return rows


def taylor_projection(rows: list[dict], target_now: float, p: dict) -> dict | None:
    """Next month: Taylor rate with the latest inputs carried forward. Next
    meeting: smoothed rule i_next = rho_m*i + (1-rho_m)*i*, rho_m = rho_q^(1/meetings per quarter)."""
    if not rows:
        return None
    last = rows[-1]
    nxt = month_end(add_months(date.fromisoformat(last["date"]).replace(day=1), 1))
    rho_m = p["smoothing_per_quarter"] ** (1 / p["meetings_per_quarter"])
    smoothed = rho_m * target_now + (1 - rho_m) * last["taylor"]
    change_bp = (smoothed - target_now) * 100
    return {"next_month": nxt.isoformat(), "taylor_next_month": last["taylor"],
            "smoothed_next_meeting": round(smoothed, 3), "smoothed_change_bp": round(change_bp, 1),
            "gap_bp": round((last["taylor"] - target_now) * 100, 1), "rho_per_meeting": round(rho_m, 3),
            "inputs": {"inflation": last["inflation"], "inflation_asof": last["inflation_asof"],
                       "unemployment": last["unemployment"], "unemployment_asof": last["date"]}}


# --------------------------------------------------------------------------
# US 10-year term premium decomposition
# --------------------------------------------------------------------------

def term_premium_series(kw_yield: Series, kw_tp: Series, acm_tp: Series, start: date) -> list[dict]:
    """10-year zero-coupon yield = expected average short rate + term premium
    (Kim-Wright model, Federal Reserve Board). ACM (NY Fed) term premium added
    as a second estimate where available."""
    rows = []
    for d, y in kw_yield.items(start):
        tp = kw_tp.on_or_before(d)
        if not tp or tp[0] != d:
            continue
        acm = acm_tp.on_or_before(d) if acm_tp else None
        rows.append({"date": d.isoformat(), "yield10": round(y, 4),
                     "expected": round(y - tp[1], 4), "tp_kw": round(tp[1], 4),
                     "tp_acm": round(acm[1], 4) if acm and (d - acm[0]).days <= 7 else None})
    return rows
