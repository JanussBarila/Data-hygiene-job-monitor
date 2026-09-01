r"""Реальные вакансии CV.lv через MCP Центрального статистического управления Латвии.

Запуск в PowerShell: uv run .\job_filter_mcp.py
Нужен интернет. Дополнительные библиотеки и API-ключи не нужны.

Первая версия использует источник CV.lv (src=2), страну Latvia и нативные
поля объявления. Изменять для начала нужно только НАСТРОЙКИ ниже.
Источник: https://mcp-hc.stat.gov.lv/ — CC BY 4.0.
Документация: https://mcp.stat.gov.lv/hc?lang=en
"""

import csv
import json
import socket
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

# 1. НАСТРОЙКИ: минимальная НИЖНЯЯ граница месячной зарплаты из объявления.
# Например, диапазон 2500–3500 не пройдёт порог 3000.
MIN_SALARY = 2400
DAYS_BACK = 5                  # Последнее наблюдение: не более 7 дней назад.
CITY = ""                      # "" — вся Латвия; "Rīga" — поиск по городу.
KEYWORDS = [
    # Аналитика данных и Power BI
    "data analyst",
    "datu analīti",
    "business analyst",
    "biznesa analīti",
    "bi analyst",
    "power bi",
    "business intelligence",
    "reporting analyst",
    "pārskatu sagatavo",
    "atskaišu",
    "data quality",
    "datu kvalit",
    "data steward",
    "data governance",

    # Бизнес-процессы и операционная работа
    "business process",
    "biznesa proces",
    "process analyst",
    "procesu analīti",
    "process improvement",
    "continuous improvement",
    "operational excellence",
    "operations analyst",
    "operations manager",
    "procesu pilnveid",
    "procesu attīst",

    # HR-аналитика и системы
    "hr analyst",
    "hr analytics",
    "hr data",
    "hr digital",
    "people analytics",
    "people analyst",
    "workforce analyst",
    "personāla analīt",
    "personāla dat",
    "compensation analyst",
    "atalgojuma analīt",
    "hris",

    # Финансы и контроллинг
    "financial analyst",
    "finanšu analīti",
    "business controller",
    "financial controller",
    "biznesa kontrolier",
    "finanšu kontrolier",
    "budget analyst",
    "fp&a",

    # Логистика, закупки и планирование
    "supply chain",
    "demand planner",
    "demand planning",
    "supply planner",
    "supply planning",
    "logistics manager",
    "logistics analyst",
    "head of logistics",
    "loģistikas vadīt",
    "loģistikas direkt",
    "loģistikas analīti",
    "procurement analyst",
    "procurement specialist",
    "iepirkumu analīti",
    "iepirkumu speciālist",
]
KEYWORDS += [
    "analyst", "analīti", "analiti", "analītik", "analytics",
    "data", "datu",
    "logistics", "loģistik", "logistik",
    "kontrol", "biznesa",
]
MAX_RESULTS = 150

ENDPOINT = "https://mcp-hc.stat.gov.lv/mcp"
FOLDER = Path(__file__).resolve().parent


# 2. СОЕДИНЕНИЕ. MCP передаёт запросы и ответы в формате JSON по HTTPS.
class MCPClient:
    def __init__(self):
        self.number = 0
        self.session_id = None
        self.protocol = "2025-03-26"

    def request(self, method, params, notification=False):
        self.number += 1
        message = {"jsonrpc": "2.0", "method": method, "params": params}
        if not notification:
            message["id"] = self.number
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": self.protocol,
        }
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        request = Request(ENDPOINT, data=json.dumps(message).encode("utf-8"), headers=headers)
        with urlopen(request, timeout=45) as response:
            self.session_id = response.headers.get("Mcp-Session-Id", self.session_id)
            if notification:
                return None
            if "text/event-stream" in response.headers.get("Content-Type", ""):
                reply = self.read_events(response, self.number)
            else:
                reply = json.loads(response.read().decode("utf-8"))
        if reply.get("id") != self.number:
            raise RuntimeError("Сервер вернул ответ с неожиданным номером запроса.")
        if "error" in reply:
            raise RuntimeError(str(reply["error"]))
        return reply["result"]

    @staticmethod
    def read_events(response, number):
        data = []
        for raw_line in response:
            line = raw_line.decode("utf-8").rstrip("\r\n")
            if line.startswith("data:"):
                data.append(line[5:].lstrip(" "))
            elif not line and data:
                payload = json.loads("\n".join(data))
                messages = payload if isinstance(payload, list) else [payload]
                for message in messages:
                    if message.get("id") == number:
                        return message
                data = []
        raise RuntimeError("Сервер закрыл поток, не вернув результат.")

    def connect(self):
        result = self.request("initialize", {
            "protocolVersion": self.protocol,
            "capabilities": {},
            "clientInfo": {"name": "personal-job-filter", "version": "1.0"},
        })
        self.protocol = result["protocolVersion"]
        self.request("notifications/initialized", {}, notification=True)

    def tool(self, name, arguments):
        result = self.request("tools/call", {"name": name, "arguments": arguments})
        text = "\n".join(item["text"] for item in result.get("content", []) if item.get("type") == "text")
        if result.get("isError"):
            raise RuntimeError(text or "Сервер сообщил об ошибке инструмента.")
        return result.get("structuredContent") or json.loads(text)


# 3. SQL: выбираем только нужные данные. Запрос ничего не меняет в базе.
def sql_text(value):
    # Экранируем кавычки в настройках, а слова ищем буквально, без SQL-шаблонов.
    return "'" + str(value).replace("'", "''") + "'"


def build_query():
    if not isinstance(MIN_SALARY, (int, float)) or not 0 <= MIN_SALARY <= 1_000_000:
        raise ValueError("MIN_SALARY должен быть неотрицательным числом.")
    if not isinstance(DAYS_BACK, int) or not 0 <= DAYS_BACK <= 365:
        raise ValueError("DAYS_BACK должен быть целым числом от 0 до 365.")
    if not isinstance(MAX_RESULTS, int) or not 1 <= MAX_RESULTS <= 500:
        raise ValueError("MAX_RESULTS должен быть целым числом от 1 до 500.")
    words = [str(word).strip().lower() for word in KEYWORDS if str(word).strip()]
    title_filter = " OR ".join(f"strpos(lower(title), {sql_text(word)}) > 0" for word in words) or "TRUE"
    city_filter = f"strpos(lower(city), lower({sql_text(CITY.strip())})) > 0" if CITY.strip() else "TRUE"
    return f"""
WITH source_rows AS (
    SELECT vru.id, vru.ad_id, vru.first_seen, vru.last_seen,
           vra.ad->>'employerName' AS company,
           vra.ad->>'positionTitle' AS title,
           vra.ad->>'townId' AS city,
           vra.ad->>'countryId' AS country,
           btrim(vra.ad->>'salaryFrom') AS salary_from_text,
           btrim(vra.ad->>'salaryTo') AS salary_to_text,
           lower(btrim(vra.ad->>'hourlySalary')) AS hourly_salary,
           vra.ad->>'expirationDate' AS expires_at,
           vra.ad->>'remoteWorkType' AS remote_type
    FROM hc.vacancies_raw_unique vru
    JOIN hc.vacancies_raw_all vra ON vra.id = vru.raw_id
    WHERE vra.src = 2
), recent AS (
    SELECT DISTINCT ON (ad_id)
           id, ad_id, company, title, city, first_seen, last_seen,
           expires_at, remote_type, country, hourly_salary,
           CASE WHEN salary_from_text ~ '^[0-9]+([.][0-9]+)?$'
                THEN salary_from_text::numeric END AS salary_from_eur,
           CASE WHEN salary_to_text ~ '^[0-9]+([.][0-9]+)?$'
                THEN salary_to_text::numeric END AS salary_to_eur
    FROM source_rows
    WHERE last_seen BETWEEN CURRENT_DATE - {DAYS_BACK} AND CURRENT_DATE
      AND ad_id ~ '^[0-9]+$'
    ORDER BY ad_id, last_seen DESC, first_seen DESC, id DESC
), filtered AS (
    SELECT ad_id, company, title, city, salary_from_eur, salary_to_eur,
           first_seen::text AS first_seen, last_seen::text AS last_seen,
           expires_at, remote_type,
           'https://www.cv.lv/lv/vacancy/' || ad_id AS url
    FROM recent
    WHERE country = 'Latvia'
      AND hourly_salary = 'false'
      AND salary_from_eur >= {MIN_SALARY}
      AND expires_at ~ '^[0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}}T'
      AND left(expires_at, 10) >= CURRENT_DATE::text
      AND ({title_filter}) AND ({city_filter})
)
SELECT CURRENT_DATE::text AS database_date,
       (SELECT MAX(last_seen)::text FROM source_rows) AS source_last_seen,
       (SELECT COUNT(*) FROM filtered) AS total_matching,
       COALESCE((
           SELECT json_agg(page) FROM (
               SELECT ad_id, company, title, city, salary_from_eur, salary_to_eur,
                      first_seen, last_seen, expires_at, remote_type, url
               FROM filtered
               ORDER BY salary_from_eur DESC, first_seen DESC, ad_id
               LIMIT {MAX_RESULTS}
           ) page
       ), '[]'::json) AS vacancies
"""


# 4. ВЫГРУЗКА. CSV можно открыть в Excel или подключить к Power BI.
def spreadsheet_text(value):
    # Текст внешнего объявления остаётся текстом при открытии в Excel.
    if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


def save_results(snapshot):
    jobs = snapshot["vacancies"]
    if not isinstance(jobs, list):
        raise RuntimeError("Неожиданный формат списка вакансий.")
    now = datetime.now(timezone.utc)
    valid_jobs = []
    for job in jobs:
        try:
            deadline = datetime.fromisoformat(job["expires_at"].replace("Z", "+00:00"))
            if deadline.tzinfo is not None and deadline >= now:
                valid_jobs.append(job)
        except (ValueError, TypeError):
            continue
    jobs = valid_jobs
    fields = ["ad_id", "company", "title", "city", "salary_from_eur", "salary_to_eur",
              "first_seen", "last_seen", "expires_at", "remote_type", "url"]
    csv_path = FOLDER / "vacancies_live.csv"
    with csv_path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fields, delimiter=";", extrasaction="ignore")
        writer.writeheader()
        writer.writerows({key: spreadsheet_text(value) for key, value in job.items()} for job in jobs)

    lines = [
        "ВАКАНСИИ CV.LV ЧЕРЕЗ MCP — РЕЗУЛЬТАТ ОТБОРА",
        f"Выгружено: {now.isoformat(timespec='seconds')}",
        f"Дата сервера: {snapshot['database_date']}",
        f"Последнее наблюдение CV.lv в базе: {snapshot['source_last_seen']}",
        f"Порог нижней границы зарплаты: {MIN_SALARY} EUR/месяц.",
        f"Последнее наблюдение за {DAYS_BACK} дней; страна Latvia; город: {CITY or 'любой'}.",
        "Название содержит: " + (", ".join(KEYWORDS) or "любой текст"),
        f"Совпадений в SQL по датам и настройкам: {snapshot['total_matching']}.",
        f"Сохранено после проверки точного срока: {len(jobs)} (лимит {MAX_RESULTS}).",
        "Объявления без зарплаты, признака месячной оплаты или срока исключены.",
        "Условия взяты из сохранённых объявлений; открытость проверь по ссылке.",
        "Источник: CSP Latvia, https://mcp-hc.stat.gov.lv/ ; исходные объявления CV.lv.",
        "Лицензия базы: CC BY 4.0, https://creativecommons.org/licenses/by/4.0/",
        "",
    ]
    latest = snapshot.get("source_last_seen")
    if latest and (date.fromisoformat(snapshot["database_date"]) - date.fromisoformat(latest)).days > 2:
        lines.append("Внимание: последнее наблюдение источника старше двух дней.")
    if int(snapshot["total_matching"]) > MAX_RESULTS:
        lines.append("Достигнут лимит: выгружена только часть совпадений. Уточни настройки.")
    for number, job in enumerate(jobs, 1):
        salary = f"от {job['salary_from_eur']:g}"
        if job["salary_to_eur"] is not None:
            salary = f"{job['salary_from_eur']:g}–{job['salary_to_eur']:g}"
        lines.extend([
            f"{number}. {job['title']} — {job['company']}",
            f"   {job['city']} | {salary} EUR/месяц | {job['remote_type'] or 'формат не указан'}",
            f"   Последнее наблюдение: {job['last_seen']}; срок: {job['expires_at']}",
            f"   {job['url']}",
            "",
        ])
    if not jobs:
        lines.append("Совпадений нет. Попробуй уменьшить MIN_SALARY или расширить KEYWORDS.")
    report = "\n".join(lines)
    report_path = FOLDER / "vacancies_live_report.txt"
    report_path.write_text(report, encoding="utf-8-sig")
    print(report)
    print(f"\nТаблица: {csv_path}\nОтчёт: {report_path}")


def main():
    query = build_query()
    print("1/3 Подключаюсь к MCP-серверу...", flush=True)
    client = MCPClient()
    client.connect()
    print("2/3 Получаю описание данных...", flush=True)
    client.tool("getContext", {})
    print("3/3 Загружаю вакансии по настройкам...", flush=True)
    result = client.tool("execute_sql", {"sql": query})
    rows = result.get("data")
    if not isinstance(rows, list) or len(rows) != 1:
        raise RuntimeError("Сервер не вернул ожидаемый результат SQL: " + str(result)[:500])
    save_results(rows[0])


if __name__ == "__main__":
    try:
        main()
    except HTTPError as error:
        print(f"Ошибка HTTP {error.code}. Сервер не выполнил запрос. Попробуй повторить позже.", file=sys.stderr)
        sys.exit(1)
    except (URLError, TimeoutError, socket.timeout) as error:
        print(f"Нет ответа от сервера. Проверь интернет и повтори запуск. Детали: {error}", file=sys.stderr)
        sys.exit(1)
    except (RuntimeError, ValueError, OSError, KeyError, TypeError) as error:
        print(f"Не удалось завершить выгрузку: {error}", file=sys.stderr)
        sys.exit(1)
