"""Личные email-уведомления к job_tracker.py; Python 3.10+, без pip-зависимостей.

Настройка: python vacancies_email.py --setup
Обновление и отправка: python job_tracker_email.py
Секрет хранится через Windows DPAPI для текущего пользователя.
"""
import argparse
import base64
import ctypes
import getpass
import html
import json
import os
import re
import smtplib
import sqlite3
import ssl
import tempfile
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from email.message import EmailMessage
from email.policy import SMTP
from email.utils import format_datetime, formataddr
from pathlib import Path

FOLDER = Path(__file__).resolve().parent
CONFIG = "vacancies_email_config.json"
STATE = "vacancies_email.sqlite3"
PROVIDERS = {
    "inbox.lv": ("mail.inbox.lv", 587, "starttls"),
    "gmail.com": ("smtp.gmail.com", 465, "ssl"),
    "googlemail.com": ("smtp.gmail.com", 465, "ssl"),
}


class MailBusy(Exception):
    pass


class DeliveryUnknown(Exception):
    pass


class MailRateLimited(Exception):
    pass


@contextmanager
def email_lock(folder):
    with (Path(folder) / "vacancies_email.lock").open("a+b") as file:
        if file.seek(0, 2) == 0:
            file.write(b"0")
            file.flush()
        file.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise MailBusy("Другая проверка с рассылкой уже работает.") from error
        try:
            yield
        finally:
            file.seek(0)
            if os.name == "nt":
                msvcrt.locking(file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(file.fileno(), fcntl.LOCK_UN)


def atomic_write(path, content):
    path = Path(path)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as file:
            temporary = Path(file.name)
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    finally:
        if temporary and temporary.exists():
            temporary.unlink()


def protect_secret(data, decrypt=False):
    """CurrentUser DPAPI, UI запрещён; память Crypt32 освобождается через LocalFree."""
    if os.name != "nt":
        raise RuntimeError("Сохранение почтового пароля поддерживается только в Windows.")
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]

    buffer = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    source, result = Blob(len(data), buffer), Blob()
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    function = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    function.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.POINTER(Blob),
                         ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    function.restype = wintypes.BOOL
    if not function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(result)):
        raise RuntimeError(f"Windows не смог обработать пароль (код {ctypes.get_last_error()}). "
                           "Повтори настройку под своим пользователем Windows.")
    try:
        return ctypes.string_at(result.data, result.size)
    finally:
        kernel.LocalFree(result.data)


def email_address(value):
    if not isinstance(value, str):
        raise ValueError("Email должен быть текстом.")
    value = value.strip()
    # Только один обычный ASCII-адрес, без display name, списков или заголовков.
    if not re.fullmatch(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?\.[A-Za-z]{2,63}", value):
        raise ValueError("Введи один полный email, например name@inbox.lv.")
    local, domain = value.rsplit("@", 1)
    if len(value) > 254 or local.startswith(".") or local.endswith(".") or ".." in value:
        raise ValueError("Проверь написание email.")
    return local + "@" + domain.lower()


def parse_recipients(value):
    """Список адресов без дублей; каждый элемент проходит обычную строгую проверку."""
    if isinstance(value, str):
        parts = [part.strip() for part in re.split(r"[,;\r\n]+", value) if part.strip()]
    elif isinstance(value, list):
        parts = value
    else:
        raise ValueError("Укажи адреса получателей через запятую, ; или с новой строки.")
    result, seen = [], set()
    for index, part in enumerate(parts, 1):
        try:
            address = email_address(part)
        except ValueError as error:
            raise ValueError(f"Проверь адрес получателя № {index}: нужен полный email без имени и лишнего текста.") from error
        if address.lower() not in seen:
            result.append(address)
            seen.add(address.lower())
    if not result:
        raise ValueError("Добавь хотя бы одного получателя.")
    return result


def config_recipients(config):
    return parse_recipients(config.get("recipients", config.get("recipient", "")))


def load_config(folder):
    path = Path(folder) / CONFIG
    if not path.is_file():
        raise RuntimeError("Сначала запусти install_email_notifications.ps1 для настройки почты.")
    config = json.loads(path.read_text(encoding="utf-8"))
    if config.get("version") not in (1, 2):
        raise ValueError("Неизвестная версия почтовых настроек.")
    if config["version"] == 2 and not isinstance(config.get("recipients"), list):
        raise ValueError("Список получателей повреждён. Открой окно настроек и сохрани адреса заново.")
    config["recipients"] = config_recipients(config)
    config["recipient"] = config["recipients"][0]
    config["sender"] = email_address(config["sender"])
    provider = PROVIDERS.get(config["sender"].split("@")[1])
    if provider is None or (config["host"], config["port"], config["security"]) != provider:
        raise ValueError("Почтовые настройки изменены. Повтори --setup.")
    return config


def unlock_password(config):
    return protect_secret(base64.b64decode(config["password_dpapi"], validate=True), decrypt=True).decode("utf-8")


@contextmanager
def connection(config, password):
    context = ssl.create_default_context()
    smtp = None
    try:
        if config["security"] == "ssl":
            smtp = smtplib.SMTP_SSL(config["host"], config["port"], timeout=30, context=context)
        else:
            smtp = smtplib.SMTP(config["host"], config["port"], timeout=30)
            smtp.ehlo()
            smtp.starttls(context=context)
        smtp.ehlo()
        smtp.login(config["sender"], password)
        yield smtp
    finally:
        if smtp is not None:
            # Ошибка QUIT после положительного ответа DATA не должна вызывать повтор письма.
            try:
                smtp.quit()
            except (OSError, smtplib.SMTPException):
                smtp.close()


def smtp_send(config, password, message, before_data):
    with connection(config, password) as smtp:
        code, response = smtp.mail(config["sender"])
        if code != 250:
            raise smtplib.SMTPSenderRefused(code, response, config["sender"])
        code, response = smtp.rcpt(config["recipient"])
        if code not in (250, 251):
            raise smtplib.SMTPRecipientsRefused({config["recipient"]: (code, response)})
        content = message.as_bytes(policy=SMTP)
        before_data()  # commit ДО передачи письма; после аварии статус остаётся неопределённым
        try:
            smtp.data(content)
        except smtplib.SMTPDataError:
            raise  # Получен явный отказ сервера: повтор при следующем запуске допустим.
        except (OSError, smtplib.SMTPException) as error:
            raise DeliveryUnknown("Связь оборвалась во время передачи письма. "
                                  "Проверь почту и запусти vacancies_email.py --resolve.") from error


def setup(folder):
    if os.name != "nt":
        raise RuntimeError("Запусти мастер на своём компьютере Windows.")
    if not __import__("sys").stdin.isatty():
        raise RuntimeError("Настройку нужно запускать в обычном терминале PowerShell.")
    print("Настройка личных уведомлений. Пароль вводится только здесь, в терминале.")
    print("Отправлять можно через твой Inbox.lv или Gmail; получателей может быть несколько.")
    with email_lock(folder):
        old = load_config(folder) if (Path(folder) / CONFIG).exists() else {}
        sender = email_address(input(f"Твой адрес отправителя [{old.get('sender', '')}]: ").strip()
                               or old.get("sender", ""))
        provider = PROVIDERS.get(sender.rsplit("@", 1)[1])
        if provider is None:
            raise ValueError("Этот мастер поддерживает отправку с @inbox.lv или @gmail.com.")
        default_recipient = "; ".join(config_recipients(old)) if old else sender
        recipients = parse_recipients(input(f"Получатели через запятую или ; [{default_recipient}]: ").strip()
                                      or default_recipient)
        if sender.endswith("@inbox.lv"):
            print("Специальный пароль Inbox.lv: https://email.inbox.lv/prefs?group=enable_pop3")
        else:
            print("Пароль приложения Google: https://myaccount.google.com/apppasswords")
            print("Для него требуется двухэтапная аутентификация.")
        password = getpass.getpass("Специальный пароль / пароль приложения (символы скрыты): ")
        if not password:
            raise ValueError("Пароль не введён.")
        if provider[0] == "smtp.gmail.com":
            password = password.replace(" ", "")
        config = dict(version=2, sender=sender, recipient=recipients[0], recipients=recipients, host=provider[0],
                      port=provider[1], security=provider[2])
        print(f"Проверяю защищённое подключение: {sender}; получателей: {len(recipients)}")
        with connection(config, password):
            pass  # только проверка входа, письмо отправит свежий запуск трекера
        encrypted = protect_secret(password.encode("utf-8"))
        if protect_secret(encrypted, decrypt=True).decode("utf-8") != password:
            raise RuntimeError("Не пройдена проверка сохранения пароля Windows.")
        config["password_dpapi"] = base64.b64encode(encrypted).decode("ascii")
        atomic_write(Path(folder) / CONFIG, json.dumps(config, ensure_ascii=False, indent=2).encode("utf-8"))
        print("Настройки сохранены. Первый успешный запуск пришлёт текущую подборку.")
        print("Дальше — только ещё не отправленные вакансии. Без новых вакансий писем не будет.")


def latest_snapshot(folder):
    path = Path(folder) / "vacancies_history.sqlite3"
    if not path.is_file():
        return None
    db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        if db.execute("PRAGMA user_version").fetchone()[0] != 1:
            raise RuntimeError("Формат истории изменился: нужно обновить почтовое дополнение.")
        row = db.execute("SELECT payload FROM runs WHERE published=1 ORDER BY run_id DESC LIMIT 1").fetchone()
        return json.loads(row[0]) if row else None
    finally:
        db.close()


def eligible_jobs(payload, now):
    observed = datetime.fromisoformat(payload["observed_at"])
    if observed.tzinfo is None or not -timedelta(minutes=2) <= now - observed <= timedelta(minutes=15):
        raise RuntimeError("Нет свежего успешного обновления. Старый список по почте не отправлен.")
    jobs = {}
    for raw in payload["live"]:
        job = dict(raw)
        ad_id = str(job["ad_id"])
        if not re.fullmatch(r"[0-9]+", ad_id):
            raise ValueError("Некорректный ID вакансии.")
        try:
            expires = datetime.fromisoformat(job["expires_at"].replace("Z", "+00:00"))
        except (ValueError, TypeError):
            continue
        if expires.tzinfo is None or expires <= now:
            continue
        job["ad_id"] = ad_id
        job["url"] = f"https://www.cv.lv/lv/vacancy/{ad_id}"
        jobs[ad_id] = job
    return list(jobs.values())


def lv_number(value):
    """Latvian separators without changing the process locale or salary precision."""
    try:
        number = Decimal(str(value))
        if not number.is_finite():
            return "nav norādīts"
        result = format(number, ",f")
        if "." in result:
            result = result.rstrip("0").rstrip(".")
        return result.replace(",", "\u00a0").replace(".", ",")
    except (InvalidOperation, ValueError):
        return "nav norādīts"


def lv_date(value, with_time=False):
    """Keep times explicit in UTC: do not silently shift an advert's deadline."""
    if not value:
        return "nav norādīts"
    try:
        stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if with_time and stamp.tzinfo is not None:
            return stamp.astimezone(timezone.utc).strftime("%d.%m.%Y, %H:%M UTC")
        return stamp.strftime("%d.%m.%Y")
    except ValueError:
        return "nav norādīts"


def build_message(config, jobs, payload, batch_id, now, *, test=False):
    """DATA HIGIENE digest; inline styles and tables also work without remote images."""
    esc = lambda value: html.escape(str(value), quote=True)
    settings = payload["settings"]
    edition_date = lv_date(payload["local_day"])
    salary_filter = f"no {lv_number(settings['MIN_SALARY'])} € / mēn."
    city_filter = settings["CITY"] or "Visas pilsētas Latvijā"
    days = int(settings["DAYS_BACK"])
    period = f"{days} diena" if days % 10 == 1 and days % 100 != 11 else f"{days} dienas"
    source_date = lv_date(payload.get("source_last_seen"))
    checked_at = lv_date(payload["observed_at"], with_time=True)
    intro = ("Sveiki! Šeit apkopotas vakances, kas atbilst izvēlētajiem filtriem "
             "un vēl nav nosūtītas uz šo e-pasta adresi.")
    if test:
        intro = ("PĀRBAUDES VĒSTULE. Šis ir vienreizējs e-pasta pārbaudes sūtījums ar pašreizējo "
                 "vakanču izlasi. Tajā var būt arī iepriekš nosūtītas vakances.")
        if not jobs:
            intro += " Pašlaik izvēlētajiem filtriem nav atbilstošu, nebeigušos sludinājumu."
    note = ("Nosacījumi iegūti no saglabātiem sludinājumiem. Pirms pieteikšanās pārbaudi "
            "CV.lv, vai vakance joprojām ir pieejama. Šajā vēstulē var būt arī agrāk publicētas vakances.")
    attribution = ("Datu avots: CSP Latvia, https://mcp-hc.stat.gov.lv/ ; sludinājumi: CV.lv. "
                   "Datubāzes licence: CC BY 4.0, https://creativecommons.org/licenses/by/4.0/")
    plain = ["DATA HIGIENE", f"Tava vakanču izlase | {edition_date}", "", intro,
             f"Vakanču skaits: {len(jobs)}. Algas filtrs: {salary_filter}.",
             f"Pilsēta: {city_filter}. Novērojumu periods: {period}.",
             f"Jaunākais CV.lv novērojums datubāzē: {source_date}.",
             f"Atlase pārbaudīta: {checked_at}.", ""]
    formats = {"HYBRID": "Hibrīddarbs", "ON_SITE": "Klātienē", "REMOTE": "Attālināti",
               "FULLY_REMOTE": "Attālināti", "PARTIALLY_REMOTE": "Hibrīddarbs"}
    cards = []
    for index, job in enumerate(jobs, 1):
        low = lv_number(job["salary_from_eur"])
        salary = (f"{low}–{lv_number(job['salary_to_eur'])}" if job.get("salary_to_eur")
                  else f"no {low}") + " €"
        work_format = formats.get(str(job.get("remote_type", "")).upper(), "Darba forma nav norādīta")
        location = job.get("city") or "Pilsēta nav norādīta"
        dates = (f"Pēdējoreiz novērots: {lv_date(job['last_seen'])}. "
                 f"Termiņš sludinājumā: {lv_date(job['expires_at'], with_time=True)}.")
        # Derive the link from the validated numeric ID, never from source HTML.
        ad_id = str(job["ad_id"])
        if not re.fullmatch(r"[0-9]+", ad_id):
            raise ValueError("Некорректный ID вакансии.")
        url = f"https://www.cv.lv/lv/vacancy/{ad_id}"
        plain.extend([f"{index:02d}. {job['title']}", job["company"],
                      f"{salary} / mēn. | {location} | {work_format}", dates,
                      f"Skatīt vakanci: {url}", ""])
        cards.append(
            '<tr><td style="padding:0 0 14px">'
            '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
            'style="background:#fff;border:1px solid #dfe7f0;border-radius:12px;table-layout:fixed">'
            '<tr><td class="card" style="padding:22px 24px;word-wrap:break-word;color:#526980;font-size:14px">'
            f'<h2 style="margin:0 0 5px;font-size:20px;line-height:1.35;color:#0b1628">{esc(job["title"])}</h2>'
            f'<p style="margin:0 0 16px">{esc(job["company"])}</p>'
            f'<p style="margin:0 0 6px;font-size:23px;font-weight:bold;color:#164f93">{esc(salary)} '
            '<span style="font-size:13px;font-weight:normal;color:#526980">/ mēn.</span></p>'
            f'<p style="margin:0 0 14px;font-size:13px;color:#354b63">{esc(location)} · {work_format}</p>'
            f'<a href="{url}" style="display:inline-block;border:10px solid #eaf2ff;border-radius:6px;background:#eaf2ff;'
            'font-size:13px;font-weight:bold;color:#164f93;text-decoration:none">Skatīt vakanci CV.lv&nbsp; →</a>'
            f'<p style="margin:15px 0 0;font-size:11px;line-height:1.6">{esc(dates)}</p>'
            '</td></tr></table></td></tr>')
    sender = esc(config["sender"])
    plain.extend([note, "", "Ar cieņu,", "Janušs Barila", "DATA HIGIENE",
                  "Dati. Skaidrība. Iespējas.", config["sender"], "", attribution,
                  "Automātiska atlase pēc izvēlētajiem filtriem. Datumi ar laiku norādīti UTC."])
    body = (
        '<!doctype html><html lang="lv"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f'<title>DATA HIGIENE | Vakanču izlase | {edition_date}</title>'
        '<style>body,table,td,a{-webkit-text-size-adjust:100%;-ms-text-size-adjust:100%}'
        'table{mso-table-lspace:0pt;mso-table-rspace:0pt}'
        '@media screen and (max-width:480px){.outer{padding:12px 8px!important}'
        '.pad{padding-left:20px!important;padding-right:20px!important}'
        '.card{padding:20px!important}.hero{font-size:30px!important}'
        '.stat{font-size:22px!important}}</style></head>'
        '<body style="margin:0;padding:0;background:#f4f7fb;color:#0b1628;font-family:Arial,Helvetica,sans-serif;line-height:1.5">'
        '<div style="display:none;font-size:1px;line-height:1px;color:#f4f7fb;max-height:0;max-width:0;opacity:0;overflow:hidden;mso-hide:all">'
        f'Vakanču skaits: {len(jobs)}. {esc(salary_filter)}. {esc(city_filter)}. Izlase pēc Taviem kritērijiem.</div>'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" bgcolor="#f4f7fb">'
        '<tr><td class="outer" align="center" style="padding:32px 16px">'
        '<!--[if mso]><table role="presentation" width="680" align="center"><tr><td><![endif]-->'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        'style="max-width:680px;table-layout:fixed;text-align:left;font-family:Arial,Helvetica,sans-serif">'
        '<tr><td class="pad" bgcolor="#071321" style="background:#071321;padding:28px 34px 32px;border-top:4px solid #6aa8f7;border-radius:12px 12px 0 0">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>'
        '<td style="font-size:16px;font-weight:bold;letter-spacing:2px;color:#fff">DATA <span style="color:#8dbdff">HIGIENE</span></td>'
        f'<td align="right" style="text-align:right;font-size:11px;color:#b4c6dc;white-space:nowrap">{edition_date}</td></tr></table>'
        '<p style="margin:32px 0 10px;color:#8dbdff;font-size:11px;letter-spacing:2px;font-weight:bold">DARBA IESPĒJAS LATVIJĀ</p>'
        '<h1 class="hero" style="margin:0;color:#fff;font-size:38px;line-height:1.15;letter-spacing:-1px">Tava vakanču<br>izlase.</h1>'
        '<p style="margin:16px 0 0;color:#bdcce0;font-size:15px">Dati. Skaidrība. Iespējas.</p></td></tr>'
        '<tr><td class="pad" bgcolor="#eaf2ff" style="background:#eaf2ff;padding:22px 34px;border-radius:0 0 12px 12px">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>'
        '<td width="35%" valign="top" style="border-right:1px solid #cbdcf2;padding-right:12px">'
        '<p style="margin:0 0 5px;font-size:10px;font-weight:bold;letter-spacing:1px;color:#526980">VAKANČU SKAITS</p>'
        f'<p class="stat" style="margin:0;font-size:28px;font-weight:bold;line-height:1.2;color:#164f93">{len(jobs):02d}</p></td>'
        '<td valign="top" style="padding-left:22px">'
        '<p style="margin:0 0 5px;font-size:10px;font-weight:bold;letter-spacing:1px;color:#526980">ALGAS FILTRS</p>'
        f'<p class="stat" style="margin:0;font-size:26px;font-weight:bold;line-height:1.2;color:#164f93">no {esc(lv_number(settings["MIN_SALARY"]))} € '
        '<span style="font-size:12px;font-weight:normal">/ mēn.</span></p></td></tr></table></td></tr>'
        '<tr><td class="pad" style="padding:26px 8px 22px">'
        f'<p style="margin:0 0 12px;font-size:14px;line-height:1.7">{intro}</p>'
        f'<p style="margin:0;color:#526980;font-size:12px">{esc(city_filter)} · Novērojumu periods: {period}<br>'
        f'Jaunākais CV.lv novērojums datubāzē: {source_date}</p></td></tr>'
        + ''.join(cards) +
        '<tr><td class="pad" bgcolor="#eaf2ff" style="background:#eaf2ff;padding:18px 24px;border-radius:8px">'
        '<p style="margin:0 0 5px;font-size:12px;font-weight:bold;color:#164f93">PIRMS PIETEIKŠANĀS</p>'
        f'<p style="margin:0;font-size:12px;line-height:1.7;color:#354b63">{note}</p></td></tr>'
        '<tr><td class="pad" style="padding:30px 8px 26px">'
        '<p style="margin:0 0 14px;font-size:14px;color:#526980">Ar cieņu,</p>'
        '<table role="presentation" cellpadding="0" cellspacing="0"><tr>'
        '<td width="48" height="48" align="center" bgcolor="#071321" style="background:#071321;text-align:center;border-radius:8px;color:#8dbdff;font-size:17px;font-weight:bold">JB</td>'
        '<td style="padding-left:14px"><p style="margin:0;font-size:17px;font-weight:bold;color:#0b1628">Janušs Barila</p>'
        '<p style="margin:1px 0 0;font-size:10px;letter-spacing:1.5px;font-weight:bold;color:#164f93">DATA HIGIENE</p></td></tr></table>'
        f'<p style="margin:14px 0 0;font-size:13px"><a href="mailto:{sender}" style="color:#164f93;text-decoration:none">{sender}</a></p>'
        '</td></tr><tr><td class="pad" style="padding:22px 8px 0;border-top:1px solid #d9e3ef;word-wrap:break-word">'
        f'<p style="margin:0 0 8px;font-size:11px;line-height:1.7;color:#526980">Atlase pārbaudīta: {checked_at}. '
        'Datumi ar laiku norādīti UTC.<br>Automātiska atlase pēc izvēlētajiem filtriem.</p>'
        '<p style="margin:0;font-size:11px;line-height:1.7;color:#526980">Datu avots: '
        '<a href="https://mcp-hc.stat.gov.lv/" style="color:#526980">CSP Latvia</a> · Sludinājumi: CV.lv<br>'
        'Datubāzes licence: <a href="https://creativecommons.org/licenses/by/4.0/" style="color:#526980">CC BY 4.0</a>. '
        'Dati pārveidoti: filtrēti un formatēti šai izlasei.</p>'
        '</td></tr></table><!--[if mso]></td></tr></table><![endif]-->'
        '</td></tr></table></body></html>')
    message = EmailMessage(policy=SMTP)
    message["From"] = formataddr(("DATA HIGIENE | Janušs Barila", config["sender"]), charset="utf-8")
    message["To"] = config["recipient"]
    message["Subject"] = ("[TESTS] " if test else "") + f"DATA HIGIENE | Vakanču izlase: {len(jobs)} | {edition_date}"
    message["Date"] = format_datetime(now)
    message["Message-ID"] = f"<vacancies.{batch_id}@{config['sender'].rsplit('@', 1)[1]}>"
    message["Auto-Submitted"] = "auto-generated"
    message.set_content("\n".join(plain), charset="utf-8", cte="quoted-printable")
    message.add_alternative(body, subtype="html", charset="utf-8", cte="quoted-printable")
    return message, body


def open_state(folder):
    db = sqlite3.connect(Path(folder) / STATE)
    db.row_factory = sqlite3.Row
    db.executescript("""
        CREATE TABLE IF NOT EXISTS batches (
            batch_id TEXT PRIMARY KEY, recipient TEXT NOT NULL, state TEXT NOT NULL,
            created_at TEXT NOT NULL, subject TEXT NOT NULL, count INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS notified (
            recipient TEXT NOT NULL, ad_id TEXT NOT NULL, batch_id TEXT NOT NULL,
            PRIMARY KEY (recipient, ad_id)
        );
    """)
    with db:
        # Под общей блокировкой: sending из прошлого процесса означает аварийный выход.
        db.execute("UPDATE batches SET state='unknown' WHERE state='sending'")
        db.execute("UPDATE batches SET state='test_unknown' WHERE state='test_sending'")
    return db


def process_snapshot(folder, payload, config, password_getter=unlock_password, sender=smtp_send, now=None, before_send=None):
    """Вызывать под email_lock; отправляем только текущие, не истёкшие, ещё не отправленные ID."""
    live_clock = now is None
    now = now or datetime.now(timezone.utc)
    jobs = eligible_jobs(payload, now)
    recipient = config["recipient"].lower()
    db = open_state(folder)
    try:
        known = {row[0] for row in db.execute("SELECT ad_id FROM notified WHERE recipient=?", (recipient,))}
        candidates = [job for job in jobs if job["ad_id"] not in known]
        unknown = db.execute("SELECT count(*) FROM batches WHERE recipient=? AND state='unknown'", (recipient,)).fetchone()[0]
        if unknown:
            print(f"Есть писем с неподтверждённой отправкой: {unknown}. Запусти vacancies_email.py --resolve.")
        if not candidates:
            print("Новых для рассылки вакансий нет. Письмо не отправлялось.")
            return 0
        if before_send is not None:
            before_send()
            if live_clock:
                now = datetime.now(timezone.utc)
                candidates = [job for job in eligible_jobs(payload, now) if job["ad_id"] not in known]
                if not candidates:
                    print("Новых для рассылки вакансий нет: срок объявлений истёк за время ожидания.")
                    return 0
        batch_id = uuid.uuid4().hex
        message, body = build_message(config, candidates, payload, batch_id, now)
        archive = Path(folder) / "email_archive"
        archive.mkdir(exist_ok=True)
        atomic_write(archive / f"{batch_id}.eml", message.as_bytes())
        atomic_write(Path(folder) / "email_preview.html", body.encode("utf-8"))
        password = password_getter(config)

        def before_data():
            with db:
                db.execute("INSERT INTO batches VALUES (?, ?, 'sending', ?, ?, ?)",
                           (batch_id, recipient, (datetime.now(timezone.utc) if live_clock else now).isoformat(),
                            str(message["Subject"]), len(candidates)))
                db.executemany("INSERT INTO notified VALUES (?, ?, ?)",
                               [(recipient, job["ad_id"], batch_id) for job in candidates])

        try:
            sender(config, password, message, before_data)
        except DeliveryUnknown:
            with db:
                db.execute("UPDATE batches SET state='unknown' WHERE batch_id=?", (batch_id,))
            raise
        except smtplib.SMTPDataError:
            with db:
                db.execute("UPDATE batches SET state='rejected' WHERE batch_id=?", (batch_id,))
                db.execute("DELETE FROM notified WHERE batch_id=?", (batch_id,))
            raise
        with db:
            db.execute("UPDATE batches SET state='sent' WHERE batch_id=?", (batch_id,))
        print(f"Почтовый сервер принял письмо: {len(candidates)} вакансий → {config['recipient']}.")
        return len(candidates)
    finally:
        db.close()


def wait_for_send_budget(folder, config, now=None, sleeper=time.sleep):
    """Для Inbox.lv: 13 секунд между письмами, до 15 попыток этой программы в час."""
    if config["host"] != "mail.inbox.lv":
        return
    current = now or datetime.now(timezone.utc)
    db = sqlite3.connect(Path(folder) / STATE)
    try:
        times = [datetime.fromisoformat(row[0]) for row in db.execute(
            "SELECT created_at FROM batches ORDER BY created_at DESC LIMIT 15")]
    finally:
        db.close()
    recent = [stamp for stamp in times if stamp > current - timedelta(hours=1)]
    if recent and max(recent) > current + timedelta(seconds=2):
        raise MailRateLimited("Время компьютера изменилось. Проверь часы Windows и повтори запуск позже.")
    if len(recent) >= 15:
        raise MailRateLimited("Лимит Inbox.lv: эта программа уже сделала 15 попыток за час. "
                              "Оставшиеся получатели будут обработаны при следующем запуске после освобождения лимита.")
    if recent:
        delay = max(0, 13 - (current - max(recent)).total_seconds())
        if delay:
            print(f"Пауза между письмами: {delay:.0f} сек.", flush=True)
            sleeper(delay)


def process_test_snapshot(folder, payload, config, *, preview_only=False,
                          password_getter=unlock_password, sender=smtp_send,
                          now=None, rate_limiter=wait_for_send_budget):
    """Under email_lock: one explicit self-test, no reads/writes of notified IDs."""
    own_address = config["sender"]
    single = dict(config, recipient=own_address, recipients=[own_address])
    db = None
    try:
        if not preview_only:
            db = open_state(folder)
            rate_limiter(folder, single, now=now)
        checked_at = now or datetime.now(timezone.utc)
        jobs = eligible_jobs(payload, checked_at)
        batch_id = "test-" + uuid.uuid4().hex
        message, body = build_message(single, jobs, payload, batch_id, checked_at, test=True)
        preview = Path(folder) / "email_test_preview.html"
        atomic_write(preview, body.encode("utf-8"))
        print(f"Просмотр тестового письма: {preview}")
        if preview_only:
            print("Письмо подготовлено для просмотра. Отправки не было.")
            return 0
        archive = Path(folder) / "email_archive"
        archive.mkdir(exist_ok=True)
        atomic_write(archive / f"{batch_id}.eml", message.as_bytes())
        print(f"Отправляю одно тестовое письмо на свой адрес: {own_address}.", flush=True)
        password = password_getter(single)

        def before_data():
            stamp = now or datetime.now(timezone.utc)
            with db:
                db.execute("INSERT INTO batches VALUES (?, ?, 'test_sending', ?, ?, ?)",
                           (batch_id, own_address.lower(), stamp.isoformat(),
                            str(message["Subject"]), len(jobs)))

        try:
            sender(single, password, message, before_data)
        except DeliveryUnknown as error:
            with db:
                db.execute("UPDATE batches SET state='test_unknown' WHERE batch_id=?", (batch_id,))
            raise DeliveryUnknown("Результат тестовой отправки неизвестен. Проверь входящие и спам "
                                  "перед новым ручным запуском --test; автоматического повтора теста нет.") from error
        except smtplib.SMTPDataError:
            with db:
                db.execute("UPDATE batches SET state='test_rejected' WHERE batch_id=?", (batch_id,))
            raise
        with db:
            db.execute("UPDATE batches SET state='test_sent' WHERE batch_id=?", (batch_id,))
        print(f"Почтовый сервер принял одно тестовое письмо → {own_address}. "
              "Ищи тему с [TESTS]. История обычных уведомлений сохранена.")
        return 1
    finally:
        if db is not None:
            db.close()


def process_recipients(folder, payload, config, password_getter=unlock_password, sender=smtp_send,
                       now=None, rate_limiter=wait_for_send_budget):
    """Одно письмо на адрес; успехи сохраняются независимо от ошибок у других адресов."""
    recipients = config_recipients(config)
    summary = dict(sent=0, unchanged=0, failed=0, deferred=0)
    for index, recipient in enumerate(recipients):
        single = dict(config, recipient=recipient, recipients=[recipient])
        print(f"Получатель {index + 1}/{len(recipients)}: {recipient}", flush=True)
        try:
            count = process_snapshot(folder, payload, single, password_getter, sender, now,
                                     before_send=lambda:rate_limiter(folder, single, now=now))
            summary["sent" if count else "unchanged"] += 1
        except MailRateLimited as error:
            print(error)
            summary["deferred"] = len(recipients) - index
            break
        except smtplib.SMTPAuthenticationError as error:
            print(error_message(error))
            summary["failed"] += 1
            summary["deferred"] = len(recipients) - index - 1
            break
        except Exception as error:
            print(f"Не завершена отправка на {recipient}: {error_message(error)}")
            summary["failed"] += 1
    print(f"Итог рассылки: писем принято — {summary['sent']}; без новых — {summary['unchanged']}; "
          f"с ошибкой — {summary['failed']}; отложено получателей — {summary['deferred']}.")
    return summary


def status(folder, resolve=False):
    with email_lock(folder):
        config = load_config(folder)
        print(f"Отправитель: {config['sender']}; получатели: {'; '.join(config_recipients(config))}")
        db = open_state(folder)
        try:
            rows = db.execute("SELECT * FROM batches ORDER BY created_at DESC").fetchall()
            print("Всего писем принято сервером:", sum(row["state"] == "sent" for row in rows))
            print("Тестовых писем принято сервером:", sum(row["state"] == "test_sent" for row in rows))
            for row in rows:
                if row["state"] == "test_unknown":
                    print(f"Неизвестный результат теста: {row['subject']} → {row['recipient']}. "
                          "Проверь почту перед следующим --test; автоматически тест не повторяется.")
                    continue
                if row["state"] != "unknown":
                    continue
                print(f"Неизвестный результат: {row['subject']} → {row['recipient']}")
                print(f"Копия: {Path(folder) / 'email_archive' / (row['batch_id'] + '.eml')}")
                if resolve:
                    print("Сначала проверь входящие и спам. 1 — письмо получил; 2 — не получил, разрешить повтор; Enter — оставить.")
                    choice = input("Выбор: ").strip()
                    with db:
                        if choice == "1":
                            db.execute("UPDATE batches SET state='sent' WHERE batch_id=?", (row["batch_id"],))
                        elif choice == "2":
                            db.execute("UPDATE batches SET state='retry' WHERE batch_id=?", (row["batch_id"],))
                            db.execute("DELETE FROM notified WHERE batch_id=?", (row["batch_id"],))
        finally:
            db.close()


def error_message(error):
    # SMTP-ответы не печатаются: диагностике достаточно типа и кода, без лишних данных сервера.
    if isinstance(error, smtplib.SMTPAuthenticationError):
        return "Почта отклонила вход. Проверь полный email, специальный пароль и доступ SMTP; повтори --setup."
    if isinstance(error, smtplib.SMTPResponseException):
        return f"Почтовый сервер вернул код {error.smtp_code}. Проверь настройки и повтори запуск позже."
    if isinstance(error, smtplib.SMTPRecipientsRefused):
        return "Почтовый сервер отклонил адрес получателя. Повтори --setup и проверь email."
    if isinstance(error, DeliveryUnknown):
        return str(error)
    if isinstance(error, (OSError, smtplib.SMTPException)):
        return f"Не удалось завершить почтовое подключение ({type(error).__name__}). Проверь интернет и повтори позже."
    return str(error)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--setup", action="store_true")
    group.add_argument("--status", action="store_true")
    group.add_argument("--resolve", action="store_true")
    args = parser.parse_args()
    try:
        if args.setup:
            setup(FOLDER)
        else:
            status(FOLDER, resolve=args.resolve)
    except (KeyboardInterrupt, EOFError):
        print("Настройка отменена.")
        raise SystemExit(1)
    except Exception as error:
        print(error_message(error))
        raise SystemExit(1)
