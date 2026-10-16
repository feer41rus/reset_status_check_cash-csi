# 🧾 Reset Status App — работа с документами касс

[![Release](https://img.shields.io/github/v/release/feer41rus/reset_status_check_cash-csi)](https://github.com/feer41rus/reset_status_check_cash-csi/releases/latest)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
![Platform](https://img.shields.io/badge/platform-Linux-blue)
![Python](https://img.shields.io/badge/python-3.8%2B-blue)

GUI-утилита для Linux, которая массово сбрасывает (или меняет) статус
отправки документов на сервер в таблицах `ch_*` и `loy_*` базы данных
PostgreSQL кассы Set Retail, а также массово меняет `shopindex` в `ch_shift`.

> Автор: **Счастливый Владислав** · Разработано с помощью Claude Code
> Документация CSI: https://crystals.atlassian.net/wiki/spaces/SR10SUPPORT/pages/467927151
> Протестировано на версии Set Retail (Cash): 10.0.5.13

**Зачем это нужно.** Во время атак на ЦОД Яндекса мы потеряли CSI: Centrum и все
чеки за один день. Чтобы касса заново отправила документы, нужно было поменять
статус отправки в SQL. Объяснять каждому сотруднику и вручную править 5 таблиц —
трудозатратно, поэтому и появилось это приложение.

> ⚠️ Утилита выполняет массовые `UPDATE` в рабочей базе кассы.
> Перед запуском **сделайте резервную копию** и используйте кнопку
> «🔎 Проверить (без изменений)».

---

## 📥 Быстрый старт

### Вариант 1. AppImage (Python не нужен)

1. Скачайте `ResetStatusApp-1.0.0-x86_64.AppImage` со страницы
   [Releases](https://github.com/feer41rus/reset_status_check_cash-csi/releases/latest).
2. Запустите:
   ```bash
   chmod +x ResetStatusApp-1.0.0-x86_64.AppImage
   ./ResetStatusApp-1.0.0-x86_64.AppImage
   ```

- Нужен `libfuse2` (Ubuntu 24.04: `sudo apt install libfuse2t64`) или запуск с `--appimage-extract-and-run`.
- Работает на дистрибутивах с glibc не старее, чем на машине сборки.
- Отчёты пишутся в `~/.local/share/reset_status_app/logs/`
  (путь можно переопределить переменной `RESET_STATUS_LOGS_DIR`).

### Вариант 2. Из исходников

```bash
# системные пакеты (Ubuntu / Debian)
sudo apt install -y python3 python3-venv python3-tk

git clone https://github.com/feer41rus/reset_status_check_cash-csi.git
cd reset_status_check_cash-csi
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python3 main.py
```

<details>
<summary>Системные пакеты для других дистрибутивов</summary>

**Fedora / RHEL:**
```bash
sudo dnf install -y python3 python3-pip python3-tkinter
```

**Arch / Manjaro:**
```bash
sudo pacman -S python tk
```
</details>

При первом запуске автоматически создадутся:

- папка `logs/` рядом с `main.py` (в AppImage — `~/.local/share/reset_status_app/logs/`);
- файл настроек `~/.config/reset_status_app/settings.json`.

---

## 📦 Возможности

| Функция | Описание |
|---|---|
| 📄 **Сброс статусов** | Обновляет `senttoserverstatus` в выбранных таблицах за указанный период. |
| 🏪 **Замена shopindex** | Массово меняет `shopindex` в `ch_shift` за период (опционально — только для строк с заданным старым значением). Есть экспорт в CSV. |
| 🔎 **Проверить (без изменений)** | Dry-run: сколько строк попадёт под UPDATE и с какими текущими значениями. |
| 🔌 **Проверка связи с БД** | Отдельно проверяет базы `cash` и `discount`, показывает версию PostgreSQL и наличие нужных таблиц. |
| 💾 **Сохранение настроек** | В `~/.config/reset_status_app/settings.json`. Автосохранение — при запуске операции и закрытии окна. |
| 📄 **Отчёт** | На каждый запуск создаётся `logs/YYYY-MM-DD_HH-MM-SS.txt` со списком затронутых строк и их старыми значениями. |
| 📂 **Открыть logs/** | Открывает папку с отчётами в файловом менеджере. |
| ↩ **Безопасность транзакций** | Строки блокируются `SELECT … FOR UPDATE` и обновляются строго по `id`. `COMMIT` в `cash` и `discount` — только после успешной обработки обеих баз; при ошибке — `ROLLBACK` обеих. `lock_timeout` 30 с. |
| 🧵 **Многопоточность** | Работа с БД идёт в фоне, интерфейс не зависает. |

---

## 🗂 Поддерживаемые таблицы

| Таблица | База данных | Назначение |
|---|---|---|
| `ch_introduction` | `cash` | Внесение денег |
| `ch_purchase` | `cash` | Чеки (заголовки чеков) |
| `ch_reportshift` | `cash` | Z/X-отчёт |
| `ch_withdrawal` | `cash` | Изъятия наличных |
| `loy_transaction` | `discount` | Транзакции/Проводки/Продажи |
| `ch_shift` | `cash` | Смены (вкладка «Замена shopindex») |

## 📊 Статусы `senttoserverstatus`

| Код | Название | Описание |
|---|---|---|
| 0 | `NO_SENT` | Документ не отправлялся |
| 1 | `WAIT_ACKNOWLEDGEMENT` | Отправляется |
| 2 | `SENT` | Отправлено |
| 3 | `SENT_ERROR` | Ошибка отправки |
| 4 | `REQUESTED` | Запрошена повторная отправка с перезаписью |
| 5 | `UNCOMMITED` | Отправка завершилась ошибкой |
| 6 | `PENDING` | Ожидает серверного расчёта скидок |

---

## 🛠 Использование

### Вкладка «Сброс статусов»

1. **Подключение** — IP/хост, порт (обычно `5432`), пользователь, пароль.
2. **Имена баз** — обычно `cash` и `discount`.
3. **Период** — начало (включительно) и конец (не включительно), формат времени `ЧЧ:ММ:СС`.
   Используется правило `>= start AND < end`: `2026-11-09 00:00:00 — 2026-11-10 00:00:00`
   покрывает **весь день** 9 ноября, не захватывая 10-е.
4. **Новый статус** — из выпадающего списка (0–6).
5. **Таблицы** — отметьте галочками нужные (по умолчанию все 5).
6. **🔌 Проверить связь с БД** — рекомендуется перед первым запуском.
7. **🔎 Проверить (без изменений)** — посмотреть, сколько строк будет изменено.
8. **▶ Выполнить** — подтверждение → `UPDATE` по каждой таблице → `COMMIT` → отчёт.

### Вкладка «Замена shopindex»

1. Укажите период по `shiftcreate`.
2. **Старое значение** — пусто = все смены периода.
3. **Новое значение** — обязательно, целое число.
4. Галочка «Только строки, где shopindex отличается от нового» (рекомендуется).
5. **📤 Экспорт в CSV** — выгрузить затрагиваемые смены в `logs/` для проверки.
6. **🔎 Проверить** → **▶ Выполнить замену shopindex**.

---

## 📄 Формат отчёта

Каждый файл в `logs/` называется `YYYY-MM-DD_HH-MM-SS.txt`:

```
======================================================================
ОТЧЁТ О СБРОСЕ СТАТУСА ОТПРАВКИ
======================================================================
Начало операции:      2026-11-09 15:30:45
Период (datecreate):  2026-11-09 00:00:00 — 2026-11-10 00:00:00
Новый статус:         0
Хост:                 192.168.1.10:5432
Пользователь:         postgres
БД cash:              cash
БД discount:          discount
======================================================================

### БАЗА ДАННЫХ: cash ###

--- Таблица: ch_introduction (Внесение денег) ---
  id=101, datecreate=2026-11-09 08:14:22, numberfield=5  |  статус: 2 -> 0
  id=102, datecreate=2026-11-09 09:02:11, numberfield=6  |  статус: 1 -> 0
  → Обновлено строк: 2

  ✅ COMMIT для cash выполнен.

### БАЗА ДАННЫХ: discount ###

--- Таблица: loy_transaction (Транзакции/Проводки/Продажи) ---
  id=7701, transaction_time=2026-11-09 08:20:01  |  статус: 6 -> 0
  → Обновлено строк: 1

  ✅ COMMIT для discount выполнен.

======================================================================
ИТОГО:
  cash.ch_introduction: 2
  discount.loy_transaction: 1
  Всего обновлено строк: 3
======================================================================
```

## 📁 Файл настроек

`~/.config/reset_status_app/settings.json`:

```json
{
  "host": "192.168.1.10",
  "port": "5432",
  "user": "postgres",
  "password": "secret",
  "db_cash": "cash",
  "db_discount": "discount",
  "date_start": "2026-11-09",
  "time_start": "00:00:00",
  "date_end": "2026-11-10",
  "time_end": "00:00:00",
  "status_index": 0,
  "tables": {
    "ch_introduction": true,
    "ch_purchase": true,
    "ch_reportshift": true,
    "ch_withdrawal": true,
    "loy_transaction": true
  }
}
```

> **Внимание:** пароль хранится в **открытом виде**. Файл создаётся с правами `600`.

---

## 🏗 Сборка AppImage

Нужны `python3`, `python3-tk`, `python3-venv`, `libfuse2` и интернет:

```bash
./packaging/build_appimage.sh
```

Результат: `dist/ResetStatusApp-<версия>-x86_64.AppImage` (~24 МБ, внутри Python, Tk,
psycopg2/libpq, tkcalendar). Версия берётся из константы `APP_VERSION` в `main.py`.
Для широкой совместимости собирайте на самом старом поддерживаемом дистрибутиве.

---

## 📂 Структура проекта

```
reset_status_check_cash-csi/
├── main.py                    # Приложение (GUI + логика)
├── requirements.txt           # Python-зависимости
├── assets/                    # Логотип и иконки интерфейса
├── packaging/
│   ├── build_appimage.sh      # Сборка AppImage
│   ├── AppRun                 # Точка входа AppImage
│   └── reset-status-app.desktop
├── CHANGELOG.md
├── LICENSE
└── README.md
```

---

## 🧩 Настройка под себя

В `main.py` легко адаптировать:

- **Список таблиц** — `TABLES_CASH`, `TABLES_DISCOUNT`.
- **Список статусов** — `STATUSES`.
- **Поля отчёта** — `REPORT_FIELDS`, `SHIFT_REPORT_FIELDS`, `SHIFT_EXPORT_FIELDS`.
- **Пути** — `LOGS_DIR`, `SETTINGS_DIR`, `SETTINGS_FILE`.
- **Лимиты** — `GUI_ROWS_LIMIT`, `LOCK_TIMEOUT_MS`, `CONNECT_TIMEOUT_S`.

---

## ❓ Частые вопросы

**Q: «Проверка связи с БД» падает с `could not connect to server`.**
A: Проверьте IP, порт, что PostgreSQL слушает внешние подключения
(`listen_addresses` в `postgresql.conf`) и что `pg_hba.conf` разрешает
подключение вашему пользователю с вашего адреса.

**Q: Что будет, если `UPDATE` упадёт на середине?**
A: Обе базы обрабатываются в открытых транзакциях, `COMMIT` делается только
в конце. При ошибке в любой таблице выполняется `ROLLBACK` в обеих базах.
Единственный остаточный риск — сбой ровно между `COMMIT` в `cash` и `COMMIT`
в `discount`; это будет явно написано в отчёте.

**Q: Что значит `Updated Rows: -1` в DBeaver?**
A: Это не ошибка: клиент сообщает «команда выполнена, число строк неизвестно».
Реальное количество смотрите в отчёте в `logs/`.

**Q: Пароль со спецсимволами?**
A: Экранирование не требуется — `psycopg2` передаёт пароль как есть.

---

## ⚠ Ограничения

- Только **PostgreSQL** и только **Linux**.
- Пароль в JSON хранится в открытом виде.
- В окно выводятся первые 300 строк по каждой таблице (`GUI_ROWS_LIMIT`), в отчёт — все.

---

## 📜 Лицензия

[MIT](LICENSE) — можно свободно использовать, копировать, изменять, распространять,
в том числе в коммерческих целях. Программа предоставляется «как есть», без каких-либо
гарантий: автор не несёт ответственности за изменения в ваших базах данных.
