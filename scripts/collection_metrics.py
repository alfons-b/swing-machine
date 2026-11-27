"""One markdown row of aggregate collection metrics (counts only, no vendor data)."""

from __future__ import annotations

import sys
from datetime import UTC, datetime

from sqlalchemy import func, select

from idx_trading.db.models import (
    CorporateAction,
    DailyPrice,
    DataQualityIssue,
    FinancialReport,
    Instrument,
)
from idx_trading.db.session import make_engine, make_session_factory, session_scope


def main(status: str) -> None:
    eng = make_engine()
    try:
        with session_scope(make_session_factory(eng)) as s:
            per = (
                select(DailyPrice.instrument_id, func.count().label("n"))
                .where(DailyPrice.is_current.is_(True), DailyPrice.validation_status != "invalid")
                .group_by(DailyPrice.instrument_id)
                .subquery()
            )
            covered = s.scalar(select(func.count()).select_from(per).where(per.c.n >= 200)) or 0
            bars = s.scalar(select(func.count()).select_from(DailyPrice)) or 0
            latest = s.scalar(select(func.max(DailyPrice.trade_date)))
            instruments = s.scalar(select(func.count()).select_from(Instrument)) or 0
            ca = s.scalar(select(func.count(func.distinct(CorporateAction.instrument_id)))) or 0
            reports = s.scalar(select(func.count()).select_from(FinancialReport)) or 0
            open_err = (
                s.scalar(
                    select(func.count())
                    .select_from(DataQualityIssue)
                    .where(DataQualityIssue.status == "open", DataQualityIssue.severity == "error")
                )
                or 0
            )
    finally:
        eng.dispose()
    print(
        f"| {datetime.now(tz=UTC):%Y-%m-%d %H:%M} | {status} | {instruments} | {covered} | {bars:,} | "
        f"{latest} | {ca} | {reports} | {open_err} |"
    )


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "?")
