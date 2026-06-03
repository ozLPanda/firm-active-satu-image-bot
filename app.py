from __future__ import annotations

import os
from pathlib import Path
import queue
import shutil
import threading
import tkinter as tk
import webbrowser
from tkinter import filedialog, messagebox, ttk

from bot import ExistingImageItem, SatuImageBot, StopRequested
from catalog import load_catalog, scan_images
from reports import ReportWriter


APP_DIR = Path(__file__).resolve().parent


def _app_data_dir() -> Path:
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / "SatuImageBot"
    return Path.home() / "AppData" / "Local" / "SatuImageBot"


APP_DATA_DIR = _app_data_dir()
PROFILE_DIR = APP_DATA_DIR / "profile"
REPORT_DIR = APP_DATA_DIR / "reports"


def migrate_legacy_data() -> None:
    APP_DATA_DIR.mkdir(parents=True, exist_ok=True)
    legacy_roots = (
        APP_DIR,
        APP_DIR.parent,
        Path.cwd(),
    )
    for root in legacy_roots:
        legacy_profile = root / "profile"
        if legacy_profile.exists() and legacy_profile.resolve() != PROFILE_DIR.resolve() and not PROFILE_DIR.exists():
            shutil.copytree(legacy_profile, PROFILE_DIR, dirs_exist_ok=True)
            break


class ExistingImagesDialog(tk.Toplevel):
    def __init__(self, parent: tk.Tk, items: list[ExistingImageItem]) -> None:
        super().__init__(parent)
        self.title("Товары с существующими изображениями")
        self.geometry("900x420")
        self.result: tuple[str, list[ExistingImageItem]] = ("skip", [])
        self.items = items

        ttk.Label(self, text="У этих товаров уже есть изображения. Выберите действие.").pack(
            anchor="w", padx=12, pady=(12, 6)
        )

        self.listbox = tk.Listbox(self, selectmode=tk.EXTENDED)
        self.listbox.pack(fill=tk.BOTH, expand=True, padx=12, pady=6)
        for item in items:
            self.listbox.insert(tk.END, f"{item.code} | {item.actual_name} | {item.product_url}")

        button_frame = ttk.Frame(self)
        button_frame.pack(fill=tk.X, padx=12, pady=12)
        ttk.Button(button_frame, text="Пропустить все", command=self._skip).pack(side=tk.LEFT)
        ttk.Button(button_frame, text="Заменить выбранные", command=self._replace_selected).pack(side=tk.LEFT, padx=8)
        ttk.Button(button_frame, text="Заменить все", command=self._replace_all).pack(side=tk.LEFT)

        self.transient(parent)
        self.grab_set()

    def _skip(self) -> None:
        self.result = ("skip", [])
        self.destroy()

    def _replace_selected(self) -> None:
        selected = [self.items[index] for index in self.listbox.curselection()]
        self.result = ("replace", selected)
        self.destroy()

    def _replace_all(self) -> None:
        self.result = ("replace", self.items)
        self.destroy()


class App(tk.Tk):
    def __init__(self) -> None:
        migrate_legacy_data()
        super().__init__()
        self.title("Satu Image Upload Bot")
        self.geometry("760x360")
        self.resizable(True, False)

        self.status_queue: queue.Queue[tuple[str, str]] = queue.Queue()
        self.worker: threading.Thread | None = None
        self.bot: SatuImageBot | None = None
        self.authorization_event = threading.Event()
        self.current_url = ""

        self.image_folder_var = tk.StringVar()
        self.excel_path_var = tk.StringVar()
        self.status_var = tk.StringVar(value="Готов к запуску.")
        self.url_var = tk.StringVar(value="")

        self._build_ui()
        self.after(200, self._poll_status)

    def _build_ui(self) -> None:
        main = ttk.Frame(self, padding=14)
        main.pack(fill=tk.BOTH, expand=True)

        ttk.Label(main, text="Папка с изображениями").grid(row=0, column=0, sticky="w")
        ttk.Entry(main, textvariable=self.image_folder_var).grid(row=1, column=0, sticky="ew", pady=(2, 10))
        ttk.Button(main, text="Выбрать", command=self._choose_image_folder).grid(row=1, column=1, padx=(8, 0), pady=(2, 10))

        ttk.Label(main, text="Excel с артикулами и названиями").grid(row=2, column=0, sticky="w")
        ttk.Entry(main, textvariable=self.excel_path_var).grid(row=3, column=0, sticky="ew", pady=(2, 10))
        ttk.Button(main, text="Выбрать", command=self._choose_excel).grid(row=3, column=1, padx=(8, 0), pady=(2, 10))

        ttk.Label(main, text="Статус").grid(row=4, column=0, sticky="w")
        ttk.Label(main, textvariable=self.status_var, wraplength=700).grid(row=5, column=0, columnspan=2, sticky="ew", pady=(2, 10))

        ttk.Label(main, text="Текущий товар").grid(row=6, column=0, sticky="w")
        url_label = ttk.Label(main, textvariable=self.url_var, foreground="#0645ad", cursor="hand2", wraplength=700)
        url_label.grid(row=7, column=0, columnspan=2, sticky="ew", pady=(2, 12))
        url_label.bind("<Button-1>", lambda _event: self._open_current_url())

        buttons = ttk.Frame(main)
        buttons.grid(row=8, column=0, columnspan=2, sticky="w")
        self.start_button = ttk.Button(buttons, text="Старт", command=self._start)
        self.start_button.pack(side=tk.LEFT)
        self.stop_button = ttk.Button(buttons, text="Стоп", command=self._stop, state=tk.DISABLED)
        self.stop_button.pack(side=tk.LEFT, padx=8)
        self.authorized_button = ttk.Button(
            buttons,
            text="Я авторизован",
            command=self._confirm_authorized,
            state=tk.DISABLED,
        )
        self.authorized_button.pack(side=tk.LEFT)
        ttk.Button(buttons, text="Открыть текущий товар", command=self._open_current_url).pack(side=tk.LEFT)

        main.columnconfigure(0, weight=1)

    def _choose_image_folder(self) -> None:
        folder = filedialog.askdirectory(title="Выберите папку с изображениями")
        if folder:
            self.image_folder_var.set(folder)

    def _choose_excel(self) -> None:
        path = filedialog.askopenfilename(
            title="Выберите Excel-файл",
            filetypes=(("Excel files", "*.xlsx *.xlsm"), ("All files", "*.*")),
        )
        if path:
            self.excel_path_var.set(path)

    def _start(self) -> None:
        if self.worker and self.worker.is_alive():
            return

        image_folder = Path(self.image_folder_var.get().strip())
        excel_path = Path(self.excel_path_var.get().strip())
        if not image_folder.is_dir():
            messagebox.showerror("Ошибка", "Выберите существующую папку с изображениями.")
            return
        if not excel_path.is_file():
            messagebox.showerror("Ошибка", "Выберите существующий Excel-файл.")
            return

        self.start_button.configure(state=tk.DISABLED)
        self.stop_button.configure(state=tk.NORMAL)
        self.authorized_button.configure(state=tk.NORMAL)
        self.authorization_event.clear()
        self.status_var.set("Подготовка данных...")

        self.worker = threading.Thread(target=self._run_worker, args=(image_folder, excel_path), daemon=True)
        self.worker.start()

    def _stop(self) -> None:
        if self.bot:
            self.bot.stop()
        self.authorization_event.set()
        self.status_var.set("Остановка после текущей операции...")

    def _confirm_authorized(self) -> None:
        self.authorization_event.set()
        self.authorized_button.configure(state=tk.DISABLED)
        self.status_var.set("Авторизация подтверждена. Бот продолжает работу...")

    def _run_worker(self, image_folder: Path, excel_path: Path) -> None:
        awaiting_existing_decision = False
        try:
            catalog = load_catalog(excel_path)
            images = scan_images(image_folder, catalog)
            if not images:
                self.status_queue.put(("В папке не найдено поддерживаемых изображений.", ""))
                return

            report_writer = ReportWriter(REPORT_DIR)
            self.bot = SatuImageBot(
                PROFILE_DIR,
                report_writer,
                self._queue_status,
                authorization_event=self.authorization_event,
            )
            existing = self.bot.run(images)
            self.status_queue.put((f"Основной проход завершен. Отчет: {report_writer.csv_path}", ""))
            if existing:
                awaiting_existing_decision = True
                self.status_queue.put(("__EXISTING__", existing))  # type: ignore[arg-type]
                return
        except StopRequested:
            self.status_queue.put(("Процесс остановлен пользователем.", ""))
        except Exception as exc:
            self.status_queue.put((f"Ошибка: {exc}", ""))
        finally:
            if not awaiting_existing_decision:
                self.status_queue.put(("__DONE__", ""))

    def _queue_status(self, status: str, url: str = "") -> None:
        self.status_queue.put((status, url))

    def _poll_status(self) -> None:
        try:
            while True:
                status, url = self.status_queue.get_nowait()
                if status == "__DONE__":
                    self.start_button.configure(state=tk.NORMAL)
                    self.stop_button.configure(state=tk.DISABLED)
                    self.authorized_button.configure(state=tk.DISABLED)
                    self.bot = None
                elif status == "__EXISTING__":
                    self._handle_existing_images(url)  # type: ignore[arg-type]
                else:
                    self.status_var.set(status)
                    if isinstance(url, str) and url:
                        self.current_url = url
                        self.url_var.set(url)
        except queue.Empty:
            pass
        self.after(200, self._poll_status)

    def _handle_existing_images(self, items: list[ExistingImageItem]) -> None:
        if not items or not self.bot:
            return
        dialog = ExistingImagesDialog(self, items)
        self.wait_window(dialog)
        action, selected = dialog.result
        if action == "replace" and selected:
            self.status_var.set("Запуск замены выбранных изображений...")
            self.start_button.configure(state=tk.DISABLED)
            self.stop_button.configure(state=tk.NORMAL)
            self.authorized_button.configure(state=tk.NORMAL)
            self.authorization_event.clear()
            bot = self.bot
            self.worker = threading.Thread(target=self._replace_worker, args=(bot, selected), daemon=True)
            self.worker.start()
        else:
            self.bot.mark_skipped_existing(items, status="skipped_by_user")
            self.status_queue.put(("Товары с существующими изображениями пропущены.", ""))
            self.status_queue.put(("__DONE__", ""))

    def _replace_worker(self, bot: SatuImageBot, selected: list[ExistingImageItem]) -> None:
        try:
            bot.replace_existing(selected)
            skipped = [item for item in bot.existing_images if item not in selected]
            bot.mark_skipped_existing(skipped, status="skipped_by_user")
            self.status_queue.put(("Замена выбранных изображений завершена.", ""))
        except StopRequested:
            self.status_queue.put(("Замена остановлена пользователем.", ""))
        except Exception as exc:
            self.status_queue.put((f"Ошибка при замене: {exc}", ""))
        finally:
            self.status_queue.put(("__DONE__", ""))

    def _open_current_url(self) -> None:
        if self.current_url:
            webbrowser.open(self.current_url)


if __name__ == "__main__":
    App().mainloop()
