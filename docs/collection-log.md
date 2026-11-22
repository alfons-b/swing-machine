# Daily collection log (2026-11-22 → 2026-11-29)

Written by the scheduled agent after each daily run. Counts only: no vendor data is
committed (EODHD redistribution is denied). Status: healthy / degraded / failed.
"Stocks ≥200 bars" ≈ stocks with about a year of history.

| Run (UTC) | Status | Instruments | Stocks ≥200 bars | Daily bars | Latest session | Stocks with corp. actions | Financial reports | Open errors |
|---|---|---|---|---|---|---|---|---|
Traceback (most recent call last):
  File "/home/user/swing-machine/.venv/lib/python3.13/site-packages/sqlalchemy/engine/base.py", line 154, in __init__
    self._dbapi_connection = engine.raw_connection()
                             ~~~~~~~~~~~~~~~~~~~~~^^
  File "/home/user/swing-machine/.venv/lib/python3.13/site-packages/sqlalchemy/engine/base.py", line 3303, in raw_connection
    return self.pool.connect()
           ~~~~~~~~~~~~~~~~~^^
  File "/home/user/swing-machine/.venv/lib/python3.13/site-packages/sqlalchemy/pool/base.py", line 460, in connect
    return _ConnectionFairy._checkout(self)
           ~~~~~~~~~~~~~~~~~~~~~~~~~~^^^^^^
  File "/home/user/swing-machine/.venv/lib/python3.13/site-packages/sqlalchemy/pool/base.py", line 1355, in _checkout
    fairy = _ConnectionRecord.checkout(pool)
  File "/home/user/swing-machine/.venv/lib/python3.13/site-packages/sqlalchemy/pool/base.py", line 728, in checkout
    rec = pool._do_get()
  File "/home/user/swing-machine/.venv/lib/python3.13/site-packages/sqlalchemy/pool/impl.py", line 176, in _do_get
    with util.safe_reraise():
         ~~~~~~~~~~~~~~~~~^^
  File "/home/user/swing-machine/.venv/lib/python3.13/site-packages/sqlalchemy/util/langhelpers.py", line 166, in __exit__
    raise exc_value.with_traceback(exc_tb)
  File "/home/user/swing-machine/.venv/lib/python3.13/site-packages/sqlalchemy/pool/impl.py", line 174, in _do_get
    return self._create_connection()
           ~~~~~~~~~~~~~~~~~~~~~~~^^
  File "/home/user/swing-machine/.venv/lib/python3.13/site-packages/sqlalchemy/pool/base.py", line 401, in _create_connection
    return _ConnectionRecord(self)
  File "/home/user/swing-machine/.venv/lib/python3.13/site-packages/sqlalchemy/pool/base.py", line 690, in __init__
    self.__connect()
    ~~~~~~~~~~~~~~^^
  File "/home/user/swing-machine/.venv/lib/python3.13/site-packages/sqlalchemy/pool/base.py", line 947, in __connect
    with util.safe_reraise():
         ~~~~~~~~~~~~~~~~~^^
  File "/home/user/swing-machine/.venv/lib/python3.13/site-packages/sqlalchemy/util/langhelpers.py", line 166, in __exit__
    raise exc_value.with_traceback(exc_tb)
  File "/home/user/swing-machine/.venv/lib/python3.13/site-packages/sqlalchemy/pool/base.py", line 943, in __connect
    self.dbapi_connection = connection = pool._invoke_creator(self)
                                         ~~~~~~~~~~~~~~~~~~~~^^^^^^
  File "/home/user/swing-machine/.venv/lib/python3.13/site-packages/sqlalchemy/engine/create.py", line 645, in connect
    return dialect.connect(*cargs_tup, **cparams)
           ~~~~~~~~~~~~~~~^^^^^^^^^^^^^^^^^^^^^^^
  File "/home/user/swing-machine/.venv/lib/python3.13/site-packages/sqlalchemy/engine/default.py", line 869, in connect
    return self.loaded_dbapi.connect(*cargs, **cparams)  # type: ignore[no-any-return]  # NOQA: E501
           ~~~~~~~~~~~~~~~~~~~~~~~~~^^^^^^^^^^^^^^^^^^^
  File "/home/user/swing-machine/.venv/lib/python3.13/site-packages/psycopg/connection.py", line 126, in connect
    raise last_ex.with_traceback(None)
psycopg.OperationalError: connection failed: connection to server at "127.0.0.1", port 5432 failed: Connection refused
	Is the server running on that host and accepting TCP/IP connections?

The above exception was the direct cause of the following exception:

Traceback (most recent call last):
  File "/home/user/swing-machine/scripts/collection_metrics.py", line 42, in <module>
    main(sys.argv[1] if len(sys.argv) > 1 else "?")
    ~~~~^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "/home/user/swing-machine/scripts/collection_metrics.py", line 27, in main
    covered = s.scalar(select(func.count()).select_from(per).where(per.c.n >= 200)) or 0
              ~~~~~~~~^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "/home/user/swing-machine/.venv/lib/python3.13/site-packages/sqlalchemy/orm/session.py", line 2616, in scalar
    return self._execute_internal(
           ~~~~~~~~~~~~~~~~~~~~~~^
        statement,
        ^^^^^^^^^^
    ...<4 lines>...
        **kw,
        ^^^^^
    )
    ^
  File "/home/user/swing-machine/.venv/lib/python3.13/site-packages/sqlalchemy/orm/session.py", line 2432, in _execute_internal
    conn = self._connection_for_bind(bind)
  File "/home/user/swing-machine/.venv/lib/python3.13/site-packages/sqlalchemy/orm/session.py", line 2255, in _connection_for_bind
    return trans._connection_for_bind(engine, execution_options)
           ~~~~~~~~~~~~~~~~~~~~~~~~~~^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "<sqlalchemy generated _go() wrapper for sqlalchemy.orm.session.SessionTransaction._connection_for_bind>", line 2, in _connection_for_bind
    return target(fn, self, bind, execution_options)
  File "/home/user/swing-machine/.venv/lib/python3.13/site-packages/sqlalchemy/orm/state_changes.py", line 137, in _go
    ret_value = fn(self, *arg, **kw)
  File "/home/user/swing-machine/.venv/lib/python3.13/site-packages/sqlalchemy/orm/session.py", line 1282, in _connection_for_bind
    conn = bind.connect()
  File "/home/user/swing-machine/.venv/lib/python3.13/site-packages/sqlalchemy/engine/base.py", line 3279, in connect
    return self._connection_cls(self)
           ~~~~~~~~~~~~~~~~~~~~^^^^^^
  File "/home/user/swing-machine/.venv/lib/python3.13/site-packages/sqlalchemy/engine/base.py", line 156, in __init__
    Connection._handle_dbapi_exception_noconnection(
    ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~^
        err, dialect, engine
        ^^^^^^^^^^^^^^^^^^^^
    )
    ^
  File "/home/user/swing-machine/.venv/lib/python3.13/site-packages/sqlalchemy/engine/base.py", line 2436, in _handle_dbapi_exception_noconnection
    raise sqlalchemy_exception.with_traceback(exc_info[2]) from e
  File "/home/user/swing-machine/.venv/lib/python3.13/site-packages/sqlalchemy/engine/base.py", line 154, in __init__
    self._dbapi_connection = engine.raw_connection()
                             ~~~~~~~~~~~~~~~~~~~~~^^
  File "/home/user/swing-machine/.venv/lib/python3.13/site-packages/sqlalchemy/engine/base.py", line 3303, in raw_connection
    return self.pool.connect()
           ~~~~~~~~~~~~~~~~~^^
  File "/home/user/swing-machine/.venv/lib/python3.13/site-packages/sqlalchemy/pool/base.py", line 460, in connect
    return _ConnectionFairy._checkout(self)
           ~~~~~~~~~~~~~~~~~~~~~~~~~~^^^^^^
  File "/home/user/swing-machine/.venv/lib/python3.13/site-packages/sqlalchemy/pool/base.py", line 1355, in _checkout
    fairy = _ConnectionRecord.checkout(pool)
  File "/home/user/swing-machine/.venv/lib/python3.13/site-packages/sqlalchemy/pool/base.py", line 728, in checkout
    rec = pool._do_get()
  File "/home/user/swing-machine/.venv/lib/python3.13/site-packages/sqlalchemy/pool/impl.py", line 176, in _do_get
    with util.safe_reraise():
         ~~~~~~~~~~~~~~~~~^^
  File "/home/user/swing-machine/.venv/lib/python3.13/site-packages/sqlalchemy/util/langhelpers.py", line 166, in __exit__
    raise exc_value.with_traceback(exc_tb)
  File "/home/user/swing-machine/.venv/lib/python3.13/site-packages/sqlalchemy/pool/impl.py", line 174, in _do_get
    return self._create_connection()
           ~~~~~~~~~~~~~~~~~~~~~~~^^
  File "/home/user/swing-machine/.venv/lib/python3.13/site-packages/sqlalchemy/pool/base.py", line 401, in _create_connection
    return _ConnectionRecord(self)
  File "/home/user/swing-machine/.venv/lib/python3.13/site-packages/sqlalchemy/pool/base.py", line 690, in __init__
    self.__connect()
    ~~~~~~~~~~~~~~^^
  File "/home/user/swing-machine/.venv/lib/python3.13/site-packages/sqlalchemy/pool/base.py", line 947, in __connect
    with util.safe_reraise():
         ~~~~~~~~~~~~~~~~~^^
  File "/home/user/swing-machine/.venv/lib/python3.13/site-packages/sqlalchemy/util/langhelpers.py", line 166, in __exit__
    raise exc_value.with_traceback(exc_tb)
  File "/home/user/swing-machine/.venv/lib/python3.13/site-packages/sqlalchemy/pool/base.py", line 943, in __connect
    self.dbapi_connection = connection = pool._invoke_creator(self)
                                         ~~~~~~~~~~~~~~~~~~~~^^^^^^
  File "/home/user/swing-machine/.venv/lib/python3.13/site-packages/sqlalchemy/engine/create.py", line 645, in connect
    return dialect.connect(*cargs_tup, **cparams)
           ~~~~~~~~~~~~~~~^^^^^^^^^^^^^^^^^^^^^^^
  File "/home/user/swing-machine/.venv/lib/python3.13/site-packages/sqlalchemy/engine/default.py", line 869, in connect
    return self.loaded_dbapi.connect(*cargs, **cparams)  # type: ignore[no-any-return]  # NOQA: E501
           ~~~~~~~~~~~~~~~~~~~~~~~~~^^^^^^^^^^^^^^^^^^^
  File "/home/user/swing-machine/.venv/lib/python3.13/site-packages/psycopg/connection.py", line 126, in connect
    raise last_ex.with_traceback(None)
sqlalchemy.exc.OperationalError: (psycopg.OperationalError) connection failed: connection to server at "127.0.0.1", port 5432 failed: Connection refused
	Is the server running on that host and accepting TCP/IP connections?
(Background on this error at: https://sqlalche.me/e/21/e3q8)
| 2026-11-21 23:59 | baseline | 923 | 422 | 101,243 | 2026-11-20 | 37 | 6 | 320 |
