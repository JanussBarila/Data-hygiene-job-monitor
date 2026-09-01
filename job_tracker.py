r"""История вакансий и новые объявления. Запуск: uv run .\job_tracker.py

Положи рядом со своим job_filter_mcp.py. Фильтры читаются из него.
Новые = впервые обнаруженные твоей программой, а не обязательно опубликованные сегодня.
vacancies_new.csv накапливает новые объявления за текущий день по времени компьютера.
Повторный запуск в тот же день не очищает уже найденные за день объявления.
История хранится в SQLite и экспортируется в CSV; снимки каждого запуска — в snapshots.
Дополнительные библиотеки не нужны. Источник: https://mcp-hc.stat.gov.lv/ (CC BY 4.0).
"""

import csv
import importlib.util
import io
import json
import os
import sqlite3
import sys
import tempfile
import traceback
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

FOLDER = Path(__file__).resolve().parent
FIELDS = ["ad_id", "company", "title", "city", "salary_from_eur", "salary_to_eur",
          "first_seen", "last_seen", "expires_at", "remote_type", "url"]
HISTORY_FIELDS = FIELDS + ["first_found_at", "last_found_at", "new_for_me_on", "in_latest_selection"]
CHANGE_FIELDS = ["observed_at", "ad_id", "title", "field", "old_value", "new_value", "url"]
WATCH_FIELDS = ["company", "title", "city", "salary_from_eur", "salary_to_eur", "expires_at", "remote_type"]


class AlreadyRunning(Exception):
    pass


@contextmanager
def run_lock(folder):
    """Системная блокировка освобождается даже после аварийного завершения Python."""
    with (folder / "vacancies.lock").open("a+b") as lock:
        if lock.seek(0, 2) == 0:
            lock.write(b"0")
            lock.flush()
        lock.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise AlreadyRunning("Другой запуск уже работает. Этот запуск пропущен.") from error
        try:
            yield
        finally:
            lock.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def source_module(folder):
    path = folder / "job_filter_mcp.py"
    if not path.is_file():
        raise RuntimeError("Положи job_tracker.py рядом с твоим job_filter_mcp.py.")
    spec = importlib.util.spec_from_file_location("user_job_filter", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fetch_snapshot(source):
    query = source.build_query()
    print("1/3 Подключаюсь к MCP...", flush=True)
    client = source.MCPClient()
    client.connect()
    print("2/3 Получаю описание данных...", flush=True)
    client.tool("getContext", {})
    print("3/3 Получаю вакансии и сравниваю с историей...", flush=True)
    result = client.tool("execute_sql", {"sql": query})
    rows = result.get("data")
    if not isinstance(rows, list) or len(rows) != 1:
        raise RuntimeError("Неожиданный ответ сервера. История не обновлена.")
    return rows[0]


def text_value(value):
    return "" if value is None else str(value)


def number_text(value):
    if value in (None, ""):
        return ""
    try:
        number = Decimal(str(value).replace(",", "."))
        if not number.is_finite() or number < 0:
            raise InvalidOperation
        return format(number.normalize(), "f")
    except InvalidOperation as error:
        raise ValueError(f"Некорректная зарплата: {value!r}") from error


def normalize_job(row):
    job = {key: text_value(row.get(key)) for key in FIELDS}
    if not job["ad_id"].isdigit():
        raise ValueError("В строке отсутствует корректный ad_id вакансии CV.lv.")
    for key in ("salary_from_eur", "salary_to_eur"):
        job[key] = number_text(row.get(key))
    return job


def baseline_from_csv(folder):
    path = folder / "vacancies_live.csv"
    if not path.exists():
        return None
    with path.open(encoding="utf-8-sig", newline="") as file:
        reader = csv.DictReader(file, delimiter=";")
        if not set(FIELDS).issubset(reader.fieldnames or []):
            raise ValueError("У vacancies_live.csv изменились столбцы. Восстанови исходную выгрузку.")
        rows = []
        for row in reader:
            for key, value in row.items():
                if isinstance(value, str) and value.startswith("'") and value[1:].lstrip().startswith(("=", "+", "-", "@")):
                    row[key] = value[1:]
            rows.append(normalize_job(row))
    observed = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(timespec="seconds")
    return rows, observed


def initialize_database(db):
    version = db.execute("PRAGMA user_version").fetchone()[0]
    if version not in (0, 1):
        raise RuntimeError("Версия базы истории не поддерживается этой программой.")
    db.executescript("""
        CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS vacancies (
            ad_id TEXT PRIMARY KEY, payload TEXT NOT NULL,
            first_found_at TEXT NOT NULL, last_found_at TEXT NOT NULL,
            new_for_me_on TEXT NOT NULL, in_latest_selection INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS changes (
            sequence INTEGER PRIMARY KEY, local_day TEXT NOT NULL, payload TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS runs (
            run_id TEXT PRIMARY KEY, payload TEXT NOT NULL, published INTEGER NOT NULL DEFAULT 0
        );
        PRAGMA user_version = 1;
    """)


def csv_text(rows, fields):
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=fields, delimiter=";", extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        safe = {}
        for key, value in row.items():
            if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
                value = "'" + value
            safe[key] = value
        writer.writerow(safe)
    return buffer.getvalue()


def atomic_write(path, content, encoding="utf-8-sig"):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding=encoding, newline="", dir=path.parent,
                                         prefix=path.name + ".", suffix=".tmp", delete=False) as file:
            temp_path = Path(file.name)
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temp_path, path)
    finally:
        if temp_path and temp_path.exists():
            temp_path.unlink()


def make_report(payload):
    settings = payload["settings"]
    lines = [
        "ВАКАНСИИ — ИСТОРИЯ И НОВЫЕ ОБЪЯВЛЕНИЯ",
        f"Успешный запрос: {payload['observed_at']}",
        f"Дата компьютера для подборки новых: {payload['local_day']}",
        f"Последнее наблюдение источника CV.lv: {payload['source_last_seen']}",
        f"Текущая подборка: {len(payload['live'])}; всего ID в истории: {len(payload['history'])}.",
        f"Новых за этот запуск: {payload['new_this_run']}; новых для тебя сегодня: {len(payload['new_today'])}.",
        f"Изменений полей и присутствия за запуск: {payload['change_count']}.",
        f"Фильтр: от {settings['MIN_SALARY']} EUR/месяц, {settings['DAYS_BACK']} дней, город: {settings['CITY'] or 'любой'}.",
        "Ключевые слова: " + ", ".join(settings["KEYWORDS"]),
        "Новые = впервые замеченные твоей программой. Это не дата публикации.",
        "vacancies_new.csv содержит новые за весь сегодняшний день, включая повторные запуски.",
        "in_latest_selection = no означает отсутствие в текущей подборке; это не подтверждение закрытия.",
        "Источник: CSP Latvia, https://mcp-hc.stat.gov.lv/ ; объявления CV.lv.",
        "Лицензия базы: CC BY 4.0, https://creativecommons.org/licenses/by/4.0/",
    ]
    if payload["initial_mode"]:
        lines.append("Первый запуск: сохранён исходный список. Уже известные объявления не считаются новыми.")
    if payload["filters_changed"]:
        lines.append("Настройки изменились. Новая для тебя запись могла публиковаться раньше.")
    if payload["skipped_deadlines"]:
        lines.append(f"Исключено по точному сроку или некорректной дате: {payload['skipped_deadlines']}.")
    if payload["source_last_seen"] and (date.fromisoformat(payload["database_date"]) - date.fromisoformat(payload["source_last_seen"])).days > 2:
        lines.append("Последнее наблюдение источника старше двух дней: проверь дату перед откликом.")
    lines.append("\nНОВЫЕ ДЛЯ ТЕБЯ СЕГОДНЯ")
    for job in payload["new_today"]:
        upper = f"–{job['salary_to_eur']}" if job["salary_to_eur"] else "+"
        lines.extend([f"{job['title']} — {job['company']}",
                      f"{job['city']} | {job['salary_from_eur']}{upper} EUR/месяц | в подборке: {job['in_latest_selection']}",
                      job["url"], ""])
    if not payload["new_today"]:
        lines.append("Сегодня новых для тебя объявлений пока нет.")
    return "\n".join(lines) + "\n"


def publish_run(folder, payload):
    # Снимок и новые за конкретный запуск остаются в архиве, даже когда наступит новый день.
    run_id = payload["run_id"]
    atomic_write(folder / "snapshots" / f"{run_id}.json", json.dumps(payload, ensure_ascii=False, indent=2), "utf-8")
    atomic_write(folder / "snapshots" / f"{run_id}_new.csv", csv_text(payload["new_at_run"], HISTORY_FIELDS))
    atomic_write(folder / "vacancies_live.csv", csv_text(payload["live"], FIELDS))
    atomic_write(folder / "vacancies_new.csv", csv_text(payload["new_today"], HISTORY_FIELDS))
    atomic_write(folder / "vacancies_history.csv", csv_text(payload["history"], HISTORY_FIELDS))
    atomic_write(folder / "vacancies_changes.csv", csv_text(payload["changes_today"], CHANGE_FIELDS))
    atomic_write(folder / "vacancies_live_report.txt", make_report(payload))


def track_once(folder, fetcher, settings, now=None):
    """fetcher отделён от хранения: можно проверять сравнение без сетевых запросов."""
    folder = Path(folder)
    with run_lock(folder):
        db = sqlite3.connect(folder / "vacancies_history.sqlite3")
        db.row_factory = sqlite3.Row
        try:
            initialize_database(db)
            # Если Excel помешал предыдущей записи CSV, повторяем сохранение из базы.
            for pending in db.execute("SELECT run_id, payload FROM runs WHERE published = 0 ORDER BY run_id").fetchall():
                publish_run(folder, json.loads(pending["payload"]))
                with db:
                    db.execute("UPDATE runs SET published = 1 WHERE run_id = ?", (pending["run_id"],))

            snapshot = fetcher()
            raw_jobs = snapshot.get("vacancies")
            total = int(snapshot["total_matching"])
            if not isinstance(raw_jobs, list) or total != len(raw_jobs):
                raise RuntimeError("Выгрузка обрезана или неполна. Увеличь MAX_RESULTS в job_filter_mcp.py (до 500) или уточни фильтр. История не обновлена.")
            current_time = now or datetime.now().astimezone()
            observed = current_time.astimezone(timezone.utc).isoformat(timespec="microseconds")
            local_day = current_time.date().isoformat()
            run_id = current_time.astimezone(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
            jobs, skipped = {}, 0
            for raw_job in raw_jobs:
                job = normalize_job(raw_job)
                try:
                    deadline = datetime.fromisoformat(job["expires_at"].replace("Z", "+00:00"))
                    if deadline.tzinfo is None or deadline < current_time:
                        skipped += 1
                        continue
                except ValueError:
                    skipped += 1
                    continue
                if job["ad_id"] in jobs:
                    raise RuntimeError("Сервер вернул повторяющиеся ID. История не обновлена.")
                jobs[job["ad_id"]] = job
            # Проверяем даты до записи истории, чтобы повреждённый ответ не стал новым состоянием.
            date.fromisoformat(snapshot["database_date"])
            if snapshot.get("source_last_seen"):
                date.fromisoformat(snapshot["source_last_seen"])

            settings_json = json.dumps(settings, ensure_ascii=False, sort_keys=True)
            with db:
                initialized = db.execute("SELECT value FROM meta WHERE key = 'initialized'").fetchone() is not None
                old_settings = db.execute("SELECT value FROM meta WHERE key = 'settings'").fetchone()
                filters_changed = old_settings is not None and old_settings[0] != settings_json
                baseline = baseline_from_csv(folder) if not initialized else None
                if baseline is not None:
                    for job in baseline[0]:
                        db.execute("INSERT OR IGNORE INTO vacancies VALUES (?, ?, ?, ?, '', 1)",
                                   (job["ad_id"], json.dumps(job, ensure_ascii=False), baseline[1], baseline[1]))
                baseline_only = not initialized and baseline is None
                previous = {row["ad_id"]: dict(row) for row in db.execute("SELECT * FROM vacancies")}
                events, new_ids = [], []

                def event(job, field, old, new):
                    item = dict(observed_at=observed, ad_id=job["ad_id"], title=job["title"], field=field,
                                old_value=old, new_value=new, url=job["url"])
                    events.append(item)
                    db.execute("INSERT INTO changes(local_day, payload) VALUES (?, ?)",
                               (local_day, json.dumps(item, ensure_ascii=False)))

                for ad_id, old in previous.items():
                    if old["in_latest_selection"] and ad_id not in jobs:
                        event(json.loads(old["payload"]), "in_latest_selection", "yes", "no")
                db.execute("UPDATE vacancies SET in_latest_selection = 0")
                for ad_id, job in jobs.items():
                    old = previous.get(ad_id)
                    if old:
                        before = json.loads(old["payload"])
                        for field in WATCH_FIELDS:
                            if before[field] != job[field]:
                                event(job, field, before[field], job[field])
                        if not old["in_latest_selection"]:
                            event(job, "in_latest_selection", "no", "yes")
                        first_found, new_day = old["first_found_at"], old["new_for_me_on"]
                    else:
                        first_found = observed
                        new_day = "" if baseline_only else local_day
                        if not baseline_only:
                            new_ids.append(ad_id)
                    db.execute("""INSERT INTO vacancies VALUES (?, ?, ?, ?, ?, 1)
                               ON CONFLICT(ad_id) DO UPDATE SET payload=excluded.payload,
                               last_found_at=excluded.last_found_at, in_latest_selection=1""",
                               (ad_id, json.dumps(job, ensure_ascii=False), first_found, observed, new_day))

                history = []
                for row in db.execute("SELECT * FROM vacancies ORDER BY first_found_at DESC, ad_id"):
                    job = json.loads(row["payload"])
                    job.update(first_found_at=row["first_found_at"], last_found_at=row["last_found_at"],
                               new_for_me_on=row["new_for_me_on"],
                               in_latest_selection="yes" if row["in_latest_selection"] else "no")
                    history.append(job)
                payload = dict(run_id=run_id, observed_at=observed, local_day=local_day,
                               database_date=snapshot["database_date"], source_last_seen=snapshot.get("source_last_seen"),
                               settings=settings, live=list(jobs.values()), history=history,
                               new_today=[job for job in history if job["new_for_me_on"] == local_day],
                               new_at_run=[job for job in history if job["ad_id"] in new_ids],
                               new_this_run=len(new_ids), change_count=len(events),
                               changes_today=[json.loads(row[0]) for row in db.execute(
                                   "SELECT payload FROM changes WHERE local_day = ? ORDER BY sequence", (local_day,))],
                               filters_changed=filters_changed, initial_mode=not initialized, skipped_deadlines=skipped)
                db.execute("INSERT INTO runs(run_id, payload) VALUES (?, ?)", (run_id, json.dumps(payload, ensure_ascii=False)))
                db.execute("INSERT OR REPLACE INTO meta VALUES ('initialized', 'yes')")
                db.execute("INSERT OR REPLACE INTO meta VALUES ('settings', ?)", (settings_json,))
            # База уже хранит результат. При сбое экспорта он будет восстановлен при следующем запуске.
            publish_run(folder, payload)
            with db:
                db.execute("UPDATE runs SET published = 1 WHERE run_id = ?", (run_id,))
            return payload
        finally:
            db.close()


class Tee:
    def __init__(self, console, log):
        self.console, self.log = console, log

    def write(self, text):
        self.console.write(text)
        self.log.write(text)
        return len(text)

    def flush(self):
        self.console.flush()
        self.log.flush()


def main():
    source = source_module(FOLDER)
    # Читаем текущие настройки пользователя; исходный Python-файл не переписывается.
    settings = {"MIN_SALARY": source.MIN_SALARY, "DAYS_BACK": source.DAYS_BACK,
                "CITY": source.CITY, "KEYWORDS": sorted(set(str(word).strip().lower() for word in source.KEYWORDS if str(word).strip()))}
    payload = track_once(FOLDER, lambda: fetch_snapshot(source), settings)
    print(make_report(payload))
    print(f"Новые за сегодня: {FOLDER / 'vacancies_new.csv'}")
    print(f"История: {FOLDER / 'vacancies_history.csv'}")
    print(f"Изменения: {FOLDER / 'vacancies_changes.csv'}")


if __name__ == "__main__":
    (FOLDER / "logs").mkdir(exist_ok=True)
    log_path = FOLDER / "logs" / (datetime.now().strftime("run_%Y%m%d_%H%M%S_%f") + ".log")
    exit_code = 0
    with log_path.open("w", encoding="utf-8") as log:
        with redirect_stdout(Tee(sys.stdout, log)), redirect_stderr(Tee(sys.stderr, log)):
            try:
                main()
            except AlreadyRunning as error:
                print(error)
            except PermissionError as error:
                print("Не удалось записать файл. Закрой CSV в Excel и повтори запуск. Проверь доступ к папке.", file=sys.stderr)
                print(error, file=sys.stderr)
                exit_code = 1
            except Exception as error:
                print(f"Запуск не завершён: {error}", file=sys.stderr)
                traceback.print_exc(file=log)
                exit_code = 1
    print(f"Журнал: {log_path}")
    sys.exit(exit_code)
