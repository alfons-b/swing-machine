"""`idx` command-line interface (thin layer over the services)."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import typer
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from idx_trading.clock import exchange_today
from idx_trading.config import get_settings
from idx_trading.db.enums import PermissionPurpose
from idx_trading.db.session import make_engine, make_session_factory, session_scope
from idx_trading.errors import IdxTradingError
from idx_trading.logging_setup import configure_logging

app = typer.Typer(help="IDX AI Trading Intelligence — data foundation CLI", no_args_is_help=True)
db_app = typer.Typer(help="Database migrations", no_args_is_help=True)
sources_app = typer.Typer(help="Data source registry", no_args_is_help=True)
cal_app = typer.Typer(help="Trading calendar", no_args_is_help=True)
inst_app = typer.Typer(help="Instrument master", no_args_is_help=True)
ingest_app = typer.Typer(help="EOD ingestion", no_args_is_help=True)
for sub, name in (
    (db_app, "db"),
    (sources_app, "sources"),
    (cal_app, "calendar"),
    (inst_app, "instruments"),
    (ingest_app, "ingest"),
):
    app.add_typer(sub, name=name)


@contextmanager
def _db() -> Iterator[Session]:
    """One engine per command, always disposed (no leaked connections)."""
    engine = make_engine()
    try:
        with session_scope(make_session_factory(engine)) as s:
            yield s
    finally:
        engine.dispose()


@contextmanager
def _factory() -> Iterator[sessionmaker[Session]]:
    engine = make_engine()
    try:
        yield make_session_factory(engine)
    finally:
        engine.dispose()


@app.callback()
def _main() -> None:
    s = get_settings()
    configure_logging(s.log_level, s.log_json)


def _fail(exc: Exception) -> None:
    typer.secho(f"error: {exc}", fg=typer.colors.RED, err=True)
    raise typer.Exit(code=1)


# ------------------------------------------------------------------------- db
@db_app.command("upgrade")
def db_upgrade(revision: str = "head") -> None:
    """Apply migrations to IDX_DATABASE_URL."""
    from alembic import command
    from alembic.config import Config

    root = Path(__file__).resolve().parents[3]
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", get_settings().database_url)
    command.upgrade(cfg, revision)


# -------------------------------------------------------------------- sources
@sources_app.command("sync")
def sources_sync(path: Annotated[Path | None, typer.Option()] = None) -> None:
    """Mirror config/sources.yaml into the database."""
    from idx_trading.registry import load_registry, sync_registry

    try:
        reg = load_registry(path or get_settings().sources_file)
        with _db() as s:
            r = sync_registry(s, reg)
    except IdxTradingError as exc:
        _fail(exc)
    typer.echo(
        f"created={r.created} updated={r.updated} unchanged={len(r.unchanged)} "
        f"inactive(missing from file)={r.missing_from_file}"
    )


@sources_app.command("list")
def sources_list() -> None:
    from idx_trading.db.models import DataSource

    with _db() as s:
        for src in s.scalars(select(DataSource).order_by(DataSource.source_id)):
            perms = ", ".join(f"{p.purpose}={p.status}" for p in src.permissions) or "-"
            typer.echo(f"{src.source_id:24} {src.source_status:15} {perms}")


# ------------------------------------------------------------------- calendar
@cal_app.command("seed")
def calendar_seed(
    start: Annotated[datetime, typer.Option(formats=["%Y-%m-%d"])],
    end: Annotated[datetime, typer.Option(formats=["%Y-%m-%d"])],
) -> None:
    """Seed the XIDX calendar from exchange_calendars."""
    from idx_trading.calendar import seed_from_exchange_calendars

    try:
        with _db() as s:
            code = get_settings().calendar_code
            n = seed_from_exchange_calendars(s, start.date(), end.date(), code)
    except IdxTradingError as exc:
        _fail(exc)
    typer.echo(f"calendar rows written: {n}")


# ---------------------------------------------------------------- instruments
@inst_app.command("import")
def instruments_import(
    csv_path: Path,
    ticker_changes: Annotated[Path | None, typer.Option()] = None,
    source: str = "manual_csv",
) -> None:
    """Import instruments (and optional ticker changes) from CSV files."""
    from idx_trading.instruments import InstrumentService
    from idx_trading.instruments.importer import import_instruments, import_ticker_changes

    try:
        with _db() as s:
            svc = InstrumentService(s)
            r = import_instruments(svc, csv_path, source)
            typer.echo(f"instruments: created={r.created} updated={r.updated} unchanged={r.unchanged}")
            if ticker_changes:
                t = import_ticker_changes(svc, ticker_changes, source)
                typer.echo(f"ticker changes: applied={t.updated} already_present={t.unchanged}")
    except IdxTradingError as exc:
        _fail(exc)


@inst_app.command("universe")
def instruments_universe(
    on: Annotated[datetime | None, typer.Option(formats=["%Y-%m-%d"])] = None,
    eligible_only: bool = False,
) -> None:
    from idx_trading.instruments import InstrumentService

    d = on.date() if on else exchange_today()
    with _db() as s:
        members = InstrumentService(s).universe_on(d)
    for m in members:
        if eligible_only and not m.eligible:
            continue
        typer.echo(f"{m.ticker:8} {m.status.value:10} {'ELIGIBLE' if m.eligible else m.reason}")
    typer.echo(f"total={len(members)} eligible={sum(m.eligible for m in members)}")


# --------------------------------------------------------------------- ingest
@ingest_app.command("csv")
def ingest_csv(
    files: list[Path],
    profile: str = "standard",
    trade_date: Annotated[str | None, typer.Option(help="Date for files without a date column")] = None,
    decimal: str = ".",
    provider: Annotated[str | None, typer.Option(help="Where the file came from")] = None,
    supersede: Annotated[bool, typer.Option(help="Store revisions as new versions")] = False,
) -> None:
    """Ingest user-supplied EOD CSV files."""
    from idx_trading.ingestion.adapters import CsvEodAdapter
    from idx_trading.ingestion.archive import RawArchive
    from idx_trading.ingestion.pipeline import EodIngestionPipeline
    from idx_trading.ingestion.schemas import FetchRequest
    from idx_trading.ingestion.writer import RevisionPolicy

    st = get_settings()
    req = FetchRequest(
        paths=tuple(str(f) for f in files),
        options={"trade_date": trade_date, "decimal": decimal, "provider": provider},
    )
    try:
        with _factory() as factory:
            pipe = EodIngestionPipeline(
                factory,
                RawArchive(st.raw_archive_dir),
                extreme_move_threshold=st.extreme_move_threshold,
                calendar_code=st.calendar_code,
            )
            summary = pipe.run(
                CsvEodAdapter(profile),
                req,
                policy=RevisionPolicy.SUPERSEDE if supersede else RevisionPolicy.FLAG,
            )
    except IdxTradingError as exc:
        _fail(exc)
    typer.echo(f"run {summary.run_id} {summary.status.value}: {summary.counts}")
    if summary.error:
        typer.echo(summary.error)
    if summary.status.value == "failed":
        raise typer.Exit(code=2)


@ingest_app.command("runs")
def ingest_runs(limit: int = 20) -> None:
    from idx_trading.db.models import IngestionRun

    with _db() as s:
        q = select(IngestionRun).order_by(IngestionRun.run_id.desc()).limit(limit)
        for r in s.scalars(q):
            typer.echo(
                f"{r.run_id:5} {r.started_at:%Y-%m-%d %H:%M} {r.source_id:14} {r.status:9} "
                f"recv={r.records_received} ins={r.records_inserted} same={r.records_unchanged} "
                f"conf={r.records_conflicted} rej={r.records_rejected} warn={r.records_warned}"
            )


@ingest_app.command("freshness")
def ingest_freshness(source: str = "manual_csv") -> None:
    """Report the expected latest session vs stored data, and catch-up gaps."""
    from idx_trading.ingestion.coverage import check_freshness

    st = get_settings()
    with _db() as s:
        f = check_freshness(
            s,
            source,
            datetime.now(tz=UTC),
            st.eod_ready_buffer_minutes,
            calendar_code=st.calendar_code,
        )
    typer.echo(
        f"expected={f.expected_session} latest_stored={f.latest_stored} stale={f.is_stale} "
        f"missing={[d.isoformat() for d in f.missing_sessions]}"
    )


def _eodhd_parts(budget: int | None):
    from idx_trading.ingestion.adapters.eodhd import CallBudget
    from idx_trading.ingestion.http import HttpClient

    st = get_settings()
    if st.eodhd_api_token is None or not st.eodhd_api_token.get_secret_value():
        _fail(IdxTradingError("IDX_EODHD_API_TOKEN is not set (see docs/adr/0008)"))
    assert st.eodhd_api_token is not None
    # EODHD allows 1,000 requests/minute; stay far below it.
    client = HttpClient(min_interval_seconds=0.25)
    return st, st.eodhd_api_token.get_secret_value(), client, CallBudget(budget or st.eodhd_daily_call_budget)


@ingest_app.command("eodhd")
def ingest_eodhd(
    tickers: Annotated[str | None, typer.Option(help="Comma-separated IDX tickers")] = None,
    universe: Annotated[bool, typer.Option(help="All eligible instruments on --end")] = False,
    end: Annotated[datetime | None, typer.Option(formats=["%Y-%m-%d"])] = None,
    budget: Annotated[int | None, typer.Option(help="Max API calls for this run")] = None,
    dry_run: Annotated[bool, typer.Option(help="Print the plan; send nothing")] = False,
    supersede: bool = False,
) -> None:
    """Per-ticker EOD refresh from EODHD (works on the Free plan; stalest first)."""
    from datetime import timedelta

    from idx_trading.calendar import CalendarService
    from idx_trading.clock import now_utc
    from idx_trading.ingestion.adapters.eodhd import EodhdEodAdapter, TickerWindow
    from idx_trading.ingestion.archive import RawArchive
    from idx_trading.ingestion.coverage import plan_refresh
    from idx_trading.ingestion.pipeline import EodIngestionPipeline
    from idx_trading.ingestion.schemas import FetchRequest
    from idx_trading.ingestion.writer import RevisionPolicy
    from idx_trading.instruments import InstrumentService

    if bool(tickers) == universe:
        _fail(IdxTradingError("give exactly one of --tickers or --universe"))
    st = get_settings()
    with _db() as s:
        if end is not None:
            target = end.date()
        else:
            found = CalendarService(s, st.calendar_code).latest_completed_session(
                now_utc(), st.eod_ready_buffer_minutes
            )
            if found is None:
                _fail(IdxTradingError("no completed session in the calendar; seed it first"))
            assert found is not None
            target = found
        svc = InstrumentService(s)
        if universe:
            members = [(m.ticker, m.instrument_id) for m in svc.universe_on(target) if m.eligible]
        else:
            assert tickers is not None
            members = []
            for t in (x.strip() for x in tickers.split(",") if x.strip()):
                iid = svc.try_resolve(t, target)
                if iid is None:
                    _fail(IdxTradingError(f"{t} is not in the instrument master on {target}"))
                assert iid is not None
                members.append((t.upper(), iid))
        cap = budget or st.eodhd_daily_call_budget
        history_start = target - timedelta(days=st.eodhd_history_days - 1)
        plan = plan_refresh(s, "eodhd", members, target, cap, history_start)
    up_to_date = len(members) - len(plan)
    typer.echo(f"target={target} candidates={len(members)} planned={len(plan)} budget={cap}")
    for item in plan:
        typer.echo(f"  {item.ticker:8} from {item.start} (stored up to {item.latest_stored})")
    if dry_run or not plan:
        if not plan:
            typer.echo(f"nothing to do ({up_to_date} already current)")
        return

    st, token, client, call_budget = _eodhd_parts(cap)
    adapter = EodhdEodAdapter(
        token, client, call_budget, exchange=st.eodhd_exchange, base_url=st.eodhd_base_url
    )
    req = FetchRequest(
        end_date=target,
        options={
            "mode": "per_ticker",
            "windows": [TickerWindow(i.ticker, i.start, target) for i in plan],
        },
    )
    try:
        with _factory() as factory:
            summary = EodIngestionPipeline(
                factory,
                RawArchive(st.raw_archive_dir),
                extreme_move_threshold=st.extreme_move_threshold,
                calendar_code=st.calendar_code,
            ).run(
                adapter,
                req,
                job_name="eodhd_per_ticker",
                policy=RevisionPolicy.SUPERSEDE if supersede else RevisionPolicy.FLAG,
            )
    except IdxTradingError as exc:
        _fail(exc)
    typer.echo(f"run {summary.run_id} {summary.status.value}: {summary.counts} calls={call_budget.used}")
    if summary.status.value == "failed":
        raise typer.Exit(code=2)


@ingest_app.command("eodhd-bulk")
def ingest_eodhd_bulk(
    on: Annotated[datetime, typer.Option("--date", formats=["%Y-%m-%d"])],
    supersede: bool = False,
) -> None:
    """Whole-exchange EOD for one session (100 API calls; EODHD paid plans only)."""
    from idx_trading.ingestion.adapters.eodhd import COST_BULK, EodhdEodAdapter
    from idx_trading.ingestion.archive import RawArchive
    from idx_trading.ingestion.pipeline import EodIngestionPipeline
    from idx_trading.ingestion.schemas import FetchRequest
    from idx_trading.ingestion.writer import RevisionPolicy

    st, token, client, call_budget = _eodhd_parts(None)
    if call_budget.limit < COST_BULK:
        _fail(
            IdxTradingError(
                f"bulk costs {COST_BULK} calls; budget is {call_budget.limit} "
                "(bulk is not part of the EODHD Free plan)"
            )
        )
    adapter = EodhdEodAdapter(
        token, client, call_budget, exchange=st.eodhd_exchange, base_url=st.eodhd_base_url
    )
    try:
        with _factory() as factory:
            summary = EodIngestionPipeline(
                factory,
                RawArchive(st.raw_archive_dir),
                extreme_move_threshold=st.extreme_move_threshold,
                calendar_code=st.calendar_code,
            ).run(
                adapter,
                FetchRequest(end_date=on.date(), options={"mode": "bulk"}),
                job_name="eodhd_bulk",
                policy=RevisionPolicy.SUPERSEDE if supersede else RevisionPolicy.FLAG,
            )
    except IdxTradingError as exc:
        _fail(exc)
    typer.echo(f"run {summary.run_id} {summary.status.value}: {summary.counts}")
    if summary.status.value == "failed":
        raise typer.Exit(code=2)


@inst_app.command("eodhd-diff")
def instruments_eodhd_diff(
    delisted: Annotated[bool, typer.Option(help="Also compare EODHD's delisted list")] = False,
) -> None:
    """Compare EODHD's JK ticker list with the instrument master (read-only).

    Nothing is written: EODHD gives no listing/delisting dates, and inventing them
    would corrupt ticker history (ADR 0002). Use the output to complete the CSV.
    """
    from idx_trading.clock import exchange_today
    from idx_trading.ingestion.adapters.eodhd import fetch_symbol_list
    from idx_trading.instruments import InstrumentService
    from idx_trading.registry import require_permission

    st, token, client, call_budget = _eodhd_parts(None)
    with _db() as s:
        require_permission(s, "eodhd", PermissionPurpose.AUTOMATED_INGESTION)
        known = {m.ticker for m in InstrumentService(s).universe_on(exchange_today())}
    try:
        active = fetch_symbol_list(
            token, client, call_budget, exchange=st.eodhd_exchange, base_url=st.eodhd_base_url
        )
        gone = (
            fetch_symbol_list(
                token,
                client,
                call_budget,
                exchange=st.eodhd_exchange,
                delisted=True,
                base_url=st.eodhd_base_url,
            )
            if delisted
            else []
        )
    except IdxTradingError as exc:
        _fail(exc)
    vendor = {x.Code.upper(): x for x in active}
    typer.echo(f"EODHD active={len(vendor)} master(today)={len(known)} calls={call_budget.used}")
    for code in sorted(set(vendor) - known):
        v = vendor[code]
        typer.echo(f"  missing in master: {code:8} {v.Type or '':16} {v.Name or ''}")
    for code in sorted(known - set(vendor)):
        typer.echo(f"  not at EODHD:      {code}")
    if delisted:
        typer.echo(f"EODHD delisted={len(gone)} (no dates provided; add with dates via CSV)")
        for x in sorted(gone, key=lambda g: g.Code):
            typer.echo(f"  delisted: {x.Code:8} {x.Name or ''}")


@ingest_app.command("replay")
def ingest_replay(
    source: Annotated[str, typer.Option(help="Archived source to replay: eodhd | manual_csv")],
    profile: Annotated[str, typer.Option(help="CSV profile (manual_csv only)")] = "standard",
    supersede: bool = False,
) -> None:
    """Rebuild prices from the raw archive (no source calls, no quota used)."""
    from idx_trading.ingestion.adapters import CsvEodAdapter
    from idx_trading.ingestion.adapters.base import SourceAdapter
    from idx_trading.ingestion.adapters.eodhd import CallBudget, EodhdEodAdapter
    from idx_trading.ingestion.adapters.replay import ArchiveReplayAdapter
    from idx_trading.ingestion.archive import RawArchive
    from idx_trading.ingestion.http import HttpClient
    from idx_trading.ingestion.pipeline import EodIngestionPipeline
    from idx_trading.ingestion.schemas import FetchRequest
    from idx_trading.ingestion.writer import RevisionPolicy

    st = get_settings()
    parser: SourceAdapter
    if source == "eodhd":
        # Parser only: it never sends a request, so a placeholder token is fine.
        parser = EodhdEodAdapter("replay", HttpClient(), CallBudget(1), exchange=st.eodhd_exchange)
    elif source == "manual_csv":
        parser = CsvEodAdapter(profile)
    else:
        _fail(IdxTradingError(f"replay is not supported for source {source!r}"))
    try:
        with _factory() as factory:
            summary = EodIngestionPipeline(
                factory,
                RawArchive(st.raw_archive_dir),
                extreme_move_threshold=st.extreme_move_threshold,
                calendar_code=st.calendar_code,
            ).run(
                ArchiveReplayAdapter(parser, st.raw_archive_dir),
                FetchRequest(),
                job_name=f"replay_{source}",
                policy=RevisionPolicy.SUPERSEDE if supersede else RevisionPolicy.FLAG,
            )
    except (IdxTradingError, OSError) as exc:
        _fail(exc)
    typer.echo(f"run {summary.run_id} {summary.status.value}: {summary.counts}")
    if summary.status.value == "failed":
        raise typer.Exit(code=2)


@sources_app.command("check")
def sources_check(source_id: str, purpose: PermissionPurpose) -> None:
    """Exit 0 if `source_id` is approved for `purpose`, else 1."""
    from idx_trading.registry import require_permission

    try:
        with _db() as s:
            require_permission(s, source_id, purpose)
    except IdxTradingError as exc:
        _fail(exc)
    typer.echo("permitted")


if __name__ == "__main__":
    app()
