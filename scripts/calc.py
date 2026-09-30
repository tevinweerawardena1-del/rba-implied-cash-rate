"""
Implied RBA cash rate path from ASX 30 Day Interbank Cash Rate Futures.

Pure calculation code (no network access) so it can be unit-tested.

The maths
---------
Each ASX IB futures contract settles on the AVERAGE Interbank Overnight Cash
Rate (IBOCR) over its calendar month, so

    implied monthly average = 100 - futures price

A new cash rate target applies from the day after the RBA's decision. For a
month containing an effective date, with D days in the month, d1 days at the
old rate and d2 = D - d1 days at the new rate:

    average = (d1 * r_before + d2 * r_after) / D
    =>  r_after = (average * D - r_before * d1) / d2

When d2 is small (a meeting late in the month) that division amplifies tiny
price errors, so wherever the month AFTER the meeting month contains no other
meeting, we read r_after straight off that "clean" month's contract instead.

The futures price the IBOCR, which trades a few basis points away from the
cash rate TARGET. We subtract today's spread (IBOCR - target) from every
contract so the results are in target terms.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass, asdict
from datetime import date, timedelta

# Minimum days at the new rate within the meeting month for the formula method.
MIN_DAYS_AFTER = 7


@dataclass
class MeetingResult:
    decision_date: str      # ISO date the decision is announced
    effective_date: str     # ISO date the new target applies from
    implied_rate: float     # expected cash rate target after this meeting (%)
    rate_before: float      # expected target going into this meeting (%)
    change_bp: float        # expected change AT this meeting (bp)
    cumulative_bp: float    # expected change vs today's target (bp)
    move_fraction: float    # change_bp / 25: +0.4 = 40% chance of a 25bp hike
    method: str             # "clean-month", "formula" or "full-month"


def month_start(d: date) -> date:
    return d.replace(day=1)


def next_month(d: date) -> date:
    y, m = d.year + (d.month == 12), d.month % 12 + 1
    return date(y, m, 1)


def days_in_month(d: date) -> int:
    return calendar.monthrange(d.year, d.month)[1]


def implied_path(
    futures: dict[date, float],
    target: float,
    target_as_of: date,
    spread: float,
    decision_dates: list[date],
) -> list[MeetingResult]:
    """
    futures        {first day of contract month: implied yield in %}
    target         current cash rate target (%)
    target_as_of   last date the target series covers; meetings whose new
                   rate takes effect after this date are treated as pending
    spread         IBOCR minus target (%), e.g. -0.01
    decision_dates RBA decision dates
    """
    adj = {month_start(m): y - spread for m, y in futures.items()}
    if not adj:
        return []

    last_month = max(adj)
    effective = sorted(d + timedelta(days=1) for d in decision_dates)
    pending = [e for e in effective
               if e > target_as_of and month_start(e) <= last_month]

    effective_months = {month_start(e) for e in effective}
    results: list[MeetingResult] = []
    r_prev = target

    for e in pending:
        m = month_start(e)
        if m not in adj:
            break
        nm = next_month(m)
        D = days_in_month(m)
        d1 = e.day - 1          # days in the month before the change
        d2 = D - d1

        if d1 == 0:
            r_after, method = adj[m], "full-month"
        elif nm in adj and nm not in effective_months:
            r_after, method = adj[nm], "clean-month"
        elif d2 >= MIN_DAYS_AFTER:
            r_after, method = (adj[m] * D - r_prev * d1) / d2, "formula"
        else:
            # Too few days at the new rate to back it out reliably (usually
            # the last meeting on the futures strip). Stop the path here.
            break

        change_bp = (r_after - r_prev) * 100
        results.append(MeetingResult(
            decision_date=(e - timedelta(days=1)).isoformat(),
            effective_date=e.isoformat(),
            implied_rate=round(r_after, 4),
            rate_before=round(r_prev, 4),
            change_bp=round(change_bp, 2),
            cumulative_bp=round((r_after - target) * 100, 2),
            move_fraction=round(change_bp / 25, 4),
            method=method,
        ))
        r_prev = r_after

    return results


def results_as_dicts(results: list[MeetingResult]) -> list[dict]:
    return [asdict(r) for r in results]
