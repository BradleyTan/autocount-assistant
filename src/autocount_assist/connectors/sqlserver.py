"""Read-only SQL Server connector for AutoCount account book databases.

What keeps it read-only:
1. Every statement passes sql_guard.check_read_only() before it is sent.
2. Before the first query on an account book, the login's own rights are checked.
   A login that can change data is refused unless allow_privileged_login is set.
3. The login should be db_datareader only (see `autocount-assist setup-login`).

What keeps it out of the users' way:
- a lock timeout and low deadlock priority, so a diagnostic query never blocks or
  kills a user's posting;
- READ UNCOMMITTED, so it takes no shared locks;
- a query timeout and a row limit.

Every returned row is masked.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from typing import Any

from ..config import Settings
from ..masking import Masker
from ..sql_guard import check_read_only

SYSTEM_DBS = {"master", "tempdb", "model", "msdb"}
MAX_ROWS = 1000
_IDENT = re.compile(r"^[A-Za-z0-9_\- .$#@]+$")
_PLACEHOLDER = re.compile(r"\{([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)?)\}")
_MODERN_DRIVER = re.compile(r"^ODBC Driver (\d+(?:\.\d+)?) for SQL Server$")

# Session settings only; they change nothing in the database.
_SESSION_SETUP = (
    "SET NOCOUNT ON; SET LOCK_TIMEOUT {lock_ms}; SET DEADLOCK_PRIORITY LOW; "
    "SET TRANSACTION ISOLATION LEVEL READ UNCOMMITTED;"
)

_LOGIN_INFO = (
    "SELECT SUSER_SNAME() AS login_name, DB_NAME() AS database_name, "
    "ISNULL(IS_SRVROLEMEMBER('sysadmin'), 0) AS is_sysadmin, "
    "ISNULL(IS_MEMBER('db_owner'), 0) AS is_db_owner, "
    "ISNULL(IS_MEMBER('db_datawriter'), 0) AS is_datawriter, "
    "ISNULL(IS_MEMBER('db_ddladmin'), 0) AS is_ddladmin, "
    "ISNULL(HAS_PERMS_BY_NAME(DB_NAME(), 'DATABASE', 'INSERT'), 0) AS can_insert, "
    "ISNULL(HAS_PERMS_BY_NAME(DB_NAME(), 'DATABASE', 'UPDATE'), 0) AS can_update, "
    "ISNULL(HAS_PERMS_BY_NAME(DB_NAME(), 'DATABASE', 'DELETE'), 0) AS can_delete, "
    "ISNULL(HAS_PERMS_BY_NAME(DB_NAME(), 'DATABASE', 'ALTER'), 0) AS can_alter, "
    "ISNULL(HAS_PERMS_BY_NAME(DB_NAME(), 'DATABASE', 'EXECUTE'), 0) AS can_execute"
)
_WRITE_FLAGS = ("is_sysadmin", "is_db_owner", "is_datawriter", "is_ddladmin",
                "can_insert", "can_update", "can_delete", "can_alter")

_SERVER_INFO = (
    "SELECT CAST(SERVERPROPERTY('ProductVersion') AS nvarchar(64)) AS product_version, "
    "CAST(SERVERPROPERTY('ProductLevel') AS nvarchar(64)) AS product_level, "
    "CAST(SERVERPROPERTY('Edition') AS nvarchar(128)) AS edition, "
    "CAST(SERVERPROPERTY('EngineEdition') AS int) AS engine_edition, "
    "DB_NAME() AS database_name, "
    "CAST(DATABASEPROPERTYEX(DB_NAME(), 'Status') AS nvarchar(32)) AS status, "
    "CAST(DATABASEPROPERTYEX(DB_NAME(), 'Recovery') AS nvarchar(32)) AS recovery_model, "
    "CAST(DATABASEPROPERTYEX(DB_NAME(), 'Collation') AS nvarchar(128)) AS collation, "
    "(SELECT compatibility_level FROM sys.databases WHERE name = DB_NAME()) AS compatibility_level"
)
_FILE_SIZES = (
    "SELECT type_desc, CAST(SUM(CAST(size AS bigint)) * 8 / 1024 AS int) AS size_mb "
    "FROM sys.database_files GROUP BY type_desc"
)
_FILE_PATHS = "SELECT type_desc, physical_name FROM sys.database_files"
_ROW_COUNTS = (
    "SELECT TOP {top} t.name AS table_name, SUM(p.rows) AS row_count "
    "FROM sys.tables t JOIN sys.partitions p ON p.object_id = t.object_id AND p.index_id IN (0, 1) "
    "GROUP BY t.name ORDER BY SUM(p.rows) DESC, t.name"
)
SQL_MAJOR_NAMES = {16: "2022", 15: "2019", 14: "2017", 13: "2016", 12: "2014", 11: "2012", 10: "2008"}
EXPRESS_ENGINE_EDITION = 4
EXPRESS_DATA_LIMIT_MB = 10 * 1024  # per database, data files only (SQL Server 2016 Express and later)


class SqlUnavailable(Exception):
    """No usable database connection; checks that need SQL report missing evidence."""


class PrivilegedLogin(SqlUnavailable):
    """The login can change data, and the settings do not allow that."""


# ---- connection string ------------------------------------------------------

def available_drivers() -> list[str]:
    try:
        import pyodbc
    except ImportError:
        return []
    return list(pyodbc.drivers())


def pick_driver(configured: str, available: Iterable[str]) -> str | None:
    """The configured driver, or with "auto" the newest SQL Server driver installed."""
    names = list(available)
    if configured and configured.lower() != "auto":
        return configured
    modern = sorted(((float(m.group(1)), n) for n in names if (m := _MODERN_DRIVER.match(n))), reverse=True)
    if modern:
        return modern[0][1]
    for legacy in ("SQL Server Native Client 11.0", "SQL Server"):
        if legacy in names:
            return legacy
    return None


def _odbc_value(value: str) -> str:
    """Quote a connection-string value so ; and } inside it cannot add attributes."""
    if re.search(r"[;{}=]", value) or value != value.strip():
        return "{" + value.replace("}", "}}") + "}"
    return value


def build_connection_string(settings: Settings, database: str | None, driver: str | None = None) -> str:
    driver = driver or pick_driver(settings.odbc_driver, available_drivers()) or "ODBC Driver 18 for SQL Server"
    parts = [f"DRIVER={{{driver}}}", f"SERVER={_odbc_value(settings.sql_server)}"]
    if driver != "SQL Server":  # the legacy driver does not know these keywords
        parts += ["ApplicationIntent=ReadOnly", "Encrypt=yes", "TrustServerCertificate=yes"]
    parts.append("APP=autocount-assist")
    if database:
        parts.append(f"DATABASE={_odbc_value(database)}")
    user = settings.sql_user()
    if user:
        parts.append(f"UID={_odbc_value(user)}")
        parts.append(f"PWD={_odbc_value(settings.sql_password() or '')}")
    else:
        parts.append("Trusted_Connection=yes")
    return ";".join(parts)


def _pyodbc_connect(conn_str: str):
    try:
        import pyodbc
    except ImportError as e:
        raise SqlUnavailable("pyodbc is not installed (run: py -m pip install pyodbc)") from e
    try:
        return pyodbc.connect(conn_str, readonly=True, autocommit=True, timeout=10)
    except pyodbc.Error as e:
        # Never echo the connection string: it may contain the password.
        raise SqlUnavailable(f"Could not connect to SQL Server: {_error_text(e)}") from None


def _error_text(e: Exception) -> str:
    text = str(e.args[-1]) if e.args else str(e)
    return re.sub(r"(?i)(PWD|Password)\s*=\s*[^;]*", r"\1=<SECRET>", text)[:500]


def quote_ident(name: str) -> str:
    return "[" + name.replace("]", "]]") + "]"


def render_query(template: str, mapping: dict[str, str]) -> str:
    """Fill {alias} and {alias.column} placeholders. Unknown placeholders raise KeyError."""
    return _PLACEHOLDER.sub(lambda m: mapping[m.group(1)], template)


def placeholders(template: str) -> set[str]:
    return set(_PLACEHOLDER.findall(template))


def normalise_objects(objects: dict[str, Any] | None) -> dict[str, tuple[list[str], dict[str, list[str]]]]:
    """{alias: {table: A|[A, B], columns: {c: X|[X, Y]}}} -> {alias: ([A, B], {c: [X, Y]})}."""
    out: dict[str, tuple[list[str], dict[str, list[str]]]] = {}
    for alias, spec in (objects or {}).items():
        tables = spec.get("table", alias)
        tables = [tables] if isinstance(tables, str) else list(tables)
        cols = {c: ([v] if isinstance(v, str) else list(v)) for c, v in (spec.get("columns") or {}).items()}
        out[alias] = (tables, cols)
    return out


class SqlServerConnector:
    def __init__(self, settings: Settings, masker: Masker, database: str | None = None,
                 connect: Callable[[str], Any] | None = None):
        self.settings = settings
        self.masker = masker
        self.database = database
        self._connect = connect or _pyodbc_connect
        self._conn: Any = None
        self._catalog: dict[str, tuple[str, dict[str, tuple[str, str]]]] | None = None
        self._login: dict[str, Any] | None = None
        self._login_checked = False

    def __repr__(self) -> str:  # never include credentials
        return f"SqlServerConnector(server={self.settings.sql_server!r}, database={self.database!r})"

    # ---- connection --------------------------------------------------------

    def _connection(self):
        if self._conn is None:
            conn = self._connect(build_connection_string(self.settings, self.database))
            try:
                conn.timeout = int(self.settings.query_timeout)
            except Exception:  # not every driver object allows it
                pass
            cur = conn.cursor()
            try:
                cur.execute(_SESSION_SETUP.format(lock_ms=int(self.settings.lock_timeout_ms)))
            except Exception as e:
                raise SqlUnavailable(f"Could not prepare the SQL session: {_error_text(e)}") from None
            finally:
                cur.close()
            self._conn = conn
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            finally:
                self._conn = None

    def reachable(self) -> tuple[bool, str]:
        """(True, "") when a connection opens, else (False, the driver's error text)."""
        try:
            self._connection()
        except SqlUnavailable as e:
            return False, str(e)
        return True, ""

    def _run(self, sql: str, params: tuple, limit: int) -> list[dict[str, Any]]:
        check_read_only(sql)
        cur = self._connection().cursor()
        try:
            try:
                if params:
                    cur.execute(sql, params)
                else:
                    cur.execute(sql)
                cols = [d[0] for d in cur.description or []]
                return [dict(zip(cols, r, strict=False)) for r in cur.fetchmany(max(1, min(limit, 200000)))]
            except SqlUnavailable:
                raise
            except Exception as e:
                if type(e).__module__.split(".")[0] == "pyodbc":
                    raise SqlUnavailable(f"Query failed: {_error_text(e)}") from None
                raise
        finally:
            cur.close()

    # ---- login -------------------------------------------------------------

    def login_info(self) -> dict[str, Any]:
        """Who we are connected as and whether that login could change data."""
        if self._login is None:
            rows = self._run(_LOGIN_INFO, (), 1)
            info = dict(rows[0]) if rows else {}
            info["can_write"] = any(bool(info.get(k)) for k in _WRITE_FLAGS)
            info["write_rights"] = [k for k in _WRITE_FLAGS if info.get(k)]
            self._login = info
        return self._login

    def ensure_read_only_login(self) -> None:
        if self._login_checked or not self.database:
            return
        info = self.login_info()
        if info["can_write"] and not self.settings.allow_privileged_login:
            raise PrivilegedLogin(
                f"The login '{info.get('login_name')}' can change data in {self.database} "
                f"({', '.join(info['write_rights'])}). Database checks need a read-only login: run "
                "`autocount-assist setup-login` to create one. To accept the risk and continue with "
                "this login, set allow_privileged_login = true in the config file.")
        self._login_checked = True

    # ---- queries -----------------------------------------------------------

    def query(self, sql: str, params: tuple = (), mask: bool = True, limit: int = 200,
              unmasked: Iterable[str] = ()) -> list[dict[str, Any]]:
        check_read_only(sql)  # before anything else, so a refused statement never opens a connection
        self.ensure_read_only_login()
        rows = self._run(sql, params, limit)
        return [self.masker.mask_row(r, unmasked) for r in rows] if mask else rows

    def list_databases(self) -> list[dict[str, Any]]:
        """User databases, with a flag for those that look like AutoCount account books."""
        rows = self._run("SELECT name, state_desc, create_date FROM sys.databases ORDER BY name", (), 5000)
        out = []
        for r in rows:
            if r["name"].lower() in SYSTEM_DBS:
                continue
            r["looks_like_account_book"] = r["name"].upper().startswith("AED_")
            out.append(r)
        return out

    # ---- catalog -----------------------------------------------------------

    def catalog(self) -> dict[str, tuple[str, dict[str, tuple[str, str]]]]:
        """{table_lower: (TableName, {column_lower: (ColumnName, data_type)})} for this database."""
        if self._catalog is None:
            rows = self.query(
                "SELECT TABLE_NAME, COLUMN_NAME, DATA_TYPE FROM INFORMATION_SCHEMA.COLUMNS",
                mask=False, limit=200000)
            cat: dict[str, tuple[str, dict[str, tuple[str, str]]]] = {}
            for r in rows:
                entry = cat.setdefault(r["TABLE_NAME"].lower(), (r["TABLE_NAME"], {}))
                entry[1][r["COLUMN_NAME"].lower()] = (r["COLUMN_NAME"], str(r.get("DATA_TYPE") or ""))
            self._catalog = cat
        return self._catalog

    def columns(self) -> dict[str, set[str]]:
        """{table_lower: {column_lower, ...}} for the current database."""
        return {t: set(cols) for t, (_, cols) in self.catalog().items()}

    def missing_objects(self, requires: dict[str, list[str]]) -> list[str]:
        """Return 'Table' or 'Table.Column' names that don't exist in this database."""
        cols = self.columns()
        missing = []
        for table, needed in (requires or {}).items():
            have = cols.get(table.lower())
            if have is None:
                missing.append(table)
                continue
            missing.extend(f"{table}.{c}" for c in needed if c.lower() not in have)
        return missing

    def resolve_objects(self, objects: dict[str, Any] | None) -> tuple[dict[str, str], list[str]]:
        """Match a check's table/column candidates against this database.

        Returns ({alias: "[Table]", "alias.col": "[Column]"}, problems). For each alias the
        first candidate table that has every needed column wins, so one playbook works
        across AutoCount versions that renamed a table or a field.
        """
        cat = self.catalog()
        mapping: dict[str, str] = {}
        problems: list[str] = []
        for alias, (tables, cols) in normalise_objects(objects).items():
            best: str | None = None
            for table in tables:
                entry = cat.get(table.lower())
                if entry is None:
                    continue
                actual_table, have = entry
                found = {c: next((have[x.lower()][0] for x in cands if x.lower() in have), None)
                         for c, cands in cols.items()}
                lacking = [f"{actual_table}.{'|'.join(cols[c])}" for c, v in found.items() if v is None]
                if not lacking:
                    mapping[alias] = quote_ident(actual_table)
                    mapping.update({f"{alias}.{c}": quote_ident(v) for c, v in found.items() if v})
                    best = None
                    break
                best = best or ", ".join(lacking)
            else:
                problems.append(best or f"table {'|'.join(tables)}")
        return mapping, problems

    def schema(self) -> dict[str, Any]:
        """Tables, columns and foreign keys for the knowledge base (structure only, no rows)."""
        columns = self.query(
            "SELECT TABLE_NAME, COLUMN_NAME, DATA_TYPE, CHARACTER_MAXIMUM_LENGTH, IS_NULLABLE "
            "FROM INFORMATION_SCHEMA.COLUMNS ORDER BY TABLE_NAME, ORDINAL_POSITION",
            mask=False, limit=200000)
        fks = self.query(
            "SELECT fk.name AS constraint_name, tp.name AS parent_table, cp.name AS parent_column, "
            "tr.name AS ref_table, cr.name AS ref_column "
            "FROM sys.foreign_keys fk "
            "JOIN sys.foreign_key_columns fkc ON fkc.constraint_object_id = fk.object_id "
            "JOIN sys.tables tp ON tp.object_id = fkc.parent_object_id "
            "JOIN sys.columns cp ON cp.object_id = fkc.parent_object_id AND cp.column_id = fkc.parent_column_id "
            "JOIN sys.tables tr ON tr.object_id = fkc.referenced_object_id "
            "JOIN sys.columns cr ON cr.object_id = fkc.referenced_object_id AND cr.column_id = fkc.referenced_column_id",
            mask=False, limit=100000)
        return {"columns": columns, "foreign_keys": fks}

    def tables_like(self, pattern: str) -> list[str]:
        if not _IDENT.match(pattern.replace("%", "")):
            raise ValueError("Invalid table pattern")
        rows = self.query(
            "SELECT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME LIKE ? ORDER BY TABLE_NAME",
            (pattern,), mask=False)
        return [r["TABLE_NAME"] for r in rows]

    # ---- server and database facts ------------------------------------------

    def server_info(self) -> dict[str, Any]:
        """SQL Server version/edition and the state, size and settings of this database."""
        rows = self.query(_SERVER_INFO, mask=False, limit=1)
        info: dict[str, Any] = dict(rows[0]) if rows else {}
        version = str(info.get("product_version") or "")
        major = int(version.split(".")[0]) if version.split(".")[0].isdigit() else None
        info["major_version"] = major
        info["release"] = SQL_MAJOR_NAMES.get(major or -1)
        info["is_express"] = info.get("engine_edition") == EXPRESS_ENGINE_EDITION
        sizes = {str(r["type_desc"]): int(r["size_mb"] or 0)
                 for r in self.query(_FILE_SIZES, mask=False, limit=10)}
        info["data_mb"] = sizes.get("ROWS", 0)
        info["log_mb"] = sizes.get("LOG", 0)
        if info["is_express"]:
            info["express_limit_mb"] = EXPRESS_DATA_LIMIT_MB
            info["express_used_pct"] = round(100 * info["data_mb"] / EXPRESS_DATA_LIMIT_MB, 1)
        return info

    def database_files(self) -> list[dict[str, Any]]:
        """Data and log file paths of this database, as seen by the SQL Server machine."""
        return self.query(_FILE_PATHS, mask=False, limit=50)

    def row_counts(self, top: int = 15) -> list[dict[str, Any]]:
        """Largest tables by row count, from metadata (no table is scanned)."""
        return self.query(_ROW_COUNTS.format(top=max(1, min(int(top), 500))), mask=False, limit=500)

    def latest_timestamp(self, table: str) -> tuple[str, Any] | None:
        """Newest value of the first date/time column of a table: (column, value)."""
        entry = self.catalog().get(table.lower())
        if entry is None:
            return None
        actual, cols = entry
        for name, dtype in cols.values():
            if dtype.lower() in ("datetime", "datetime2", "smalldatetime", "datetimeoffset"):
                rows = self.query(
                    f"SELECT MAX({quote_ident(name)}) AS latest FROM {quote_ident(actual)}", mask=False, limit=1)
                return name, (rows[0]["latest"] if rows else None)
        return None
