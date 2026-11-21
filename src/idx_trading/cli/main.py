"""`idx` command-line interface (thin layer over the services)."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
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
ca_app = typer.Typer(help="Corporate actions and adjustment factors", no_args_is_help=True)
prices_app = typer.Typer(help="Price series (raw / split / total-return)", no_args_is_help=True)
backup_app = typer.Typer(help="Backups, restore tests and archive copies", no_args_is_help=True)
fin_app = typer.Typer(help="Point-in-time fundamentals (financial reports, ratios)", no_args_is_help=True)
for sub, name in (
    (db_app, "db"),
    (sources_app, "sources"),
    (cal_app, "calendar"),
    (inst_app, "instruments"),
    (ingest_app, "ingest"),
    (ca_app, "ca"),
    (prices_app, "prices"),
    (fin_app, "fin"),
    (backup_app, "backup"),
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
    universe: Annotated[bool, typer.Option(help="All listed instruments on --end")] = False,
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
            # Collect data for every listed common stock; BUY eligibility is a later filter.
            members = [(m.ticker, m.instrument_id) for m in svc.collection_universe_on(target)]
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
    dataset: Annotated[str, typer.Option(help="eod_prices | corporate_actions")] = "eod_prices",
    supersede: bool = False,
) -> None:
    """Rebuild prices or corporate actions from the raw archive (no source calls)."""
    from idx_trading.ingestion.adapters import CsvEodAdapter
    from idx_trading.ingestion.adapters.base import SourceAdapter
    from idx_trading.ingestion.adapters.eodhd import CallBudget, EodhdEodAdapter
    from idx_trading.ingestion.adapters.replay import EOD_KINDS, ArchiveReplayAdapter
    from idx_trading.ingestion.archive import RawArchive
    from idx_trading.ingestion.http import HttpClient
    from idx_trading.ingestion.pipeline import EodIngestionPipeline
    from idx_trading.ingestion.schemas import FetchRequest
    from idx_trading.ingestion.writer import RevisionPolicy

    st = get_settings()
    if dataset == "corporate_actions":
        _replay_corporate_actions(source, supersede)
        return
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
                ArchiveReplayAdapter(parser, st.raw_archive_dir, EOD_KINDS),
                FetchRequest(),
                job_name=f"replay_{source}",
                policy=RevisionPolicy.SUPERSEDE if supersede else RevisionPolicy.FLAG,
            )
    except (IdxTradingError, OSError) as exc:
        _fail(exc)
    typer.echo(f"run {summary.run_id} {summary.status.value}: {summary.counts}")
    if summary.status.value == "failed":
        raise typer.Exit(code=2)


def _resolve_members(s: Session, tickers: str | None, universe: bool, on: date) -> list[tuple[str, int]]:
    from idx_trading.instruments import InstrumentService

    if bool(tickers) == universe:
        _fail(IdxTradingError("give exactly one of --tickers or --universe"))
    svc = InstrumentService(s)
    if universe:
        # Every listed common stock: corporate actions matter for all of them.
        return [(m.ticker, m.instrument_id) for m in svc.collection_universe_on(on)]
    out = []
    assert tickers is not None
    for t in (x.strip().upper() for x in tickers.split(",") if x.strip()):
        iid = svc.try_resolve(t, on)
        if iid is None:
            _fail(IdxTradingError(f"{t} is not in the instrument master on {on}"))
        assert iid is not None
        out.append((t, iid))
    return out


@ca_app.command("fetch-eodhd")
def ca_fetch_eodhd(
    tickers: Annotated[str | None, typer.Option(help="Comma-separated IDX tickers")] = None,
    universe: Annotated[bool, typer.Option(help="All listed instruments")] = False,
    budget: Annotated[int | None, typer.Option(help="Max API calls (2 per ticker)")] = None,
    dry_run: bool = False,
    supersede: bool = False,
) -> None:
    """Dividends + splits from EODHD (2 calls per ticker; least recently checked first)."""
    from datetime import timedelta

    from idx_trading.clock import exchange_today
    from idx_trading.corporate_actions.adapters import EodhdCorporateActionAdapter
    from idx_trading.corporate_actions.pipeline import CorporateActionPipeline
    from idx_trading.db.models import ArtifactFetch
    from idx_trading.ingestion.adapters.eodhd import TickerWindow
    from idx_trading.ingestion.archive import RawArchive
    from idx_trading.ingestion.schemas import FetchRequest
    from idx_trading.ingestion.writer import RevisionPolicy

    st = get_settings()
    today = exchange_today()
    with _db() as s:
        members = _resolve_members(s, tickers, universe, today)
        last: dict[str, datetime] = {}
        for f in s.scalars(
            select(ArtifactFetch).where(ArtifactFetch.source_id == "eodhd", ArtifactFetch.kind == "splits")
        ):
            if f.ticker and (f.extra or {}).get("http_status", 200) == 200:
                last[f.ticker] = max(last.get(f.ticker, f.retrieved_at), f.retrieved_at)
    cap = budget or st.eodhd_daily_call_budget
    order = sorted(members, key=lambda m: (m[0] in last, str(last.get(m[0], "")), m[0]))
    plan = order[: cap // 2]
    start = today - timedelta(days=st.eodhd_history_days - 1)
    typer.echo(f"candidates={len(members)} planned={len(plan)} calls={2 * len(plan)} budget={cap}")
    for t, _ in plan:
        typer.echo(f"  {t:8} last checked {last.get(t)}")
    if dry_run or not plan:
        return
    st, token, client, call_budget = _eodhd_parts(cap)
    adapter = EodhdCorporateActionAdapter(
        token, client, call_budget, exchange=st.eodhd_exchange, base_url=st.eodhd_base_url
    )
    req = FetchRequest(options={"windows": [TickerWindow(t, start, None) for t, _ in plan]})
    try:
        with _factory() as factory:
            summary = CorporateActionPipeline(
                factory, RawArchive(st.raw_archive_dir), calendar_code=st.calendar_code
            ).run(
                adapter,
                req,
                job_name="eodhd_corporate_actions",
                policy=RevisionPolicy.SUPERSEDE if supersede else RevisionPolicy.FLAG,
            )
    except IdxTradingError as exc:
        _fail(exc)
    typer.echo(f"run {summary.run_id} {summary.status.value}: {summary.counts} calls={call_budget.used}")
    if summary.status.value == "failed":
        raise typer.Exit(code=2)


@ca_app.command("import")
def ca_import(files: list[Path], supersede: bool = False) -> None:
    """Import corporate actions you entered from official announcements (CSV)."""
    from idx_trading.corporate_actions.adapters import CsvCorporateActionAdapter
    from idx_trading.corporate_actions.pipeline import CorporateActionPipeline
    from idx_trading.ingestion.archive import RawArchive
    from idx_trading.ingestion.schemas import FetchRequest
    from idx_trading.ingestion.writer import RevisionPolicy

    st = get_settings()
    try:
        with _factory() as factory:
            summary = CorporateActionPipeline(
                factory, RawArchive(st.raw_archive_dir), calendar_code=st.calendar_code
            ).run(
                CsvCorporateActionAdapter(),
                FetchRequest(paths=tuple(str(f) for f in files)),
                job_name="manual_corporate_actions",
                policy=RevisionPolicy.SUPERSEDE if supersede else RevisionPolicy.FLAG,
            )
    except IdxTradingError as exc:
        _fail(exc)
    typer.echo(f"run {summary.run_id} {summary.status.value}: {summary.counts}")
    if summary.status.value == "failed":
        raise typer.Exit(code=2)


@ca_app.command("factors")
def ca_factors() -> None:
    """(Re)compute versioned adjustment factors from the current corporate actions."""
    from idx_trading.corporate_actions.factors import FactorService

    with _db() as s:
        r = FactorService(s, get_settings().calendar_code).recompute()
    typer.echo(
        f"inserted={r.inserted} unchanged={r.unchanged} superseded={r.superseded} "
        f"retired={r.retired} duplicates_skipped={r.skipped_duplicates} unresolved={len(r.unresolved)}"
    )
    for u in r.unresolved:
        typer.echo(f"  UNRESOLVED {u}")
    if r.unresolved:
        raise typer.Exit(code=2)


@ca_app.command("reconcile")
def ca_reconcile(
    tickers: Annotated[str | None, typer.Option()] = None,
    universe: bool = False,
) -> None:
    """Check our factors against the vendor's adjusted close, and splits against prices."""
    from idx_trading.clock import exchange_today
    from idx_trading.corporate_actions.reconcile import (
        check_split_price_consistency,
        reconcile_vendor_adjusted_close,
    )
    from idx_trading.db.models import ReconciliationResult
    from idx_trading.ingestion.archive import RawArchive

    st = get_settings()
    with _db() as s:
        ids = [iid for _, iid in _resolve_members(s, tickers, universe, exchange_today())]
        v = reconcile_vendor_adjusted_close(s, RawArchive(st.raw_archive_dir), ids)
        sp = check_split_price_consistency(s, ids, calendar_code=st.calendar_code, batch_id=v.batch_id)
        rows = list(
            s.scalars(select(ReconciliationResult).where(ReconciliationResult.batch_id == v.batch_id))
        )
        for r in rows:
            name = (r.details or {}).get("ticker", r.instrument_id)
            typer.echo(
                f"  {r.check_name:24} {name!s:8} {r.trade_date or '':10} {r.status:4} "
                f"{r.difference or ''} (tol {r.tolerance})"
            )
    typer.echo(
        f"batch {v.batch_id}: vendor pass={v.passed} warn={v.warned} fail={v.failed} "
        f"skipped={len(v.skipped)}; splits pass={sp.passed} fail={sp.failed}"
    )
    for msg in v.skipped + sp.skipped:
        typer.echo(f"  skipped: {msg}")
    if v.failed or sp.failed:
        raise typer.Exit(code=2)


@prices_app.command("show")
def prices_show(
    ticker: str,
    adjust: Annotated[str, typer.Option(help="raw | split | total_return")] = "total_return",
    start: Annotated[datetime | None, typer.Option(formats=["%Y-%m-%d"])] = None,
    end: Annotated[datetime | None, typer.Option(formats=["%Y-%m-%d"])] = None,
    as_of: Annotated[datetime | None, typer.Option(formats=["%Y-%m-%d"])] = None,
    csv_out: Annotated[Path | None, typer.Option("--csv", help="Write CSV to this path")] = None,
) -> None:
    """Print or export a price series for one ticker."""
    import csv as _csv

    from idx_trading.clock import exchange_today
    from idx_trading.corporate_actions.adjusted import Adjustment, adjusted_bars
    from idx_trading.instruments import InstrumentService

    if adjust not in {a.value for a in Adjustment}:
        _fail(IdxTradingError("--adjust must be raw, split or total_return"))
    kind = Adjustment(adjust)
    with _db() as s:
        iid = InstrumentService(s).try_resolve(ticker, end.date() if end else exchange_today())
        if iid is None:
            _fail(IdxTradingError(f"{ticker} not found"))
        assert iid is not None
        bars = adjusted_bars(
            s,
            iid,
            kind,
            start=start.date() if start else None,
            end=end.date() if end else None,
            as_of=as_of.date() if as_of else None,
        )
    cols = ["date", "open", "high", "low", "close", "volume", "price_factor", "source", "volume_verified"]

    def px(v: object) -> str:
        return "" if v is None else f"{v:.4f}"

    lines = [
        [
            b.trade_date.isoformat(),
            px(b.open),
            px(b.high),
            px(b.low),
            px(b.close),
            "" if b.volume is None else f"{b.volume:.0f}",
            f"{b.price_factor:.8f}",
            b.source_id,
            str(b.volume_verified),
        ]
        for b in bars
    ]
    if csv_out:
        with csv_out.open("w", newline="", encoding="utf-8") as fh:
            w = _csv.writer(fh)
            w.writerow(cols)
            w.writerows(lines)
        typer.echo(f"wrote {len(lines)} rows to {csv_out}")
        return
    typer.echo("  ".join(cols))
    for ln in lines:
        typer.echo("  ".join(ln))


def _replay_corporate_actions(source: str, supersede: bool) -> None:
    from idx_trading.corporate_actions.adapters import (
        ArchiveReplayActionAdapter,
        CorporateActionAdapter,
        CsvCorporateActionAdapter,
        EodhdCorporateActionAdapter,
    )
    from idx_trading.corporate_actions.pipeline import CorporateActionPipeline
    from idx_trading.ingestion.adapters.eodhd import CallBudget
    from idx_trading.ingestion.archive import RawArchive
    from idx_trading.ingestion.http import HttpClient
    from idx_trading.ingestion.schemas import FetchRequest
    from idx_trading.ingestion.writer import RevisionPolicy

    st = get_settings()
    parser: CorporateActionAdapter
    if source == "eodhd":
        parser = EodhdCorporateActionAdapter("replay", HttpClient(), CallBudget(1))
    elif source == "manual_csv":
        parser = CsvCorporateActionAdapter()
    else:
        _fail(IdxTradingError(f"replay is not supported for source {source!r}"))
    try:
        with _factory() as factory:
            summary = CorporateActionPipeline(
                factory, RawArchive(st.raw_archive_dir), calendar_code=st.calendar_code
            ).run(
                ArchiveReplayActionAdapter(parser, st.raw_archive_dir),
                FetchRequest(),
                job_name=f"replay_{source}_corporate_actions",
                policy=RevisionPolicy.SUPERSEDE if supersede else RevisionPolicy.FLAG,
            )
    except (IdxTradingError, OSError) as exc:
        _fail(exc)
    typer.echo(f"run {summary.run_id} {summary.status.value}: {summary.counts}")
    if summary.status.value == "failed":
        raise typer.Exit(code=2)


@fin_app.command("fetch-bca")
def fin_fetch_bca(
    limit: Annotated[int, typer.Option(help="Max new PDFs to download this run")] = 8,
) -> None:
    """Download new BCA quarterly publication reports from its IR page and parse them."""
    from idx_trading.db.models import ArtifactFetch
    from idx_trading.fundamentals.adapters import BcaIrAdapter
    from idx_trading.fundamentals.pipeline import FinancialReportPipeline
    from idx_trading.ingestion.archive import RawArchive
    from idx_trading.ingestion.http import HttpClient
    from idx_trading.ingestion.schemas import FetchRequest

    st = get_settings()
    with _db() as s:
        done = {
            f.origin
            for f in s.scalars(
                select(ArtifactFetch).where(
                    ArtifactFetch.source_id == "bca_ir", ArtifactFetch.kind == "bank_publication_quarterly"
                )
            )
        }
    client = HttpClient(min_interval_seconds=2.0)  # polite: self-imposed limit (ADR 0010)
    req = FetchRequest(options={"skip_urls": sorted(o for o in done if o), "limit": limit})
    try:
        with _factory() as factory:
            summary = FinancialReportPipeline(
                factory, RawArchive(st.raw_archive_dir), calendar_code=st.calendar_code
            ).run(BcaIrAdapter(client), req, job_name="bca_ir_financial_reports")
    except IdxTradingError as exc:
        _fail(exc)
    typer.echo(
        f"run {summary.run_id} {summary.status.value}: {summary.counts} requests={client.requests_sent}"
    )
    if summary.status.value == "failed" and summary.counts.get("received"):
        raise typer.Exit(code=2)


@fin_app.command("import-pdf")
def fin_import_pdf(
    file: Path,
    ticker: Annotated[str, typer.Option()],
    published_on: Annotated[str, typer.Option(help="Publication date YYYY-MM-DD (from the IDX/issuer site)")],
    origin_url: Annotated[str | None, typer.Option(help="Where the file was downloaded from")] = None,
    date_from_filename: Annotated[
        bool, typer.Option(help="The publication date is the file name's date prefix")
    ] = False,
) -> None:
    """Import a bank publication report PDF you saved yourself."""
    from idx_trading.fundamentals.adapters import ManualPublicationAdapter
    from idx_trading.fundamentals.pipeline import FinancialReportPipeline
    from idx_trading.ingestion.archive import RawArchive
    from idx_trading.ingestion.schemas import FetchRequest

    st = get_settings()
    req = FetchRequest(
        paths=(str(file),),
        options={
            "ticker": ticker,
            "published_on": published_on,
            "origin_url": origin_url,
            "published_at_basis": "filename_date" if date_from_filename else "stated_by_owner",
        },
    )
    try:
        with _factory() as factory:
            summary = FinancialReportPipeline(
                factory, RawArchive(st.raw_archive_dir), calendar_code=st.calendar_code
            ).run(ManualPublicationAdapter(), req, job_name="manual_financial_report")
    except (IdxTradingError, ValueError) as exc:
        _fail(exc)
    typer.echo(f"run {summary.run_id} {summary.status.value}: {summary.counts}")
    if summary.status.value == "failed":
        raise typer.Exit(code=2)


@fin_app.command("show")
def fin_show(
    ticker: str,
    as_of: Annotated[datetime | None, typer.Option(formats=["%Y-%m-%d"])] = None,
    basis: str = "consolidated",
) -> None:
    """Facts known on --as-of for the latest available period (point in time)."""
    from idx_trading.clock import exchange_today
    from idx_trading.db.models import Instrument
    from idx_trading.fundamentals.concepts import CONCEPTS
    from idx_trading.fundamentals.pit import fact_as_of, latest_period
    from idx_trading.instruments import InstrumentService

    d = as_of.date() if as_of else exchange_today()
    with _db() as s:
        iid = InstrumentService(s).try_resolve(ticker, d)
        inst = s.get(Instrument, iid) if iid else None
        if inst is None:
            _fail(IdxTradingError(f"{ticker} not found on {d}"))
        assert inst is not None
        p = latest_period(s, inst.issuer_id, d)
        if p is None:
            typer.echo(f"no report available on {d}")
            return
        typer.echo(f"{ticker} as of {d}: latest period {p} ({basis})")
        for name, c in CONCEPTS.items():
            f = fact_as_of(s, inst.issuer_id, name, p, d, basis)
            if f is not None:
                typer.echo(f"  {name:30} {f.value:>28,.0f} {c.unit:10} report {f.report_id}")


@fin_app.command("ratios")
def fin_ratios(
    ticker: str,
    as_of: Annotated[datetime | None, typer.Option(formats=["%Y-%m-%d"])] = None,
) -> None:
    """Point-in-time valuation and quality ratios (FR-007)."""
    from idx_trading.clock import exchange_today
    from idx_trading.fundamentals.ratios import compute_ratios
    from idx_trading.instruments import InstrumentService

    d = as_of.date() if as_of else exchange_today()
    with _db() as s:
        iid = InstrumentService(s).try_resolve(ticker, d)
        if iid is None:
            _fail(IdxTradingError(f"{ticker} not found on {d}"))
        assert iid is not None
        r = compute_ratios(s, iid, d)
    typer.echo(f"{ticker} as of {d} (latest period {r.period_end}, price {r.price} on {r.price_date})")
    pct = {"roe", "roa", "ni_growth_yoy", "ldr"}
    for k, v in r.as_dict().items():
        if k in ("period_end", "price", "price_date"):
            continue
        if v is None:
            shown = "n/a"
        elif k in pct:
            shown = f"{v:.2%}"
        elif k == "shares_implied":
            shown = f"{v:,.0f} (±{r.shares_rel_uncertainty:.2%})"
        else:
            shown = f"{v:,.2f}"
        typer.echo(f"  {k:16} {shown}")
    for n in r.notes:
        typer.echo(f"  note: {n}")


@ingest_app.command("reconcile")
def ingest_reconcile(
    start: Annotated[datetime | None, typer.Option(formats=["%Y-%m-%d"])] = None,
    end: Annotated[datetime | None, typer.Option(formats=["%Y-%m-%d"])] = None,
) -> None:
    """Compare bars from different sources for the same stock and session."""
    from idx_trading.ingestion.reconcile_sources import reconcile_sources

    with _db() as s:
        r = reconcile_sources(s, start=start.date() if start else None, end=end.date() if end else None)
    typer.echo(f"batch {r.batch_id}: compared={r.compared} pass={r.passed} warn={r.warned} fail={r.failed}")
    for pair, st in sorted(r.by_pair.items()):
        typer.echo(f"  {pair:24} {st}")
    if r.compared == 0:
        typer.echo("  nothing to compare: no session has bars from two sources")
    if r.failed:
        raise typer.Exit(code=2)


@backup_app.command("create")
def backup_create() -> None:
    """Back up the database (consistent snapshot) and write a manifest."""
    from idx_trading.ops.backup import create_backup

    st = get_settings()
    try:
        m = create_backup(st.database_url, st.backup_dir)
    except (RuntimeError, ValueError, OSError) as exc:
        _fail(IdxTradingError(str(exc)))
    total = sum(m.row_counts.values())
    typer.echo(
        f"backup {m.backup_id}: {m.kind} {m.size_bytes:,} bytes, {total:,} rows, "
        f"revision {m.alembic_revision}"
    )


@backup_app.command("verify")
def backup_verify(
    backup_id: Annotated[str | None, typer.Argument(help="Default: the newest")] = None,
) -> None:
    """Checksum + full restore into a scratch database, row counts compared."""
    from idx_trading.ops.backup import load_manifests, verify_backup

    st = get_settings()
    ms = load_manifests(st.backup_dir) if st.backup_dir.exists() else []
    m = next((x for x in ms if x.backup_id == backup_id), None) if backup_id else (ms[-1] if ms else None)
    if m is None:
        _fail(IdxTradingError("backup not found"))
    assert m is not None
    r = verify_backup(m, st.backup_dir, st.database_url)
    typer.echo(
        f"verify {m.backup_id}: checksum={r.checksum_ok} restore={r.restore_ok} revision={r.revision_ok} "
        f"mismatched_tables={r.mismatched_tables or 'none'}"
    )
    if r.error:
        typer.echo(f"  error: {r.error}")
    if not r.ok:
        raise typer.Exit(code=2)


@backup_app.command("prune")
def backup_prune(keep_days: Annotated[int | None, typer.Option()] = None) -> None:
    """Delete backups older than the retention period (the newest is always kept)."""
    from idx_trading.ops.backup import prune_backups

    st = get_settings()
    removed = prune_backups(st.backup_dir, keep_days or st.backup_keep_days)
    typer.echo(f"removed {len(removed)}: {removed}")


@backup_app.command("archive")
def backup_archive(
    dest: Annotated[Path | None, typer.Option(help="Default: <backup_dir>/raw")] = None,
) -> None:
    """Copy new raw-archive files to the backup location, verifying every checksum."""
    from idx_trading.ops.backup import sync_archive

    st = get_settings()
    r = sync_archive(st.raw_archive_dir, dest or st.backup_dir / "raw")
    typer.echo(
        f"copied={r.copied} verified={r.verified} repaired={len(r.repaired_destination)} "
        f"corrupt_source={r.corrupt_source}"
    )
    if r.corrupt_source:
        raise typer.Exit(code=2)


@backup_app.command("status")
def backup_status_cmd() -> None:
    """healthy | degraded | failed, with reasons (PRD §18.2/18.3)."""
    from idx_trading.ops.backup import backup_status

    b = backup_status(get_settings().backup_dir)
    typer.echo(f"{b.state}: latest={b.latest_backup} last_verified={b.latest_verified}")
    for r in b.reasons:
        typer.echo(f"  - {r}")
    if b.state != "healthy":
        raise typer.Exit(code=1)


@app.command("daily")
def daily(
    eod_budget: Annotated[
        int | None, typer.Option(help="EODHD calls for prices (default: budget - ca)")
    ] = None,
    ca_budget: Annotated[int, typer.Option(help="EODHD calls for dividends/splits (2 per ticker)")] = 4,
    verify_every_days: Annotated[int, typer.Option(help="Restore-test the newest backup this often")] = 30,
) -> None:
    """The whole EOD routine (PRD §8.1): prices, corporate actions, factors, checks, backup.

    Each step runs on its own; a failing optional step does not stop the others
    (PRD §18.3). Exit code: 0 healthy, 1 degraded, 2 failed. Schedule it once per
    trading day after 17:30 WIB (Windows Task Scheduler / cron).
    """
    import json as _json
    import traceback

    from idx_trading.ops.backup import VERIFY_SUFFIX, load_manifests

    st = get_settings()
    total = st.eodhd_daily_call_budget
    eod = eod_budget if eod_budget is not None else max(total - ca_budget, 0)
    results: list[tuple[str, str, str]] = []

    def step(name: str, critical: bool, fn, *args, **kwargs) -> None:
        try:
            fn(*args, **kwargs)
            results.append((name, "ok", ""))
        except typer.Exit as e:
            code = e.exit_code or 0
            results.append(
                (name, "ok" if code == 0 else ("failed" if critical else "degraded"), f"exit {code}")
            )
        except Exception as exc:
            results.append((name, "failed" if critical else "degraded", f"{type(exc).__name__}: {exc}"))
            typer.echo(traceback.format_exc()[-1500:], err=True)

    have_token = st.eodhd_api_token is not None and bool(st.eodhd_api_token.get_secret_value())
    step("sources sync", True, sources_sync, None)
    if have_token and eod > 0:
        step(
            "eod prices (eodhd)",
            True,
            ingest_eodhd,
            tickers=None,
            universe=True,
            end=None,
            budget=eod,
            dry_run=False,
            supersede=False,
        )
    else:
        results.append(("eod prices (eodhd)", "degraded", "no token or zero budget"))
    if have_token and ca_budget >= 2:
        step(
            "corporate actions (eodhd)",
            False,
            ca_fetch_eodhd,
            tickers=None,
            universe=True,
            budget=ca_budget,
            dry_run=False,
            supersede=False,
        )
    step("adjustment factors", False, ca_factors)
    step("vendor reconciliation", False, ca_reconcile, tickers=None, universe=True)
    step("cross-source reconciliation", False, ingest_reconcile, start=None, end=None)
    step("freshness", False, ingest_freshness, source="eodhd")
    step("backup", True, backup_create)
    step("archive copy", True, backup_archive, dest=None)
    step("prune backups", False, backup_prune, keep_days=None)
    ms = load_manifests(st.backup_dir) if st.backup_dir.exists() else []
    last_ok = None
    for m in ms:
        vp = st.backup_dir / f"{m.backup_id}{VERIFY_SUFFIX}"
        if vp.exists():
            v = _json.loads(vp.read_text())
            if v["checksum_ok"] and v["restore_ok"] and v["revision_ok"] and not v["mismatched_tables"]:
                last_ok = datetime.fromisoformat(v["checked_at"])
    if last_ok is None or last_ok < datetime.now(tz=UTC) - timedelta(days=verify_every_days):
        step("restore test", False, backup_verify, None)

    worst = (
        "failed"
        if any(r[1] == "failed" for r in results)
        else ("degraded" if any(r[1] == "degraded" for r in results) else "healthy")
    )
    typer.echo(f"\n== daily: {worst}")
    for name, status, detail in results:
        typer.echo(f"  {status:8} {name:28} {detail}")
    raise typer.Exit(code={"healthy": 0, "degraded": 1, "failed": 2}[worst])


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
