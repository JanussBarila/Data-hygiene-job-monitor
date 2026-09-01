"""Окно настройки писем. Открывай через Email_Settings.cmd."""
import base64
import hashlib
import json
import os
import queue
import subprocess
import sys
import threading
import webbrowser
from pathlib import Path

import vacancies_email as mail

FOLDER = Path(__file__).resolve().parent


def revision(folder):
    path = Path(folder) / mail.CONFIG
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None


def read_settings(folder):
    with mail.email_lock(folder):
        current = mail.load_config(folder) if (Path(folder) / mail.CONFIG).exists() else {}
        return current, revision(folder)


def prepare_config(sender, recipient, password, old):
    """Пустой пароль сохраняет прежний только для того же отправителя."""
    try:
        sender = mail.email_address(sender)
    except ValueError as error:
        raise ValueError("Проверь поле «Откуда отправлять»: нужен полный email.") from error
    recipients = mail.parse_recipients(recipient)
    provider = mail.PROVIDERS.get(sender.rsplit("@", 1)[1])
    if provider is None:
        raise ValueError("Для отправки поддерживаются Inbox.lv и Gmail. Получатель может быть на другой почте.")
    config = dict(old)
    config.update(version=2, sender=sender, recipient=recipients[0], recipients=recipients,
                  host=provider[0], port=provider[1], security=provider[2])
    if not password:
        if sender.lower() != old.get("sender", "").lower() or not old.get("password_dpapi"):
            raise ValueError("Введи специальный пароль для этого отправителя.")
    else:
        if provider[0] == "smtp.gmail.com":
            password = password.replace(" ", "")
        if not password or password.isspace():
            raise ValueError("Поле пароля содержит только пробелы.")
        encrypted = mail.protect_secret(password.encode("utf-8"))
        if mail.protect_secret(encrypted, decrypt=True).decode("utf-8") != password:
            raise RuntimeError("Windows не смог проверить сохранение пароля. Попробуй ещё раз.")
        config["password_dpapi"] = base64.b64encode(encrypted).decode("ascii")
    return config


def save_config(folder, config, expected_revision):
    with mail.email_lock(folder):
        if revision(folder) != expected_revision:
            raise RuntimeError("Настройки изменились в другом окне. Закрой это окно и открой заново.")
        mail.atomic_write(Path(folder) / mail.CONFIG,
                          json.dumps(config, ensure_ascii=False, indent=2).encode("utf-8"))
        return revision(folder)


def check_connection(config):
    with mail.connection(config, mail.unlock_password(config)):
        pass


def run_delivery(folder):
    script = Path(folder) / "job_tracker_email.py"
    if not script.is_file():
        raise RuntimeError("Не найден job_tracker_email.py. Перенеси все файлы дополнения в папку проекта.")
    python = Path(sys.executable)
    if os.name == "nt" and python.with_name("python.exe").exists():
        python = python.with_name("python.exe")
    result = subprocess.run([str(python), "-X", "utf8", str(script)], cwd=folder,
                            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15 * 60,
                            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    output = (result.stdout or "") + "\n" + (result.stderr or "")
    for line in reversed(output.splitlines()):
        if line.startswith(("Итог рассылки", "Почтовый сервер принял", "Новых для рассылки", "Нового завершённого",
                            "Рассылка не завершена", "Трекер завершился", "Другая проверка")):
            return result.returncode == 0, line
    if result.returncode:
        return False, "Запуск завершился с ошибкой. Подробности — в папке logs рядом с программой."
    return True, "Проверка завершена. Результат записан в папку logs."


class SettingsWindow:
    def __init__(self, root, folder=FOLDER):
        import tkinter as tk
        from tkinter import ttk
        self.root, self.folder = root, Path(folder)
        self.old, self.saved_revision = read_settings(self.folder)
        self.busy = False
        self.results = queue.Queue()
        self.sender = tk.StringVar(value=self.old.get("sender", ""))
        self.recipient = tk.StringVar(value="\n".join(mail.config_recipients(self.old)) if self.old else "")
        self.password = tk.StringVar()
        self.to_self = tk.BooleanVar(value=not self.old or self.sender.get().lower() == self.recipient.get().lower())
        self.show_password = tk.BooleanVar(value=False)
        self.password_hint = tk.StringVar()
        self.status = tk.StringVar(value="Измени адреса и нажми «Сохранить».")
        self.last_other_recipient = self.recipient.get()
        self.controls = []
        self.root.title("Настройки писем о вакансиях")
        self.root.configure(background="#f2f5fa")
        self.root.minsize(640, 660)
        self.root.geometry("700x710")
        style = ttk.Style(root)
        style.theme_use("clam")
        style.configure("TFrame", background="#f2f5fa")
        style.configure("Card.TFrame", background="white")
        style.configure("TLabel", background="#f2f5fa", foreground="#192b40", font=("Segoe UI", 11))
        style.configure("Card.TLabel", background="white")
        style.configure("Hint.TLabel", background="white", foreground="#5c6a7c", font=("Segoe UI", 10))
        style.configure("Title.TLabel", font=("Segoe UI", 21, "bold"))
        style.configure("TEntry", padding=8, font=("Segoe UI", 11), fieldbackground="white")
        style.configure("TCheckbutton", background="white", font=("Segoe UI", 10))
        style.map("TCheckbutton", background=[("active", "white")])
        style.configure("TButton", font=("Segoe UI", 10), padding=(12, 9))
        style.configure("Save.TButton", background="#2563eb", foreground="white")
        style.map("Save.TButton", background=[("active", "#1d4ed8"), ("disabled", "#c1cad7")])

        # При большом масштабе Windows форма прокручивается, поэтому кнопки доступны.
        viewport = tk.Canvas(root, background="#f2f5fa", highlightthickness=0)
        scrollbar = ttk.Scrollbar(root, orient="vertical", command=viewport.yview)
        viewport.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        viewport.pack(side="left", fill="both", expand=True)
        outer = ttk.Frame(viewport, padding=20)
        content = viewport.create_window((0, 0), window=outer, anchor="nw")
        outer.bind("<Configure>", lambda _:viewport.configure(scrollregion=viewport.bbox("all")))
        viewport.bind("<Configure>", lambda event:viewport.itemconfigure(content, width=event.width))
        root.bind("<MouseWheel>", lambda event:viewport.yview_scroll(-int(event.delta / 120), "units"))
        ttk.Label(outer, text="Письма о вакансиях", style="Title.TLabel").pack(anchor="w")
        ttk.Label(outer, text="Выбери почту для отправки и получения.").pack(anchor="w", pady=(3, 18))
        card = ttk.Frame(outer, style="Card.TFrame", padding=16)
        card.pack(fill="x")
        card.columnconfigure(0, weight=1)
        ttk.Label(card, text="Откуда отправлять", style="Card.TLabel").grid(row=0, column=0, sticky="w")
        self.sender_entry = ttk.Entry(card, textvariable=self.sender, font=("Segoe UI", 11))
        self.sender_entry.grid(row=1, column=0, sticky="ew", pady=(5, 16))
        ttk.Label(card, text="Получатели — один или несколько адресов", style="Card.TLabel").grid(row=2, column=0, sticky="w")
        self.recipient_entry = tk.Text(card, height=3, width=30, wrap="word", font=("Segoe UI", 11),
                                       padx=8, pady=7, borderwidth=1, relief="solid", undo=True)
        self.recipient_entry.grid(row=3, column=0, sticky="ew", pady=(5, 7))
        self.recipient_entry.bind("<<Modified>>", self.recipient_edited)
        ttk.Label(card, text="Через запятую, ; или с новой строки. Каждому — отдельное письмо.",
                  style="Hint.TLabel", wraplength=540).grid(row=4, column=0, sticky="w", pady=(0, 6))
        self.self_check = ttk.Checkbutton(card, text="Только самому себе", variable=self.to_self, command=self.toggle_self)
        self.self_check.grid(row=5, column=0, sticky="w", pady=(0, 16))
        ttk.Label(card, text="Специальный пароль почты", style="Card.TLabel").grid(row=6, column=0, sticky="w")
        self.password_entry = ttk.Entry(card, textvariable=self.password, show="•", font=("Segoe UI", 11))
        self.password_entry.grid(row=7, column=0, sticky="ew", pady=(5, 5))
        self.password_check = ttk.Checkbutton(card, text="Показать введённый пароль", variable=self.show_password,
                                             command=lambda:self.password_entry.configure(show="" if self.show_password.get() else "•"))
        self.password_check.grid(row=8, column=0, sticky="w")
        self.hint_label = ttk.Label(card, textvariable=self.password_hint, style="Hint.TLabel", wraplength=550)
        self.hint_label.grid(row=9, column=0, sticky="w", pady=(8, 6))
        self.help_link = ttk.Label(card, text="Где получить специальный пароль?", style="Hint.TLabel", cursor="hand2", foreground="#2563eb")
        self.help_link.grid(row=10, column=0, sticky="w")
        self.help_link.bind("<Button-1>", lambda _:self.open_help())
        self.controls.extend([self.sender_entry, self.recipient_entry, self.password_entry, self.self_check, self.password_check])

        buttons = ttk.Frame(outer)
        buttons.pack(fill="x", pady=(18, 12))
        self.save_button = ttk.Button(buttons, text="Сохранить", style="Save.TButton", command=self.save)
        self.save_button.pack(side="right")
        self.test_button = ttk.Button(buttons, text="Проверить подключение", command=self.test)
        self.test_button.pack(side="left")
        self.send_button = ttk.Button(buttons, text="Обновить и отправить", command=self.send_now)
        self.send_button.pack(side="left", padx=(8, 0))
        self.controls.extend([self.save_button, self.test_button, self.send_button])
        self.status_label = ttk.Label(outer, textvariable=self.status, wraplength=630)
        self.status_label.pack(fill="x", anchor="w")
        self.footer = ttk.Label(outer, text="«Сохранить» — для следующей рассылки. «Обновить и отправить» — проверить вакансии и отправить сейчас.", font=("Segoe UI", 10),
                                foreground="#5c6a7c", wraplength=630)
        self.footer.pack(anchor="w", pady=(10, 0))
        self.root.bind("<Configure>", self.resize)
        self.sender.trace_add("write", self.sender_changed)
        self.recipient.trace_add("write", self.form_changed)
        self.recipient.trace_add("write", self.sync_recipient_widget)
        self.password.trace_add("write", self.form_changed)
        self.root.bind("<Control-s>", lambda _:self.save() if not self.busy else None)
        self.root.after(120, self.poll)
        self.sync_recipient_widget()
        self.sender_changed()
        self.status.set("Настройки загружены. Можно изменить адреса." if self.old else "Введи свой Inbox.lv, специальный пароль и нажми «Сохранить».")
        self.sender_entry.focus_set()

    def resize(self, event):
        if event.widget is self.root:
            self.hint_label.configure(wraplength=max(300, event.width - 104))
            self.status_label.configure(wraplength=max(300, event.width - 56))
            self.footer.configure(wraplength=max(300, event.width - 56))

    def set_status(self, text, error=False):
        self.status.set(text)
        self.status_label.configure(foreground="#b42318" if error else "#192b40")

    def form_changed(self, *_):
        if not self.busy:
            self.set_status("Есть изменения. Нажми «Сохранить», чтобы применить их.")

    def sync_recipient_widget(self, *_):
        if self.recipient_entry.get("1.0", "end-1c") != self.recipient.get():
            self.recipient_entry.configure(state="normal")
            self.recipient_entry.delete("1.0", "end")
            self.recipient_entry.insert("1.0", self.recipient.get())
            self.recipient_entry.edit_modified(False)
        self.recipient_entry.configure(state="disabled" if self.to_self.get() or self.busy else "normal")

    def recipient_edited(self, *_):
        if self.recipient_entry.edit_modified():
            self.recipient_entry.edit_modified(False)
            self.recipient.set(self.recipient_entry.get("1.0", "end-1c"))

    def sender_changed(self, *_):
        if self.to_self.get():
            self.recipient.set(self.sender.get())
        if not self.busy:
            self.recipient_entry.configure(state="disabled" if self.to_self.get() else "normal")
        same_sender = self.sender.get().strip().lower() == self.old.get("sender", "").lower()
        if same_sender and self.old.get("password_dpapi"):
            self.password_hint.set("Пароль уже сохранён. Оставь поле пустым, чтобы использовать его, или введи новый.")
        else:
            self.password_hint.set("Введи специальный пароль этого отправителя. После сохранения он будет скрыт.")
        self.form_changed()

    def toggle_self(self):
        if self.to_self.get():
            self.last_other_recipient = self.recipient.get()
        else:
            self.recipient.set(self.last_other_recipient)
        self.sender_changed()

    def open_help(self):
        domain = self.sender.get().strip().lower().split("@")[-1]
        url = ("https://myaccount.google.com/apppasswords" if domain in ("gmail.com", "googlemail.com")
               else "https://email.inbox.lv/prefs?group=enable_pop3")
        webbrowser.open(url)

    def config_from_form(self):
        return prepare_config(self.sender.get(), self.recipient.get(), self.password.get(), self.old)

    def save(self):
        if self.busy:
            return False
        try:
            config = self.config_from_form()
            new_revision = save_config(self.folder, config, self.saved_revision)
        except Exception as error:
            self.set_status(mail.error_message(error), error=True)
            return False
        self.old, self.saved_revision = config, new_revision
        self.sender.set(config["sender"])
        self.recipient.set("\n".join(config["recipients"]))
        self.password.set("")
        self.show_password.set(False)
        self.password_entry.configure(show="•")
        self.sender_changed()
        self.set_status(f"Сохранено. Отправитель: {config['sender']}. Получателей: {len(config['recipients'])}.")
        return True

    def send_now(self):
        if self.busy or not self.save():
            return
        self.busy = True
        for widget in self.controls:
            widget.configure(state="disabled")
        self.set_status("Обновляю вакансии и отправляю новые. Проверка может занять несколько минут.")

        def worker():
            try:
                self.results.put(run_delivery(self.folder))
            except subprocess.TimeoutExpired:
                self.results.put((False, "Проверка заняла слишком много времени. Посмотри последний журнал в папке logs."))
            except Exception as error:
                self.results.put((False, mail.error_message(error)))

        threading.Thread(target=worker, daemon=True).start()

    def test(self):
        if self.busy:
            return
        try:
            config = self.config_from_form()
        except Exception as error:
            self.set_status(mail.error_message(error), error=True)
            return
        self.busy = True
        for widget in self.controls:
            widget.configure(state="disabled")
        self.set_status("Проверяю вход в почту… Можно подождать до минуты.")

        def worker():
            try:
                check_connection(config)
                self.results.put((True, "Подключение работает. Чтобы применить изменения, нажми «Сохранить»."))
            except Exception as error:
                self.results.put((False, mail.error_message(error)))

        threading.Thread(target=worker, daemon=True).start()

    def poll(self):
        try:
            success, text = self.results.get_nowait()
        except queue.Empty:
            pass
        else:
            self.busy = False
            for widget in self.controls:
                widget.configure(state="normal")
            self.recipient_entry.configure(state="disabled" if self.to_self.get() else "normal")
            self.set_status(text, error=not success)
        self.root.after(120, self.poll)


def main():
    import tkinter as tk
    from tkinter import messagebox
    root = tk.Tk()
    try:
        if not (FOLDER / "job_tracker.py").is_file():
            raise RuntimeError("Перенеси все файлы дополнения в папку, где находится твой job_tracker.py, и открой Email_Settings.cmd снова.")
        SettingsWindow(root)
    except Exception as error:
        root.withdraw()
        messagebox.showerror("Настройки почты", mail.error_message(error), parent=root)
        root.destroy()
        return 1
    root.mainloop()
    return 0


if __name__ == "__main__":
    try:
        result = main()
    except Exception as error:
        import os
        if os.name == "nt":
            import ctypes
            ctypes.windll.user32.MessageBoxW(None,
                "Не удалось открыть окно. Проверь наличие Python с компонентом Tcl/Tk.\n" + mail.error_message(error),
                "Настройки почты", 16)
        else:
            print(mail.error_message(error))
        result = 1
    raise SystemExit(result)
