"""Macro event calendar — keyless, known-in-advance information.

FOMC decision days are published by the Fed YEARS ahead, so this is the
cheapest legitimate information expansion available: perfectly point-in-time
(the schedule for session D was public long before D), zero API keys, zero
lookahead. Event days are regime days — announcement afternoons dominate the
open→close leg — and the day BEFORE carries the documented pre-FOMC drift.

Source: federalreserve.gov/monetarypolicy/fomccalendars.htm (+ the historical
pages per year). Dates below are the DECISION days (the final day of each
scheduled meeting, when the statement lands at 14:00 ET), 2015–2027, plus the
two famous 2020 emergency actions mapped to the first affected session.

Coverage is explicit: outside [COVERAGE_START, COVERAGE_END] the features
return None ("no read") rather than a silent 0 — sessions before 2015 simply
don't carry the variable instead of carrying a wrong one.
"""

from __future__ import annotations

import datetime as dt

COVERAGE_START = "2015-01-01"
COVERAGE_END = "2027-12-31"

# Final (decision/statement) day of every scheduled FOMC meeting.
FOMC_DECISION_DATES: frozenset[str] = frozenset({
    # 2015
    "2015-01-28", "2015-03-18", "2015-04-29", "2015-06-17",
    "2015-07-29", "2015-09-17", "2015-10-28", "2015-12-16",
    # 2016
    "2016-01-27", "2016-03-16", "2016-04-27", "2016-06-15",
    "2016-07-27", "2016-09-21", "2016-11-02", "2016-12-14",
    # 2017
    "2017-02-01", "2017-03-15", "2017-05-03", "2017-06-14",
    "2017-07-26", "2017-09-20", "2017-11-01", "2017-12-13",
    # 2018
    "2018-01-31", "2018-03-21", "2018-05-02", "2018-06-13",
    "2018-08-01", "2018-09-26", "2018-11-08", "2018-12-19",
    # 2019
    "2019-01-30", "2019-03-20", "2019-05-01", "2019-06-19",
    "2019-07-31", "2019-09-18", "2019-10-30", "2019-12-11",
    # 2020 (incl. the two emergency cuts: 3/3 intraday; 3/15 was a Sunday —
    # mapped to Monday 3/16, the first session trading on that information)
    "2020-01-29", "2020-03-03", "2020-03-16", "2020-04-29", "2020-06-10",
    "2020-07-29", "2020-09-16", "2020-11-05", "2020-12-16",
    # 2021
    "2021-01-27", "2021-03-17", "2021-04-28", "2021-06-16",
    "2021-07-28", "2021-09-22", "2021-11-03", "2021-12-15",
    # 2022
    "2022-01-26", "2022-03-16", "2022-05-04", "2022-06-15",
    "2022-07-27", "2022-09-21", "2022-11-02", "2022-12-14",
    # 2023
    "2023-02-01", "2023-03-22", "2023-05-03", "2023-06-14",
    "2023-07-26", "2023-09-20", "2023-11-01", "2023-12-13",
    # 2024
    "2024-01-31", "2024-03-20", "2024-05-01", "2024-06-12",
    "2024-07-31", "2024-09-18", "2024-11-07", "2024-12-18",
    # 2025
    "2025-01-29", "2025-03-19", "2025-05-07", "2025-06-18",
    "2025-07-30", "2025-09-17", "2025-10-29", "2025-12-10",
    # 2026
    "2026-01-28", "2026-03-18", "2026-04-29", "2026-06-17",
    "2026-07-29", "2026-09-16", "2026-10-28", "2026-12-09",
    # 2027
    "2027-01-27", "2027-03-17", "2027-04-28", "2027-06-09",
    "2027-07-28", "2027-09-15", "2027-10-27", "2027-12-08",
})


def _in_coverage(date: str) -> bool:
    return COVERAGE_START <= date <= COVERAGE_END


def _next_sessions(date: str, days: int = 3) -> list[str]:
    """The next few CALENDAR days that could be the following US session
    (weekends skipped; NYSE holidays approximated as ordinary weekdays)."""
    d = dt.date.fromisoformat(date)
    out = []
    step = d
    while len(out) < days:
        step += dt.timedelta(days=1)
        if step.weekday() < 5:
            out.append(step.isoformat())
    return out


def fomc_day(date: str) -> float | None:
    """1.0 when session `date` is an FOMC decision day; 0.0 otherwise;
    None outside table coverage (no read — never a fabricated zero)."""
    if not _in_coverage(date):
        return None
    return 1.0 if date in FOMC_DECISION_DATES else 0.0


def fomc_eve(date: str) -> float | None:
    """1.0 when the NEXT session is an FOMC decision day (the pre-FOMC drift
    window); approximated as 'the next weekday that is a decision day'."""
    if not _in_coverage(date):
        return None
    nxt = _next_sessions(date, days=1)[0]
    return 1.0 if nxt in FOMC_DECISION_DATES else 0.0
