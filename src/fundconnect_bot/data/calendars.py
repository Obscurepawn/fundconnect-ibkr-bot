"""DST-safe US/HK exchange calendars, holiday deferral, and SEC EDGAR PIT availability rules."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, time, timedelta
from functools import lru_cache
from typing import Any, Literal

import exchange_calendars as xcals  # type: ignore[import-untyped]

from ..config import HK_TZ, NY_TZ

type MarketCode = Literal["US", "HK"]


@lru_cache(maxsize=4)
def get_exchange_calendar(market: MarketCode) -> Any:
    """Return cached XNYS or XHKG exchange calendar."""
    code = "XNYS" if market == "US" else "XHKG"
    return xcals.get_calendar(code)


def is_market_session(
    market: MarketCode,
    day: date,
    *,
    hk_open_dates: frozenset[date] | set[date] | None = None,
) -> bool:
    """Check whether `day` is a regular trading session on XNYS or XHKG."""
    if market == "HK" and hk_open_dates is not None:
        return day in hk_open_dates
    cal = get_exchange_calendar(market)
    return bool(cal.is_session(day.isoformat()))


def resolve_execution_session(
    market: MarketCode,
    scheduled_day: date,
    *,
    hk_open_dates: frozenset[date] | set[date] | None = None,
) -> tuple[date, bool]:
    """Return `(valid_session, deferred_due_to_holiday)`.

    If `scheduled_day` (typically Monday) is an exchange holiday or weekend on XNYS/XHKG,
    automatically defers to the next valid trading session (e.g. Tuesday).
    """
    cursor = scheduled_day
    for _ in range(15):
        if is_market_session(market, cursor, hk_open_dates=hk_open_dates):
            return cursor, cursor != scheduled_day
        cursor += timedelta(days=1)
    raise ValueError(f"No open {market} session within 15 days from {scheduled_day}")


def next_market_session(
    market: MarketCode,
    after_day: date,
    *,
    hk_open_dates: frozenset[date] | set[date] | None = None,
) -> date:
    """Return strictly next open trading session `> after_day`."""
    resolved, _ = resolve_execution_session(
        market, after_day + timedelta(days=1), hk_open_dates=hk_open_dates
    )
    return resolved


def latest_completed_week_end(
    market: MarketCode,
    as_of: date,
    *,
    hk_open_dates: frozenset[date] | set[date] | None = None,
) -> date:
    """Find the most recent completed week-ending trading session `<= as_of`."""
    cursor = as_of
    for _ in range(15):
        if is_market_session(market, cursor, hk_open_dates=hk_open_dates):
            nxt = next_market_session(market, cursor, hk_open_dates=hk_open_dates)
            if cursor.isocalendar()[:2] != nxt.isocalendar()[:2]:
                return cursor
        cursor -= timedelta(days=1)
    raise ValueError(f"Unable to locate week-ending {market} session on or before {as_of}")


def us_open_in_hkt(session_day: date, *, offset_minutes: int = 0) -> datetime:
    """Convert 09:30 America/New_York on `session_day` into Asia/Hong_Kong (DST-aware).

    During US Daylight Saving Time (EDT, UTC-4), 09:30 ET == 21:30 HKT.
    During US Standard Time (EST, UTC-5), 09:30 ET == 22:30 HKT.
    """
    open_et = datetime.combine(session_day, time(9, 30), tzinfo=NY_TZ) + timedelta(
        minutes=offset_minutes
    )
    return open_et.astimezone(HK_TZ)


def hk_open_in_hkt(session_day: date, *, offset_minutes: int = 0) -> datetime:
    """Return 09:30 Asia/Hong_Kong (+ optional offset minutes) on `session_day`."""
    return datetime.combine(session_day, time(9, 30), tzinfo=HK_TZ) + timedelta(
        minutes=offset_minutes
    )


def us_close_and_cutoff(session_day: date) -> tuple[datetime, datetime]:
    """Return `(close_utc, cutoff_utc)` for an XNYS session (accounts for 13:00 ET half-days)."""
    cal = get_exchange_calendar("US")
    close_ny = cal.session_close(session_day.isoformat()).to_pydatetime().astimezone(NY_TZ)
    cutoff_ny = close_ny + timedelta(minutes=30)
    return close_ny.astimezone(UTC), cutoff_ny.astimezone(UTC)


def hk_close_time(session_day: date) -> datetime:
    """Return HKEX session close in HKT (accounts for Christmas/New Year/LNY Eve half-days)."""
    cal = get_exchange_calendar("HK")
    iso = session_day.isoformat()
    if cal.is_session(iso):
        return cal.session_close(iso).to_pydatetime().astimezone(HK_TZ)  # type: ignore[no-any-return]
    return datetime.combine(session_day, time(16, 0), tzinfo=HK_TZ)


def week_end_sessions(sessions: Sequence[date], next_session: date) -> frozenset[date]:
    """Identify ISO week-ending sessions identically to TradeMaster's week_end schedule."""
    following = (*sessions[1:], next_session)
    return frozenset(
        sess
        for sess, nxt in zip(sessions, following, strict=True)
        if sess.isocalendar()[:2] != nxt.isocalendar()[:2]
    )


def parse_sec_available_utc(
    row: Mapping[str, Any],
) -> tuple[datetime, date, datetime | None]:
    """Conservative UTC availability timestamp for a US SEC EDGAR quarterly filing.

    Interprets bare timestamps in `America/New_York` and takes `max(ny_to_utc, bare_utc)`
    so after-close 16:05 ET filings are never treated as visible before 16:00 ET close.
    """
    cal = get_exchange_calendar("US")
    filing_raw = row.get("filingDate")
    accepted_raw = row.get("acceptedDate")
    candidates: list[datetime] = []
    filing_day: date | None = None
    accepted_utc: datetime | None = None

    if filing_raw is not None and str(filing_raw).strip() != "":
        filing_day = date.fromisoformat(str(filing_raw)[:10])
        next_sess = cal.date_to_session(filing_day + timedelta(days=1), direction="next")
        filing_next_open = cal.session_open(next_sess).to_pydatetime().astimezone(UTC)
        if accepted_raw is None or str(accepted_raw).strip() == "":
            candidates.append(filing_next_open)

    if accepted_raw is not None and str(accepted_raw).strip() != "":
        txt = str(accepted_raw).strip().replace(" ", "T")
        parsed = datetime.fromisoformat(txt)
        if parsed.tzinfo is None:
            as_ny = parsed.replace(tzinfo=NY_TZ).astimezone(UTC)
            as_utc = parsed.replace(tzinfo=UTC)
            accepted_utc = max(as_ny, as_utc)
        else:
            accepted_utc = parsed.astimezone(UTC)
        candidates.append(accepted_utc)
        if filing_day is not None and filing_day > accepted_utc.astimezone(NY_TZ).date():
            next_sess = cal.date_to_session(filing_day + timedelta(days=1), direction="next")
            candidates.append(cal.session_open(next_sess).to_pydatetime().astimezone(UTC))

    if not candidates:
        raise ValueError(f"Missing SEC publication timestamps for {row.get('mapped_symbol')}")
    report_date = date.fromisoformat(str(row["date"])[:10])
    return max(candidates), report_date, accepted_utc
