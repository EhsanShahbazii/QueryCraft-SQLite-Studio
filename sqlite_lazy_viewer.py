import csv
import ctypes
import gc
import os
import queue
import sqlite3
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tkinter import BOTH, END, HORIZONTAL, LEFT, RIGHT, VERTICAL, W, X, Y, filedialog, messagebox
import tkinter.font as tkfont
import tkinter as tk
from tkinter import ttk

try:
    import psutil
except Exception:
    psutil = None

try:
    import pandas as pd
except Exception:
    pd = None


APP_TITLE = "QueryCraft SQLite Studio"
MAX_CELL_CHARS = 500
FOOTER_SECTION_HEIGHT = 54

OPERATOR_LABELS = {
    "equals": "equals",
    "not_equals": "does not equal",
    "contains": "contains",
    "not_contains": "does not contain",
    "starts_with": "starts with",
    "ends_with": "ends with",
    "greater_than": "is greater than",
    "greater_equal": "is greater than or equal to",
    "less_than": "is less than",
    "less_equal": "is less than or equal to",
    "between": "is between",
    "is_empty": "is empty",
    "is_not_empty": "is not empty",
    "is_null": "is null",
    "is_not_null": "is not null",
}

FILTER_OPERATORS = {
    "text": [
        "contains",
        "not_contains",
        "starts_with",
        "ends_with",
        "equals",
        "not_equals",
        "is_empty",
        "is_not_empty",
        "is_null",
        "is_not_null",
    ],
    "number": [
        "equals",
        "not_equals",
        "greater_than",
        "greater_equal",
        "less_than",
        "less_equal",
        "between",
        "is_null",
        "is_not_null",
    ],
    "date": [
        "equals",
        "not_equals",
        "greater_than",
        "greater_equal",
        "less_than",
        "less_equal",
        "between",
        "starts_with",
        "is_null",
        "is_not_null",
    ],
    "blob": ["is_null", "is_not_null"],
}

NO_VALUE_OPERATORS = {"is_null", "is_not_null", "is_empty", "is_not_empty"}


def readable_bytes(value):
    if value is None:
        return "n/a"
    value = float(value)
    units = ["B", "KB", "MB", "GB", "TB"]
    for unit in units:
        if abs(value) < 1024 or unit == units[-1]:
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{value:.1f} TB"


def safe_cell_value(value):
    if value is None:
        return "NULL"
    if isinstance(value, bytes):
        return f"<BLOB {len(value):,} bytes>"
    text = str(value)
    if len(text) > MAX_CELL_CHARS:
        return text[:MAX_CELL_CHARS] + "..."
    return text


def quote_identifier(name):
    return '"' + name.replace('"', '""') + '"'


def escape_like(value):
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class MemoryStatusEx(ctypes.Structure):
    _fields_ = [
        ("dwLength", ctypes.c_ulong),
        ("dwMemoryLoad", ctypes.c_ulong),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


class ProcessMemoryCounters(ctypes.Structure):
    _fields_ = [
        ("cb", ctypes.c_ulong),
        ("PageFaultCount", ctypes.c_ulong),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
    ]


def windows_total_memory():
    try:
        status = MemoryStatusEx()
        status.dwLength = ctypes.sizeof(MemoryStatusEx)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return status.ullTotalPhys
    except Exception:
        return None
    return None


def windows_process_memory():
    try:
        counters = ProcessMemoryCounters()
        counters.cb = ctypes.sizeof(ProcessMemoryCounters)
        ctypes.windll.kernel32.GetCurrentProcess.restype = ctypes.c_void_p
        ctypes.windll.psapi.GetProcessMemoryInfo.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ProcessMemoryCounters),
            ctypes.c_ulong,
        ]
        ctypes.windll.psapi.GetProcessMemoryInfo.restype = ctypes.c_int
        handle = ctypes.windll.kernel32.GetCurrentProcess()
        ok = ctypes.windll.psapi.GetProcessMemoryInfo(
            handle,
            ctypes.byref(counters),
            counters.cb,
        )
        if ok:
            return counters.WorkingSetSize
    except Exception:
        return None
    return None


class FileTime(ctypes.Structure):
    _fields_ = [
        ("dwLowDateTime", ctypes.c_ulong),
        ("dwHighDateTime", ctypes.c_ulong),
    ]


def filetime_to_seconds(filetime):
    ticks = (filetime.dwHighDateTime << 32) + filetime.dwLowDateTime
    return ticks / 10_000_000


def windows_system_cpu_times():
    try:
        idle = FileTime()
        kernel = FileTime()
        user = FileTime()
        ok = ctypes.windll.kernel32.GetSystemTimes(
            ctypes.byref(idle),
            ctypes.byref(kernel),
            ctypes.byref(user),
        )
        if ok:
            idle_seconds = filetime_to_seconds(idle)
            total_seconds = filetime_to_seconds(kernel) + filetime_to_seconds(user)
            return idle_seconds, total_seconds
    except Exception:
        return None
    return None


class SQLiteWorker:
    def __init__(self, ui_queue):
        self.ui_queue = ui_queue
        self.executor = ThreadPoolExecutor(max_workers=1)
        self.lock = threading.Lock()
        self.latest_task_by_action = {}

    def run(self, task_id, db_path, action, **kwargs):
        with self.lock:
            self.latest_task_by_action[action] = task_id
        self.executor.submit(self._execute, task_id, db_path, action, kwargs)

    def _is_stale(self, task_id, action):
        if action not in {"load_tables", "load_page"}:
            return False
        with self.lock:
            return self.latest_task_by_action.get(action) != task_id

    def _connect(self, db_path, read_only=True):
        path = Path(db_path).resolve().as_posix()
        if read_only:
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
        else:
            conn = sqlite3.connect(str(Path(db_path).resolve()), timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 10000")
        return conn

    def _execute(self, task_id, db_path, action, kwargs):
        if self._is_stale(task_id, action):
            return
        try:
            with self._connect(db_path, read_only=action != "update_cell") as conn:
                if self._is_stale(task_id, action):
                    return
                if action == "load_tables":
                    result = self._load_tables(conn)
                elif action == "load_page":
                    result = self._load_page(conn, **kwargs)
                elif action == "count_rows":
                    result = self._count_rows(conn, **kwargs)
                elif action == "get_cell":
                    result = self._get_cell(conn, **kwargs)
                elif action == "update_cell":
                    result = self._update_cell(conn, **kwargs)
                elif action == "run_sql":
                    result = self._run_sql(conn, **kwargs)
                else:
                    raise ValueError(f"Unknown action: {action}")
            if self._is_stale(task_id, action):
                return
            self.ui_queue.put((task_id, action, result, None))
        except Exception as exc:
            self.ui_queue.put((task_id, action, None, exc))

    def _load_tables(self, conn):
        started = time.perf_counter()
        rows = conn.execute(
            """
            SELECT type, name, sql
            FROM sqlite_master
            WHERE type IN ('table', 'view')
              AND name NOT LIKE 'sqlite_%'
            ORDER BY type, name
            """
        ).fetchall()
        table_sizes, estimated_sizes = self._table_sizes(conn, rows)
        return [
            {
                "type": row["type"],
                "name": row["name"],
                "sql": row["sql"] or "",
                "bytes": table_sizes.get(row["name"]),
                "estimated_bytes": row["name"] in estimated_sizes,
                "columns": self._table_columns(conn, row["name"]),
                "loaded_ms": (time.perf_counter() - started) * 1000,
            }
            for row in rows
        ]

    def _table_sizes(self, conn, schema_rows):
        try:
            return {
                row["name"]: row["bytes"]
                for row in conn.execute(
                    "SELECT name, SUM(pgsize) AS bytes FROM dbstat GROUP BY name"
                )
            }, set()
        except sqlite3.DatabaseError:
            sizes = {}
            estimated = set()
            for row in schema_rows:
                if row["type"] != "table":
                    continue
                size = self._estimate_table_size(conn, row["name"])
                if size is not None:
                    sizes[row["name"]] = size
                    estimated.add(row["name"])
            return sizes, estimated

    def _estimate_table_size(self, conn, table):
        try:
            count = conn.execute(
                f"SELECT COUNT(*) AS count FROM {quote_identifier(table)}"
            ).fetchone()["count"]
            if count == 0:
                return 0
            sample_count = 0
            payload_total = 0
            for row in conn.execute(f"SELECT * FROM {quote_identifier(table)} LIMIT 200"):
                sample_count += 1
                payload_total += self._row_payload_bytes(row)
            if sample_count == 0:
                return 0
            avg_payload = payload_total / sample_count
            return int(avg_payload * count * 1.35)
        except sqlite3.DatabaseError:
            return None

    def _row_payload_bytes(self, row):
        total = 0
        for value in row:
            if value is None:
                total += 1
            elif isinstance(value, bytes):
                total += len(value)
            elif isinstance(value, (int, float)):
                total += 8
            else:
                total += len(str(value).encode("utf-8", errors="replace"))
        return total

    def _table_columns(self, conn, table):
        return [
            {
                "cid": row["cid"],
                "name": row["name"],
                "type": row["type"] or "",
                "notnull": row["notnull"],
                "default": row["dflt_value"],
                "pk": row["pk"],
            }
            for row in conn.execute(f"PRAGMA table_info({quote_identifier(table)})")
        ]

    def _order_clause(self, columns, sort_column=None, sort_direction="ASC"):
        column_names = {column["name"] for column in columns}
        if sort_column in column_names:
            direction = "DESC" if str(sort_direction).upper() == "DESC" else "ASC"
            return f" ORDER BY {quote_identifier(sort_column)} {direction}"

        pk_columns = sorted(
            [column for column in columns if column["pk"]],
            key=lambda column: column["pk"],
        )
        if pk_columns:
            names = ", ".join(quote_identifier(column["name"]) for column in pk_columns)
            return f" ORDER BY {names}"
        return ""

    def _primary_key_columns(self, columns):
        return [
            column["name"]
            for column in sorted(
                [column for column in columns if column["pk"]],
                key=lambda column: column["pk"],
            )
        ]

    def _where_clause(self, filters, columns):
        if not filters:
            return "", []
        column_names = {column["name"] for column in columns}
        clauses = []
        params = []
        for filter_item in filters:
            column = filter_item.get("column")
            operator = filter_item.get("operator")
            if column not in column_names or operator not in OPERATOR_LABELS:
                continue
            quoted = quote_identifier(column)
            value = filter_item.get("value", "")
            value2 = filter_item.get("value2", "")
            kind = filter_item.get("kind", "text")
            text_expr = f"CAST({quoted} AS TEXT)"
            value_expr = "CAST(? AS REAL)" if kind == "number" else "?"
            numeric_expr = f"CAST({quoted} AS REAL)" if kind == "number" else quoted

            if operator == "is_null":
                clauses.append(f"{quoted} IS NULL")
            elif operator == "is_not_null":
                clauses.append(f"{quoted} IS NOT NULL")
            elif operator == "is_empty":
                clauses.append(f"({quoted} IS NULL OR {text_expr} = '')")
            elif operator == "is_not_empty":
                clauses.append(f"({quoted} IS NOT NULL AND {text_expr} != '')")
            elif operator == "contains":
                clauses.append(f"{text_expr} LIKE ? ESCAPE '\\'")
                params.append(f"%{escape_like(value)}%")
            elif operator == "not_contains":
                clauses.append(f"{text_expr} NOT LIKE ? ESCAPE '\\'")
                params.append(f"%{escape_like(value)}%")
            elif operator == "starts_with":
                clauses.append(f"{text_expr} LIKE ? ESCAPE '\\'")
                params.append(f"{escape_like(value)}%")
            elif operator == "ends_with":
                clauses.append(f"{text_expr} LIKE ? ESCAPE '\\'")
                params.append(f"%{escape_like(value)}")
            elif operator == "equals":
                clauses.append(f"{numeric_expr} = {value_expr}")
                params.append(value)
            elif operator == "not_equals":
                clauses.append(f"{numeric_expr} != {value_expr}")
                params.append(value)
            elif operator == "greater_than":
                clauses.append(f"{numeric_expr} > {value_expr}")
                params.append(value)
            elif operator == "greater_equal":
                clauses.append(f"{numeric_expr} >= {value_expr}")
                params.append(value)
            elif operator == "less_than":
                clauses.append(f"{numeric_expr} < {value_expr}")
                params.append(value)
            elif operator == "less_equal":
                clauses.append(f"{numeric_expr} <= {value_expr}")
                params.append(value)
            elif operator == "between":
                if kind == "number":
                    clauses.append(f"{numeric_expr} BETWEEN CAST(? AS REAL) AND CAST(? AS REAL)")
                else:
                    clauses.append(f"{numeric_expr} BETWEEN ? AND ?")
                params.extend([value, value2])

        if not clauses:
            return "", []
        return " WHERE " + " AND ".join(f"({clause})" for clause in clauses), params

    def _load_page(
        self,
        conn,
        table,
        offset,
        limit,
        filters=None,
        sort_column=None,
        sort_direction="ASC",
        load_all=False,
    ):
        started = time.perf_counter()
        columns = self._table_columns(conn, table)
        column_names = [column["name"] for column in columns]
        select_list = ", ".join(quote_identifier(name) for name in column_names) or "*"
        where_sql, params = self._where_clause(filters or [], columns)
        sql = (
            f"SELECT {select_list} FROM {quote_identifier(table)}"
            f"{where_sql}{self._order_clause(columns, sort_column, sort_direction)}"
        )
        if load_all:
            cursor = conn.execute(sql, params)
        else:
            sql += " LIMIT ? OFFSET ?"
            cursor = conn.execute(sql, [*params, limit, offset])
        key_columns = self._primary_key_columns(columns)
        row_keys = []
        rows = []
        for row in cursor:
            rows.append([safe_cell_value(row[name]) for name in column_names])
            if key_columns:
                row_keys.append(
                    {
                        "columns": key_columns,
                        "values": [row[name] for name in key_columns],
                    }
                )
            else:
                row_keys.append(None)
        return {
            "table": table,
            "columns": columns,
            "rows": rows,
            "row_keys": row_keys,
            "offset": offset,
            "limit": limit,
            "load_all": load_all,
            "filters": filters or [],
            "sort_column": sort_column,
            "sort_direction": sort_direction,
            "elapsed_ms": (time.perf_counter() - started) * 1000,
        }

    def _where_from_key(self, key):
        if not key or not key.get("columns"):
            raise ValueError("This row cannot be edited because it has no primary key.")
        clauses = [f"{quote_identifier(column)} IS ?" for column in key["columns"]]
        return " AND ".join(clauses), list(key["values"])

    def _get_cell(self, conn, table, column, key):
        columns = self._table_columns(conn, table)
        column_names = {item["name"] for item in columns}
        if column not in column_names:
            raise ValueError("Column not found.")
        where_sql, params = self._where_from_key(key)
        row = conn.execute(
            f"SELECT {quote_identifier(column)} AS value FROM {quote_identifier(table)} WHERE {where_sql} LIMIT 1",
            params,
        ).fetchone()
        if row is None:
            raise ValueError("Row not found. It may have changed.")
        value = row["value"]
        if isinstance(value, bytes):
            value_text = value.hex()
            is_blob = True
        elif value is None:
            value_text = ""
            is_blob = False
        else:
            value_text = str(value)
            is_blob = False
        return {
            "table": table,
            "column": column,
            "key": key,
            "value": value_text,
            "is_null": value is None,
            "is_blob": is_blob,
        }

    def _update_cell(self, conn, table, column, key, value, set_null=False):
        columns = self._table_columns(conn, table)
        column_names = {item["name"] for item in columns}
        if column not in column_names:
            raise ValueError("Column not found.")
        where_sql, where_params = self._where_from_key(key)
        new_value = None if set_null else value
        cursor = conn.execute(
            f"UPDATE {quote_identifier(table)} SET {quote_identifier(column)} = ? WHERE {where_sql}",
            [new_value, *where_params],
        )
        conn.commit()
        if cursor.rowcount == 0:
            raise ValueError("No rows were updated. It may have changed.")
        return {
            "table": table,
            "column": column,
            "updated_rows": cursor.rowcount,
        }

    def _run_sql(self, conn, sql, max_rows):
        started = time.perf_counter()
        statement = sql.strip().rstrip(";").strip()
        if not statement:
            raise ValueError("Write a SQL statement first.")
        cursor = conn.execute(statement)
        columns = [item[0] for item in cursor.description] if cursor.description else []
        fetched = cursor.fetchmany(int(max_rows) + 1) if columns else []
        rows = [
            [safe_cell_value(value) for value in row]
            for row in fetched[: int(max_rows)]
        ]
        return {
            "columns": columns,
            "rows": rows,
            "truncated": len(fetched) > int(max_rows),
            "elapsed_ms": (time.perf_counter() - started) * 1000,
            "max_rows": int(max_rows),
        }

    def _count_rows(self, conn, table, filters=None):
        started = time.perf_counter()
        columns = self._table_columns(conn, table)
        where_sql, params = self._where_clause(filters or [], columns)
        row = conn.execute(
            f"SELECT COUNT(*) AS count FROM {quote_identifier(table)}{where_sql}",
            params,
        ).fetchone()
        return {
            "table": table,
            "count": row["count"],
            "filters": filters or [],
            "elapsed_ms": (time.perf_counter() - started) * 1000,
        }


class SQLiteLazyViewer(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_TITLE)
        self._set_app_icon()
        self.geometry("1320x780")
        self.minsize(1120, 620)
        self.after(0, self._maximize_window)

        initial_db = os.environ.pop("QUERYCRAFT_DB_PATH", "")
        self.db_path = tk.StringVar(value=initial_db)
        self.page_size = tk.IntVar(value=100)
        self.offset = 0
        self.current_table = None
        self.current_columns = []
        self.current_rows = []
        self.current_row_keys = []
        self.sort_column = None
        self.sort_direction = "ASC"
        self.full_view_enabled = False
        self.tables = []
        self.schemas = {}
        self.last_column_count = 0
        self.last_elapsed_ms = None
        self.last_count_info = "Count: n/a"
        self.table_size_by_name = {}
        self.table_size_is_estimated = {}
        self.table_item_to_name = {}
        self.table_type_by_name = {}
        self.pending_cell_edit = None
        self.active_filters = []
        self.column_type_by_name = {}
        self.operator_key_by_label = {}
        self.table_size_sort_desc = True
        self.count_cache = {}
        self.pending_load_after = None
        self.filters_visible = False
        self.sql_visible = False
        self.sql_max_rows = tk.IntVar(value=500)
        self.process = psutil.Process(os.getpid()) if psutil else None
        self.last_system_cpu_times = windows_system_cpu_times()
        self.last_cpu_refresh_time = 0
        self.task_id = 0
        self.latest_task_id = 0
        self.ui_queue = queue.Queue()
        self.worker = SQLiteWorker(self.ui_queue)

        self._build_ui()
        self.after(100, self._drain_queue)
        self.after(1000, self._update_footer_details)
        if self.db_path.get():
            self.load_tables()

    def _set_app_icon(self):
        icon = tk.PhotoImage(width=32, height=32)
        icon.put("#0f172a", to=(0, 0, 32, 32))
        icon.put("#38bdf8", to=(6, 6, 26, 10))
        icon.put("#0ea5e9", to=(5, 9, 27, 15))
        icon.put("#22c55e", to=(6, 15, 26, 20))
        icon.put("#16a34a", to=(5, 19, 27, 25))
        icon.put("#a7f3d0", to=(9, 7, 23, 8))
        icon.put("#e0f2fe", to=(10, 16, 22, 17))
        icon.put("#f8fafc", to=(8, 25, 24, 27))
        icon.put("#facc15", to=(24, 4, 28, 8))
        icon.put("#facc15", to=(25, 3, 27, 9))
        self.app_icon = icon
        self.iconphoto(True, self.app_icon)

    def _build_ui(self):
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        top = ttk.Frame(self, padding=(10, 10, 10, 6))
        top.grid(row=0, column=0, sticky="ew")
        top.columnconfigure(1, weight=1)

        ttk.Label(top, text="Database").grid(row=0, column=0, sticky=W, padx=(0, 8))
        ttk.Entry(top, textvariable=self.db_path).grid(row=0, column=1, sticky="ew")
        ttk.Button(top, text="Open", command=self.choose_database).grid(row=0, column=2, padx=(8, 0))
        ttk.Button(top, text="Reload", command=self.load_tables).grid(row=0, column=3, padx=(8, 0))

        body = ttk.PanedWindow(self, orient=HORIZONTAL)
        body.grid(row=1, column=0, sticky="nsew", padx=10, pady=(0, 6))

        left = ttk.Frame(body, width=260)
        self.left_panel = left
        left.rowconfigure(1, weight=1)
        left.columnconfigure(0, weight=1)
        body.add(left, weight=0)

        ttk.Label(left, text="Tables").grid(row=0, column=0, sticky=W, pady=(0, 4))
        self.table_list = ttk.Treeview(
            left,
            columns=("size",),
            show="tree headings",
            selectmode="browse",
            height=12,
        )
        self.table_list.heading("#0", text="Name")
        self.table_list.heading("size", text="Size", command=self.sort_tables_by_size)
        self.table_list.column("#0", width=160, minwidth=120, stretch=True)
        self.table_list.column("size", width=96, minwidth=70, stretch=False, anchor="e")
        self.table_list.grid(row=1, column=0, sticky="nsew")
        self.table_list.bind("<<TreeviewSelect>>", self.on_table_selected)
        self.table_list.tag_configure("table", background="#f3f7ff")
        self.table_list.tag_configure("column", background="#ffffff", foreground="#374151")
        self.table_yscroll = ttk.Scrollbar(left, orient=VERTICAL, command=self.table_list.yview)
        self.table_yscroll.grid(row=1, column=1, sticky="ns")
        self.table_xscroll = ttk.Scrollbar(left, orient=HORIZONTAL, command=self.table_list.xview)
        self.table_xscroll.grid(row=2, column=0, sticky="ew", pady=(2, 0))
        self.table_list.configure(
            xscrollcommand=self.table_xscroll.set,
            yscrollcommand=self.table_yscroll.set,
        )

        right = ttk.Frame(body)
        right.rowconfigure(3, weight=1)
        right.columnconfigure(0, weight=1)
        body.add(right, weight=1)

        controls = ttk.Frame(right)
        controls.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        controls.columnconfigure(9, weight=1)

        ttk.Label(controls, text="Page size").grid(row=0, column=0, padx=(0, 6))
        ttk.Spinbox(
            controls,
            from_=10,
            to=5000,
            increment=50,
            textvariable=self.page_size,
            width=7,
            command=self.reload_current_page,
        ).grid(row=0, column=1, padx=(0, 10))
        ttk.Button(controls, text="First", command=self.first_page).grid(row=0, column=2, padx=(0, 4))
        ttk.Button(controls, text="Prev", command=self.prev_page).grid(row=0, column=3, padx=(0, 4))
        ttk.Button(controls, text="Next", command=self.next_page).grid(row=0, column=4, padx=(0, 4))
        self.full_view_button = ttk.Button(controls, text="Full view", command=self.toggle_full_view)
        self.full_view_button.grid(row=0, column=5, padx=(0, 4))
        ttk.Button(controls, text="All rows", command=self.load_all_rows).grid(row=0, column=6, padx=(0, 10))
        ttk.Button(controls, text="Count rows", command=self.count_rows).grid(row=0, column=7, padx=(0, 4))
        ttk.Button(controls, text="Schema", command=self.show_schema_modal).grid(row=0, column=10, padx=(0, 4))
        ttk.Button(controls, text="Export CSV", command=self.export_current_page).grid(row=0, column=11, padx=(0, 4))
        ttk.Button(controls, text="Export Parquet", command=self.export_current_parquet).grid(row=0, column=12)
        ttk.Button(controls, text="Clean RAM", command=self.clean_ram).grid(row=0, column=13, padx=(8, 0))
        self.sql_toggle_button = ttk.Button(controls, text="Show SQL", command=self.toggle_sql_panel)
        self.sql_toggle_button.grid(row=0, column=14, padx=(8, 0))

        self.sql_panel = ttk.Frame(right)
        self.sql_panel.grid(row=1, column=0, sticky="ew", pady=(0, 6))
        self.sql_panel.columnconfigure(0, weight=1)
        self.sql_panel.rowconfigure(0, weight=1)

        self.sql_text = tk.Text(self.sql_panel, height=5, wrap="none", undo=True)
        self.sql_text.grid(row=0, column=0, columnspan=6, sticky="ew")
        self.sql_text.insert("1.0", "SELECT * FROM tracks LIMIT 50;")
        sql_scroll = ttk.Scrollbar(self.sql_panel, orient=VERTICAL, command=self.sql_text.yview)
        sql_scroll.grid(row=0, column=6, sticky="ns")
        self.sql_text.configure(yscrollcommand=sql_scroll.set)
        ttk.Button(self.sql_panel, text="Run SQL", command=self.run_sql).grid(row=1, column=0, sticky="w", pady=(5, 0))
        ttk.Button(self.sql_panel, text="Clear", command=lambda: self.sql_text.delete("1.0", END)).grid(row=1, column=1, sticky="w", padx=(6, 0), pady=(5, 0))
        ttk.Label(self.sql_panel, text="Max rows").grid(row=1, column=2, sticky="e", padx=(12, 4), pady=(5, 0))
        ttk.Spinbox(self.sql_panel, from_=10, to=50000, increment=100, textvariable=self.sql_max_rows, width=8).grid(row=1, column=3, sticky="w", pady=(5, 0))
        self.sql_info = tk.StringVar(value="Read-only SQL. Results appear in the grid below.")
        ttk.Label(self.sql_panel, textvariable=self.sql_info).grid(row=1, column=4, columnspan=2, sticky="w", padx=(12, 0), pady=(5, 0))
        self.sql_panel.grid_remove()

        filters = ttk.Frame(right)
        filters.grid(row=2, column=0, sticky="ew", pady=(0, 6))
        filters.columnconfigure(5, weight=1)

        ttk.Label(filters, text="Column").grid(row=0, column=0, padx=(0, 6))
        self.filter_column = tk.StringVar(value="")
        self.filter_combo = ttk.Combobox(filters, textvariable=self.filter_column, width=24, state="readonly")
        self.filter_combo.grid(row=0, column=1, padx=(0, 10))
        self.filter_combo.bind("<<ComboboxSelected>>", self.on_filter_column_changed)

        ttk.Label(filters, text="Condition").grid(row=0, column=2, padx=(0, 6))
        self.filter_operator = tk.StringVar(value="")
        self.filter_operator_combo = ttk.Combobox(
            filters,
            textvariable=self.filter_operator,
            width=25,
            state="readonly",
        )
        self.filter_operator_combo.grid(row=0, column=3, padx=(0, 10))
        self.filter_operator_combo.bind("<<ComboboxSelected>>", self.on_filter_operator_changed)

        ttk.Label(filters, text="Value").grid(row=0, column=4, padx=(0, 6))
        self.filter_value = tk.StringVar(value="")
        self.filter_value_entry = ttk.Entry(filters, textvariable=self.filter_value)
        self.filter_value_entry.grid(row=0, column=5, sticky="ew", padx=(0, 8))
        self.filter_value_entry.bind("<Return>", lambda _event: self.add_filter())

        ttk.Label(filters, text="And").grid(row=0, column=6, padx=(0, 6))
        self.filter_value2 = tk.StringVar(value="")
        self.filter_value2_entry = ttk.Entry(filters, textvariable=self.filter_value2, width=16)
        self.filter_value2_entry.grid(row=0, column=7, padx=(0, 8))
        self.filter_value2_entry.bind("<Return>", lambda _event: self.add_filter())

        ttk.Button(filters, text="Add filter", command=self.add_filter).grid(row=0, column=8, padx=(0, 4))
        ttk.Button(filters, text="Remove", command=self.remove_selected_filter).grid(row=0, column=9, padx=(0, 4))
        ttk.Button(filters, text="Clear all", command=self.clear_filter).grid(row=0, column=10, padx=(0, 4))
        self.filters_toggle_button = ttk.Button(
            filters,
            text="Show filters",
            command=self.toggle_filters_list,
        )
        self.filters_toggle_button.grid(row=0, column=11)

        self.filter_list = ttk.Treeview(
            filters,
            columns=("column", "condition", "value"),
            show="headings",
            height=3,
            selectmode="browse",
        )
        self.filter_list.heading("column", text="Active column")
        self.filter_list.heading("condition", text="Condition")
        self.filter_list.heading("value", text="Value")
        self.filter_list.column("column", width=160, minwidth=100, stretch=True)
        self.filter_list.column("condition", width=190, minwidth=120, stretch=True)
        self.filter_list.column("value", width=220, minwidth=120, stretch=True)
        self.filter_list.grid(row=1, column=0, columnspan=12, sticky="ew", pady=(6, 0))
        self.filter_list.grid_remove()

        grid_frame = ttk.Frame(right)
        grid_frame.grid(row=3, column=0, sticky="nsew")
        grid_frame.rowconfigure(0, weight=1)
        grid_frame.columnconfigure(0, weight=1)

        self.tree = ttk.Treeview(grid_frame, show="headings")
        self.tree.grid(row=0, column=0, sticky="nsew")
        self.tree.tag_configure("odd", background="#f8fbff")
        self.tree.tag_configure("even", background="#eef5ff")
        self.tree.bind("<Double-1>", self.on_cell_double_click)
        yscroll = ttk.Scrollbar(grid_frame, orient=VERTICAL, command=self.tree.yview)
        yscroll.grid(row=0, column=1, sticky="ns")
        xscroll = ttk.Scrollbar(grid_frame, orient=HORIZONTAL, command=self.tree.xview)
        xscroll.grid(row=1, column=0, sticky="ew")
        self.tree.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)

        bottom = ttk.Frame(self, padding=(10, 0, 10, 10))
        bottom.grid(row=2, column=0, sticky="ew")
        self.footer_widths = [135, 150, 120, 125, 150, 150, 150]
        for column, width in enumerate(self.footer_widths):
            bottom.columnconfigure(column, weight=1, minsize=width)
        self.status = tk.StringVar(value="Ready")
        ttk.Label(bottom, textvariable=self.status).grid(
            row=0, column=0, columnspan=7, sticky="w", pady=(0, 5)
        )
        self.db_size_detail = tk.StringVar(value="n/a")
        self.table_size_detail = tk.StringVar(value="n/a")
        self.column_count_detail = tk.StringVar(value="0")
        self.elapsed_detail = tk.StringVar(value="n/a")
        self.row_count_detail = tk.StringVar(value="n/a")
        self._make_footer_section(bottom, "Database Size", self.db_size_detail, 0)
        self._make_footer_section(bottom, "Table Size", self.table_size_detail, 1)
        self._make_footer_section(bottom, "Column Count", self.column_count_detail, 2)
        self._make_footer_section(bottom, "Time Elapsed", self.elapsed_detail, 3)
        self._make_footer_section(bottom, "Row Count", self.row_count_detail, 4)

        self.ram_text = tk.StringVar(value="RAM n/a")
        self.ram_canvas = self._make_meter_section(bottom, "RAM", self.ram_text, 5)
        self.cpu_text = tk.StringVar(value="CPU n/a")
        self.cpu_canvas = self._make_meter_section(bottom, "CPU", self.cpu_text, 6)

    def _maximize_window(self):
        try:
            self.state("zoomed")
        except tk.TclError:
            try:
                self.attributes("-zoomed", True)
            except tk.TclError:
                pass

    def _make_footer_section(self, parent, title, variable, column):
        section = ttk.LabelFrame(
            parent,
            text=title,
            padding=(6, 3),
            width=self.footer_widths[column],
            height=FOOTER_SECTION_HEIGHT,
        )
        section.grid_propagate(False)
        padx = (0, 4) if column < len(self.footer_widths) - 1 else (0, 0)
        section.grid(row=1, column=column, sticky="ew", padx=padx)
        section.columnconfigure(0, weight=1)
        label = ttk.Label(section, textvariable=variable, anchor="w")
        label.grid(row=0, column=0, sticky="ew")
        return section

    def _make_meter_section(self, parent, title, variable, column):
        section = ttk.LabelFrame(
            parent,
            text=title,
            padding=(6, 3),
            width=self.footer_widths[column],
            height=FOOTER_SECTION_HEIGHT,
        )
        section.grid_propagate(False)
        section.grid(row=1, column=column, sticky="ew", padx=(4, 0))
        section.columnconfigure(0, weight=1)
        ttk.Label(section, textvariable=variable, anchor="w").grid(row=0, column=0, sticky="ew")
        canvas = tk.Canvas(
            section,
            width=110,
            height=12,
            highlightthickness=0,
            bg="#e5e7eb",
        )
        canvas.grid(row=1, column=0, sticky="ew", pady=(3, 0))
        return canvas

    def _next_task_id(self):
        self.task_id += 1
        self.latest_task_id = self.task_id
        return self.task_id

    def _drain_queue(self):
        while True:
            try:
                task_id, action, result, error = self.ui_queue.get_nowait()
            except queue.Empty:
                break
            if task_id != self.latest_task_id and action not in {"count_rows", "get_cell", "update_cell"}:
                continue
            if error:
                self.status.set("Error")
                messagebox.showerror("SQLite error", str(error))
                continue
            if action == "load_tables":
                self._show_tables(result)
            elif action == "load_page":
                self._show_page(result)
            elif action == "count_rows":
                self._store_count_cache(result)
                self.last_count_info = (
                    f"Count {result['count']:,} rows in {result['elapsed_ms']:.1f} ms"
                )
                self.status.set(f"{result['table']}: {self.last_count_info}")
                self._refresh_footer_details()
            elif action == "get_cell":
                self._show_edit_modal(result)
            elif action == "update_cell":
                self.status.set(
                    f"Updated {result['table']}.{result['column']} ({result['updated_rows']} row)"
                )
                self.count_cache = {}
                self.load_page()
            elif action == "run_sql":
                self._show_sql_result(result)
        self.after(100, self._drain_queue)

    def choose_database(self):
        path = filedialog.askopenfilename(
            title="Open SQLite database",
            filetypes=[
                ("SQLite databases", "*.sqlite *.sqlite3 *.db *.db3"),
                ("All files", "*.*"),
            ],
        )
        if path:
            self.db_path.set(path)
            self.load_tables()

    def load_tables(self):
        db_path = self.db_path.get().strip()
        if not db_path:
            messagebox.showwarning("No database", "Choose a SQLite database first.")
            return
        if not Path(db_path).exists():
            messagebox.showerror("Missing database", f"File not found:\n{db_path}")
            return
        self.status.set("Loading table list...")
        self.table_list.delete(*self.table_list.get_children())
        self._clear_grid()
        self.current_table = None
        self.last_column_count = 0
        self.last_elapsed_ms = None
        self.last_count_info = "Count: n/a"
        self.table_size_by_name = {}
        self.table_size_is_estimated = {}
        self.table_item_to_name = {}
        self.count_cache = {}
        self._refresh_footer_details()
        self.worker.run(self._next_task_id(), db_path, "load_tables")

    def _show_tables(self, tables):
        self.tables = tables
        self.schemas = {table["name"]: table["sql"] for table in tables}
        self.table_size_by_name = {table["name"]: table.get("bytes") for table in tables}
        self.table_type_by_name = {table["name"]: table.get("type") for table in tables}
        self.table_size_is_estimated = {
            table["name"]: table.get("estimated_bytes", False) for table in tables
        }
        self._populate_table_tree()
        self._sync_table_list_width()
        self.status.set(f"Loaded {len(tables)} tables/views. Select one to view rows.")
        self._refresh_footer_details()
        if tables:
            first_item = f"table:{self._sorted_tables()[0]['name']}"
            self.table_list.selection_set(first_item)
            self.table_list.focus(first_item)
            self.on_table_selected()

    def _sorted_tables(self):
        direction = -1 if self.table_size_sort_desc else 1
        return sorted(
            self.tables,
            key=lambda table: (
                table.get("bytes") is None,
                direction * (table.get("bytes") or 0),
                table["name"].lower(),
            ),
        )

    def sort_tables_by_size(self):
        self.table_size_sort_desc = not self.table_size_sort_desc
        current = self.current_table
        self._populate_table_tree()
        self._sync_table_list_width()
        if current:
            item = f"table:{current}"
            if self.table_list.exists(item):
                self.table_list.selection_set(item)
                self.table_list.focus(item)
                self.table_list.see(item)

    def _populate_table_tree(self):
        self.table_item_to_name = {}
        self.table_list.delete(*self.table_list.get_children())
        sort_arrow = " v" if self.table_size_sort_desc else " ^"
        self.table_list.heading("size", text=f"Size{sort_arrow}", command=self.sort_tables_by_size)
        for table in self._sorted_tables():
            table_name = table["name"]
            table_item = f"table:{table_name}"
            self.table_item_to_name[table_item] = table_name
            size_prefix = "~" if table.get("estimated_bytes") else ""
            size_text = (
                f"{size_prefix}{readable_bytes(table.get('bytes'))}"
                if table.get("bytes") is not None
                else "n/a"
            )
            self.table_list.insert(
                "",
                END,
                iid=table_item,
                text=table_name,
                values=(size_text,),
                tags=("table",),
            )
            for column in table.get("columns", []):
                column_name = column["name"]
                column_item = f"column:{table_name}:{column_name}"
                self.table_item_to_name[column_item] = table_name
                suffixes = []
                column_type = column["type"] or "ANY"
                suffixes.append(column_type)
                if column["pk"]:
                    suffixes.append("PK")
                if column["notnull"]:
                    suffixes.append("NOT NULL")
                self.table_list.insert(
                    table_item,
                    END,
                    iid=column_item,
                    text=f"  {column_name} ({', '.join(suffixes)})",
                    values=("",),
                    tags=("column",),
                )

    def _sync_table_list_width(self):
        if not self.tables:
            self.left_panel.configure(width=180)
            self.table_list.column("#0", width=140)
            return
        names = []
        for table in self.tables:
            names.append(table["name"])
            names.extend(f"  {column['name']}" for column in table.get("columns", []))
        font = tkfont.nametofont("TkDefaultFont")
        longest_px = max(font.measure(name) for name in names)
        width_px = min(max(longest_px + 170, 260), 520)
        self.left_panel.configure(width=width_px)
        self.table_list.column("#0", width=min(max(longest_px + 36, 150), 350))

    def on_table_selected(self, _event=None):
        selection = self.table_list.selection()
        if not selection:
            return
        table = self.table_item_to_name.get(selection[0])
        if not table:
            return
        self.current_table = table
        self.offset = 0
        self.sort_column = None
        self.sort_direction = "ASC"
        self.active_filters = []
        self._render_active_filters()
        self.filter_column.set("")
        self.filter_value.set("")
        self.filter_value2.set("")
        self._restore_count_info_from_cache()
        self._refresh_footer_details()
        self.load_page()

    def show_schema_modal(self):
        if not self.current_table:
            messagebox.showwarning("No table", "Select a table first.")
            return

        modal = tk.Toplevel(self)
        modal.title(f"Schema - {self.current_table}")
        modal.geometry("760x420")
        modal.minsize(520, 300)
        modal.transient(self)

        modal.columnconfigure(0, weight=1)
        modal.rowconfigure(1, weight=1)

        ttk.Label(modal, text=self.current_table, font=("TkDefaultFont", 12, "bold")).grid(
            row=0, column=0, sticky=W, padx=10, pady=(10, 6)
        )

        frame = ttk.Frame(modal, padding=(10, 0, 10, 10))
        frame.grid(row=1, column=0, sticky="nsew")
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)

        text = tk.Text(frame, wrap="none")
        text.grid(row=0, column=0, sticky="nsew")
        yscroll = ttk.Scrollbar(frame, orient=VERTICAL, command=text.yview)
        yscroll.grid(row=0, column=1, sticky="ns")
        xscroll = ttk.Scrollbar(frame, orient=HORIZONTAL, command=text.xview)
        xscroll.grid(row=1, column=0, sticky="ew")
        text.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)
        text.insert("1.0", self.schemas.get(self.current_table, "Schema not available."))
        text.configure(state="disabled")

        ttk.Button(modal, text="Close", command=modal.destroy).grid(
            row=2, column=0, sticky="e", padx=10, pady=(0, 10)
        )
        self._center_modal(modal, 760, 420)

    def _center_modal(self, modal, width, height):
        self.update_idletasks()
        modal.update_idletasks()
        parent_x = self.winfo_rootx()
        parent_y = self.winfo_rooty()
        parent_w = self.winfo_width()
        parent_h = self.winfo_height()
        x = parent_x + max((parent_w - width) // 2, 0)
        y = parent_y + max((parent_h - height) // 2, 0)
        modal.geometry(f"{width}x{height}+{x}+{y}")

    def on_cell_double_click(self, event):
        if self.tree.identify_region(event.x, event.y) != "cell":
            return
        if self.table_type_by_name.get(self.current_table) != "table":
            messagebox.showwarning("Cannot edit", "Only real tables can be edited.")
            return
        item = self.tree.identify_row(event.y)
        column_id = self.tree.identify_column(event.x)
        if not item or not column_id:
            return
        try:
            row_index = self.tree.index(item)
            column_index = int(column_id[1:]) - 1
        except (TypeError, ValueError):
            return
        if row_index >= len(self.current_row_keys) or column_index >= len(self.current_columns):
            return
        key = self.current_row_keys[row_index]
        if not key:
            messagebox.showwarning(
                "Cannot edit",
                "This row cannot be edited safely because it has no primary key.",
            )
            return
        column = self.current_columns[column_index]["name"]
        self.status.set(f"Loading {self.current_table}.{column} for editing...")
        self.worker.run(
            self._next_task_id(),
            self.db_path.get().strip(),
            "get_cell",
            table=self.current_table,
            column=column,
            key=key,
        )

    def _show_edit_modal(self, cell):
        modal = tk.Toplevel(self)
        modal.title(f"Edit {cell['table']}.{cell['column']}")
        modal.geometry("640x420")
        modal.minsize(520, 320)
        modal.transient(self)
        modal.columnconfigure(0, weight=1)
        modal.rowconfigure(1, weight=1)

        title = f"{cell['table']}.{cell['column']}"
        ttk.Label(modal, text=title, font=("TkDefaultFont", 12, "bold")).grid(
            row=0, column=0, sticky=W, padx=10, pady=(10, 6)
        )

        frame = ttk.Frame(modal, padding=(10, 0, 10, 10))
        frame.grid(row=1, column=0, sticky="nsew")
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)

        text = tk.Text(frame, wrap="word", undo=True)
        text.grid(row=0, column=0, sticky="nsew")
        yscroll = ttk.Scrollbar(frame, orient=VERTICAL, command=text.yview)
        yscroll.grid(row=0, column=1, sticky="ns")
        text.configure(yscrollcommand=yscroll.set)
        text.insert("1.0", cell["value"])

        set_null = tk.BooleanVar(value=cell["is_null"])
        actions = ttk.Frame(modal, padding=(10, 0, 10, 10))
        actions.grid(row=2, column=0, sticky="ew")
        actions.columnconfigure(0, weight=1)
        ttk.Checkbutton(actions, text="Set NULL", variable=set_null).grid(
            row=0, column=0, sticky="w"
        )

        def save_edit():
            if cell.get("is_blob"):
                messagebox.showwarning(
                    "Cannot edit BLOB",
                    "BLOB editing is not supported in this viewer.",
                )
                return
            value = text.get("1.0", "end-1c")
            modal.destroy()
            self.status.set(f"Saving {cell['table']}.{cell['column']}...")
            self.worker.run(
                self._next_task_id(),
                self.db_path.get().strip(),
                "update_cell",
                table=cell["table"],
                column=cell["column"],
                key=cell["key"],
                value=value,
                set_null=set_null.get(),
            )

        ttk.Button(actions, text="Save", command=save_edit).grid(row=0, column=1, padx=(0, 6))
        ttk.Button(actions, text="Cancel", command=modal.destroy).grid(row=0, column=2)
        self._center_modal(modal, 640, 420)

    def _page_size(self):
        try:
            size = int(self.page_size.get())
        except (TypeError, ValueError):
            size = 100
        return max(1, min(size, 5000))

    def _filter_kind(self, declared_type):
        normalized = (declared_type or "").lower()
        if any(token in normalized for token in ("int", "real", "floa", "doub", "num", "dec")):
            return "number"
        if any(token in normalized for token in ("date", "time")):
            return "date"
        if "blob" in normalized:
            return "blob"
        return "text"

    def on_filter_column_changed(self, _event=None):
        column = self.filter_column.get()
        kind = self._filter_kind(self.column_type_by_name.get(column, ""))
        operators = FILTER_OPERATORS.get(kind, FILTER_OPERATORS["text"])
        labels = [OPERATOR_LABELS[operator] for operator in operators]
        self.operator_key_by_label = {
            OPERATOR_LABELS[operator]: operator for operator in operators
        }
        self.filter_operator_combo.configure(values=labels)
        if labels:
            self.filter_operator.set(labels[0])
        else:
            self.filter_operator.set("")
        self.on_filter_operator_changed()

    def on_filter_operator_changed(self, _event=None):
        operator = self._selected_operator()
        needs_value = operator not in NO_VALUE_OPERATORS
        needs_second_value = operator == "between"
        self.filter_value_entry.configure(state="normal" if needs_value else "disabled")
        self.filter_value2_entry.configure(state="normal" if needs_second_value else "disabled")
        if not needs_value:
            self.filter_value.set("")
        if not needs_second_value:
            self.filter_value2.set("")

    def _selected_operator(self):
        return self.operator_key_by_label.get(self.filter_operator.get(), "")

    def _validate_filter_values(self, column, operator, value, value2, kind):
        if not column:
            messagebox.showwarning("No column", "Choose a filter column first.")
            return False
        if not operator:
            messagebox.showwarning("No condition", "Choose a filter condition first.")
            return False
        if operator not in NO_VALUE_OPERATORS and value == "":
            messagebox.showwarning("Missing value", "Enter a value for this filter.")
            return False
        if operator == "between" and value2 == "":
            messagebox.showwarning("Missing value", "Enter both values for a between filter.")
            return False
        if kind == "number" and operator not in NO_VALUE_OPERATORS:
            values = [value2] if operator == "between" else []
            values.insert(0, value)
            try:
                for item in values:
                    float(item)
            except ValueError:
                messagebox.showwarning("Invalid number", "This column expects numeric filter values.")
                return False
        return True

    def add_filter(self):
        column = self.filter_column.get()
        operator = self._selected_operator()
        value = self.filter_value.get().strip()
        value2 = self.filter_value2.get().strip()
        kind = self._filter_kind(self.column_type_by_name.get(column, ""))
        if not self._validate_filter_values(column, operator, value, value2, kind):
            return
        self.active_filters.append(
            {
                "column": column,
                "operator": operator,
                "value": value,
                "value2": value2,
                "kind": kind,
            }
        )
        self.filter_value.set("")
        self.filter_value2.set("")
        self._render_active_filters()
        self._restore_count_info_from_cache()
        self._refresh_footer_details()
        self.offset = 0
        self.load_page()

    def remove_selected_filter(self):
        selection = self.filter_list.selection()
        if not selection:
            return
        try:
            index = int(selection[0].split(":", 1)[1])
        except (IndexError, ValueError):
            return
        if 0 <= index < len(self.active_filters):
            del self.active_filters[index]
        if not self.active_filters:
            self._reset_filter_builder()
        self._render_active_filters()
        self._restore_count_info_from_cache()
        self._refresh_footer_details()
        self.offset = 0
        self.load_page()

    def _reset_filter_builder(self):
        self.filter_value.set("")
        self.filter_value2.set("")
        values = list(self.filter_combo.cget("values"))
        if values:
            self.filter_column.set(values[0])
            self.on_filter_column_changed()

    def _render_active_filters(self):
        self.filter_list.delete(*self.filter_list.get_children())
        for index, filter_item in enumerate(self.active_filters):
            value = filter_item.get("value", "")
            if filter_item.get("operator") == "between":
                value = f"{value} and {filter_item.get('value2', '')}"
            if filter_item.get("operator") in NO_VALUE_OPERATORS:
                value = ""
            self.filter_list.insert(
                "",
                END,
                iid=f"filter:{index}",
                values=(
                    filter_item["column"],
                    OPERATOR_LABELS.get(filter_item["operator"], filter_item["operator"]),
                    value,
                ),
            )
        label = "Hide filters" if self.filters_visible else "Show filters"
        if self.active_filters:
            label += f" ({len(self.active_filters)})"
        self.filters_toggle_button.configure(text=label)

    def toggle_filters_list(self):
        self.filters_visible = not self.filters_visible
        if self.filters_visible:
            self.filter_list.grid()
        else:
            self.filter_list.grid_remove()
        self._render_active_filters()

    def toggle_sql_panel(self):
        self.sql_visible = not self.sql_visible
        if self.sql_visible:
            self.sql_panel.grid()
            self.sql_toggle_button.configure(text="Hide SQL")
        else:
            self.sql_panel.grid_remove()
            self.sql_toggle_button.configure(text="Show SQL")

    def run_sql(self):
        sql = self.sql_text.get("1.0", "end-1c").strip()
        if not sql:
            messagebox.showwarning("No SQL", "Write a SQL command first.")
            return
        self.status.set("Running SQL...")
        self.worker.run(
            self._next_task_id(),
            self.db_path.get().strip(),
            "run_sql",
            sql=sql,
            max_rows=self.sql_max_rows.get(),
        )

    def _show_sql_result(self, result):
        self.current_table = None
        self.current_columns = [{"name": column, "type": ""} for column in result["columns"]]
        self.current_rows = result["rows"]
        self.current_row_keys = [None for _row in self.current_rows]
        self.tree.delete(*self.tree.get_children())
        self.tree["columns"] = result["columns"]
        for column in result["columns"]:
            self.tree.heading(column, text=column)
            self.tree.column(column, minwidth=80, width=min(max(len(column) * 12, 100), 260), stretch=True)
        for index, row in enumerate(self.current_rows):
            tag = "even" if index % 2 == 0 else "odd"
            self.tree.insert("", END, values=row, tags=(tag,))
        if self.full_view_enabled:
            self._apply_full_view_column_widths()
        truncated = " truncated" if result["truncated"] else ""
        self.last_column_count = len(result["columns"])
        self.last_elapsed_ms = result["elapsed_ms"]
        self.last_count_info = f"Count {len(result['rows']):,} rows{truncated}"
        self.sql_info.set(
            f"{len(result['rows']):,} rows / {len(result['columns'])} columns / {result['elapsed_ms']:.1f} ms{truncated}"
        )
        self.status.set(f"SQL finished in {result['elapsed_ms']:.1f} ms")
        self._refresh_footer_details()

    def load_page(self):
        if not self.current_table:
            return
        limit = self._page_size()
        self.status.set(
            f"Loading {self.current_table} rows {self.offset + 1:,}-{self.offset + limit:,}..."
        )
        if self.pending_load_after is not None:
            self.after_cancel(self.pending_load_after)
        self.pending_load_after = self.after(140, self._run_load_page)

    def _run_load_page(self):
        self.pending_load_after = None
        if not self.current_table:
            return
        limit = self._page_size()
        self.worker.run(
            self._next_task_id(),
            self.db_path.get().strip(),
            "load_page",
            table=self.current_table,
            offset=self.offset,
            limit=limit,
            filters=list(self.active_filters),
            sort_column=self.sort_column,
            sort_direction=self.sort_direction,
        )

    def load_all_rows(self):
        if not self.current_table:
            return
        if not messagebox.askyesno(
            "Load all rows",
            "This may use a lot of memory for huge tables. Load all matching rows anyway?",
        ):
            return
        self.offset = 0
        self.status.set(f"Loading all rows from {self.current_table}...")
        if self.pending_load_after is not None:
            self.after_cancel(self.pending_load_after)
            self.pending_load_after = None
        self.worker.run(
            self._next_task_id(),
            self.db_path.get().strip(),
            "load_page",
            table=self.current_table,
            offset=0,
            limit=0,
            filters=list(self.active_filters),
            sort_column=self.sort_column,
            sort_direction=self.sort_direction,
            load_all=True,
        )

    def _show_page(self, result):
        self.current_columns = result["columns"]
        self.current_rows = result["rows"]
        self.current_row_keys = result.get("row_keys", [])
        column_names = [column["name"] for column in self.current_columns]

        self.column_type_by_name = {column["name"]: column["type"] for column in self.current_columns}
        values = column_names
        self.filter_combo.configure(values=values)
        if self.filter_column.get() not in values:
            self.filter_column.set(values[0] if values else "")
            self.on_filter_column_changed()

        self.tree.delete(*self.tree.get_children())
        self.tree["columns"] = column_names
        for column in column_names:
            heading_text = column
            if result["sort_column"] == column:
                heading_text = f"{column} {'v' if result['sort_direction'] == 'DESC' else '^'}"
            self.tree.heading(
                column,
                text=heading_text,
                command=lambda name=column: self.sort_by_column(name),
            )
            self.tree.column(column, minwidth=80, width=min(max(len(column) * 12, 100), 260), stretch=True)

        for index, row in enumerate(self.current_rows):
            tag = "even" if index % 2 == 0 else "odd"
            self.tree.insert("", END, values=[self._display_value(value) for value in row], tags=(tag,))

        if self.full_view_enabled:
            self._apply_full_view_column_widths()

        shown = len(self.current_rows)
        start = result["offset"] + 1 if shown else result["offset"]
        end = result["offset"] + shown
        filter_note = ""
        if result["filters"]:
            filter_note = f" with {len(result['filters'])} filter(s)"
        sort_note = ""
        if result["sort_column"]:
            sort_note = f" sorted by {result['sort_column']} {result['sort_direction']}"
        self.last_column_count = len(self.current_columns)
        self.last_elapsed_ms = result["elapsed_ms"]
        self.status.set(
            (
                f"{result['table']}: showing all {shown:,} loaded rows{filter_note}{sort_note}"
                if result.get("load_all")
                else f"{result['table']}: showing rows {start:,}-{end:,}{filter_note}{sort_note}"
            )
        )
        self._refresh_footer_details()

    def toggle_full_view(self):
        if not self.current_columns:
            return
        self.full_view_enabled = not self.full_view_enabled
        if self.full_view_enabled:
            self._apply_full_view_column_widths()
            self.full_view_button.configure(text="Reset view")
            self.status.set("Full view: columns fitted to visible values.")
        else:
            self._reset_column_widths()
            self.full_view_button.configure(text="Full view")
            self.status.set("View reset to compact column widths.")

    def _apply_full_view_column_widths(self):
        if not self.current_columns:
            return
        font = tkfont.nametofont("TkDefaultFont")
        for column_index, column in enumerate(self.current_columns):
            column_name = column["name"]
            heading = self.tree.heading(column_name, "text") or column_name
            longest_px = font.measure(str(heading))
            for row in self.current_rows:
                if column_index < len(row):
                    longest_px = max(longest_px, font.measure(self._display_value(row[column_index])))
            width = min(max(longest_px + 32, 80), 700)
            self.tree.column(column_name, width=width, minwidth=60, stretch=False)

    def _reset_column_widths(self):
        for column in self.current_columns:
            column_name = column["name"]
            width = min(max(len(column_name) * 12, 100), 260)
            self.tree.column(column_name, minwidth=80, width=width, stretch=True)

    def _display_value(self, value):
        if value is None:
            return "NULL"
        if isinstance(value, bytes):
            return f"<BLOB {len(value):,} bytes>"
        text = str(value)
        if len(text) > MAX_CELL_CHARS:
            return text[:MAX_CELL_CHARS] + "..."
        return text

    def _clear_grid(self):
        self.current_columns = []
        self.current_rows = []
        self.current_row_keys = []
        self.tree.delete(*self.tree.get_children())
        self.tree["columns"] = []
        self.last_column_count = 0
        self.last_elapsed_ms = None
        self._refresh_footer_details()

    def clean_ram(self):
        if not messagebox.askyesno(
            "Clean RAM",
            "To fully release memory back to Windows, the app needs to restart. Continue?",
        ):
            return
        if self.pending_load_after is not None:
            self.after_cancel(self.pending_load_after)
            self.pending_load_after = None
        self.current_columns = []
        self.current_rows = []
        self.current_row_keys = []
        self.tree.delete(*self.tree.get_children())
        self.tree["columns"] = []
        self.count_cache.clear()
        self.pending_cell_edit = None
        self.last_column_count = 0
        self.last_elapsed_ms = None
        self.last_count_info = "Count: n/a"
        collected = gc.collect()
        self.status.set(f"Cleaned RAM cache ({collected} objects collected). Restarting...")
        self._refresh_footer_details()
        db_path = self.db_path.get().strip()
        if db_path:
            os.environ["QUERYCRAFT_DB_PATH"] = db_path
        self.after(250, self._restart_app)

    def _restart_app(self):
        try:
            self.worker.executor.shutdown(wait=False, cancel_futures=True)
        except Exception:
            pass
        script = str(Path(__file__).resolve())
        os.execv(sys.executable, [sys.executable, script])

    def _path_size(self, path):
        try:
            return Path(path).stat().st_size
        except OSError:
            return None

    def _selected_table_bytes(self):
        if not self.current_table:
            return None
        return self.table_size_by_name.get(self.current_table)

    def _normalized_db_path(self):
        db_path = self.db_path.get().strip()
        if not db_path:
            return ""
        try:
            return str(Path(db_path).resolve())
        except OSError:
            return db_path

    def _filters_cache_key(self, filters=None):
        return tuple(
            (
                item.get("column", ""),
                item.get("operator", ""),
                item.get("value", ""),
                item.get("value2", ""),
                item.get("kind", ""),
            )
            for item in (filters if filters is not None else self.active_filters)
        )

    def _count_cache_key(self, table=None, filters=None):
        return (
            self._normalized_db_path(),
            table or self.current_table or "",
            self._filters_cache_key(filters),
        )

    def _store_count_cache(self, result):
        key = self._count_cache_key(result["table"], result.get("filters", []))
        self.count_cache[key] = {
            "count": result["count"],
            "elapsed_ms": result["elapsed_ms"],
        }

    def _restore_count_info_from_cache(self):
        cached = self.count_cache.get(self._count_cache_key())
        if cached:
            self.last_count_info = (
                f"Count {cached['count']:,} rows cached ({cached['elapsed_ms']:.1f} ms)"
            )
        else:
            self.last_count_info = "Count: n/a"

    def _clip_text(self, value, limit=34):
        text = str(value)
        if len(text) <= limit:
            return text
        return text[: max(0, limit - 3)] + "..."

    def _refresh_footer_details(self):
        db_path = self.db_path.get().strip()
        main_size = self._path_size(db_path) if db_path else None
        table_size = self._selected_table_bytes()
        table_name = self.current_table or "no table"
        table_size_prefix = "~" if self.table_size_is_estimated.get(table_name) else ""
        table_percent = (
            f" ({(table_size / main_size) * 100:.1f}%)"
            if table_size is not None and main_size
            else ""
        )
        count_text = self.last_count_info.replace("Count ", "").replace("Count: ", "")

        self.db_size_detail.set(readable_bytes(main_size))
        self.table_size_detail.set(
            f"{table_size_prefix}{readable_bytes(table_size)}{table_percent}"
        )
        self.column_count_detail.set(str(self.last_column_count))
        self.elapsed_detail.set(
            f"{self.last_elapsed_ms:.1f} ms" if self.last_elapsed_ms is not None else "n/a"
        )
        self.row_count_detail.set(self._clip_text(count_text or "n/a", 24))
        self._refresh_ram_meter()
        now = time.perf_counter()
        if now - self.last_cpu_refresh_time >= 5:
            self._refresh_cpu_meter()
            self.last_cpu_refresh_time = now

    def _refresh_ram_meter(self):
        try:
            if self.process and psutil:
                rss = self.process.memory_info().rss
                memory = psutil.virtual_memory()
                total = memory.total
                system_percent = memory.percent
                system_used = memory.used
            else:
                rss = windows_process_memory()
                total = windows_total_memory()
                status = MemoryStatusEx()
                status.dwLength = ctypes.sizeof(MemoryStatusEx)
                if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                    system_used = status.ullTotalPhys - status.ullAvailPhys
                    system_percent = (system_used / status.ullTotalPhys) * 100 if status.ullTotalPhys else 0
                else:
                    system_used = None
                    system_percent = 0
            if not rss or not total:
                self.ram_text.set("RAM n/a")
                self._draw_meter_bar(self.ram_canvas, 0, 0)
                return
            app_percent = (rss / total) * 100
        except Exception:
            self.ram_text.set("RAM n/a")
            self._draw_meter_bar(self.ram_canvas, 0, 0)
            return
        self.ram_text.set(f"RAM {system_percent:.1f}%/{app_percent:.1f}%")
        self._draw_meter_bar(self.ram_canvas, system_percent, app_percent)

    def _refresh_cpu_meter(self):
        try:
            if psutil:
                total_percent = psutil.cpu_percent(interval=None)
            else:
                current_system_times = windows_system_cpu_times()
                if current_system_times and self.last_system_cpu_times:
                    idle_delta = current_system_times[0] - self.last_system_cpu_times[0]
                    total_delta = current_system_times[1] - self.last_system_cpu_times[1]
                    total_percent = (
                        (1 - idle_delta / total_delta) * 100 if total_delta > 0 else 0
                    )
                else:
                    total_percent = 0
                self.last_system_cpu_times = current_system_times
        except Exception:
            self.cpu_text.set("CPU n/a")
            self._draw_meter_bar(self.cpu_canvas, 0, 0)
            return
        self.cpu_text.set(f"CPU {total_percent:.1f}%")
        self._draw_meter_bar(self.cpu_canvas, total_percent, 0)

    def _draw_meter_bar(self, canvas, system_percent, app_percent):
        canvas.delete("all")
        width = max(canvas.winfo_width(), int(canvas.cget("width")))
        height = int(canvas.cget("height"))
        canvas.create_rectangle(0, 0, width, height, fill="#e5e7eb", outline="#cbd5e1")
        system_width = int(width * min(max(system_percent, 0), 100) / 100)
        if system_width > 0:
            canvas.create_rectangle(0, 0, system_width, height, fill="#3b82f6", outline="")
        app_width = int(width * min(max(app_percent, 0), 100) / 100)
        if app_percent > 0:
            app_width = max(app_width, 2)
        if app_width > 0:
            canvas.create_rectangle(0, 0, min(app_width, width), height, fill="#22c55e", outline="")

    def _update_footer_details(self):
        self._refresh_footer_details()
        self.after(1000, self._update_footer_details)

    def reload_current_page(self):
        self.offset = max(0, self.offset)
        self.load_page()

    def first_page(self):
        self.offset = 0
        self.load_page()

    def prev_page(self):
        self.offset = max(0, self.offset - self._page_size())
        self.load_page()

    def next_page(self):
        self.offset += self._page_size()
        self.load_page()

    def apply_filter(self):
        self.offset = 0
        self.load_page()

    def clear_filter(self):
        self.active_filters = []
        self._reset_filter_builder()
        self._render_active_filters()
        self._restore_count_info_from_cache()
        self._refresh_footer_details()
        self.offset = 0
        self.load_page()

    def sort_by_column(self, column):
        if self.sort_column == column:
            self.sort_direction = "DESC" if self.sort_direction == "ASC" else "ASC"
        else:
            self.sort_column = column
            self.sort_direction = "ASC"
        self.offset = 0
        self.load_page()

    def count_rows(self):
        if not self.current_table:
            return
        cached = self.count_cache.get(self._count_cache_key())
        if cached:
            self._restore_count_info_from_cache()
            self.status.set(f"{self.current_table}: {self.last_count_info}")
            self._refresh_footer_details()
            return
        self.status.set(f"Counting {self.current_table} rows...")
        self.worker.run(
            self.task_id + 1,
            self.db_path.get().strip(),
            "count_rows",
            table=self.current_table,
            filters=list(self.active_filters),
        )

    def export_current_page(self):
        if not self.current_columns:
            messagebox.showwarning("No page", "Load a table page before exporting.")
            return
        path = filedialog.asksaveasfilename(
            title="Export current page",
            defaultextension=".csv",
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")],
        )
        if not path:
            return
        column_names = [column["name"] for column in self.current_columns]
        with open(path, "w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.writer(handle)
            writer.writerow(column_names)
            writer.writerows(self.current_rows)
        self.status.set(f"Exported {len(self.current_rows):,} rows to {path}")

    def export_current_parquet(self):
        if not self.current_columns:
            messagebox.showwarning("No page", "Load a table page before exporting.")
            return
        if pd is None:
            messagebox.showerror(
                "Parquet unavailable",
                "Parquet export needs pandas and pyarrow installed in this Python environment.",
            )
            return
        path = filedialog.asksaveasfilename(
            title="Export current view as Parquet",
            defaultextension=".parquet",
            filetypes=[("Parquet files", "*.parquet"), ("All files", "*.*")],
        )
        if not path:
            return
        column_names = [column["name"] for column in self.current_columns]
        try:
            frame = pd.DataFrame(self.current_rows, columns=column_names)
            frame.to_parquet(path, index=False)
        except Exception as exc:
            messagebox.showerror("Parquet export failed", str(exc))
            return
        self.status.set(f"Exported {len(self.current_rows):,} rows to {path}")


if __name__ == "__main__":
    app = SQLiteLazyViewer()
    app.mainloop()
