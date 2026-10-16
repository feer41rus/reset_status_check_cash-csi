#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Приложение для работы с документами касс:
  1. Сброс статуса отправки в ch_* и loy_* таблицах.
  2. Массовая замена shopindex в ch_shift.
GUI для Linux (Tkinter) + сохранение настроек в JSON + отчёты в logs/.

Важно про потоки: Tkinter НЕ потокобезопасен. Любое обращение к виджетам
из фонового потока может уронить процесс (segfault в libtk8.6.so).
Поэтому фоновые потоки работают только с БД и файлами, а в GUI пишут
исключительно через очередь (UiBridge), которую разбирает главный поток.
"""

import copy
import csv
import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import traceback
import tkinter as tk
from dataclasses import dataclass
from datetime import datetime, timedelta
from tkinter import ttk, messagebox
from tkinter import font as tkfont

try:
    import psycopg2
    from psycopg2 import sql
except ImportError:
    raise SystemExit("Установите psycopg2: pip install psycopg2-binary")

try:
    from tkcalendar import DateEntry
    HAS_CALENDAR = True
except ImportError:
    HAS_CALENDAR = False


# ============================================================
# ПУТИ И КОНСТАНТЫ
# ============================================================

APP_ID = "reset_status_app"
APP_TITLE = "Работа с документами касс"
APP_VERSION = "1.0.0"
APP_AUTHOR = "Счастливый Владислав"
APP_CREDITS = "Разработано с помощью Claude Code"

# Собранное приложение (PyInstaller / AppImage) лежит на read-only
# файловой системе, поэтому логи пишем в ~/.local/share. При запуске
# из исходников — как раньше, в logs/ рядом с main.py.
IS_FROZEN = bool(getattr(sys, "frozen", False) or os.environ.get("APPIMAGE"))
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# PyInstaller распаковывает данные (assets/) в sys._MEIPASS
ASSETS_DIR = os.path.join(getattr(sys, "_MEIPASS", BASE_DIR), "assets")

_xdg_config = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
_xdg_data = os.environ.get("XDG_DATA_HOME") or os.path.join(os.path.expanduser("~"), ".local", "share")

SETTINGS_DIR = os.path.join(_xdg_config, APP_ID)
SETTINGS_FILE = os.path.join(SETTINGS_DIR, "settings.json")
LOGS_DIR = os.environ.get("RESET_STATUS_LOGS_DIR") or (
    os.path.join(_xdg_data, APP_ID, "logs") if IS_FROZEN else os.path.join(BASE_DIR, "logs")
)

# Сколько строк на таблицу показывать в окне. В файл отчёта пишутся все.
GUI_ROWS_LIMIT = 300
# Сколько ждать блокировку строки (мс), если её держит кассовый сервис.
LOCK_TIMEOUT_MS = 30_000
CONNECT_TIMEOUT_S = 10

TABLES_CASH = [
    ("ch_introduction", "Внесение денег"),
    ("ch_purchase",     "Чеки (заголовки чеков)"),
    ("ch_reportshift",  "Z/X-отчёт"),
    ("ch_withdrawal",   "Изъятия наличных"),
]

TABLES_DISCOUNT = [
    ("loy_transaction", "Транзакции/Проводки/Продажи"),
]

TABLE_DESC = dict(TABLES_CASH + TABLES_DISCOUNT)

STATUSES = [
    (0, "NO_SENT — не отправлялся"),
    (1, "WAIT_ACKNOWLEDGEMENT — отправляется"),
    (2, "SENT — отправлено"),
    (3, "SENT_ERROR — ошибка отправки"),
    (4, "REQUESTED — запрошена переотправка"),
    (5, "UNCOMMITED — отправка завершилась ошибкой"),
    (6, "PENDING — ожидает серверного расчёта"),
]


# ============================================================
# КОНФИГУРАЦИЯ ПОЛЕЙ ПО ТАБЛИЦАМ
# ============================================================

REPORT_FIELDS = {
    "ch_introduction": ["id", "datecreate", "numberfield"],
    "ch_purchase":     ["id", "datecreate", "fiscaldocnum", "numberfield"],
    "ch_reportshift":  ["id", "datecreate", "fiscaldocnum", "numberfield"],
    "ch_withdrawal":   ["id", "datecreate", "numberfield"],
    "loy_transaction": ["id", "transaction_time", "purchase_number",
                        "shift_number", "shop_number"],
}

DATE_FIELD = {
    "ch_introduction": "datecreate",
    "ch_purchase":     "datecreate",
    "ch_reportshift":  "datecreate",
    "ch_withdrawal":   "datecreate",
    "loy_transaction": "transaction_time",
}

STATUS_FIELD = {
    "ch_introduction": "senttoserverstatus",
    "ch_purchase":     "senttoserverstatus",
    "ch_reportshift":  "senttoserverstatus",
    "ch_withdrawal":   "senttoserverstatus",
    "loy_transaction": "sent_to_server_status",
}

STATUS_TYPE = {
    "ch_introduction": "text",
    "ch_purchase":     "text",
    "ch_reportshift":  "text",
    "ch_withdrawal":   "text",
    "loy_transaction": "int",
}

SHIFT_REPORT_FIELDS = ["id", "shiftcreate", "shopindex", "cashnum", "numshift"]
SHIFT_EXPORT_FIELDS = ["id", "cashnum", "fiscalnum", "numshift", "shopindex",
                       "shiftopen", "shiftclose", "shiftcreate", "inn", "kpp"]


# ============================================================
# НАСТРОЙКИ (JSON)
# ============================================================

def default_settings():
    today = datetime.now()
    yesterday = today - timedelta(days=1)
    return {
        "host": "127.0.0.1",
        "port": "5432",
        "user": "postgres",
        "password": "",
        "db_cash": "cash",
        "db_discount": "discount",
        # --- вкладка "Сброс статусов" ---
        "date_start": yesterday.strftime("%Y-%m-%d"),
        "time_start": "00:00:00",
        "date_end": today.strftime("%Y-%m-%d"),
        "time_end": "00:00:00",
        "status_index": 0,
        "tables": {name: True for name in TABLE_DESC},
        # --- вкладка "Замена shopindex" ---
        "si_date_start": yesterday.strftime("%Y-%m-%d"),
        "si_time_start": "00:00:00",
        "si_date_end": today.strftime("%Y-%m-%d"),
        "si_time_end": "00:00:00",
        "si_old_shopindex": "",
        "si_new_shopindex": "",
        "si_only_changed": True,
    }


def load_settings():
    result = default_settings()
    if not os.path.exists(SETTINGS_FILE):
        return result
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("ожидался JSON-объект")
        tables = data.pop("tables", None)
        result.update(data)
        if isinstance(tables, dict):
            result["tables"].update(tables)
    except Exception as e:
        print(f"[settings] Ошибка чтения JSON: {e}", file=sys.stderr)
    return result


def save_settings(settings):
    """Атомарная запись с правами 600 (в файле лежит пароль)."""
    try:
        os.makedirs(SETTINGS_DIR, mode=0o700, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=SETTINGS_DIR, prefix=".settings.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(settings, f, ensure_ascii=False, indent=2)
            os.chmod(tmp, 0o600)
            os.replace(tmp, SETTINGS_FILE)
        except Exception:
            os.unlink(tmp)
            raise
    except Exception as e:
        print(f"[settings] Ошибка сохранения JSON: {e}", file=sys.stderr)


def ensure_logs_dir():
    os.makedirs(LOGS_DIR, exist_ok=True)
    return LOGS_DIR


def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def parse_time(value):
    value = value.strip() or "00:00:00"
    for fmt in ("%H:%M:%S", "%H:%M"):
        try:
            return datetime.strptime(value, fmt).time()
        except ValueError:
            pass
    raise ValueError(f"Неверное время «{value}», ожидается ЧЧ:ММ:СС")


# ============================================================
# РАБОТА С БД (без GUI — вызывается из фонового потока)
# ============================================================

@dataclass(frozen=True)
class ConnParams:
    host: str
    port: int
    user: str
    password: str

    def connect(self, dbname):
        conn = psycopg2.connect(
            host=self.host,
            port=self.port,
            user=self.user,
            password=self.password,
            dbname=dbname,
            connect_timeout=CONNECT_TIMEOUT_S,
            application_name=APP_ID,
            options=f"-c lock_timeout={LOCK_TIMEOUT_MS}",
        )
        conn.autocommit = False
        return conn


class Report:
    """Пишет полный отчёт в файл и (через emit) — в окно приложения."""

    def __init__(self, path, emit):
        self.path = path
        self.emit = emit
        self._file = open(path, "w", encoding="utf-8") if path else None

    def write(self, text="", gui=True):
        if self._file:
            self._file.write(text + "\n")
        if gui:
            self.emit(text)

    def rows(self, lines):
        """Строки данных: все в файл, в окно — не больше GUI_ROWS_LIMIT."""
        for i, line in enumerate(lines):
            self.write(line, gui=i < GUI_ROWS_LIMIT)
        if len(lines) > GUI_ROWS_LIMIT:
            self.emit(f"  … ещё {len(lines) - GUI_ROWS_LIMIT} строк — см. файл отчёта")

    def close(self):
        if self._file:
            self._file.close()
            self._file = None


def _period_where(date_col):
    return sql.SQL("{c} >= %s AND {c} < %s").format(c=sql.Identifier(date_col))


def select_status_rows(cur, table, start_dt, end_dt, lock):
    """Возвращает (colnames, rows) с полями отчёта + текущим статусом."""
    fields = REPORT_FIELDS[table] + [STATUS_FIELD[table]]
    query = sql.SQL("SELECT {fields} FROM public.{tbl} WHERE {where} ORDER BY {date}{lock}").format(
        fields=sql.SQL(", ").join(map(sql.Identifier, fields)),
        tbl=sql.Identifier(table),
        where=_period_where(DATE_FIELD[table]),
        date=sql.Identifier(DATE_FIELD[table]),
        lock=sql.SQL(" FOR UPDATE") if lock else sql.SQL(""),
    )
    cur.execute(query, (start_dt, end_dt))
    return [d[0] for d in cur.description], cur.fetchall()


def update_status_by_ids(cur, table, ids, status):
    status_val = str(status) if STATUS_TYPE[table] == "text" else status
    cur.execute(
        sql.SQL("UPDATE public.{tbl} SET {col} = %s WHERE id = ANY(%s)").format(
            tbl=sql.Identifier(table), col=sql.Identifier(STATUS_FIELD[table])),
        (status_val, ids),
    )
    return cur.rowcount


def count_statuses(cur, table, start_dt, end_dt):
    """{текущий статус: количество} за период."""
    col = sql.Identifier(STATUS_FIELD[table])
    cur.execute(
        sql.SQL("SELECT {col}::text, COUNT(*) FROM public.{tbl} WHERE {where} "
                "GROUP BY 1 ORDER BY 1").format(
            col=col, tbl=sql.Identifier(table), where=_period_where(DATE_FIELD[table])),
        (start_dt, end_dt),
    )
    return dict(cur.fetchall())


def run_status_reset(conn_params, plan, start_dt, end_dt, status, report):
    """
    plan: [(dbname, [table, ...]), ...]

    Строки выбираются с FOR UPDATE и обновляются строго по их id, поэтому
    в отчёт попадают ровно те строки, что были изменены. COMMIT делается
    только после того, как успешно отработали ВСЕ базы — ошибка в discount
    больше не оставляет cash наполовину изменённой.
    """
    summary = []
    conns = []
    committed = []

    def rollback_rest():
        for dbname, conn in conns:
            if dbname in committed:
                continue
            try:
                conn.rollback()
                report.write(f"  ❌ Ошибка — выполнен ROLLBACK для {dbname}.")
            except Exception:
                pass
        if committed:
            report.write(f"  ⚠ Уже зафиксированы (COMMIT): {', '.join(committed)}")
        report.write()

    try:
        for dbname, tables in plan:
            report.write(f"### БАЗА ДАННЫХ: {dbname} ###")
            report.write()
            conn = conn_params.connect(dbname)
            conns.append((dbname, conn))
            with conn.cursor() as cur:
                for table in tables:
                    report.write(f"--- Таблица: {table} ({TABLE_DESC[table]}) ---")
                    colnames, rows = select_status_rows(cur, table, start_dt, end_dt, lock=True)
                    if not rows:
                        report.write("  (нет строк за указанный период)")
                        report.write()
                        summary.append((dbname, table, 0))
                        continue

                    status_col = STATUS_FIELD[table]
                    lines, ids = [], []
                    for row in rows:
                        row_dict = dict(zip(colnames, row))
                        old_status = row_dict.pop(status_col)
                        ids.append(row_dict["id"])
                        details = ", ".join(f"{k}={v}" for k, v in row_dict.items())
                        lines.append(f"  {details}  |  статус: {old_status} -> {status}")
                    report.rows(lines)

                    updated = update_status_by_ids(cur, table, ids, status)
                    report.write(f"  → Обновлено строк: {updated}")
                    report.write()
                    summary.append((dbname, table, updated))

        for dbname, conn in conns:
            conn.commit()
            committed.append(dbname)
            report.write(f"  ✅ COMMIT для {dbname} выполнен.")
        report.write()
    except Exception:
        rollback_rest()
        raise
    finally:
        for _, conn in conns:
            conn.close()

    return summary


def _shift_where(start_dt, end_dt, old_val, new_val, only_changed):
    parts = [_period_where("shiftcreate")]
    params = [start_dt, end_dt]
    if old_val is not None:
        parts.append(sql.SQL('"shopindex" = %s'))
        params.append(old_val)
    if only_changed:
        parts.append(sql.SQL('"shopindex" IS DISTINCT FROM %s'))
        params.append(new_val)
    return sql.SQL(" AND ").join(parts), params


def select_shifts(cur, fields, where_args, lock=False):
    where, params = _shift_where(*where_args)
    cur.execute(
        sql.SQL('SELECT {f} FROM public."ch_shift" WHERE {w} ORDER BY "shiftcreate"{lock}').format(
            f=sql.SQL(", ").join(map(sql.Identifier, fields)), w=where,
            lock=sql.SQL(" FOR UPDATE") if lock else sql.SQL("")),
        params,
    )
    return [d[0] for d in cur.description], cur.fetchall()


def count_shifts(cur, where_args):
    where, params = _shift_where(*where_args)
    cur.execute(sql.SQL('SELECT COUNT(*) FROM public."ch_shift" WHERE {w}').format(w=where), params)
    return cur.fetchone()[0]


def run_shopindex_update(conn_params, dbname, where_args, report):
    new_val = where_args[3]
    conn = conn_params.connect(dbname)
    try:
        with conn.cursor() as cur:
            _, rows = select_shifts(cur, SHIFT_REPORT_FIELDS, where_args, lock=True)
            if not rows:
                report.write("(нет строк, попадающих под условия)")
                report.write()
                updated = 0
            else:
                report.rows([
                    f"  id={rid}, shiftcreate={sc}, cashnum={cn}, numshift={ns}  "
                    f"|  shopindex: {si} -> {new_val}"
                    for rid, sc, si, cn, ns in rows
                ])
                cur.execute('UPDATE public."ch_shift" SET "shopindex" = %s WHERE id = ANY(%s)',
                            (new_val, [r[0] for r in rows]))
                updated = cur.rowcount
                report.write(f"  → Обновлено строк: {updated}")
                report.write()
        conn.commit()
        report.write("  ✅ COMMIT выполнен.")
        report.write()
        return updated
    except Exception:
        conn.rollback()
        report.write("  ❌ Ошибка — выполнен ROLLBACK.")
        report.write()
        raise
    finally:
        conn.close()


def check_databases(conn_params, databases, emit):
    """databases: [(label, dbname, [ожидаемые таблицы])]"""
    for label, dbname, expected in databases:
        emit(f"\n📦 Проверка БД {label}: {dbname}")
        try:
            conn = conn_params.connect(dbname)
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT version();")
                    emit("   ✔ Подключение OK")
                    emit(f"   🐘 {cur.fetchone()[0].split(',')[0]}")
                    cur.execute("""
                        SELECT table_name
                        FROM information_schema.tables
                        WHERE table_schema = 'public'
                          AND table_name = ANY(%s)
                        ORDER BY table_name
                    """, (expected,))
                    found = [r[0] for r in cur.fetchall()]
                    emit(f"   📋 Найдено таблиц: {len(found)}/{len(expected)}")
                    for tbl in found:
                        emit(f"      • {tbl}")
                    missing = set(expected) - set(found)
                    if missing:
                        emit(f"   ⚠ Отсутствуют: {', '.join(sorted(missing))}")
            finally:
                conn.rollback()
                conn.close()
        except Exception as e:
            emit(f"   ❌ Ошибка: {e}")
    emit("\n🏁 Проверка завершена.")


# ============================================================
# Оформление
# ============================================================

COLORS = {
    "bg":         "#F3F4F8",
    "surface":    "#FFFFFF",
    "border":     "#E3E6EE",
    "text":       "#1F2430",
    "muted":      "#6B7280",
    "accent":     "#4F46E5",
    "accent_hi":  "#4338CA",
    "accent_lo":  "#EEF2FF",
    "danger":     "#DC2626",
    "danger_hi":  "#B91C1C",
    "disabled":   "#A5A9B8",
    "console_bg": "#0F172A",
    "console_fg": "#E2E8F0",
    "ok":         "#4ADE80",
    "err":        "#F87171",
    "warn":       "#FBBF24",
    "head":       "#7DD3FC",
    "dim":        "#64748B",
}


def _pick_font(candidates, fallback):
    available = set(tkfont.families())
    return next((f for f in candidates if f in available), fallback)


def apply_theme(root):
    """Светлая «плоская» тема поверх clam. Возвращает словарь шрифтов."""
    c = COLORS
    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass

    family = _pick_font(("Inter", "Cantarell", "Ubuntu", "Noto Sans", "DejaVu Sans"), "TkDefaultFont")
    mono = _pick_font(("JetBrains Mono", "Ubuntu Mono", "DejaVu Sans Mono", "Noto Sans Mono"), "TkFixedFont")
    for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont"):
        tkfont.nametofont(name).configure(family=family, size=10)
    fonts = {
        "base": (family, 10),
        "bold": (family, 10, "bold"),
        "title": (family, 15, "bold"),
        "big": (family, 17, "bold"),
        "small": (family, 9),
        "mono": (mono, 9),
    }

    root.configure(background=c["bg"])
    root.option_add("*TCombobox*Listbox.background", c["surface"])
    root.option_add("*TCombobox*Listbox.foreground", c["text"])
    root.option_add("*TCombobox*Listbox.selectBackground", c["accent"])
    root.option_add("*TCombobox*Listbox.selectForeground", "#FFFFFF")
    root.option_add("*TCombobox*Listbox.font", fonts["base"])

    style.configure(".", background=c["bg"], foreground=c["text"], font=fonts["base"],
                    bordercolor=c["border"], lightcolor=c["border"], darkcolor=c["border"],
                    troughcolor=c["bg"], focuscolor=c["accent"], selectbackground=c["accent"],
                    selectforeground="#FFFFFF", insertcolor=c["text"])
    style.configure("TFrame", background=c["bg"])
    style.configure("Surface.TFrame", background=c["surface"])
    style.configure("Header.TFrame", background=c["surface"])
    style.configure("TLabel", background=c["bg"])
    style.configure("Surface.TLabel", background=c["surface"])
    style.configure("Muted.TLabel", background=c["surface"], foreground=c["muted"], font=fonts["small"])
    style.configure("Title.TLabel", background=c["surface"], font=fonts["title"])
    style.configure("Status.TLabel", background=c["bg"], foreground=c["muted"], font=fonts["small"],
                    padding=(12, 4))

    # Карточки
    style.configure("TLabelframe", background=c["surface"], bordercolor=c["border"],
                    relief="solid", borderwidth=1, padding=(10, 6, 10, 8))
    style.configure("TLabelframe.Label", background=c["surface"], foreground=c["muted"], font=fonts["bold"])

    # Кнопки
    style.configure("TButton", background=c["surface"], foreground=c["text"], padding=(12, 6),
                    relief="flat", borderwidth=1, bordercolor=c["border"], focusthickness=0)
    style.map("TButton",
              background=[("disabled", c["bg"]), ("pressed", c["border"]), ("active", c["accent_lo"])],
              foreground=[("disabled", c["disabled"])],
              bordercolor=[("active", c["accent"])])
    for name, base, hi in (("Accent", c["accent"], c["accent_hi"]), ("Danger", c["danger"], c["danger_hi"])):
        style.configure(f"{name}.TButton", background=base, foreground="#FFFFFF", bordercolor=base,
                        font=fonts["bold"], padding=(16, 7))
        style.map(f"{name}.TButton",
                  background=[("disabled", c["border"]), ("pressed", hi), ("active", hi)],
                  foreground=[("disabled", c["disabled"])],
                  bordercolor=[("disabled", c["border"]), ("active", hi)])
    style.configure("Soft.TButton", background=c["accent_lo"], foreground=c["accent"],
                    bordercolor=c["accent_lo"], font=fonts["bold"], padding=(14, 7))
    style.map("Soft.TButton",
              background=[("disabled", c["bg"]), ("pressed", "#C7D2FE"), ("active", "#E0E7FF")],
              foreground=[("disabled", c["disabled"])],
              bordercolor=[("disabled", c["bg"]), ("active", "#E0E7FF")])
    style.configure("Ghost.TButton", background=c["bg"], foreground=c["accent"], bordercolor=c["bg"],
                    padding=(10, 4))
    style.map("Ghost.TButton", background=[("active", c["accent_lo"])], bordercolor=[("active", c["accent_lo"])])
    style.configure("Link.TButton", background=c["surface"], foreground=c["accent"], bordercolor=c["surface"],
                    padding=(10, 6))
    style.map("Link.TButton", background=[("active", c["accent_lo"])], bordercolor=[("active", c["accent_lo"])])

    # Поля ввода
    field = dict(fieldbackground=c["surface"], background=c["surface"], foreground=c["text"],
                 bordercolor=c["border"], lightcolor=c["surface"], darkcolor=c["surface"], padding=(6, 4))
    for name in ("TEntry", "TCombobox", "DateEntry"):
        style.configure(name, **field)
        style.map(name, bordercolor=[("focus", c["accent"])], lightcolor=[("focus", c["accent"])],
                  fieldbackground=[("readonly", c["surface"])],
                  foreground=[("disabled", c["disabled"])])
    style.configure("TCombobox", arrowcolor=c["muted"])
    style.configure("DateEntry", arrowcolor=c["muted"])

    style.configure("TCheckbutton", background=c["surface"], foreground=c["text"], padding=(0, 2))
    style.map("TCheckbutton", background=[("active", c["surface"])])
    cb_off, cb_on = load_image("checkbox_off.png"), load_image("checkbox_on.png")
    if cb_off and cb_on:
        root._theme_images = (cb_off, cb_on)  # держим ссылки, иначе Tk удалит картинки
        style.element_create("Modern.Checkbutton.indicator", "image", cb_off,
                             ("selected", cb_on), sticky="w")
        style.layout("TCheckbutton", [("Checkbutton.padding", {"sticky": "nswe", "children": [
            ("Modern.Checkbutton.indicator", {"side": "left", "sticky": ""}),
            ("Checkbutton.focus", {"side": "left", "sticky": "w", "children": [
                ("Checkbutton.label", {"sticky": "nswe"})]})]})])

    # Вкладки
    style.configure("TNotebook", background=c["bg"], borderwidth=0, tabmargins=(0, 4, 0, 0))
    style.configure("TNotebook.Tab", background=c["bg"], foreground=c["muted"], padding=(16, 7),
                    borderwidth=0, font=fonts["bold"])
    style.map("TNotebook.Tab",
              background=[("selected", c["surface"]), ("active", c["accent_lo"])],
              foreground=[("selected", c["accent"])],
              lightcolor=[("selected", c["accent"])])

    style.configure("Vertical.TScrollbar", background="#1E293B", troughcolor=c["console_bg"],
                    bordercolor=c["console_bg"], arrowcolor=c["dim"], lightcolor="#1E293B",
                    darkcolor="#1E293B", gripcount=0)
    style.map("Vertical.TScrollbar", background=[("active", "#334155")])
    style.configure("TSeparator", background=c["border"])
    return fonts


def _short_path(path):
    home = os.path.expanduser("~")
    return "~" + path[len(home):] if path.startswith(home + os.sep) else path


def load_image(name):
    path = os.path.join(ASSETS_DIR, name)
    try:
        return tk.PhotoImage(file=path)
    except (tk.TclError, OSError):
        return None


# ============================================================
# Мост поток → GUI
# ============================================================

class UiBridge:
    """Фоновые потоки кладут сюда задания, главный поток их выполняет."""

    POLL_MS = 50

    def __init__(self, root, log_sink):
        self.root = root
        self.log_sink = log_sink
        self.q = queue.Queue()
        self.root.after(self.POLL_MS, self._poll)

    def log(self, text):
        self.q.put(("log", text))

    def call(self, fn, *args):
        self.q.put(("call", (fn, args)))

    def _poll(self):
        lines = []
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "log":
                    lines.append(payload)
                    continue
                if lines:
                    self.log_sink(lines)
                    lines = []
                fn, args = payload
                try:
                    fn(*args)
                except Exception:
                    traceback.print_exc()
        except queue.Empty:
            pass
        if lines:
            self.log_sink(lines)
        self.root.after(self.POLL_MS, self._poll)


# ============================================================
# GUI
# ============================================================

class ResetStatusApp:
    def __init__(self, root):
        self.root = root
        self.root.title(f"{APP_TITLE} — v{APP_VERSION}")
        height = max(780, min(980, self.root.winfo_screenheight() - 90))
        self.root.geometry(f"940x{height}")
        self.root.minsize(860, 760)

        self.fonts = apply_theme(root)
        self.images = {size: load_image(f"logo_{size}.png") for size in (32, 48, 64, 96, 256)}
        icons = [img for size, img in self.images.items() if img and size in (32, 64, 256)]
        if icons:
            self.root.iconphoto(True, *icons)

        self.settings = load_settings()
        ensure_logs_dir()
        self.busy = False

        self._build_ui()
        self.ui = UiBridge(root, self._append_log)
        self._apply_settings()

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # --------------------------------------------------------
    # Общий каркас окна
    # --------------------------------------------------------
    def _build_header(self):
        header = ttk.Frame(self.root, style="Header.TFrame", padding=(16, 10))
        header.pack(fill="x")
        if self.images.get(48):
            ttk.Label(header, image=self.images[48], style="Surface.TLabel").pack(side="left", padx=(0, 12))
        titles = ttk.Frame(header, style="Header.TFrame")
        titles.pack(side="left")
        ttk.Label(titles, text=APP_TITLE, style="Title.TLabel").pack(anchor="w")
        ttk.Label(titles, text="Сброс статусов отправки · замена shopindex · PostgreSQL",
                  style="Muted.TLabel").pack(anchor="w")
        ttk.Button(header, text="ⓘ  О приложении", style="Link.TButton",
                   command=self._show_about).pack(side="right")
        ttk.Separator(self.root).pack(fill="x")

    def _build_ui(self):
        pad = {"padx": 14, "pady": 6}
        self._build_header()

        # Статус-бар пакуем первым, чтобы он не пропадал при сжатии окна
        self.status_bar = ttk.Label(
            self.root, style="Status.TLabel", anchor="w",
            text=f"v{APP_VERSION}   ·   Настройки: {_short_path(SETTINGS_FILE)}   ·   Логи: {_short_path(LOGS_DIR)}",
        )
        self.status_bar.pack(fill="x", side="bottom")

        # ---------- Общий блок подключения (для обеих вкладок) ----------
        conn_frame = ttk.LabelFrame(self.root, text="Подключение к PostgreSQL")
        conn_frame.pack(fill="x", padx=14, pady=(12, 6))
        conn_frame.columnconfigure(1, weight=1)
        conn_frame.columnconfigure(3, weight=1)

        def entry(row, col, label, width, **kw):
            ttk.Label(conn_frame, text=label, style="Surface.TLabel").grid(
                row=row, column=col, sticky="w", padx=(6, 8), pady=4)
            e = ttk.Entry(conn_frame, width=width, **kw)
            e.grid(row=row, column=col + 1, sticky="we", padx=(0, 16), pady=4)
            return e

        self.ent_host = entry(0, 0, "IP / Host", 28)
        self.ent_port = entry(0, 2, "Порт", 8)
        self.ent_user = entry(1, 0, "Пользователь", 28)
        self.ent_pass = entry(1, 2, "Пароль", 18, show="•")
        self.ent_db_cash = entry(2, 0, "БД cash", 18)
        self.ent_db_discount = entry(2, 2, "БД discount", 18)

        self.var_show_pass = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            conn_frame, text="Показать пароль",
            variable=self.var_show_pass, command=self._toggle_password
        ).grid(row=1, column=4, sticky="w")

        # ---------- Общие кнопки ----------
        btn_frame = ttk.Frame(self.root)
        btn_frame.pack(fill="x", **pad)

        self.btn_test = ttk.Button(btn_frame, text="🔌  Проверить связь", command=self._test_connection)
        self.btn_test.pack(side="left")

        self.btn_save = ttk.Button(btn_frame, text="💾  Сохранить настройки", command=self._manual_save)
        self.btn_save.pack(side="left", padx=8)

        self.btn_open_logs = ttk.Button(btn_frame, text="📂  Папка отчётов", command=self._open_logs_dir)
        self.btn_open_logs.pack(side="left")

        # ---------- Вкладки ----------
        self.nb = ttk.Notebook(self.root)
        self.nb.pack(fill="x", padx=14, pady=(4, 6))

        self.tab_status = ttk.Frame(self.nb, style="Surface.TFrame", padding=(6, 8))
        self.tab_shopindex = ttk.Frame(self.nb, style="Surface.TFrame", padding=(6, 8))
        self.nb.add(self.tab_status, text="📄  Сброс статусов")
        self.nb.add(self.tab_shopindex, text="🏪  Замена shopindex")

        self._build_tab_status(self.tab_status)
        self._build_tab_shopindex(self.tab_shopindex)

        # ---------- Общий лог (тёмная консоль) ----------
        log_head = ttk.Frame(self.root)
        log_head.pack(fill="x", padx=14, pady=(6, 0))
        ttk.Label(log_head, text="Лог выполнения", font=self.fonts["bold"],
                  foreground=COLORS["muted"]).pack(side="left")
        self.btn_clear = ttk.Button(log_head, text="Очистить", style="Ghost.TButton", command=self._clear_log)
        self.btn_clear.pack(side="right")

        log_frame = tk.Frame(self.root, background=COLORS["console_bg"])
        log_frame.pack(fill="both", expand=True, padx=14, pady=(4, 8))
        self.log = tk.Text(
            log_frame, wrap="word", height=10, state="disabled", relief="flat", borderwidth=0,
            background=COLORS["console_bg"], foreground=COLORS["console_fg"],
            insertbackground=COLORS["console_fg"], selectbackground=COLORS["accent"],
            font=self.fonts["mono"], padx=12, pady=10, highlightthickness=0,
        )
        scroll = ttk.Scrollbar(log_frame, orient="vertical", command=self.log.yview)
        self.log.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.log.pack(side="left", fill="both", expand=True)
        for tag, color in (("ok", "ok"), ("err", "err"), ("warn", "warn"), ("head", "head"), ("dim", "dim")):
            self.log.tag_configure(tag, foreground=COLORS[color])
        self.log.tag_configure("head", font=self.fonts["mono"] + ("bold",))

        # Кнопки, которые блокируются, пока идёт операция с БД
        self.action_buttons = [
            self.btn_test, self.btn_run_status, self.btn_status_check,
            self.btn_si_check, self.btn_si_run, self.btn_si_export,
        ]

    def _build_period(self, parent, title):
        date_frame = ttk.LabelFrame(parent, text=title)
        date_frame.pack(fill="x", padx=8, pady=4)
        widgets = []
        for row, label in enumerate(("Начало (включительно)", "Конец (не включительно)")):
            ttk.Label(date_frame, text=label, style="Surface.TLabel").grid(
                row=row, column=0, sticky="w", padx=(6, 12), pady=4)
            if HAS_CALENDAR:
                c = COLORS
                dt = DateEntry(
                    date_frame, width=12, date_pattern="yyyy-mm-dd", locale="ru_RU",
                    firstweekday="monday", showweeknumbers=False,
                    background=c["accent"], foreground="#FFFFFF", headersbackground=c["accent_lo"],
                    headersforeground=c["accent"], selectbackground=c["accent"],
                    normalbackground=c["surface"], weekendbackground=c["surface"],
                    othermonthbackground=c["bg"], othermonthwebackground=c["bg"],
                    bordercolor=c["border"], font=self.fonts["base"],
                )
            else:
                dt = ttk.Entry(date_frame, width=14)
            dt.grid(row=row, column=1, sticky="w", pady=4)
            ttk.Label(date_frame, text="время", style="Surface.TLabel", foreground=COLORS["muted"]).grid(
                row=row, column=2, sticky="w", padx=(16, 8), pady=4)
            tm = ttk.Entry(date_frame, width=10)
            tm.grid(row=row, column=3, sticky="w", pady=4)
            widgets += [dt, tm]
        return widgets

    # --------------------------------------------------------
    # Вкладка 1: Сброс статусов
    # --------------------------------------------------------
    def _build_tab_status(self, parent):
        pad = {"padx": 8, "pady": 4}

        (self.st_dt_start, self.st_time_start,
         self.st_dt_end, self.st_time_end) = self._build_period(
            parent, "Период (datecreate / transaction_time)")

        # Статус
        status_frame = ttk.LabelFrame(parent, text="Новый статус")
        status_frame.pack(fill="x", **pad)

        self.cmb_status = ttk.Combobox(
            status_frame,
            values=[f"{code} — {desc}" for code, desc in STATUSES],
            state="readonly",
            width=56,
        )
        self.cmb_status.grid(row=0, column=0, sticky="w", padx=6, pady=4)

        # Таблицы
        tables_frame = ttk.LabelFrame(parent, text="Таблицы для обработки")
        tables_frame.pack(fill="x", **pad)

        self.table_vars = {}
        for i, (name, _) in enumerate(TABLES_CASH):
            var = tk.BooleanVar(value=True)
            ttk.Checkbutton(tables_frame, text=name, variable=var).grid(
                row=i // 2, column=i % 2, sticky="w", padx=6)
            self.table_vars[name] = var
        for name, _ in TABLES_DISCOUNT:
            var = tk.BooleanVar(value=True)
            ttk.Checkbutton(tables_frame, text=f"{name} (БД discount)", variable=var).grid(
                row=2, column=0, columnspan=2, sticky="w", padx=6)
            self.table_vars[name] = var

        # Кнопки запуска
        btn_frame = ttk.Frame(parent, style="Surface.TFrame")
        btn_frame.pack(anchor="e", padx=8, pady=(8, 2))
        self.btn_status_check = ttk.Button(btn_frame, text="🔎  Проверить (без изменений)",
                                           style="Soft.TButton", command=self._status_dry_run)
        self.btn_status_check.pack(side="left", padx=6)
        self.btn_run_status = ttk.Button(btn_frame, text="▶  Выполнить сброс статусов",
                                         style="Danger.TButton", command=self._run_status)
        self.btn_run_status.pack(side="left", padx=6)

    # --------------------------------------------------------
    # Вкладка 2: Замена shopindex в ch_shift
    # --------------------------------------------------------
    def _build_tab_shopindex(self, parent):
        pad = {"padx": 8, "pady": 4}

        (self.si_dt_start, self.si_time_start,
         self.si_dt_end, self.si_time_end) = self._build_period(parent, "Период (shiftcreate)")

        # Параметры shopindex
        si_frame = ttk.LabelFrame(parent, text="Значения shopindex")
        si_frame.pack(fill="x", **pad)

        ttk.Label(si_frame, text="Старое значение (пусто = все)", style="Surface.TLabel").grid(row=0, column=0, sticky="w", padx=6, pady=6)
        self.si_old = ttk.Entry(si_frame, width=18)
        self.si_old.grid(row=0, column=1, sticky="w", padx=6, pady=6)

        ttk.Label(si_frame, text="Новое значение", style="Surface.TLabel").grid(row=1, column=0, sticky="w", padx=6, pady=6)
        self.si_new = ttk.Entry(si_frame, width=18)
        self.si_new.grid(row=1, column=1, sticky="w", padx=6, pady=6)

        self.var_si_only_changed = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            si_frame,
            text="Только строки, где shopindex отличается от нового (рекомендуется)",
            variable=self.var_si_only_changed,
        ).grid(row=2, column=0, columnspan=2, sticky="w", padx=6, pady=4)

        # Кнопки
        btn_frame = ttk.Frame(parent, style="Surface.TFrame")
        btn_frame.pack(anchor="e", padx=8, pady=(8, 2))

        self.btn_si_export = ttk.Button(btn_frame, text="📤  Экспорт в CSV", style="Soft.TButton",
                                        command=self._si_export_csv)
        self.btn_si_export.pack(side="left", padx=6)

        self.btn_si_check = ttk.Button(btn_frame, text="🔎  Проверить (без изменений)", style="Soft.TButton",
                                       command=self._si_dry_run)
        self.btn_si_check.pack(side="left", padx=6)

        self.btn_si_run = ttk.Button(btn_frame, text="▶  Выполнить замену shopindex",
                                     style="Danger.TButton", command=self._si_run)
        self.btn_si_run.pack(side="left", padx=6)

    # --------------------------------------------------------
    # Настройки — применение / сохранение
    # --------------------------------------------------------
    def _set_date(self, widget, value):
        if HAS_CALENDAR:
            try:
                widget.set_date(datetime.strptime(value, "%Y-%m-%d"))
            except Exception:
                pass
        else:
            widget.delete(0, "end")
            widget.insert(0, value)

    def _apply_settings(self):
        s = self.settings
        for widget, key in (
            (self.ent_host, "host"), (self.ent_port, "port"), (self.ent_user, "user"),
            (self.ent_pass, "password"), (self.ent_db_cash, "db_cash"),
            (self.ent_db_discount, "db_discount"),
            (self.st_time_start, "time_start"), (self.st_time_end, "time_end"),
            (self.si_time_start, "si_time_start"), (self.si_time_end, "si_time_end"),
            (self.si_old, "si_old_shopindex"), (self.si_new, "si_new_shopindex"),
        ):
            widget.insert(0, str(s.get(key, "")))

        self._set_date(self.st_dt_start, s.get("date_start", ""))
        self._set_date(self.st_dt_end, s.get("date_end", ""))
        self._set_date(self.si_dt_start, s.get("si_date_start", ""))
        self._set_date(self.si_dt_end, s.get("si_date_end", ""))

        idx = s.get("status_index", 0)
        self.cmb_status.current(idx if isinstance(idx, int) and 0 <= idx < len(STATUSES) else 0)

        tables = s.get("tables", {})
        for name, var in self.table_vars.items():
            var.set(bool(tables.get(name, True)))
        self.var_si_only_changed.set(bool(s.get("si_only_changed", True)))

    def _collect_settings(self):
        return {
            "host": self.ent_host.get().strip(),
            "port": self.ent_port.get().strip(),
            "user": self.ent_user.get().strip(),
            "password": self.ent_pass.get(),
            "db_cash": self.ent_db_cash.get().strip(),
            "db_discount": self.ent_db_discount.get().strip(),
            # вкладка 1
            "date_start": self._get_date_widget_value(self.st_dt_start),
            "time_start": self.st_time_start.get().strip(),
            "date_end": self._get_date_widget_value(self.st_dt_end),
            "time_end": self.st_time_end.get().strip(),
            "status_index": self.cmb_status.current(),
            "tables": {name: var.get() for name, var in self.table_vars.items()},
            # вкладка 2
            "si_date_start": self._get_date_widget_value(self.si_dt_start),
            "si_time_start": self.si_time_start.get().strip(),
            "si_date_end": self._get_date_widget_value(self.si_dt_end),
            "si_time_end": self.si_time_end.get().strip(),
            "si_old_shopindex": self.si_old.get().strip(),
            "si_new_shopindex": self.si_new.get().strip(),
            "si_only_changed": self.var_si_only_changed.get(),
        }

    def _save(self):
        self.settings = self._collect_settings()
        save_settings(self.settings)

    def _manual_save(self):
        self._save()
        self._log(f"💾 Настройки сохранены в {SETTINGS_FILE}")

    def _on_close(self):
        if self.busy and not messagebox.askyesno(
            "Операция выполняется",
            "Сейчас идёт работа с БД. Если закрыть окно, незавершённая "
            "транзакция будет отменена сервером.\n\nВсё равно закрыть?",
        ):
            return
        self._save()
        self.root.destroy()

    # --------------------------------------------------------
    # Утилиты (только главный поток!)
    # --------------------------------------------------------
    def _toggle_password(self):
        self.ent_pass.config(show="" if self.var_show_pass.get() else "*")

    @staticmethod
    def _log_tag(line):
        s = line.lstrip()
        if s.startswith(("✅", "✔", "🏁", "📄", "📤", "💾")):
            return "ok"
        if s.startswith(("❌", "💥")) or "ОШИБКА" in s or "Traceback" in s:
            return "err"
        if s.startswith(("⚠", "→", "…")):
            return "warn"
        if s.startswith(("###", "---", "🔎", "🔌", "📦", "ОТЧЁТ", "ИТОГО")):
            return "head"
        if s.startswith(("===", "(нет строк")):
            return "dim"
        return ""

    def _append_log(self, lines):
        self.log.configure(state="normal")
        args = []
        for line in lines:
            args += [line + "\n", self._log_tag(line)]
        self.log.insert("end", *args)
        self.log.see("end")
        self.log.configure(state="disabled")

    def _show_about(self):
        c = COLORS
        win = tk.Toplevel(self.root, background=c["surface"])
        win.title("О приложении")
        win.resizable(False, False)
        win.transient(self.root)

        body = ttk.Frame(win, style="Surface.TFrame", padding=(36, 28, 36, 22))
        body.pack(fill="both", expand=True)
        if self.images.get(96):
            ttk.Label(body, image=self.images[96], style="Surface.TLabel").pack(pady=(0, 14))
        ttk.Label(body, text=APP_TITLE, style="Surface.TLabel", font=self.fonts["big"]).pack()
        ttk.Label(body, text=f"Версия v{APP_VERSION}", style="Surface.TLabel",
                  foreground=c["accent"], font=self.fonts["bold"]).pack(pady=(4, 0))
        ttk.Label(body, text="Сброс статусов отправки документов касс\nи массовая замена shopindex",
                  style="Muted.TLabel", justify="center").pack(pady=(10, 16))

        ttk.Separator(body).pack(fill="x", pady=(0, 14))
        info = ttk.Frame(body, style="Surface.TFrame")
        info.pack()
        for row, (label, value) in enumerate((("Создатель", APP_AUTHOR), ("", APP_CREDITS))):
            ttk.Label(info, text=label, style="Muted.TLabel").grid(row=row, column=0, sticky="e", padx=(0, 10))
            ttk.Label(info, text=value, style="Surface.TLabel",
                      font=self.fonts["bold"] if row == 0 else self.fonts["base"]).grid(
                row=row, column=1, sticky="w", pady=2)

        tech = (f"Python {sys.version.split()[0]}  ·  Tk {self.root.tk.call('info', 'patchlevel')}"
                f"  ·  psycopg2 {psycopg2.__version__.split()[0]}")
        ttk.Label(body, text=tech, style="Muted.TLabel").pack(pady=(16, 14))
        ttk.Button(body, text="Закрыть", style="Accent.TButton", command=win.destroy).pack()

        win.bind("<Escape>", lambda e: win.destroy())
        win.update_idletasks()
        x = self.root.winfo_rootx() + (self.root.winfo_width() - win.winfo_reqwidth()) // 2
        y = self.root.winfo_rooty() + (self.root.winfo_height() - win.winfo_reqheight()) // 3
        win.geometry(f"+{max(x, 0)}+{max(y, 0)}")
        win.wait_visibility()
        win.grab_set()
        win.focus_set()

    def _log(self, text):
        self._append_log([text])

    def _clear_log(self):
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")

    def _open_logs_dir(self):
        env = dict(os.environ)
        if IS_FROZEN:
            # Не отдаём внешним программам библиотеки из бандла
            orig = env.pop("LD_LIBRARY_PATH_ORIG", None)
            if orig:
                env["LD_LIBRARY_PATH"] = orig
            else:
                env.pop("LD_LIBRARY_PATH", None)
        try:
            ensure_logs_dir()
            subprocess.Popen(["xdg-open", LOGS_DIR], env=env)
        except Exception as e:
            self._log(f"Не удалось открыть папку logs/: {e}")

    def _get_date_widget_value(self, widget):
        if HAS_CALENDAR:
            return widget.get_date().strftime("%Y-%m-%d")
        return widget.get().strip()

    def _parse_period(self, dt_widget_start, time_start, dt_widget_end, time_end):
        def parse(dt_widget, tm_widget):
            date = datetime.strptime(self._get_date_widget_value(dt_widget), "%Y-%m-%d").date()
            return datetime.combine(date, parse_time(tm_widget.get()))

        start_dt = parse(dt_widget_start, time_start)
        end_dt = parse(dt_widget_end, time_end)
        if start_dt >= end_dt:
            raise ValueError("Дата начала должна быть меньше даты конца")
        return start_dt, end_dt

    def _conn_params(self):
        port_raw = self.ent_port.get().strip() or "5432"
        if not port_raw.isdigit():
            raise ValueError(f"Неверный порт «{port_raw}»")
        host = self.ent_host.get().strip()
        if not host:
            raise ValueError("Не указан адрес сервера")
        return ConnParams(host=host, port=int(port_raw),
                          user=self.ent_user.get().strip(), password=self.ent_pass.get())

    def _set_busy(self, busy):
        self.busy = busy
        state = "disabled" if busy else "normal"
        for btn in self.action_buttons:
            btn.config(state=state)
        self.root.config(cursor="watch" if busy else "")

    def _start(self, target, *args):
        """Запускает операцию в фоне; GUI блокируется до её окончания."""
        if self.busy:
            return

        def runner():
            try:
                target(*args)
            except Exception as e:
                self.ui.log(f"\n💥 ОШИБКА: {e}\n{traceback.format_exc()}")
                self.ui.call(messagebox.showerror, "Ошибка", str(e))
            finally:
                self.ui.call(self._set_busy, False)

        self._set_busy(True)
        threading.Thread(target=runner, daemon=True).start()

    def _open_report(self, suffix=""):
        ensure_logs_dir()
        ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        return Report(os.path.join(LOGS_DIR, f"{ts}{suffix}.txt"), self.ui.log)

    def _report_header(self, report, title, lines):
        report.write("=" * 70)
        report.write(title)
        report.write("=" * 70)
        report.write(f"Начало операции:      {now_str()}")
        for label, value in lines:
            report.write(f"{label + ':':<22}{value}")
        report.write("=" * 70)
        report.write()

    def _report_footer(self, report, totals):
        report.write("=" * 70)
        report.write("ИТОГО:")
        for line in totals:
            report.write(f"  {line}")
        report.write("=" * 70)
        report.write(f"Конец операции: {now_str()}")
        report.write("=" * 70)

    # --------------------------------------------------------
    # Проверка связи
    # --------------------------------------------------------
    def _test_connection(self):
        try:
            params = self._conn_params()
        except ValueError as e:
            messagebox.showerror("Ошибка ввода", str(e))
            return
        databases = [
            ("CASH", self.ent_db_cash.get().strip(), [t for t, _ in TABLES_CASH] + ["ch_shift"]),
            ("DISCOUNT", self.ent_db_discount.get().strip(), [t for t, _ in TABLES_DISCOUNT]),
        ]
        self._log("=" * 60)
        self._log("🔌 Проверка связи с БД...")
        self._start(check_databases, params, databases, self.ui.log)

    # ========================================================
    # Вкладка 1 — Сброс статусов
    # ========================================================
    def _status_params(self):
        start_dt, end_dt = self._parse_period(
            self.st_dt_start, self.st_time_start,
            self.st_dt_end, self.st_time_end
        )
        idx = self.cmb_status.current()
        if idx < 0:
            raise ValueError("Выберите новый статус")
        status = STATUSES[idx][0]

        plan = []
        for dbname, group in ((self.ent_db_cash.get().strip(), TABLES_CASH),
                              (self.ent_db_discount.get().strip(), TABLES_DISCOUNT)):
            tables = [t for t, _ in group if self.table_vars[t].get()]
            if tables:
                plan.append((dbname, tables))
        if not plan:
            raise ValueError("Не выбрано ни одной таблицы")
        return self._conn_params(), plan, start_dt, end_dt, status

    def _status_dry_run(self):
        try:
            params, plan, start_dt, end_dt, status = self._status_params()
        except Exception as e:
            messagebox.showerror("Ошибка ввода", str(e))
            return

        def worker():
            emit = self.ui.log
            emit("=" * 60)
            emit("🔎 Проверка (dry-run) — сброс статусов")
            emit(f"   Период:        {start_dt} — {end_dt}")
            emit(f"   Новый статус:  {status}")
            total = 0
            for dbname, tables in plan:
                conn = params.connect(dbname)
                try:
                    with conn.cursor() as cur:
                        for table in tables:
                            counts = count_statuses(cur, table, start_dt, end_dt)
                            n = sum(counts.values())
                            total += n
                            dist = ", ".join(f"статус {k}: {v}" for k, v in counts.items()) or "—"
                            emit(f"   {dbname}.{table}: {n} строк  ({dist})")
                finally:
                    conn.rollback()
                    conn.close()
            emit(f"   → Попадёт под UPDATE: {total} строк")

        self._start(worker)

    def _run_status(self):
        try:
            params, plan, start_dt, end_dt, status = self._status_params()
        except Exception as e:
            messagebox.showerror("Ошибка ввода", str(e))
            return

        tables_txt = "\n".join(f"  {db}: {', '.join(t)}" for db, t in plan)
        if not messagebox.askyesno(
            "Подтверждение",
            f"Сброс статусов:\n\n"
            f"Период: {start_dt} — {end_dt}\n"
            f"Новый статус: {status}\n"
            f"Таблицы:\n{tables_txt}\n\n"
            f"Продолжить?",
        ):
            return

        self._save()
        db_cash = self.ent_db_cash.get().strip()
        db_discount = self.ent_db_discount.get().strip()

        def worker():
            report = self._open_report()
            try:
                self._report_header(report, "ОТЧЁТ О СБРОСЕ СТАТУСА ОТПРАВКИ", [
                    ("Период", f"{start_dt} — {end_dt}"),
                    ("Новый статус", status),
                    ("Хост", f"{params.host}:{params.port}"),
                    ("Пользователь", params.user),
                    ("БД cash", db_cash),
                    ("БД discount", db_discount),
                ])
                try:
                    summary = run_status_reset(params, plan, start_dt, end_dt, status, report)
                except Exception as e:
                    report.write(f"💥 ОШИБКА: {e}", gui=False)
                    report.write(traceback.format_exc(), gui=False)
                    raise
                total = sum(cnt for _, _, cnt in summary)
                self._report_footer(report, [f"{db}.{t}: {cnt}" for db, t, cnt in summary]
                                    + [f"Всего обновлено строк: {total}"])
            finally:
                report.close()
            self.ui.log(f"\n📄 Отчёт сохранён: {report.path}")
            self.ui.call(messagebox.showinfo, "Готово",
                         f"Обновлено строк: {total}\n\nОтчёт: {report.path}")

        self._start(worker)

    # ========================================================
    # Вкладка 2 — Замена shopindex в ch_shift
    # ========================================================
    def _si_get_params(self):
        start_dt, end_dt = self._parse_period(
            self.si_dt_start, self.si_time_start,
            self.si_dt_end, self.si_time_end
        )
        old_raw = self.si_old.get().strip()
        new_raw = self.si_new.get().strip()
        if new_raw == "":
            raise ValueError("Новое значение shopindex обязательно")
        try:
            new_val = int(new_raw)
            old_val = int(old_raw) if old_raw != "" else None
        except ValueError:
            raise ValueError("shopindex должен быть целым числом")
        where_args = (start_dt, end_dt, old_val, new_val, self.var_si_only_changed.get())
        return self._conn_params(), self.ent_db_cash.get().strip(), where_args

    @staticmethod
    def _si_describe(where_args):
        start_dt, end_dt, old_val, new_val, only_changed = where_args
        return [
            ("Период", f"{start_dt} — {end_dt}"),
            ("Старое shopindex", old_val if old_val is not None else "все"),
            ("Новое shopindex", new_val),
            ("Только отличающиеся", "да" if only_changed else "нет"),
        ]

    def _si_dry_run(self):
        try:
            params, dbname, where_args = self._si_get_params()
        except Exception as e:
            messagebox.showerror("Ошибка ввода", str(e))
            return

        def worker():
            conn = params.connect(dbname)
            try:
                with conn.cursor() as cur:
                    cnt = count_shifts(cur, where_args)
            finally:
                conn.rollback()
                conn.close()
            self.ui.log("=" * 60)
            self.ui.log("🔎 Проверка (dry-run) — ch_shift")
            for label, value in self._si_describe(where_args):
                self.ui.log(f"   {label + ':':<21}{value}")
            self.ui.log(f"   → Попадёт под UPDATE: {cnt} строк")

        self._start(worker)

    def _si_run(self):
        try:
            params, dbname, where_args = self._si_get_params()
        except Exception as e:
            messagebox.showerror("Ошибка ввода", str(e))
            return

        desc = "\n".join(f"{label}: {value}" for label, value in self._si_describe(where_args))
        if not messagebox.askyesno("Подтверждение", f"Замена shopindex в ch_shift:\n\n{desc}\n\nПродолжить?"):
            return

        self._save()

        def worker():
            report = self._open_report("_shopindex")
            try:
                self._report_header(report, "ОТЧЁТ О ЗАМЕНЕ shopindex В ch_shift",
                                    self._si_describe(where_args) + [
                                        ("Хост", f"{params.host}:{params.port}"),
                                        ("Пользователь", params.user),
                                        ("БД", dbname),
                                    ])
                try:
                    updated = run_shopindex_update(params, dbname, where_args, report)
                except Exception as e:
                    report.write(f"💥 ОШИБКА: {e}", gui=False)
                    report.write(traceback.format_exc(), gui=False)
                    raise
                self._report_footer(report, [f"{dbname}.ch_shift: {updated}"])
            finally:
                report.close()
            self.ui.log(f"\n📄 Отчёт сохранён: {report.path}")
            self.ui.call(messagebox.showinfo, "Готово",
                         f"Обновлено строк: {updated}\n\nОтчёт: {report.path}")

        self._start(worker)

    # --------------------------------------------------------
    # Экспорт ch_shift в CSV (для проверки перед UPDATE)
    # --------------------------------------------------------
    def _si_export_csv(self):
        try:
            params, dbname, where_args = self._si_get_params()
        except Exception as e:
            messagebox.showerror("Ошибка ввода", str(e))
            return

        def worker():
            ensure_logs_dir()
            ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
            csv_path = os.path.join(LOGS_DIR, f"{ts}_ch_shift.csv")

            conn = params.connect(dbname)
            try:
                with conn.cursor() as cur:
                    colnames, rows = select_shifts(cur, SHIFT_EXPORT_FIELDS, where_args)
            finally:
                conn.rollback()
                conn.close()

            with open(csv_path, "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f, delimiter=";")
                w.writerow(colnames)
                w.writerows(rows)

            self.ui.log(f"📤 Экспортировано {len(rows)} строк в {csv_path}")
            self.ui.call(messagebox.showinfo, "Готово", f"Экспортировано: {len(rows)} строк\n\n{csv_path}")

        self._start(worker)


def main():
    root = tk.Tk(className="ResetStatusApp")
    ResetStatusApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
