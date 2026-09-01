"""Обновить вакансии и отправить уведомления; --test: одно письмо себе; --preview: только просмотр."""
import argparse
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import vacancies_email as mail

FOLDER = Path(__file__).resolve().parent


def main(*, test=False, preview=False):
    folder = FOLDER
    tracker = folder / "job_tracker.py"
    if not tracker.is_file():
        raise RuntimeError("Положи файлы дополнения рядом с существующим job_tracker.py.")
    (folder / "logs").mkdir(exist_ok=True)
    log_path = folder / "logs" / datetime.now().strftime("email_%Y%m%d_%H%M%S_%f.log")
    with mail.email_lock(folder), log_path.open("w", encoding="utf-8") as log:
        from contextlib import redirect_stdout, redirect_stderr

        class Tee:
            def __init__(self, stream):
                self.stream = stream

            def write(self, text):
                self.stream.write(text)
                log.write(text)
                log.flush()
                return len(text)

            def flush(self):
                self.stream.flush()
                log.flush()

        with redirect_stdout(Tee(sys.stdout)), redirect_stderr(Tee(sys.stderr)):
            print(f"Журнал рассылки: {log_path}")
            try:
                # Даже если почта ещё не настроена, существующая проверка продолжает работать.
                before = mail.latest_snapshot(folder)
                before_id = before["run_id"] if before else None
                print("Обновляю вакансии через твой job_tracker.py...", flush=True)
                result = subprocess.run([sys.executable, "-X", "utf8", str(tracker)],
                                        cwd=folder, timeout=12 * 60)
                if result.returncode:
                    print("Трекер завершился с ошибкой. Старый список по почте не отправлен.")
                    return result.returncode
                payload = mail.latest_snapshot(folder)
                if payload is None or payload["run_id"] == before_id:
                    print("Нового завершённого обновления нет. Письмо не отправлено.")
                    return 1 if test or preview else 0
                config = mail.load_config(folder)
                if test or preview:
                    mail.process_test_snapshot(folder, payload, config, preview_only=preview)
                    return 0
                summary = mail.process_recipients(folder, payload, config)
                return 1 if summary["failed"] or summary["deferred"] else 0
            except Exception as error:
                print("Рассылка не завершена: " + mail.error_message(error))
                return 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--test", action="store_true", help="Одно письмо на адрес отправителя, включая уже отправленные вакансии")
    group.add_argument("--preview", action="store_true", help="Обновить подборку и создать email_test_preview.html без отправки")
    args = parser.parse_args()
    try:
        raise SystemExit(main(test=args.test, preview=args.preview))
    except mail.MailBusy as error:
        print(error)
        raise SystemExit(1 if args.test or args.preview else 0)
    except Exception as error:
        print(mail.error_message(error))
        raise SystemExit(1)
