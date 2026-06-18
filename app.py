from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path
import queue
import re
import shutil
import threading
import time
import tkinter as tk
import webbrowser
import sys
from tkinter import filedialog, messagebox, ttk

from bot import DeferredUploadItem, ExistingImageItem, SatuImageBot, StopRequested
from catalog import CatalogItem, code_lookup_keys, load_catalog, load_name_catalog, processing_items
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
RUN_STATE_FILE = APP_DATA_DIR / "upload-state.json"
CATALOG_CACHE_FILE = APP_DATA_DIR / "catalog-cache.json"
EXISTING_QUEUE_FILE = APP_DATA_DIR / "existing-images-queue.json"
DEFERRED_QUEUE_FILE = APP_DATA_DIR / "deferred-upload-queue.json"
USER_SETTINGS_FILE = APP_DATA_DIR / "user-settings.json"
ADMIN_PRODUCT_URL_PREFIX = "https://my.satu.kz/cms/product/edit/"
PUBLIC_PRODUCT_URL_PATTERN = re.compile(r"^https?://(?:www\.)?mstop\.kz/p(\d+)(?:[-/.?#]|$)", re.IGNORECASE)


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


def normalize_direct_product_url(url: str) -> str:
    public_match = PUBLIC_PRODUCT_URL_PATTERN.match(url.strip())
    if public_match:
        return f"{ADMIN_PRODUCT_URL_PREFIX}{public_match.group(1)}"
    return url.strip()


def is_admin_product_url(url: str) -> bool:
    return url.startswith(ADMIN_PRODUCT_URL_PREFIX)


def load_saved_paths() -> dict[str, object]:
    try:
        if not USER_SETTINGS_FILE.is_file():
            return {
                "image_folder": "",
                "excel_path": "",
                "names_excel_path": "",
                "update_names": True,
                "update_main_price": True,
                "update_discount_system": True,
                "write_not_found_report": False,
            }
        with USER_SETTINGS_FILE.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except Exception:
        return {
            "image_folder": "",
            "excel_path": "",
            "names_excel_path": "",
            "update_names": True,
            "update_main_price": True,
            "update_discount_system": True,
            "write_not_found_report": False,
        }

    image_folder = str(data.get("image_folder") or "").strip() if isinstance(data, dict) else ""
    excel_path = str(data.get("excel_path") or "").strip() if isinstance(data, dict) else ""
    names_excel_path = str(data.get("names_excel_path") or "").strip() if isinstance(data, dict) else ""
    update_names = bool(data.get("update_names", True)) if isinstance(data, dict) else True
    update_main_price = bool(data.get("update_main_price", True)) if isinstance(data, dict) else True
    update_discount_system = bool(data.get("update_discount_system", True)) if isinstance(data, dict) else True
    write_not_found_report = bool(data.get("write_not_found_report", False)) if isinstance(data, dict) else False
    return {
        "image_folder": image_folder if image_folder and Path(image_folder).is_dir() else "",
        "excel_path": excel_path if excel_path and Path(excel_path).is_file() else "",
        "names_excel_path": names_excel_path if names_excel_path and Path(names_excel_path).is_file() else "",
        "update_names": update_names,
        "update_main_price": update_main_price,
        "update_discount_system": update_discount_system,
        "write_not_found_report": write_not_found_report,
    }


def save_saved_paths(
    image_folder: str | Path = "",
    excel_path: str | Path = "",
    names_excel_path: str | Path = "",
    update_names: bool | None = None,
    update_main_price: bool | None = None,
    update_discount_system: bool | None = None,
    write_not_found_report: bool | None = None,
) -> None:
    APP_DATA_DIR.mkdir(parents=True, exist_ok=True)
    current = load_saved_paths()
    image_folder_text = str(image_folder).strip()
    excel_path_text = str(excel_path).strip()
    names_excel_path_text = str(names_excel_path).strip()
    if image_folder_text:
        current["image_folder"] = image_folder_text if Path(image_folder_text).is_dir() else ""
    if excel_path_text:
        current["excel_path"] = excel_path_text if Path(excel_path_text).is_file() else ""
    if names_excel_path_text:
        current["names_excel_path"] = names_excel_path_text if Path(names_excel_path_text).is_file() else ""
    if update_names is not None:
        current["update_names"] = bool(update_names)
    if update_main_price is not None:
        current["update_main_price"] = bool(update_main_price)
    if update_discount_system is not None:
        current["update_discount_system"] = bool(update_discount_system)
    if write_not_found_report is not None:
        current["write_not_found_report"] = bool(write_not_found_report)
    with USER_SETTINGS_FILE.open("w", encoding="utf-8") as handle:
        json.dump(current, handle, ensure_ascii=False, indent=2)


def install_text_editing_shortcuts(root: tk.Misc) -> None:
    for widget_class in ("Entry", "TEntry", "Text"):
        root.bind_class(widget_class, "<Control-KeyPress>", lambda event: _handle_text_control_key(root, event))
        root.bind_class(widget_class, "<Shift-Insert>", lambda event: _paste_into_text_widget(root, event.widget))
        root.bind_class(widget_class, "<Button-3>", lambda event: _show_text_context_menu(root, event))


def _handle_text_control_key(root: tk.Misc, event) -> str | None:
    # Windows keycodes keep shortcuts working when the active keyboard layout is Russian.
    keycode = int(getattr(event, "keycode", 0) or 0)
    if keycode == 86:  # V
        return _paste_into_text_widget(root, event.widget)
    if keycode == 67:  # C
        return _copy_from_text_widget(root, event.widget)
    if keycode == 88:  # X
        return _cut_from_text_widget(root, event.widget)
    if keycode == 65:  # A
        return _select_all_text_widget(event.widget)
    return None


def _widget_is_text(widget) -> bool:
    try:
        return widget.winfo_class() == "Text"
    except tk.TclError:
        return False


def _widget_is_readonly(widget) -> bool:
    try:
        return str(widget.cget("state")) in {"readonly", "disabled"}
    except tk.TclError:
        return False


def _selection_present(widget) -> bool:
    try:
        if _widget_is_text(widget):
            widget.index("sel.first")
            widget.index("sel.last")
            return True
        return bool(widget.selection_present())
    except tk.TclError:
        return False


def _delete_selection(widget) -> None:
    try:
        if _widget_is_text(widget):
            widget.delete("sel.first", "sel.last")
        else:
            widget.delete("sel.first", "sel.last")
    except tk.TclError:
        pass


def _insert_text(widget, text: str) -> None:
    if _widget_is_readonly(widget):
        return
    try:
        widget.insert("insert", text)
    except tk.TclError:
        pass


def _selected_text(widget) -> str | None:
    try:
        if _widget_is_text(widget):
            return widget.get("sel.first", "sel.last")
        return widget.selection_get()
    except tk.TclError:
        return None


def _paste_into_text_widget(root: tk.Misc, widget) -> str:
    if _widget_is_readonly(widget):
        return "break"
    try:
        text = root.clipboard_get()
    except tk.TclError:
        return "break"
    if _selection_present(widget):
        _delete_selection(widget)
    _insert_text(widget, text)
    return "break"


def _copy_from_text_widget(root: tk.Misc, widget) -> str:
    text = _selected_text(widget)
    if text is None:
        return "break"
    root.clipboard_clear()
    root.clipboard_append(text)
    return "break"


def _cut_from_text_widget(root: tk.Misc, widget) -> str:
    if _widget_is_readonly(widget):
        return "break"
    text = _selected_text(widget)
    if text is None:
        return "break"
    root.clipboard_clear()
    root.clipboard_append(text)
    _delete_selection(widget)
    return "break"


def _select_all_text_widget(widget) -> str:
    try:
        if _widget_is_text(widget):
            widget.tag_add("sel", "1.0", "end-1c")
            widget.mark_set("insert", "end-1c")
        else:
            widget.select_range(0, "end")
            widget.icursor("end")
    except tk.TclError:
        pass
    return "break"


def _show_text_context_menu(root: tk.Misc, event) -> str:
    widget = event.widget
    try:
        widget.focus_set()
    except tk.TclError:
        return "break"
    editable = not _widget_is_readonly(widget)
    menu = tk.Menu(widget, tearoff=0)
    menu.add_command(label="Вырезать", command=lambda: _cut_from_text_widget(root, widget), state=tk.NORMAL if editable else tk.DISABLED)
    menu.add_command(label="Копировать", command=lambda: _copy_from_text_widget(root, widget))
    menu.add_command(label="Вставить", command=lambda: _paste_into_text_widget(root, widget), state=tk.NORMAL if editable else tk.DISABLED)
    menu.add_separator()
    menu.add_command(label="Выделить всё", command=lambda: _select_all_text_widget(widget))
    menu.tk_popup(event.x_root, event.y_root)
    return "break"


def _copy_text_to_clipboard(root: tk.Misc, text: str) -> None:
    root.clipboard_clear()
    root.clipboard_append(text)


def install_treeview_copy_menu(root: tk.Misc, tree: ttk.Treeview) -> None:
    tree.bind("<Button-3>", lambda event: _show_treeview_context_menu(root, tree, event), add="+")


def install_listbox_copy_menu(root: tk.Misc, listbox: tk.Listbox) -> None:
    listbox.bind("<Button-3>", lambda event: _show_listbox_context_menu(root, listbox, event), add="+")


def _treeview_row_text(tree: ttk.Treeview, item_id: str) -> str:
    values = tree.item(item_id, "values")
    return "\t".join(str(value) for value in values)


def _treeview_cell_text(tree: ttk.Treeview, item_id: str, column_id: str) -> str:
    values = tree.item(item_id, "values")
    if not column_id.startswith("#"):
        return ""
    try:
        index = int(column_id[1:]) - 1
    except ValueError:
        return ""
    if index < 0 or index >= len(values):
        return ""
    return str(values[index])


def _copy_treeview_selected_rows(root: tk.Misc, tree: ttk.Treeview) -> None:
    rows = [_treeview_row_text(tree, item_id) for item_id in tree.selection()]
    rows = [row for row in rows if row]
    if rows:
        _copy_text_to_clipboard(root, "\n".join(rows))


def _copy_treeview_all_rows(root: tk.Misc, tree: ttk.Treeview) -> None:
    rows = [_treeview_row_text(tree, item_id) for item_id in tree.get_children()]
    rows = [row for row in rows if row]
    if rows:
        _copy_text_to_clipboard(root, "\n".join(rows))


def _show_treeview_context_menu(root: tk.Misc, tree: ttk.Treeview, event) -> str:
    row_id = tree.identify_row(event.y)
    column_id = tree.identify_column(event.x)
    if row_id and row_id not in tree.selection():
        tree.selection_set(row_id)
    cell_text = _treeview_cell_text(tree, row_id, column_id) if row_id else ""
    selected = tree.selection()
    menu = tk.Menu(tree, tearoff=0)
    menu.add_command(
        label="Копировать ячейку",
        command=lambda: _copy_text_to_clipboard(root, cell_text),
        state=tk.NORMAL if cell_text else tk.DISABLED,
    )
    menu.add_command(
        label="Копировать выделенные строки",
        command=lambda: _copy_treeview_selected_rows(root, tree),
        state=tk.NORMAL if selected else tk.DISABLED,
    )
    menu.add_command(
        label="Копировать все строки",
        command=lambda: _copy_treeview_all_rows(root, tree),
        state=tk.NORMAL if tree.get_children() else tk.DISABLED,
    )
    menu.tk_popup(event.x_root, event.y_root)
    return "break"


def _copy_listbox_selected_rows(root: tk.Misc, listbox: tk.Listbox) -> None:
    rows = [str(listbox.get(index)) for index in listbox.curselection()]
    if rows:
        _copy_text_to_clipboard(root, "\n".join(rows))


def _copy_listbox_all_rows(root: tk.Misc, listbox: tk.Listbox) -> None:
    rows = [str(listbox.get(index)) for index in range(listbox.size())]
    if rows:
        _copy_text_to_clipboard(root, "\n".join(rows))


def _show_listbox_context_menu(root: tk.Misc, listbox: tk.Listbox, event) -> str:
    index = listbox.nearest(event.y)
    if 0 <= index < listbox.size() and index not in listbox.curselection():
        listbox.selection_clear(0, tk.END)
        listbox.selection_set(index)
    selected = listbox.curselection()
    row_text = str(listbox.get(index)) if 0 <= index < listbox.size() else ""
    menu = tk.Menu(listbox, tearoff=0)
    menu.add_command(
        label="Копировать строку",
        command=lambda: _copy_text_to_clipboard(root, row_text),
        state=tk.NORMAL if row_text else tk.DISABLED,
    )
    menu.add_command(
        label="Копировать выделенные строки",
        command=lambda: _copy_listbox_selected_rows(root, listbox),
        state=tk.NORMAL if selected else tk.DISABLED,
    )
    menu.add_command(
        label="Копировать все строки",
        command=lambda: _copy_listbox_all_rows(root, listbox),
        state=tk.NORMAL if listbox.size() else tk.DISABLED,
    )
    menu.tk_popup(event.x_root, event.y_root)
    return "break"


class ExistingImagesDialog(tk.Toplevel):
    def __init__(self, parent: tk.Tk, items: list[ExistingImageItem]) -> None:
        super().__init__(parent)
        self.title("Товары с существующими изображениями")
        self.geometry("900x420")
        self.result: tuple[str, list[ExistingImageItem]] = ("cancel", [])
        self.items = items

        ttk.Label(self, text="У этих товаров уже есть изображения. Выберите действие.").pack(
            anchor="w", padx=12, pady=(12, 6)
        )

        list_frame = ttk.Frame(self)
        list_frame.pack(fill=tk.BOTH, expand=True, padx=12, pady=6)
        self.listbox = tk.Listbox(list_frame, selectmode=tk.EXTENDED)
        list_scrollbar = ttk.Scrollbar(list_frame, orient=tk.VERTICAL, command=self.listbox.yview)
        self.listbox.configure(yscrollcommand=list_scrollbar.set)
        self.listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        list_scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        install_listbox_copy_menu(parent, self.listbox)
        for item in items:
            self.listbox.insert(tk.END, f"{item.code} | {item.actual_name} | {item.product_url}")

        button_frame = ttk.Frame(self)
        button_frame.pack(fill=tk.X, padx=12, pady=12)
        ttk.Button(button_frame, text="Пропустить все", command=self._skip).pack(side=tk.LEFT)
        ttk.Button(button_frame, text="Заменить выбранные", command=self._replace_selected).pack(side=tk.LEFT, padx=8)
        ttk.Button(button_frame, text="Заменить все", command=self._replace_all).pack(side=tk.LEFT)

        self.protocol("WM_DELETE_WINDOW", self._cancel)
        self.transient(parent)
        self.grab_set()
        show_dialog_on_top(self, parent)

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

    def _cancel(self) -> None:
        self.result = ("cancel", [])
        self.destroy()


class ChangedCatalogDialog(tk.Toplevel):
    def __init__(self, parent: tk.Tk, changes: list[dict]) -> None:
        super().__init__(parent)
        self.title("Измененные товары")
        self.geometry("1180x520")
        self.result: tuple[str, set[str]] = ("cancel", set())
        self.changes = changes

        ttk.Label(
            self,
            text="Найдены товары, которые отличаются от предыдущего успешного прохода. Выберите строки для обработки.",
        ).pack(anchor="w", padx=12, pady=(12, 6))

        columns = ("number", "code", "change", "name", "price", "availability")
        tree_frame = ttk.Frame(self)
        tree_frame.pack(fill=tk.BOTH, expand=True, padx=12, pady=6)
        self.tree = ttk.Treeview(tree_frame, columns=columns, show="headings", selectmode="extended")
        tree_scrollbar = ttk.Scrollbar(tree_frame, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscrollcommand=tree_scrollbar.set)
        self.tree.heading("number", text="№")
        self.tree.heading("code", text="Артикул")
        self.tree.heading("change", text="Изменение")
        self.tree.heading("name", text="Название")
        self.tree.heading("price", text="Цена")
        self.tree.heading("availability", text="Наличие")
        self.tree.column("number", width=50, anchor="center", stretch=False)
        self.tree.column("code", width=130, anchor="w")
        self.tree.column("change", width=130, anchor="w")
        self.tree.column("name", width=420, anchor="w")
        self.tree.column("price", width=180, anchor="w")
        self.tree.column("availability", width=220, anchor="w")
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        tree_scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        install_treeview_copy_menu(parent, self.tree)

        for index, change in enumerate(changes, start=1):
            self.tree.insert(
                "",
                tk.END,
                iid=change["code"],
                values=(
                    index,
                    change["code"],
                    change["change"],
                    change["name"],
                    change["price"],
                    change["availability"],
                ),
            )

        button_frame = ttk.Frame(self)
        button_frame.pack(fill=tk.X, padx=12, pady=12)
        ttk.Button(button_frame, text="Запустить выбранные", command=self._run_selected).pack(side=tk.LEFT)
        ttk.Button(button_frame, text="Запустить все", command=self._run_all).pack(side=tk.LEFT, padx=8)
        ttk.Button(button_frame, text="Отмена", command=self._cancel).pack(side=tk.RIGHT)

        self.protocol("WM_DELETE_WINDOW", self._cancel)
        self.transient(parent)
        self.grab_set()
        show_dialog_on_top(self, parent)

    def _run_selected(self) -> None:
        selected = set(self.tree.selection())
        if not selected:
            messagebox.showinfo("Товары не выбраны", "Выберите одну или несколько строк в таблице.", parent=self)
            return
        self.result = ("selected", selected)
        self.destroy()

    def _run_all(self) -> None:
        self.result = ("all", {change["code"] for change in self.changes})
        self.destroy()

    def _cancel(self) -> None:
        self.result = ("cancel", set())
        self.destroy()


class DeferredUploadsDialog(tk.Toplevel):
    def __init__(
        self,
        parent: tk.Tk,
        items: list[DeferredUploadItem],
        open_callback,
        upload_callback,
    ) -> None:
        super().__init__(parent)
        self.title("Товары с ошибкой сохранения")
        self.geometry("1050x520")
        self.items: list[tuple[str, DeferredUploadItem]] = [
            (f"deferred-{index}", item) for index, item in enumerate(items)
        ]
        self.open_callback = open_callback
        self.upload_callback = upload_callback
        self.item_keys: dict[str, DeferredUploadItem] = {}
        self.batch_total = 0
        self.batch_processed = 0

        ttk.Label(
            self,
            text="Эти товары были пропущены из-за ошибки на сайте. Откройте карточку, заполните нужные поля, затем загрузите изображение для выбранной строки.",
        ).pack(anchor="w", padx=12, pady=(12, 6))

        columns = ("number", "code", "name", "error", "url")
        tree_frame = ttk.Frame(self)
        tree_frame.pack(fill=tk.BOTH, expand=True, padx=12, pady=6)
        self.tree = ttk.Treeview(tree_frame, columns=columns, show="headings", selectmode="extended")
        tree_scrollbar = ttk.Scrollbar(tree_frame, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscrollcommand=tree_scrollbar.set)
        self.tree.heading("number", text="№")
        self.tree.heading("code", text="Артикул")
        self.tree.heading("name", text="Название")
        self.tree.heading("error", text="Ошибка")
        self.tree.heading("url", text="Карточка")
        self.tree.column("number", width=50, anchor="center", stretch=False)
        self.tree.column("code", width=130, anchor="w")
        self.tree.column("name", width=260, anchor="w")
        self.tree.column("error", width=330, anchor="w")
        self.tree.column("url", width=280, anchor="w")
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        tree_scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        install_treeview_copy_menu(parent, self.tree)
        self._fill_table()

        self.status_var = tk.StringVar(value="")
        ttk.Label(self, textvariable=self.status_var).pack(fill=tk.X, padx=12, pady=(0, 6))
        self.progress_var = tk.StringVar(value="")
        ttk.Label(self, textvariable=self.progress_var).pack(fill=tk.X, padx=12, pady=(0, 2))
        self.progress_bar = ttk.Progressbar(self, mode="determinate", maximum=1, value=0)
        self.progress_bar.pack(fill=tk.X, padx=12, pady=(0, 6))

        button_frame = ttk.Frame(self)
        button_frame.pack(fill=tk.X, padx=12, pady=12)
        self.open_button = ttk.Button(button_frame, text="Открыть карточку", command=self._open_selected)
        self.open_button.pack(side=tk.LEFT)
        self.upload_button = ttk.Button(button_frame, text="Повторить обработку", command=self._upload_selected)
        self.upload_button.pack(side=tk.LEFT, padx=8)
        ttk.Button(button_frame, text="Закрыть", command=self.destroy).pack(side=tk.RIGHT)

        self.transient(parent)
        show_dialog_on_top(self, parent)

    def _fill_table(self) -> None:
        for row_id in self.tree.get_children():
            self.tree.delete(row_id)
        self.item_keys.clear()
        for row_number, (key, item) in enumerate(self.items, start=1):
            self.item_keys[key] = item
            self.tree.insert(
                "",
                tk.END,
                iid=key,
                values=(row_number, item.code, item.actual_name or item.expected_name, item.message, item.product_url),
            )

    def selected_items(self) -> list[tuple[str, DeferredUploadItem]]:
        selected = self.tree.selection()
        if not selected:
            messagebox.showinfo("Товары не выбраны", "Выберите одну или несколько строк в таблице.")
            return []
        result = []
        for key in selected:
            item = self.item_keys.get(key)
            if item:
                result.append((key, item))
        return result

    def _open_selected(self) -> None:
        selected = self.selected_items()
        if selected:
            self.open_callback(selected)

    def _upload_selected(self) -> None:
        selected = self.selected_items()
        if selected:
            self.start_progress(len(selected))
            self.upload_callback(selected)

    def start_progress(self, total: int) -> None:
        self.batch_total = total
        self.batch_processed = 0
        self.progress_bar.configure(maximum=max(1, total), value=0)
        self.progress_var.set(f"Обработано 0 из {total}")
        self.set_busy(f"Загружается изображений: {total}")

    def update_progress(self, processed: int, total: int, code: str) -> None:
        self.batch_total = total
        self.batch_processed = processed
        self.progress_bar.configure(maximum=max(1, total), value=processed)
        self.progress_var.set(f"Обработано {processed} из {total}. Текущий артикул: {code}")

    def set_busy(self, message: str) -> None:
        self.status_var.set(message)
        self.upload_button.configure(state=tk.DISABLED)

    def set_ready(self, message: str) -> None:
        self.status_var.set(message)
        self.upload_button.configure(state=tk.NORMAL)

    def mark_uploaded(self, uploaded_keys: set[str]) -> None:
        self.items = [(key, item) for key, item in self.items if key not in uploaded_keys]
        self._fill_table()
        self.set_ready(f"Изображений загружено: {len(uploaded_keys)}")
        if not self.items:
            self.status_var.set("Все отложенные изображения обработаны.")


class ChangedCatalogDialog(tk.Toplevel):
    def __init__(self, parent: tk.Tk, changes: list[dict]) -> None:
        super().__init__(parent)
        self.title("Измененные товары")
        self.geometry("1380x520")
        self.result: tuple[str, set[str]] = ("cancel", set())
        self.changes = changes

        ttk.Label(
            self,
            text="Найдены товары, которые отличаются от предыдущего успешного прохода. Выберите строки для обработки.",
        ).pack(anchor="w", padx=12, pady=(12, 6))

        columns = ("number", "code", "change", "name", "price", "discounts", "availability")
        tree_frame = ttk.Frame(self)
        tree_frame.pack(fill=tk.BOTH, expand=True, padx=12, pady=6)
        self.tree = ttk.Treeview(tree_frame, columns=columns, show="headings", selectmode="extended")
        tree_scrollbar = ttk.Scrollbar(tree_frame, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscrollcommand=tree_scrollbar.set)
        self.tree.heading("number", text="№")
        self.tree.heading("code", text="Артикул")
        self.tree.heading("change", text="Изменение")
        self.tree.heading("name", text="Наименование")
        self.tree.heading("price", text="Цена")
        self.tree.heading("discounts", text="Система скидок")
        self.tree.heading("availability", text="Наличие")
        self.tree.column("number", width=50, anchor="center", stretch=False)
        self.tree.column("code", width=130, anchor="w")
        self.tree.column("change", width=140, anchor="w")
        self.tree.column("name", width=360, anchor="w")
        self.tree.column("price", width=160, anchor="w")
        self.tree.column("discounts", width=280, anchor="w")
        self.tree.column("availability", width=180, anchor="w")
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        tree_scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        install_treeview_copy_menu(parent, self.tree)

        for index, change in enumerate(changes, start=1):
            self.tree.insert(
                "",
                tk.END,
                iid=change["code"],
                values=(
                    index,
                    change["code"],
                    change["change"],
                    change["name"],
                    change["price"],
                    change.get("discounts", ""),
                    change["availability"],
                ),
            )

        button_frame = ttk.Frame(self)
        button_frame.pack(fill=tk.X, padx=12, pady=12)
        ttk.Button(button_frame, text="Отмена", command=self._cancel).pack(side=tk.RIGHT)
        ttk.Button(button_frame, text="Выбрать все", command=self._select_all).pack(side=tk.LEFT)
        ttk.Button(button_frame, text="Продолжить со всеми", command=self._process_all).pack(side=tk.LEFT, padx=8)
        ttk.Button(button_frame, text="Обработать выбранные", command=self._process_selected).pack(side=tk.LEFT)

        self.protocol("WM_DELETE_WINDOW", self._cancel)
        self.transient(parent)
        self.grab_set()
        show_dialog_on_top(self, parent)

    def _selected_codes(self) -> set[str]:
        return {str(item_id) for item_id in self.tree.selection()}

    def _select_all(self) -> None:
        self.tree.selection_set(self.tree.get_children(""))

    def _process_selected(self) -> None:
        selected = self._selected_codes()
        if not selected:
            messagebox.showinfo("Товары не выбраны", "Выберите одну или несколько строк в таблице.", parent=self)
            return
        self.result = ("selected", selected)
        self.destroy()

    def _process_all(self) -> None:
        self.result = ("all", {str(change["code"]) for change in self.changes})
        self.destroy()

    def _cancel(self) -> None:
        self.result = ("cancel", set())
        self.destroy()


def show_dialog_on_top(dialog: tk.Toplevel, parent: tk.Tk) -> None:
    dialog.update_idletasks()
    parent.update_idletasks()
    parent_x = parent.winfo_rootx()
    parent_y = parent.winfo_rooty()
    parent_width = max(1, parent.winfo_width())
    parent_height = max(1, parent.winfo_height())
    dialog_width = max(1, dialog.winfo_width())
    dialog_height = max(1, dialog.winfo_height())
    x = parent_x + max(0, (parent_width - dialog_width) // 2)
    y = parent_y + max(0, (parent_height - dialog_height) // 2)
    dialog.geometry(f"+{x}+{y}")
    dialog.deiconify()
    dialog.lift()
    dialog.focus_force()
    dialog.attributes("-topmost", True)
    dialog.after(800, lambda: dialog.attributes("-topmost", False) if dialog.winfo_exists() else None)


def catalog_snapshot(
    catalog: dict[str, CatalogItem],
    name_catalog: dict[str, object] | None = None,
) -> dict[str, dict[str, str]]:
    snapshot: dict[str, dict[str, str]] = {}
    for code, item in catalog.items():
        name_item = None
        if name_catalog:
            for key in code_lookup_keys(code):
                name_item = name_catalog.get(key)
                if name_item is not None:
                    break
        snapshot[code] = {
            "name": item.name or "",
            "price": item.price or "",
            "wholesale_price": item.wholesale_price or "",
            "sko_price": item.sko_price or "",
            "availability": item.availability or "",
            "name_ru": getattr(name_item, "name_ru", None) or "",
            "name_kz": getattr(name_item, "name_kz", None) or "",
        }
    return snapshot


def load_catalog_cache() -> dict | None:
    try:
        if not CATALOG_CACHE_FILE.is_file():
            return None
        with CATALOG_CACHE_FILE.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def save_catalog_cache(
    excel_path: Path,
    catalog: dict[str, CatalogItem],
    name_catalog: dict[str, object] | None = None,
) -> None:
    APP_DATA_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "excel_path": str(excel_path.resolve()),
        "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "products": catalog_snapshot(catalog, name_catalog),
    }
    with CATALOG_CACHE_FILE.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)


def save_catalog_cache_entries(
    excel_path: Path,
    catalog: dict[str, CatalogItem],
    codes: set[str],
    name_catalog: dict[str, object] | None = None,
) -> None:
    if not codes:
        return
    APP_DATA_DIR.mkdir(parents=True, exist_ok=True)
    cache = load_catalog_cache() or {}
    products = cache.get("products", {})
    if not isinstance(products, dict):
        products = {}
    snapshot = catalog_snapshot(catalog, name_catalog)
    for code in codes:
        if code in snapshot:
            products[code] = snapshot[code]
    cache["excel_path"] = str(excel_path.resolve())
    cache["saved_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    cache["products"] = products
    with CATALOG_CACHE_FILE.open("w", encoding="utf-8") as handle:
        json.dump(cache, handle, ensure_ascii=False, indent=2)


def catalog_changes(catalog: dict[str, CatalogItem], cache: dict | None) -> list[dict]:
    previous = cache.get("products", {}) if cache else {}
    if not isinstance(previous, dict):
        previous = {}
    current = catalog_snapshot(catalog)
    changes: list[dict] = []
    for code, values in sorted(current.items(), key=lambda item: item[0].lower()):
        old = previous.get(code)
        if not isinstance(old, dict):
            changes.append(
                {
                    "code": code,
                    "change": "новый",
                    "name": values["name"],
                    "price": values["price"],
                    "availability": values["availability"],
                }
            )
            continue
        changed_fields = [field for field in ("name", "price", "availability") if str(old.get(field, "")) != values[field]]
        if not changed_fields:
            continue
        changes.append(
            {
                "code": code,
                "change": ", ".join(changed_fields),
                "name": f"{old.get('name', '')} -> {values['name']}",
                "price": f"{old.get('price', '')} -> {values['price']}",
                "availability": f"{old.get('availability', '')} -> {values['availability']}",
            }
        )
    return changes


def catalog_snapshot(
    catalog: dict[str, CatalogItem],
    name_catalog: dict[str, object] | None = None,
) -> dict[str, dict[str, str]]:
    snapshot: dict[str, dict[str, str]] = {}
    for code, item in catalog.items():
        name_item = None
        if name_catalog:
            for key in code_lookup_keys(code):
                name_item = name_catalog.get(key)
                if name_item is not None:
                    break
        snapshot[code] = {
            "name": item.name or "",
            "price": item.price or "",
            "wholesale_price": item.wholesale_price or "",
            "sko_price": item.sko_price or "",
            "availability": item.availability or "",
            "name_ru": getattr(name_item, "name_ru", None) or "",
            "name_kz": getattr(name_item, "name_kz", None) or "",
        }
    return snapshot


def save_catalog_cache(
    excel_path: Path,
    catalog: dict[str, CatalogItem],
    name_catalog: dict[str, object] | None = None,
) -> None:
    APP_DATA_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "excel_path": str(excel_path.resolve()),
        "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "products": catalog_snapshot(catalog, name_catalog),
    }
    with CATALOG_CACHE_FILE.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)


def save_catalog_cache_entries(
    excel_path: Path,
    catalog: dict[str, CatalogItem],
    codes: set[str],
    name_catalog: dict[str, object] | None = None,
) -> None:
    if not codes:
        return
    APP_DATA_DIR.mkdir(parents=True, exist_ok=True)
    cache = load_catalog_cache() or {}
    products = cache.get("products", {})
    if not isinstance(products, dict):
        products = {}
    snapshot = catalog_snapshot(catalog, name_catalog)
    for code in codes:
        if code in snapshot:
            products[code] = snapshot[code]
    cache["excel_path"] = str(excel_path.resolve())
    cache["saved_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    cache["products"] = products
    with CATALOG_CACHE_FILE.open("w", encoding="utf-8") as handle:
        json.dump(cache, handle, ensure_ascii=False, indent=2)


def catalog_changes(
    catalog: dict[str, CatalogItem],
    cache: dict | None,
    name_catalog: dict[str, object] | None = None,
    include_names: bool = True,
    include_main_price: bool = True,
    include_discount_system: bool = True,
    include_availability: bool = True,
) -> list[dict]:
    previous = cache.get("products", {}) if cache else {}
    if not isinstance(previous, dict):
        previous = {}
    current = catalog_snapshot(catalog, name_catalog)
    changes: list[dict] = []
    for code, values in sorted(current.items(), key=lambda item: item[0].lower()):
        old = previous.get(code)
        if not isinstance(old, dict):
            changes.append(
                {
                    "code": code,
                    "change": "new",
                    "name": values["name"],
                    "price": values["price"],
                    "discounts": f"Опт: {values['wholesale_price']} | СКО: {values['sko_price']}",
                    "availability": values["availability"],
                }
            )
            continue

        changed_fields: list[str] = []
        name_changed = include_names and (
            str(old.get("name_ru", "")) != values["name_ru"]
            or str(old.get("name_kz", "")) != values["name_kz"]
        )
        if name_changed:
            changed_fields.append("names")

        main_price_changed = include_main_price and str(old.get("price", "")) != values["price"]
        if main_price_changed:
            changed_fields.append("price")

        discounts_changed = include_discount_system and (
            str(old.get("wholesale_price", "")) != values["wholesale_price"]
            or str(old.get("sko_price", "")) != values["sko_price"]
        )
        if discounts_changed:
            changed_fields.append("discounts")

        availability_changed = include_availability and str(old.get("availability", "")) != values["availability"]
        if availability_changed:
            changed_fields.append("availability")

        if not changed_fields:
            continue

        if name_changed:
            name_text = (
                f"RU: {old.get('name_ru', '')} -> {values['name_ru']} | "
                f"KZ: {old.get('name_kz', '')} -> {values['name_kz']}"
            )
        else:
            name_text = values["name"]

        if discounts_changed:
            discounts_text = (
                f"Опт: {old.get('wholesale_price', '')} -> {values['wholesale_price']} | "
                f"СКО: {old.get('sko_price', '')} -> {values['sko_price']}"
            )
        else:
            discounts_text = f"Опт: {values['wholesale_price']} | СКО: {values['sko_price']}"

        changes.append(
            {
                "code": code,
                "change": ", ".join(changed_fields),
                "name": name_text,
                "price": f"{old.get('price', '')} -> {values['price']}",
                "discounts": discounts_text,
                "availability": f"{old.get('availability', '')} -> {values['availability']}",
            }
        )
    return changes


def format_eta(seconds: float) -> str:
    if seconds <= 0:
        return "меньше минуты"
    minutes = max(1, int(round(seconds / 60)))
    hours, mins = divmod(minutes, 60)
    if hours and mins:
        return f"{hours} ч {mins} мин"
    if hours:
        return f"{hours} ч"
    return f"{mins} мин"


def _image_path_from_text(value: object) -> Path | None:
    text = "" if value is None else str(value).strip()
    return Path(text) if text else None


def _existing_item_key(item: ExistingImageItem) -> str:
    return f"{item.code}|{item.product_url}|{item.image_path or ''}"


def _deferred_item_key(item: DeferredUploadItem) -> str:
    return f"{item.code}|{item.product_url}|{item.image_path or ''}|{item.message}"


def _existing_item_to_dict(item: ExistingImageItem) -> dict:
    return {
        "code": item.code,
        "image_path": str(item.image_path) if item.image_path else "",
        "product_url": item.product_url,
        "expected_name": item.expected_name,
        "actual_name": item.actual_name,
        "price": item.price,
        "wholesale_price": item.wholesale_price,
        "sko_price": item.sko_price,
        "name_ru": item.name_ru,
        "name_kz": item.name_kz,
        "availability": item.availability,
    }


def _deferred_item_to_dict(item: DeferredUploadItem) -> dict:
    return {
        "code": item.code,
        "image_path": str(item.image_path) if item.image_path else "",
        "product_url": item.product_url,
        "expected_name": item.expected_name,
        "actual_name": item.actual_name,
        "message": item.message,
        "price": item.price,
        "wholesale_price": item.wholesale_price,
        "sko_price": item.sko_price,
        "name_ru": item.name_ru,
        "name_kz": item.name_kz,
        "availability": item.availability,
        "image_upload_required": item.image_upload_required,
    }


def _existing_item_from_dict(data: dict) -> ExistingImageItem:
    return ExistingImageItem(
        code=str(data.get("code") or ""),
        image_path=_image_path_from_text(data.get("image_path")),
        product_url=str(data.get("product_url") or ""),
        expected_name=str(data.get("expected_name") or ""),
        actual_name=str(data.get("actual_name") or ""),
        price=data.get("price"),
        wholesale_price=data.get("wholesale_price"),
        sko_price=data.get("sko_price"),
        name_ru=data.get("name_ru"),
        name_kz=data.get("name_kz"),
        availability=data.get("availability"),
    )


def _deferred_item_from_dict(data: dict) -> DeferredUploadItem:
    return DeferredUploadItem(
        code=str(data.get("code") or ""),
        image_path=_image_path_from_text(data.get("image_path")),
        product_url=str(data.get("product_url") or ""),
        expected_name=str(data.get("expected_name") or ""),
        actual_name=str(data.get("actual_name") or ""),
        message=str(data.get("message") or ""),
        price=data.get("price"),
        wholesale_price=data.get("wholesale_price"),
        sko_price=data.get("sko_price"),
        name_ru=data.get("name_ru"),
        name_kz=data.get("name_kz"),
        availability=data.get("availability"),
        image_upload_required=bool(data.get("image_upload_required", True)),
    )


def _load_json_list(path: Path) -> list[dict]:
    try:
        if not path.is_file():
            return []
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def load_existing_queue() -> list[ExistingImageItem]:
    items = []
    for data in _load_json_list(EXISTING_QUEUE_FILE):
        try:
            item = _existing_item_from_dict(data)
        except Exception:
            continue
        if item.code and item.product_url:
            items.append(item)
    return items


def load_deferred_queue() -> list[DeferredUploadItem]:
    items = []
    for data in _load_json_list(DEFERRED_QUEUE_FILE):
        try:
            item = _deferred_item_from_dict(data)
        except Exception:
            continue
        if item.code and item.product_url:
            items.append(item)
    return items


def save_existing_queue(items: list[ExistingImageItem]) -> None:
    APP_DATA_DIR.mkdir(parents=True, exist_ok=True)
    unique = {_existing_item_key(item): item for item in items}
    with EXISTING_QUEUE_FILE.open("w", encoding="utf-8") as handle:
        json.dump([_existing_item_to_dict(item) for item in unique.values()], handle, ensure_ascii=False, indent=2)


def save_deferred_queue(items: list[DeferredUploadItem]) -> None:
    APP_DATA_DIR.mkdir(parents=True, exist_ok=True)
    unique = {_deferred_item_key(item): item for item in items}
    with DEFERRED_QUEUE_FILE.open("w", encoding="utf-8") as handle:
        json.dump([_deferred_item_to_dict(item) for item in unique.values()], handle, ensure_ascii=False, indent=2)


def remove_existing_queue_items(processed: list[ExistingImageItem]) -> None:
    processed_keys = {_existing_item_key(item) for item in processed}
    remaining = [item for item in load_existing_queue() if _existing_item_key(item) not in processed_keys]
    save_existing_queue(remaining)


def remove_deferred_queue_items(processed: list[DeferredUploadItem]) -> None:
    processed_keys = {_deferred_item_key(item) for item in processed}
    remaining = [item for item in load_deferred_queue() if _deferred_item_key(item) not in processed_keys]
    save_deferred_queue(remaining)


def append_existing_queue(items: list[ExistingImageItem]) -> None:
    if items:
        save_existing_queue(load_existing_queue() + items)


def append_deferred_queue(items: list[DeferredUploadItem]) -> None:
    if items:
        save_deferred_queue(load_deferred_queue() + items)


CATALOG_CACHE_SUCCESS_STATUSES = {
    "skipped_actual_list_row",
    "fields_synced",
    "synced_without_image",
    "skipped_existing_image",
    "uploaded",
    "direct_link_replaced_image",
}


def successful_catalog_cache_codes(report_writer: ReportWriter, existing: list[ExistingImageItem]) -> set[str]:
    codes = {
        record.code
        for record in report_writer.records
        if record.code and record.status in CATALOG_CACHE_SUCCESS_STATUSES
    }
    codes.update(item.code for item in existing if item.code)
    return codes


def apply_name_catalog(images, name_catalog: dict[str, object]):
    if not name_catalog:
        return images
    enriched = []
    for image in images:
        catalog_item = None
        for key in code_lookup_keys(image.code):
            catalog_item = name_catalog.get(key)
            if catalog_item is not None:
                break
        if catalog_item is None:
            enriched.append(image)
            continue
        name_ru = getattr(catalog_item, "name_ru", None)
        name_kz = getattr(catalog_item, "name_kz", None)
        if not name_ru and not name_kz:
            enriched.append(image)
            continue
        enriched.append(replace(image, name_ru=name_ru or image.name_ru, name_kz=name_kz or image.name_kz))
    return enriched


def _parse_numeric_price(value: object) -> float | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    text = text.replace("\u00a0", "").replace(" ", "").replace(",", ".")
    try:
        return float(text)
    except ValueError:
        return None


def find_sko_price_warnings(catalog: dict[str, CatalogItem]) -> list[CatalogItem]:
    warnings: list[CatalogItem] = []
    seen_codes: set[str] = set()
    for item in catalog.values():
        if item.code in seen_codes:
            continue
        seen_codes.add(item.code)
        wholesale_price = _parse_numeric_price(item.wholesale_price)
        sko_price = _parse_numeric_price(item.sko_price)
        if wholesale_price is None or sko_price is None:
            continue
        if sko_price > wholesale_price:
            warnings.append(item)
    return warnings


class SkoPriceWarningDialog(tk.Toplevel):
    def __init__(self, parent: tk.Misc, items: list[CatalogItem]) -> None:
        super().__init__(parent)
        self.result = False
        self.title("Предупреждение по ценам")
        self.transient(parent)
        self.resizable(True, True)
        self.geometry("780x420")
        self.protocol("WM_DELETE_WINDOW", self._close)

        frame = ttk.Frame(self, padding=12)
        frame.pack(fill=tk.BOTH, expand=True)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(1, weight=1)

        ttk.Label(
            frame,
            text=(
                "Найдены записи, у которых 'СКО_цена' больше, чем 'Оптовая_цена'.\n"
                "Если продолжить, бот запустит обработку с текущими значениями."
            ),
            justify=tk.LEFT,
        ).grid(row=0, column=0, columnspan=2, sticky="w")

        columns = ("code", "name", "wholesale", "sko")
        tree = ttk.Treeview(frame, columns=columns, show="headings", height=14)
        tree.heading("code", text="Артикул")
        tree.heading("name", text="Наименование")
        tree.heading("wholesale", text="Оптовая цена")
        tree.heading("sko", text="СКО цена")
        tree.column("code", width=140, anchor="w")
        tree.column("name", width=350, anchor="w")
        tree.column("wholesale", width=120, anchor="center")
        tree.column("sko", width=120, anchor="center")

        scrollbar = ttk.Scrollbar(frame, orient=tk.VERTICAL, command=tree.yview)
        tree.configure(yscrollcommand=scrollbar.set)

        for item in items:
            tree.insert("", tk.END, values=(item.code, item.name, item.wholesale_price or "", item.sko_price or ""))

        tree.grid(row=1, column=0, sticky="nsew", pady=(10, 0))
        scrollbar.grid(row=1, column=1, sticky="ns", pady=(10, 0))

        button_row = ttk.Frame(frame)
        button_row.grid(row=2, column=0, columnspan=2, sticky="e", pady=(12, 0))
        ttk.Button(button_row, text="Продолжить", command=self._continue).pack(side=tk.RIGHT, padx=(8, 0))
        ttk.Button(button_row, text="Закрыть", command=self._close).pack(side=tk.RIGHT)

        self.bind("<Escape>", lambda _event: self._close())
        self.grab_set()
        self.wait_visibility()
        self.focus_set()

    def _continue(self) -> None:
        self.result = True
        self.destroy()

    def _close(self) -> None:
        self.result = False
        self.destroy()


class App(tk.Tk):
    def __init__(self) -> None:
        migrate_legacy_data()
        super().__init__()
        self.title("Satu Image Upload Bot")
        self.geometry("900x760")
        self.minsize(860, 720)
        self.resizable(True, True)

        self.status_queue: queue.Queue[tuple[str, str]] = queue.Queue()
        self.worker: threading.Thread | None = None
        self.bot: SatuImageBot | None = None
        self.pending_deferred_uploads: list[DeferredUploadItem] = []
        self.deferred_dialog: DeferredUploadsDialog | None = None
        self.resume_requested = False
        self.active_run_state: dict | None = None
        self.selected_changed_codes: set[str] | None = None
        self.save_catalog_cache_after_run = True
        self.current_excel_for_cache: Path | None = None
        self.current_catalog_for_cache: dict[str, CatalogItem] | None = None
        self.run_started_at: float | None = None
        self.authorization_event = threading.Event()
        self.current_url = ""
        saved_paths = load_saved_paths()

        self.image_folder_var = tk.StringVar(value=saved_paths["image_folder"])
        self.excel_path_var = tk.StringVar(value=saved_paths["excel_path"])
        self.names_excel_path_var = tk.StringVar(value=saved_paths["names_excel_path"])
        self.status_var = tk.StringVar(value="Готов к запуску.")
        self.url_var = tk.StringVar(value="")
        self.main_progress_var = tk.StringVar(value="")
        self.headless_var = tk.BooleanVar(value=False)
        self.compare_cache_var = tk.BooleanVar(value=False)
        self.update_names_var = tk.BooleanVar(value=bool(saved_paths.get("update_names", True)))
        self.update_main_price_var = tk.BooleanVar(value=bool(saved_paths.get("update_main_price", True)))
        self.update_discount_system_var = tk.BooleanVar(value=bool(saved_paths.get("update_discount_system", True)))
        self.write_not_found_report_var = tk.BooleanVar(value=bool(saved_paths.get("write_not_found_report", False)))
        self.direct_links_text: tk.Text | None = None

        install_text_editing_shortcuts(self)
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
        ttk.Label(main, text="Excel с RU/KZ наименованиями (необязательно)").grid(row=4, column=0, sticky="w")
        ttk.Entry(main, textvariable=self.names_excel_path_var).grid(row=5, column=0, sticky="ew", pady=(2, 10))
        ttk.Button(main, text="Выбрать", command=self._choose_names_excel).grid(row=5, column=1, padx=(8, 0), pady=(2, 10))

        ttk.Label(main, text="Точечные ссылки на товары для перезаписи изображений").grid(row=4, column=0, sticky="w")
        direct_links_frame = ttk.Frame(main)
        direct_links_frame.grid(row=5, column=0, columnspan=2, sticky="ew", pady=(2, 10))
        self.direct_links_text = tk.Text(direct_links_frame, height=4, wrap=tk.NONE)
        direct_links_scrollbar = ttk.Scrollbar(direct_links_frame, orient=tk.VERTICAL, command=self.direct_links_text.yview)
        self.direct_links_text.configure(yscrollcommand=direct_links_scrollbar.set)
        self.direct_links_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        direct_links_scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        ttk.Checkbutton(
            main,
            text="Фоновый режим: не показывать браузер",
            variable=self.headless_var,
        ).grid(row=6, column=0, columnspan=2, sticky="w", pady=(0, 10))

        ttk.Checkbutton(
            main,
            text="Сравнить с предыдущим успешным проходом",
            variable=self.compare_cache_var,
        ).grid(row=7, column=0, columnspan=2, sticky="w", pady=(0, 10))

        ttk.Label(main, text="Статус").grid(row=8, column=0, sticky="w")
        ttk.Label(main, textvariable=self.status_var, wraplength=700).grid(row=9, column=0, columnspan=2, sticky="ew", pady=(2, 10))

        ttk.Label(main, text="Текущий товар").grid(row=10, column=0, sticky="w")
        url_label = ttk.Label(main, textvariable=self.url_var, foreground="#0645ad", cursor="hand2", wraplength=700)
        url_label.grid(row=11, column=0, columnspan=2, sticky="ew", pady=(2, 12))
        url_label.bind("<Button-1>", lambda _event: self._open_current_url())

        ttk.Label(main, textvariable=self.main_progress_var).grid(row=12, column=0, columnspan=2, sticky="ew", pady=(0, 2))
        self.main_progress_bar = ttk.Progressbar(main, mode="determinate", maximum=1, value=0)
        self.main_progress_bar.grid(row=13, column=0, columnspan=2, sticky="ew", pady=(0, 12))

        buttons = ttk.Frame(main)
        buttons.grid(row=14, column=0, columnspan=2, sticky="w")
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
        ttk.Button(buttons, text="Открыть текущий товар", command=self._open_current_url).pack(side=tk.LEFT, padx=(8, 0))
        ttk.Button(buttons, text="Прошлые замены", command=self._open_saved_existing_queue).pack(side=tk.LEFT, padx=(8, 0))
        ttk.Button(buttons, text="Прошлые ошибки", command=self._open_saved_deferred_queue).pack(side=tk.LEFT, padx=(8, 0))

        main.columnconfigure(0, weight=1)

    def _choose_image_folder(self) -> None:
        folder = filedialog.askdirectory(title="Выберите папку с изображениями")
        if folder:
            self.image_folder_var.set(folder)
            save_saved_paths(image_folder=folder)

    def _choose_excel(self) -> None:
        path = filedialog.askopenfilename(
            title="Выберите Excel-файл",
            filetypes=(("Excel files", "*.xlsx *.xlsm"), ("All files", "*.*")),
        )
        if path:
            self.excel_path_var.set(path)
            save_saved_paths(excel_path=path)

    def _direct_product_urls(self) -> list[str]:
        if self.direct_links_text is None:
            return []
        text = self.direct_links_text.get("1.0", tk.END)
        urls: list[str] = []
        seen: set[str] = set()
        changed = False
        for line in text.splitlines():
            raw_url = line.strip()
            if not raw_url:
                continue
            url = normalize_direct_product_url(raw_url)
            changed = changed or url != raw_url
            if url in seen:
                changed = True
                continue
            seen.add(url)
            urls.append(url)
        if changed and self.direct_links_text is not None:
            self.direct_links_text.delete("1.0", tk.END)
            self.direct_links_text.insert("1.0", "\n".join(urls))
        return urls

    def _confirm_non_admin_direct_urls(self, direct_urls: list[str]) -> bool:
        non_admin_urls = [url for url in direct_urls if not is_admin_product_url(url)]
        if not non_admin_urls:
            return True
        preview = "\n".join(non_admin_urls[:10])
        if len(non_admin_urls) > 10:
            preview += f"\n... и еще {len(non_admin_urls) - 10}"
        return messagebox.askyesno(
            "Проверьте ссылки",
            "В списке есть ссылки не вида:\n"
            f"{ADMIN_PRODUCT_URL_PREFIX}\n\n"
            "Бот может открыть их некорректно.\n\n"
            f"{preview}\n\n"
            "Продолжить работу?",
        )

    def _start(self) -> None:
        if self.worker and self.worker.is_alive():
            return

        image_folder = Path(self.image_folder_var.get().strip())
        excel_path = Path(self.excel_path_var.get().strip())
        direct_urls = self._direct_product_urls()
        if direct_urls and not self._confirm_non_admin_direct_urls(direct_urls):
            return
        if not image_folder.is_dir():
            messagebox.showerror("Ошибка", "Выберите существующую папку с изображениями.")
            return
        if not excel_path.is_file():
            messagebox.showerror("Ошибка", "Выберите существующий Excel-файл.")
            return
        save_saved_paths(image_folder=image_folder, excel_path=excel_path)

        try:
            catalog = load_catalog(excel_path)
        except Exception as exc:
            messagebox.showerror("Ошибка", f"Не удалось прочитать Excel-файл:\n\n{exc}")
            return

        sko_warnings = find_sko_price_warnings(catalog)
        if sko_warnings:
            dialog = SkoPriceWarningDialog(self, sko_warnings)
            self.wait_window(dialog)
            if not dialog.result:
                self.status_var.set("Обработка отменена пользователем после проверки цен.")
                return

        sko_warnings = find_sko_price_warnings(catalog)
        if sko_warnings:
            dialog = SkoPriceWarningDialog(self, sko_warnings)
            self.wait_window(dialog)
            if not dialog.result:
                self.status_var.set("Обработка отменена пользователем после проверки цен.")
                return

        sko_warnings = find_sko_price_warnings(catalog)
        if sko_warnings:
            dialog = SkoPriceWarningDialog(self, sko_warnings)
            self.wait_window(dialog)
            if not dialog.result:
                self.status_var.set("Обработка отменена пользователем после проверки цен.")
                return

        selected_codes: set[str] | None = None
        save_cache_after_run = True
        if self.compare_cache_var.get() and not direct_urls:
            changes = catalog_changes(catalog, load_catalog_cache())
            if not changes:
                messagebox.showinfo("Изменений нет", "Новый Excel не отличается от предыдущего успешного прохода.")
                return
            self.status_var.set(f"Готовит таблицу измененных товаров: {len(changes)}")
            self.update_idletasks()
            dialog = ChangedCatalogDialog(self, changes)
            self.wait_window(dialog)
            action, selected_codes = dialog.result
            if action == "cancel":
                return
            save_cache_after_run = action == "all"

        resume_requested = False if direct_urls else self._ask_resume_previous_run(image_folder, excel_path)
        if resume_requested is None:
            return
        self.resume_requested = resume_requested
        self.selected_changed_codes = selected_codes
        self.save_catalog_cache_after_run = save_cache_after_run
        self.current_excel_for_cache = excel_path
        self.current_catalog_for_cache = catalog
        self.run_started_at = time.monotonic()
        headless = self.headless_var.get()
        self.start_button.configure(state=tk.DISABLED)
        self.stop_button.configure(state=tk.NORMAL)
        self.authorized_button.configure(state=tk.NORMAL)
        self.authorization_event.clear()
        self.status_var.set("Подготовка данных...")
        self.main_progress_var.set("")
        self.main_progress_bar.configure(maximum=1, value=0)

        self.worker = threading.Thread(
            target=self._run_worker,
            args=(image_folder, excel_path, headless, selected_codes, direct_urls),
            daemon=True,
        )
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

    def _image_key(self, path: str | Path, code: str) -> str:
        path_text = str(path).strip()
        if not path_text:
            return f"catalog|{code}"
        return f"{Path(path_text).resolve()}|{code}"

    def _load_run_state(self) -> dict | None:
        try:
            if not RUN_STATE_FILE.is_file():
                return None
            with RUN_STATE_FILE.open("r", encoding="utf-8") as handle:
                state = json.load(handle)
            if isinstance(state, dict):
                return state
        except Exception:
            return None
        return None

    def _save_run_state(self) -> None:
        if not self.active_run_state:
            return
        APP_DATA_DIR.mkdir(parents=True, exist_ok=True)
        with RUN_STATE_FILE.open("w", encoding="utf-8") as handle:
            json.dump(self.active_run_state, handle, ensure_ascii=False, indent=2)

    def _clear_run_state(self) -> None:
        self.active_run_state = None
        try:
            RUN_STATE_FILE.unlink(missing_ok=True)
        except Exception:
            pass

    def _state_matches_paths(self, state: dict, image_folder: Path, excel_path: Path) -> bool:
        try:
            return (
                Path(state.get("image_folder", "")).resolve() == image_folder.resolve()
                and Path(state.get("excel_path", "")).resolve() == excel_path.resolve()
            )
        except Exception:
            return False

    def _ask_resume_previous_run(self, image_folder: Path, excel_path: Path) -> bool | None:
        state = self._load_run_state()
        if not state or not self._state_matches_paths(state, image_folder, excel_path):
            self._clear_run_state()
            return False
        completed_count = len(state.get("completed", []))
        total_count = int(state.get("total", 0) or 0)
        answer = messagebox.askyesnocancel(
            "Найден незавершенный проход",
            (
                f"Найдено сохранение предыдущего прохода: обработано {completed_count} из {total_count}.\n\n"
                "Продолжить с места остановки?\n\n"
                "Да - продолжить.\n"
                "Нет - начать с нуля.\n"
                "Отмена - не запускать."
            ),
        )
        if answer is None:
            return None
        if answer:
            return True
        self._clear_run_state()
        return False

    def _prepare_run_state(self, image_folder: Path, excel_path: Path, images) -> set[str]:
        previous = self._load_run_state() if self.resume_requested else None
        completed = set()
        if previous and self._state_matches_paths(previous, image_folder, excel_path):
            completed = {str(item) for item in previous.get("completed", [])}
        self.active_run_state = {
            "image_folder": str(image_folder.resolve()),
            "excel_path": str(excel_path.resolve()),
            "total": len(images),
            "completed": sorted(completed),
        }
        self._save_run_state()
        return completed

    def _mark_image_completed(self, image_path: str, code: str) -> None:
        if not self.active_run_state:
            return
        completed = set(self.active_run_state.get("completed", []))
        completed.add(self._image_key(image_path, code))
        self.active_run_state["completed"] = sorted(completed)
        self._save_run_state()

    def _run_worker(
        self,
        image_folder: Path,
        excel_path: Path,
        headless: bool,
        selected_codes: set[str] | None,
        direct_urls: list[str] | None = None,
    ) -> None:
        awaiting_manual_decision = False
        try:
            catalog = load_catalog(excel_path)
            direct_urls = direct_urls or []
            if direct_urls:
                images = processing_items(image_folder, catalog)
                if not images:
                    self.status_queue.put(("Нет товаров для обработки.", ""))
                    return
                report_writer = ReportWriter(REPORT_DIR)
                self.bot = SatuImageBot(
                    PROFILE_DIR,
                    report_writer,
                    self._queue_status,
                    authorization_event=self.authorization_event,
                    headless=headless,
                )
                self.bot.replace_direct_links(direct_urls, {image.code: image for image in images})
                self.pending_deferred_uploads = list(self.bot.deferred_uploads)
                successful_codes = successful_catalog_cache_codes(report_writer, [])
                save_catalog_cache_entries(excel_path, catalog, successful_codes)
                self.status_queue.put((f"Точечная перезапись завершена. Отчет: {report_writer.csv_path}", ""))
                if self.pending_deferred_uploads:
                    append_deferred_queue(self.pending_deferred_uploads)
                    awaiting_manual_decision = True
                    self.status_queue.put(("__DEFERRED__", self.pending_deferred_uploads))  # type: ignore[arg-type]
                return

            images = processing_items(image_folder, catalog, selected_codes)
            if not images:
                self.status_queue.put(("Нет товаров для обработки.", ""))
                return
            completed_keys = self._prepare_run_state(image_folder, excel_path, images)
            if completed_keys:
                image_keys = {self._image_key(image.path, image.code) for image in images}
                matching_completed = completed_keys & image_keys
                if self.active_run_state is not None:
                    self.active_run_state["completed"] = sorted(matching_completed)
                    self._save_run_state()
                images = [
                    image
                    for image in images
                    if self._image_key(image.path, image.code) not in matching_completed
                ]
                skipped_count = len(matching_completed)
                self.status_queue.put((f"Продолжение прохода: уже обработано {skipped_count}, осталось {len(images)}.", ""))
            if not images:
                self.status_queue.put(("Все товары из сохраненного прохода уже обработаны.", ""))
                self._clear_run_state()
                return

            report_writer = ReportWriter(REPORT_DIR)
            self.bot = SatuImageBot(
                PROFILE_DIR,
                report_writer,
                self._queue_status,
                authorization_event=self.authorization_event,
                headless=headless,
            )
            existing = self.bot.run(images)
            self.pending_deferred_uploads = list(self.bot.deferred_uploads)
            self._clear_run_state()
            if selected_codes is not None:
                deferred_codes = {item.code for item in self.pending_deferred_uploads}
                successful_codes = successful_catalog_cache_codes(report_writer, existing)
                save_catalog_cache_entries(excel_path, catalog, (set(selected_codes) & successful_codes) - deferred_codes)
            elif self.save_catalog_cache_after_run and not self.pending_deferred_uploads:
                save_catalog_cache(excel_path, catalog)
            self.status_queue.put((f"Основной проход завершен. Отчет: {report_writer.csv_path}", ""))
            if existing:
                append_existing_queue(existing)
                awaiting_manual_decision = True
                self.status_queue.put(("__EXISTING__", existing))  # type: ignore[arg-type]
                return
            if self.pending_deferred_uploads:
                append_deferred_queue(self.pending_deferred_uploads)
                awaiting_manual_decision = True
                self.status_queue.put(("__DEFERRED__", self.pending_deferred_uploads))  # type: ignore[arg-type]
                return
        except StopRequested:
            self.status_queue.put(("Процесс остановлен пользователем.", ""))
        except Exception as exc:
            self.status_queue.put((f"Ошибка: {exc}", ""))
        finally:
            if not awaiting_manual_decision:
                self.status_queue.put(("__DONE__", ""))

    def _build_ui(self) -> None:
        main = ttk.Frame(self, padding=14)
        main.pack(fill=tk.BOTH, expand=True)

        ttk.Label(main, text="Папка с изображениями").grid(row=0, column=0, sticky="w")
        ttk.Entry(main, textvariable=self.image_folder_var).grid(row=1, column=0, sticky="ew", pady=(2, 10))
        ttk.Button(main, text="Выбрать", command=self._choose_image_folder).grid(row=1, column=1, padx=(8, 0), pady=(2, 10))

        ttk.Label(main, text="Excel с артикулами и названиями").grid(row=2, column=0, sticky="w")
        ttk.Entry(main, textvariable=self.excel_path_var).grid(row=3, column=0, sticky="ew", pady=(2, 10))
        ttk.Button(main, text="Выбрать", command=self._choose_excel).grid(row=3, column=1, padx=(8, 0), pady=(2, 10))

        ttk.Label(main, text="Excel с RU/KZ наименованиями (необязательно)").grid(row=4, column=0, sticky="w")
        ttk.Entry(main, textvariable=self.names_excel_path_var).grid(row=5, column=0, sticky="ew", pady=(2, 10))
        ttk.Button(main, text="Выбрать", command=self._choose_names_excel).grid(row=5, column=1, padx=(8, 0), pady=(2, 10))

        ttk.Label(main, text="Точечные ссылки на товары для перезаписи изображений").grid(row=6, column=0, sticky="w")
        direct_links_frame = ttk.Frame(main)
        direct_links_frame.grid(row=7, column=0, columnspan=2, sticky="ew", pady=(2, 10))
        self.direct_links_text = tk.Text(direct_links_frame, height=4, wrap=tk.NONE)
        direct_links_scrollbar = ttk.Scrollbar(direct_links_frame, orient=tk.VERTICAL, command=self.direct_links_text.yview)
        self.direct_links_text.configure(yscrollcommand=direct_links_scrollbar.set)
        self.direct_links_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        direct_links_scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        ttk.Checkbutton(main, text="Фоновый режим: не показывать браузер", variable=self.headless_var).grid(
            row=8, column=0, columnspan=2, sticky="w", pady=(0, 10)
        )
        ttk.Checkbutton(main, text="Сравнить с предыдущим успешным проходом", variable=self.compare_cache_var).grid(
            row=9, column=0, columnspan=2, sticky="w", pady=(0, 10)
        )

        ttk.Label(main, text="Статус").grid(row=10, column=0, sticky="w")
        ttk.Label(main, textvariable=self.status_var, wraplength=700).grid(row=11, column=0, columnspan=2, sticky="ew", pady=(2, 10))
        ttk.Label(main, text="Текущий товар").grid(row=12, column=0, sticky="w")
        url_label = ttk.Label(main, textvariable=self.url_var, foreground="#0645ad", cursor="hand2", wraplength=700)
        url_label.grid(row=13, column=0, columnspan=2, sticky="ew", pady=(2, 12))
        url_label.bind("<Button-1>", lambda _event: self._open_current_url())

        ttk.Label(main, textvariable=self.main_progress_var).grid(row=14, column=0, columnspan=2, sticky="ew", pady=(0, 2))
        self.main_progress_bar = ttk.Progressbar(main, mode="determinate", maximum=1, value=0)
        self.main_progress_bar.grid(row=15, column=0, columnspan=2, sticky="ew", pady=(0, 12))

        buttons = ttk.Frame(main)
        buttons.grid(row=16, column=0, columnspan=2, sticky="w")
        self.start_button = ttk.Button(buttons, text="Старт", command=self._start)
        self.start_button.pack(side=tk.LEFT)
        self.stop_button = ttk.Button(buttons, text="Стоп", command=self._stop, state=tk.DISABLED)
        self.stop_button.pack(side=tk.LEFT, padx=8)
        self.authorized_button = ttk.Button(buttons, text="Я авторизован", command=self._confirm_authorized, state=tk.DISABLED)
        self.authorized_button.pack(side=tk.LEFT)
        ttk.Button(buttons, text="Открыть текущий товар", command=self._open_current_url).pack(side=tk.LEFT, padx=(8, 0))
        ttk.Button(buttons, text="Прошлые замены", command=self._open_saved_existing_queue).pack(side=tk.LEFT, padx=(8, 0))
        ttk.Button(buttons, text="Прошлые ошибки", command=self._open_saved_deferred_queue).pack(side=tk.LEFT, padx=(8, 0))

        main.columnconfigure(0, weight=1)

    def _choose_names_excel(self) -> None:
        path = filedialog.askopenfilename(
            title="Выберите Excel-файл с RU/KZ наименованиями",
            filetypes=(("Excel files", "*.xlsx *.xlsm"), ("All files", "*.*")),
        )
        if path:
            self.names_excel_path_var.set(path)
            save_saved_paths(names_excel_path=path)

    def _load_optional_name_catalog(self, names_excel_path: Path | None) -> dict[str, object]:
        if names_excel_path is None:
            return {}
        return load_name_catalog(names_excel_path)

    def _start(self) -> None:
        if self.worker and self.worker.is_alive():
            return

        image_folder = Path(self.image_folder_var.get().strip())
        excel_path = Path(self.excel_path_var.get().strip())
        names_excel_text = self.names_excel_path_var.get().strip()
        names_excel_path = Path(names_excel_text) if names_excel_text else None
        direct_urls = self._direct_product_urls()
        if direct_urls and not self._confirm_non_admin_direct_urls(direct_urls):
            return
        if not image_folder.is_dir():
            messagebox.showerror("Ошибка", "Выберите существующую папку с изображениями.")
            return
        if not excel_path.is_file():
            messagebox.showerror("Ошибка", "Выберите существующий Excel-файл.")
            return
        if names_excel_path is not None and not names_excel_path.is_file():
            messagebox.showerror("Ошибка", "Файл с RU/KZ наименованиями не найден.")
            return
        save_saved_paths(image_folder=image_folder, excel_path=excel_path, names_excel_path=names_excel_path or "")

        try:
            catalog = load_catalog(excel_path)
            self._load_optional_name_catalog(names_excel_path)
        except Exception as exc:
            messagebox.showerror("Ошибка", f"Не удалось прочитать Excel-файл:\n\n{exc}")
            return

        selected_codes: set[str] | None = None
        save_cache_after_run = True
        if self.compare_cache_var.get() and not direct_urls:
            changes = catalog_changes(catalog, load_catalog_cache())
            if not changes:
                messagebox.showinfo("Изменений нет", "Новый Excel не отличается от предыдущего успешного прохода.")
                return
            self.status_var.set(f"Готовит таблицу измененных товаров: {len(changes)}")
            self.update_idletasks()
            dialog = ChangedCatalogDialog(self, changes)
            self.wait_window(dialog)
            action, selected_codes = dialog.result
            if action == "cancel":
                return
            save_cache_after_run = action == "all"

        resume_requested = False if direct_urls else self._ask_resume_previous_run(image_folder, excel_path)
        if resume_requested is None:
            return
        self.resume_requested = resume_requested
        self.selected_changed_codes = selected_codes
        self.save_catalog_cache_after_run = save_cache_after_run
        self.current_excel_for_cache = excel_path
        self.current_catalog_for_cache = catalog
        self.run_started_at = time.monotonic()
        headless = self.headless_var.get()
        self.start_button.configure(state=tk.DISABLED)
        self.stop_button.configure(state=tk.NORMAL)
        self.authorized_button.configure(state=tk.NORMAL)
        self.authorization_event.clear()
        self.status_var.set("Подготовка данных...")
        self.main_progress_var.set("")
        self.main_progress_bar.configure(maximum=1, value=0)

        self.worker = threading.Thread(
            target=self._run_worker,
            args=(image_folder, excel_path, names_excel_path, headless, selected_codes, direct_urls),
            daemon=True,
        )
        self.worker.start()

    def _start(self) -> None:
        if self.worker and self.worker.is_alive():
            return

        image_folder = Path(self.image_folder_var.get().strip())
        excel_path = Path(self.excel_path_var.get().strip())
        names_excel_text = self.names_excel_path_var.get().strip()
        names_excel_path = Path(names_excel_text) if names_excel_text else None
        direct_urls = self._direct_product_urls()
        if direct_urls and not self._confirm_non_admin_direct_urls(direct_urls):
            return
        if not image_folder.is_dir():
            messagebox.showerror("Ошибка", "Выберите существующую папку с изображениями.")
            return
        if not excel_path.is_file():
            messagebox.showerror("Ошибка", "Выберите существующий Excel-файл.")
            return
        if names_excel_path is not None and not names_excel_path.is_file():
            messagebox.showerror("Ошибка", "Файл с RU/KZ наименованиями не найден.")
            return
        save_saved_paths(image_folder=image_folder, excel_path=excel_path, names_excel_path=names_excel_path or "")

        try:
            catalog = load_catalog(excel_path)
            self._load_optional_name_catalog(names_excel_path)
        except Exception as exc:
            messagebox.showerror("Ошибка", f"Не удалось прочитать Excel-файл:\n\n{exc}")
            return

        sko_warnings = find_sko_price_warnings(catalog)
        if sko_warnings:
            dialog = SkoPriceWarningDialog(self, sko_warnings)
            self.wait_window(dialog)
            if not dialog.result:
                self.status_var.set("Обработка отменена пользователем после проверки цен.")
                return

        selected_codes: set[str] | None = None
        save_cache_after_run = True
        if self.compare_cache_var.get() and not direct_urls:
            changes = catalog_changes(catalog, load_catalog_cache())
            if not changes:
                messagebox.showinfo("Изменений нет", "Новый Excel не отличается от предыдущего успешного прохода.")
                return
            self.status_var.set(f"Готовит таблицу измененных товаров: {len(changes)}")
            self.update_idletasks()
            dialog = ChangedCatalogDialog(self, changes)
            self.wait_window(dialog)
            action, selected_codes = dialog.result
            if action == "cancel":
                return
            save_cache_after_run = action == "all"

        resume_requested = False if direct_urls else self._ask_resume_previous_run(image_folder, excel_path)
        if resume_requested is None:
            return
        self.resume_requested = resume_requested
        self.selected_changed_codes = selected_codes
        self.save_catalog_cache_after_run = save_cache_after_run
        self.current_excel_for_cache = excel_path
        self.current_catalog_for_cache = catalog
        self.run_started_at = time.monotonic()
        headless = self.headless_var.get()
        self.start_button.configure(state=tk.DISABLED)
        self.stop_button.configure(state=tk.NORMAL)
        self.authorized_button.configure(state=tk.NORMAL)
        self.authorization_event.clear()
        self.status_var.set("Подготовка данных...")
        self.main_progress_var.set("")
        self.main_progress_bar.configure(maximum=1, value=0)

        self.worker = threading.Thread(
            target=self._run_worker,
            args=(image_folder, excel_path, names_excel_path, headless, selected_codes, direct_urls),
            daemon=True,
        )
        self.worker.start()

    def _run_worker(
        self,
        image_folder: Path,
        excel_path: Path,
        names_excel_path: Path | None,
        headless: bool,
        selected_codes: set[str] | None,
        direct_urls: list[str] | None = None,
    ) -> None:
        awaiting_manual_decision = False
        try:
            catalog = load_catalog(excel_path)
            name_catalog = self._load_optional_name_catalog(names_excel_path)
            direct_urls = direct_urls or []
            if direct_urls:
                images = apply_name_catalog(processing_items(image_folder, catalog), name_catalog)
                if not images:
                    self.status_queue.put(("Нет товаров для обработки.", ""))
                    return
                report_writer = ReportWriter(REPORT_DIR)
                self.bot = SatuImageBot(
                    PROFILE_DIR,
                    report_writer,
                    self._queue_status,
                    authorization_event=self.authorization_event,
                    headless=headless,
                )
                self.bot.replace_direct_links(direct_urls, {image.code: image for image in images})
                self.pending_deferred_uploads = list(self.bot.deferred_uploads)
                successful_codes = successful_catalog_cache_codes(report_writer, [])
                save_catalog_cache_entries(excel_path, catalog, successful_codes)
                self.status_queue.put((f"Точечная перезапись завершена. Отчет: {report_writer.csv_path}", ""))
                if self.pending_deferred_uploads:
                    append_deferred_queue(self.pending_deferred_uploads)
                    awaiting_manual_decision = True
                    self.status_queue.put(("__DEFERRED__", self.pending_deferred_uploads))  # type: ignore[arg-type]
                return

            images = apply_name_catalog(processing_items(image_folder, catalog, selected_codes), name_catalog)
            if not images:
                self.status_queue.put(("Нет товаров для обработки.", ""))
                return
            completed_keys = self._prepare_run_state(image_folder, excel_path, images)
            if completed_keys:
                image_keys = {self._image_key(image.path, image.code) for image in images}
                matching_completed = completed_keys & image_keys
                if self.active_run_state is not None:
                    self.active_run_state["completed"] = sorted(matching_completed)
                    self._save_run_state()
                images = [
                    image
                    for image in images
                    if self._image_key(image.path, image.code) not in matching_completed
                ]
                skipped_count = len(matching_completed)
                self.status_queue.put((f"Продолжение прохода: уже обработано {skipped_count}, осталось {len(images)}.", ""))
            if not images:
                self.status_queue.put(("Все товары из сохраненного прохода уже обработаны.", ""))
                self._clear_run_state()
                return

            report_writer = ReportWriter(REPORT_DIR)
            self.bot = SatuImageBot(
                PROFILE_DIR,
                report_writer,
                self._queue_status,
                authorization_event=self.authorization_event,
                headless=headless,
            )
            existing = self.bot.run(images)
            self.pending_deferred_uploads = list(self.bot.deferred_uploads)
            self._clear_run_state()
            if selected_codes is not None:
                deferred_codes = {item.code for item in self.pending_deferred_uploads}
                successful_codes = successful_catalog_cache_codes(report_writer, existing)
                save_catalog_cache_entries(excel_path, catalog, (set(selected_codes) & successful_codes) - deferred_codes)
            elif self.save_catalog_cache_after_run and not self.pending_deferred_uploads:
                save_catalog_cache(excel_path, catalog)
            self.status_queue.put((f"Основной проход завершен. Отчет: {report_writer.csv_path}", ""))
            if existing:
                append_existing_queue(existing)
                awaiting_manual_decision = True
                self.status_queue.put(("__EXISTING__", existing))  # type: ignore[arg-type]
                return
            if self.pending_deferred_uploads:
                append_deferred_queue(self.pending_deferred_uploads)
                awaiting_manual_decision = True
                self.status_queue.put(("__DEFERRED__", self.pending_deferred_uploads))  # type: ignore[arg-type]
                return
        except StopRequested:
            self.status_queue.put(("Процесс остановлен пользователем.", ""))
        except Exception as exc:
            self.status_queue.put((f"Ошибка: {exc}", ""))
        finally:
            if not awaiting_manual_decision:
                self.status_queue.put(("__DONE__", ""))

    def _queue_status(self, status: str, url: str = "") -> None:
        if status == "__MAIN_PROGRESS__":
            try:
                data = json.loads(url)
                self._mark_image_completed(str(data["path"]), str(data["code"]))
            except Exception:
                pass
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
                    self.run_started_at = None
                    self.current_excel_for_cache = None
                    self.current_catalog_for_cache = None
                elif status == "__EXISTING__":
                    self._handle_existing_images(url)  # type: ignore[arg-type]
                elif status == "__AFTER_EXISTING__":
                    self._show_deferred_or_done()
                elif status == "__DEFERRED__":
                    self._handle_deferred_uploads(url)  # type: ignore[arg-type]
                elif status == "__MAIN_PROGRESS__":
                    self._handle_main_progress(url)
                elif status == "__DEFERRED_UPLOAD_PROGRESS__":
                    processed, total, code = url  # type: ignore[misc]
                    self._handle_deferred_upload_progress(processed, total, code)
                elif status == "__DEFERRED_UPLOAD_BATCH_RESULT__":
                    self._handle_deferred_upload_batch_result(url)  # type: ignore[arg-type]
                else:
                    self.status_var.set(status)
                    if isinstance(url, str) and url:
                        self.current_url = url
                        self.url_var.set(url)
        except queue.Empty:
            pass
        self.after(200, self._poll_status)

    def _handle_main_progress(self, payload: str) -> None:
        try:
            data = json.loads(payload)
            processed = int(data["processed"])
            total = int(data["total"])
            code = str(data["code"])
            image_path = str(data["path"])
        except Exception:
            return
        self._mark_image_completed(image_path, code)
        completed_count = len(self.active_run_state.get("completed", [])) if self.active_run_state else processed
        total_count = int(self.active_run_state.get("total", total)) if self.active_run_state else total
        self.main_progress_bar.configure(maximum=max(1, total_count), value=completed_count)
        eta_text = ""
        if self.run_started_at and completed_count > 0 and total_count > completed_count:
            elapsed = time.monotonic() - self.run_started_at
            remaining = (elapsed / completed_count) * (total_count - completed_count)
            eta_text = f". Примерно осталось: {format_eta(remaining)}"
        self.main_progress_var.set(
            f"Обработано {completed_count} из {total_count}. Текущий артикул: {code}{eta_text}"
        )

    def _handle_existing_images(self, items: list[ExistingImageItem]) -> None:
        if not items or not self.bot:
            return
        self.status_var.set(f"Готовит таблицу товаров с существующими изображениями: {len(items)}")
        self.update_idletasks()
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
        elif action == "skip":
            self.bot.mark_skipped_existing(items, status="skipped_by_user")
            remove_existing_queue_items(items)
            self.status_queue.put(("Товары с существующими изображениями пропущены.", ""))
            self.status_queue.put(("__AFTER_EXISTING__", ""))
        else:
            self.status_queue.put(("Окно замен закрыто. Очередь сохранена для доработки позже.", ""))
            self.status_queue.put(("__AFTER_EXISTING__", ""))

    def _replace_worker(self, bot: SatuImageBot, selected: list[ExistingImageItem]) -> None:
        try:
            bot.replace_existing(selected)
            skipped = [item for item in bot.existing_images if item not in selected]
            bot.mark_skipped_existing(skipped, status="skipped_by_user")
            remove_existing_queue_items(bot.existing_images)
            self.status_queue.put(("Замена выбранных изображений завершена.", ""))
        except StopRequested:
            self.status_queue.put(("Замена остановлена пользователем.", ""))
        except Exception as exc:
            self.status_queue.put((f"Ошибка при замене: {exc}", ""))
        finally:
            self.status_queue.put(("__AFTER_EXISTING__", ""))

    def _show_deferred_or_done(self) -> None:
        if self.pending_deferred_uploads:
            self.status_queue.put(("__DEFERRED__", self.pending_deferred_uploads))  # type: ignore[arg-type]
        else:
            self.status_queue.put(("__DONE__", ""))

    def _handle_deferred_uploads(self, items: list[DeferredUploadItem]) -> None:
        if not items or not self.bot:
            self.status_queue.put(("__DONE__", ""))
            return
        self.start_button.configure(state=tk.DISABLED)
        self.stop_button.configure(state=tk.DISABLED)
        self.authorized_button.configure(state=tk.DISABLED)
        self.status_var.set(f"Готовит таблицу товаров для ручной доработки: {len(items)}")
        self.update_idletasks()
        self.deferred_dialog = DeferredUploadsDialog(
            self,
            items,
            self._open_deferred_item,
            self._upload_deferred_item,
        )
        self.deferred_dialog.protocol("WM_DELETE_WINDOW", self._close_deferred_dialog)
        self.status_var.set(f"Есть товары для ручной доработки: {len(items)}")

    def _open_deferred_item(self, selected: list[tuple[str, DeferredUploadItem]]) -> None:
        for _key, item in selected:
            webbrowser.open(item.product_url)

    def _upload_deferred_item(self, selected: list[tuple[str, DeferredUploadItem]]) -> None:
        if not self.bot or (self.worker and self.worker.is_alive()):
            return
        self.start_button.configure(state=tk.DISABLED)
        self.stop_button.configure(state=tk.NORMAL)
        bot = self.bot
        self.worker = threading.Thread(target=self._deferred_upload_worker, args=(bot, selected), daemon=True)
        self.worker.start()

    def _deferred_upload_worker(self, bot: SatuImageBot, selected: list[tuple[str, DeferredUploadItem]]) -> None:
        results = []
        total = len(selected)
        for index, (key, item) in enumerate(selected, start=1):
            try:
                bot.retry_deferred_upload(item)
                results.append((key, item, True, ""))
            except StopRequested:
                results.append((key, item, False, "Загрузка остановлена пользователем."))
                self.status_queue.put(("__DEFERRED_UPLOAD_PROGRESS__", (index, total, item.code)))  # type: ignore[arg-type]
                break
            except Exception as exc:
                results.append((key, item, False, str(exc)))
            self.status_queue.put(("__DEFERRED_UPLOAD_PROGRESS__", (index, total, item.code)))  # type: ignore[arg-type]
        self.status_queue.put(("__DEFERRED_UPLOAD_BATCH_RESULT__", results))  # type: ignore[arg-type]

    def _handle_deferred_upload_progress(self, processed: int, total: int, code: str) -> None:
        if self.deferred_dialog and self.deferred_dialog.winfo_exists():
            self.deferred_dialog.update_progress(processed, total, code)

    def _handle_deferred_upload_batch_result(self, results: list[tuple[str, DeferredUploadItem, bool, str]]) -> None:
        self.stop_button.configure(state=tk.DISABLED)
        if not self.deferred_dialog or not self.deferred_dialog.winfo_exists():
            return
        uploaded_keys = {key for key, _item, success, _message in results if success}
        uploaded_items = [item for _key, item, success, _message in results if success]
        failed = [(item, message) for _key, item, success, message in results if not success]
        if uploaded_keys:
            self.deferred_dialog.mark_uploaded(uploaded_keys)
            remove_deferred_queue_items(uploaded_items)
            self._save_processed_catalog_cache_entries({item.code for item in uploaded_items})
        self.pending_deferred_uploads = [item for _key, item in self.deferred_dialog.items]
        if failed:
            failed_text = "; ".join(f"{item.code}: {message}" for item, message in failed)
            self.deferred_dialog.set_ready(f"Ошибка повторной загрузки: {failed_text}")
        elif self.pending_deferred_uploads:
            self.deferred_dialog.set_ready(f"Изображений загружено: {len(uploaded_keys)}")
        else:
            self._save_successful_catalog_cache()
            self.start_button.configure(state=tk.NORMAL)
            self.authorized_button.configure(state=tk.DISABLED)
            self.run_started_at = None
            self.current_excel_for_cache = None
            self.current_catalog_for_cache = None
            self.bot = None

    def _save_successful_catalog_cache(self) -> None:
        if not self.save_catalog_cache_after_run:
            return
        if self.current_excel_for_cache is None or self.current_catalog_for_cache is None:
            return
        save_catalog_cache(self.current_excel_for_cache, self.current_catalog_for_cache)

    def _save_processed_catalog_cache_entries(self, codes: set[str]) -> None:
        if not codes:
            return
        if self.current_excel_for_cache is None or self.current_catalog_for_cache is None:
            return
        save_catalog_cache_entries(self.current_excel_for_cache, self.current_catalog_for_cache, codes)

    def _close_deferred_dialog(self) -> None:
        if self.deferred_dialog:
            self.deferred_dialog.destroy()
            self.deferred_dialog = None
        self.start_button.configure(state=tk.NORMAL)
        self.stop_button.configure(state=tk.DISABLED)
        self.authorized_button.configure(state=tk.DISABLED)
        self.bot = None

    def _open_saved_existing_queue(self) -> None:
        if self.worker and self.worker.is_alive():
            messagebox.showinfo("Бот занят", "Дождитесь завершения текущей операции.")
            return
        items = load_existing_queue()
        if not items:
            messagebox.showinfo("Очередь пуста", "Нет сохраненных товаров с существующими изображениями.")
            return
        self.status_var.set(f"Открывает сохраненную таблицу замен: {len(items)}")
        self.update_idletasks()
        dialog = ExistingImagesDialog(self, items)
        self.wait_window(dialog)
        action, selected = dialog.result
        if action == "replace" and selected:
            self._start_saved_existing_replace(selected)
        elif action == "skip":
            remove_existing_queue_items(items)
            self.status_var.set("Сохраненная очередь замен очищена.")

    def _start_saved_existing_replace(self, selected: list[ExistingImageItem]) -> None:
        report_writer = ReportWriter(REPORT_DIR)
        self.bot = self._build_bot(report_writer)
        self.start_button.configure(state=tk.DISABLED)
        self.stop_button.configure(state=tk.NORMAL)
        self.authorized_button.configure(state=tk.NORMAL)
        self.authorization_event.clear()
        self.status_var.set(f"Запуск замены из сохраненной очереди: {len(selected)}")
        self.worker = threading.Thread(target=self._replace_saved_existing_worker, args=(self.bot, selected), daemon=True)
        self.worker.start()

    def _replace_saved_existing_worker(self, bot: SatuImageBot, selected: list[ExistingImageItem]) -> None:
        try:
            bot.replace_existing(selected)
            remove_existing_queue_items(selected)
            self.status_queue.put(("Замена из сохраненной очереди завершена.", ""))
        except StopRequested:
            self.status_queue.put(("Замена из сохраненной очереди остановлена пользователем.", ""))
        except Exception as exc:
            self.status_queue.put((f"Ошибка при замене из сохраненной очереди: {exc}", ""))
        finally:
            self.status_queue.put(("__DONE__", ""))

    def _open_saved_deferred_queue(self) -> None:
        if self.worker and self.worker.is_alive():
            messagebox.showinfo("Бот занят", "Дождитесь завершения текущей операции.")
            return
        items = load_deferred_queue()
        if not items:
            messagebox.showinfo("Очередь пуста", "Нет сохраненных товаров с ошибками сайта.")
            return
        report_writer = ReportWriter(REPORT_DIR)
        self.bot = self._build_bot(report_writer)
        self.status_var.set(f"Открывает сохраненную таблицу ошибок: {len(items)}")
        self.update_idletasks()
        self._handle_deferred_uploads(items)

    def _selected_update_options(self) -> dict[str, bool]:
        return {
            "update_names": self.update_names_var.get(),
            "update_main_price": self.update_main_price_var.get(),
            "update_discount_system": self.update_discount_system_var.get(),
            "write_not_found_report": self.write_not_found_report_var.get(),
        }

    def _build_bot(self, report_writer: ReportWriter) -> SatuImageBot:
        options = self._selected_update_options()
        return SatuImageBot(
            PROFILE_DIR,
            report_writer,
            self._queue_status,
            authorization_event=self.authorization_event,
            headless=self.headless_var.get(),
            update_names=options["update_names"],
            update_main_price=options["update_main_price"],
            update_discount_system=options["update_discount_system"],
        )

    def _build_ui(self) -> None:
        main = ttk.Frame(self, padding=14)
        main.pack(fill=tk.BOTH, expand=True)

        ttk.Label(main, text="Папка с изображениями").grid(row=0, column=0, sticky="w")
        ttk.Entry(main, textvariable=self.image_folder_var).grid(row=1, column=0, sticky="ew", pady=(2, 10))
        ttk.Button(main, text="Выбрать", command=self._choose_image_folder).grid(row=1, column=1, padx=(8, 0), pady=(2, 10))

        ttk.Label(main, text="Excel с артикулами и названиями").grid(row=2, column=0, sticky="w")
        ttk.Entry(main, textvariable=self.excel_path_var).grid(row=3, column=0, sticky="ew", pady=(2, 10))
        ttk.Button(main, text="Выбрать", command=self._choose_excel).grid(row=3, column=1, padx=(8, 0), pady=(2, 10))

        ttk.Label(main, text="Excel с RU/KZ наименованиями (необязательно)").grid(row=4, column=0, sticky="w")
        ttk.Entry(main, textvariable=self.names_excel_path_var).grid(row=5, column=0, sticky="ew", pady=(2, 10))
        ttk.Button(main, text="Выбрать", command=self._choose_names_excel).grid(row=5, column=1, padx=(8, 0), pady=(2, 10))

        options_frame = ttk.LabelFrame(main, text="Что обновлять", padding=10)
        options_frame.grid(row=6, column=0, columnspan=2, sticky="ew", pady=(0, 10))
        ttk.Checkbutton(options_frame, text="Наименования RU/KZ", variable=self.update_names_var).grid(row=0, column=0, sticky="w")
        ttk.Checkbutton(options_frame, text="Основная цена", variable=self.update_main_price_var).grid(row=1, column=0, sticky="w")
        ttk.Checkbutton(options_frame, text="Система скидок", variable=self.update_discount_system_var).grid(row=2, column=0, sticky="w")

        ttk.Label(main, text="Точечные ссылки на товары для перезаписи изображений").grid(row=7, column=0, sticky="w")
        direct_links_frame = ttk.Frame(main)
        direct_links_frame.grid(row=8, column=0, columnspan=2, sticky="ew", pady=(2, 10))
        self.direct_links_text = tk.Text(direct_links_frame, height=4, wrap=tk.NONE)
        direct_links_scrollbar = ttk.Scrollbar(direct_links_frame, orient=tk.VERTICAL, command=self.direct_links_text.yview)
        self.direct_links_text.configure(yscrollcommand=direct_links_scrollbar.set)
        self.direct_links_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        direct_links_scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        ttk.Checkbutton(main, text="Фоновый режим: не показывать браузер", variable=self.headless_var).grid(
            row=9, column=0, columnspan=2, sticky="w", pady=(0, 10)
        )
        ttk.Checkbutton(main, text="Сравнить с предыдущим успешным проходом", variable=self.compare_cache_var).grid(
            row=10, column=0, columnspan=2, sticky="w", pady=(0, 10)
        )

        ttk.Label(main, text="Статус").grid(row=11, column=0, sticky="w")
        ttk.Label(main, textvariable=self.status_var, wraplength=700).grid(row=12, column=0, columnspan=2, sticky="ew", pady=(2, 10))
        ttk.Label(main, text="Текущий товар").grid(row=13, column=0, sticky="w")
        url_label = ttk.Label(main, textvariable=self.url_var, foreground="#0645ad", cursor="hand2", wraplength=700)
        url_label.grid(row=14, column=0, columnspan=2, sticky="ew", pady=(2, 12))
        url_label.bind("<Button-1>", lambda _event: self._open_current_url())

        ttk.Label(main, textvariable=self.main_progress_var).grid(row=15, column=0, columnspan=2, sticky="ew", pady=(0, 2))
        self.main_progress_bar = ttk.Progressbar(main, mode="determinate", maximum=1, value=0)
        self.main_progress_bar.grid(row=16, column=0, columnspan=2, sticky="ew", pady=(0, 12))

        buttons = ttk.Frame(main)
        buttons.grid(row=17, column=0, columnspan=2, sticky="w")
        self.start_button = ttk.Button(buttons, text="Старт", command=self._start)
        self.start_button.pack(side=tk.LEFT)
        self.stop_button = ttk.Button(buttons, text="Стоп", command=self._stop, state=tk.DISABLED)
        self.stop_button.pack(side=tk.LEFT, padx=8)
        self.authorized_button = ttk.Button(buttons, text="Я авторизован", command=self._confirm_authorized, state=tk.DISABLED)
        self.authorized_button.pack(side=tk.LEFT)
        ttk.Button(buttons, text="Открыть текущий товар", command=self._open_current_url).pack(side=tk.LEFT, padx=(8, 0))
        ttk.Button(buttons, text="Прошлые замены", command=self._open_saved_existing_queue).pack(side=tk.LEFT, padx=(8, 0))
        ttk.Button(buttons, text="Прошлые ошибки", command=self._open_saved_deferred_queue).pack(side=tk.LEFT, padx=(8, 0))

        main.columnconfigure(0, weight=1)

    def _start(self) -> None:
        if self.worker and self.worker.is_alive():
            return

        image_folder = Path(self.image_folder_var.get().strip())
        excel_path = Path(self.excel_path_var.get().strip())
        names_excel_text = self.names_excel_path_var.get().strip()
        names_excel_path = Path(names_excel_text) if names_excel_text else None
        direct_urls = self._direct_product_urls()
        update_options = self._selected_update_options()
        if direct_urls and not self._confirm_non_admin_direct_urls(direct_urls):
            return
        if not image_folder.is_dir():
            messagebox.showerror("Ошибка", "Выберите существующую папку с изображениями.")
            return
        if not excel_path.is_file():
            messagebox.showerror("Ошибка", "Выберите существующий Excel-файл.")
            return
        if names_excel_path is not None and not names_excel_path.is_file():
            messagebox.showerror("Ошибка", "Файл с RU/KZ наименованиями не найден.")
            return
        save_saved_paths(
            image_folder=image_folder,
            excel_path=excel_path,
            names_excel_path=names_excel_path or "",
            update_names=update_options["update_names"],
            update_main_price=update_options["update_main_price"],
            update_discount_system=update_options["update_discount_system"],
            write_not_found_report=update_options["write_not_found_report"],
        )

        try:
            catalog = load_catalog(excel_path)
            name_catalog = self._load_optional_name_catalog(names_excel_path)
        except Exception as exc:
            messagebox.showerror("Ошибка", f"Не удалось прочитать Excel-файл:\n\n{exc}")
            return

        sko_warnings = find_sko_price_warnings(catalog)
        if sko_warnings:
            dialog = SkoPriceWarningDialog(self, sko_warnings)
            self.wait_window(dialog)
            if not dialog.result:
                self.status_var.set("Обработка отменена пользователем после проверки цен.")
                return

        selected_codes: set[str] | None = None
        save_cache_after_run = True
        if self.compare_cache_var.get() and not direct_urls:
            changes = catalog_changes(
                catalog,
                load_catalog_cache(),
                name_catalog=name_catalog,
                include_names=update_options["update_names"],
                include_main_price=update_options["update_main_price"],
                include_discount_system=update_options["update_discount_system"],
            )
            if not changes:
                messagebox.showinfo("Изменений нет", "Новый Excel не отличается от предыдущего успешного прохода.")
                return
            self.status_var.set(f"Готовит таблицу измененных товаров: {len(changes)}")
            self.update_idletasks()
            dialog = ChangedCatalogDialog(self, changes)
            self.wait_window(dialog)
            action, selected_codes = dialog.result
            if action == "cancel":
                return
            save_cache_after_run = action == "all"

        resume_requested = False if direct_urls else self._ask_resume_previous_run(image_folder, excel_path)
        if resume_requested is None:
            return
        self.resume_requested = resume_requested
        self.selected_changed_codes = selected_codes
        self.save_catalog_cache_after_run = save_cache_after_run
        self.current_excel_for_cache = excel_path
        self.current_catalog_for_cache = catalog
        self.run_started_at = time.monotonic()
        self.start_button.configure(state=tk.DISABLED)
        self.stop_button.configure(state=tk.NORMAL)
        self.authorized_button.configure(state=tk.NORMAL)
        self.authorization_event.clear()
        self.status_var.set("Подготовка данных...")
        self.main_progress_var.set("")
        self.main_progress_bar.configure(maximum=1, value=0)

        self.worker = threading.Thread(
            target=self._run_worker,
            args=(image_folder, excel_path, names_excel_path, self.headless_var.get(), selected_codes, direct_urls),
            daemon=True,
        )
        self.worker.start()

    def _run_worker(
        self,
        image_folder: Path,
        excel_path: Path,
        names_excel_path: Path | None,
        headless: bool,
        selected_codes: set[str] | None,
        direct_urls: list[str] | None = None,
    ) -> None:
        awaiting_manual_decision = False
        try:
            catalog = load_catalog(excel_path)
            name_catalog = self._load_optional_name_catalog(names_excel_path)
            direct_urls = direct_urls or []
            if direct_urls:
                images = apply_name_catalog(processing_items(image_folder, catalog), name_catalog)
                if not images:
                    self.status_queue.put(("Нет товаров для обработки.", ""))
                    return
                report_writer = ReportWriter(REPORT_DIR)
                self.bot = self._build_bot(report_writer)
                self.bot.replace_direct_links(direct_urls, {image.code: image for image in images})
                self.pending_deferred_uploads = list(self.bot.deferred_uploads)
                successful_codes = successful_catalog_cache_codes(report_writer, [])
                save_catalog_cache_entries(excel_path, catalog, successful_codes, name_catalog)
                self.status_queue.put((f"Точечная перезапись завершена. Отчет: {report_writer.csv_path}", ""))
                if self.pending_deferred_uploads:
                    append_deferred_queue(self.pending_deferred_uploads)
                    awaiting_manual_decision = True
                    self.status_queue.put(("__DEFERRED__", self.pending_deferred_uploads))  # type: ignore[arg-type]
                return

            images = apply_name_catalog(processing_items(image_folder, catalog, selected_codes), name_catalog)
            if not images:
                self.status_queue.put(("Нет товаров для обработки.", ""))
                return
            completed_keys = self._prepare_run_state(image_folder, excel_path, images)
            if completed_keys:
                image_keys = {self._image_key(image.path, image.code) for image in images}
                matching_completed = completed_keys & image_keys
                if self.active_run_state is not None:
                    self.active_run_state["completed"] = sorted(matching_completed)
                    self._save_run_state()
                images = [image for image in images if self._image_key(image.path, image.code) not in matching_completed]
                skipped_count = len(matching_completed)
                self.status_queue.put((f"Продолжение прохода: уже обработано {skipped_count}, осталось {len(images)}.", ""))
            if not images:
                self.status_queue.put(("Все товары из сохраненного прохода уже обработаны.", ""))
                self._clear_run_state()
                return

            report_writer = ReportWriter(REPORT_DIR)
            self.bot = self._build_bot(report_writer)
            existing = self.bot.run(images)
            self.pending_deferred_uploads = list(self.bot.deferred_uploads)
            self._clear_run_state()
            if selected_codes is not None:
                deferred_codes = {item.code for item in self.pending_deferred_uploads}
                successful_codes = successful_catalog_cache_codes(report_writer, existing)
                save_catalog_cache_entries(excel_path, catalog, (set(selected_codes) & successful_codes) - deferred_codes, name_catalog)
            elif self.save_catalog_cache_after_run and not self.pending_deferred_uploads:
                save_catalog_cache(excel_path, catalog, name_catalog)
            self.status_queue.put((f"Основной проход завершен. Отчет: {report_writer.csv_path}", ""))
            if existing:
                append_existing_queue(existing)
                awaiting_manual_decision = True
                self.status_queue.put(("__EXISTING__", existing))  # type: ignore[arg-type]
                return
            if self.pending_deferred_uploads:
                append_deferred_queue(self.pending_deferred_uploads)
                awaiting_manual_decision = True
                self.status_queue.put(("__DEFERRED__", self.pending_deferred_uploads))  # type: ignore[arg-type]
                return
        except StopRequested:
            self.status_queue.put(("Процесс остановлен пользователем.", ""))
        except Exception as exc:
            self.status_queue.put((f"Ошибка: {exc}", ""))
        finally:
            if not awaiting_manual_decision:
                self.status_queue.put(("__DONE__", ""))

    def _report_output_dir(self) -> Path:
        if getattr(sys, "frozen", False):
            return Path(sys.executable).resolve().parent
        return APP_DIR

    def _write_not_found_report(self, report_writer: ReportWriter) -> Path | None:
        if not self.write_not_found_report_var.get():
            return None
        codes = sorted({record.code.strip() for record in report_writer.records if record.status == "not_found" and record.code.strip()})
        if not codes:
            return None
        output_dir = self._report_output_dir()
        output_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        output_path = output_dir / f"missing-products-{stamp}.txt"
        with output_path.open("w", encoding="utf-8") as handle:
            handle.write("Артикулы товаров, которых нет на сайте\n")
            handle.write("\n".join(codes))
            handle.write("\n")
        return output_path

    def _build_ui(self) -> None:
        main = ttk.Frame(self, padding=14)
        main.pack(fill=tk.BOTH, expand=True)

        ttk.Label(main, text="Папка с изображениями").grid(row=0, column=0, sticky="w")
        ttk.Entry(main, textvariable=self.image_folder_var).grid(row=1, column=0, sticky="ew", pady=(2, 10))
        ttk.Button(main, text="Выбрать", command=self._choose_image_folder).grid(row=1, column=1, padx=(8, 0), pady=(2, 10))

        ttk.Label(main, text="Excel с артикулами и названиями").grid(row=2, column=0, sticky="w")
        ttk.Entry(main, textvariable=self.excel_path_var).grid(row=3, column=0, sticky="ew", pady=(2, 10))
        ttk.Button(main, text="Выбрать", command=self._choose_excel).grid(row=3, column=1, padx=(8, 0), pady=(2, 10))

        ttk.Label(main, text="Excel с RU/KZ наименованиями (необязательно)").grid(row=4, column=0, sticky="w")
        ttk.Entry(main, textvariable=self.names_excel_path_var).grid(row=5, column=0, sticky="ew", pady=(2, 10))
        ttk.Button(main, text="Выбрать", command=self._choose_names_excel).grid(row=5, column=1, padx=(8, 0), pady=(2, 10))

        options_frame = ttk.LabelFrame(main, text="Что обновлять", padding=10)
        options_frame.grid(row=6, column=0, columnspan=2, sticky="ew", pady=(0, 10))
        ttk.Checkbutton(options_frame, text="Наименования RU/KZ", variable=self.update_names_var).grid(row=0, column=0, sticky="w")
        ttk.Checkbutton(options_frame, text="Основная цена", variable=self.update_main_price_var).grid(row=1, column=0, sticky="w")
        ttk.Checkbutton(options_frame, text="Система скидок", variable=self.update_discount_system_var).grid(row=2, column=0, sticky="w")
        ttk.Checkbutton(
            options_frame,
            text="Сформировать TXT со списком артикулов, которых нет на сайте",
            variable=self.write_not_found_report_var,
        ).grid(row=3, column=0, sticky="w")

        ttk.Label(main, text="Точечные ссылки на товары для перезаписи изображений").grid(row=7, column=0, sticky="w")
        direct_links_frame = ttk.Frame(main)
        direct_links_frame.grid(row=8, column=0, columnspan=2, sticky="ew", pady=(2, 10))
        self.direct_links_text = tk.Text(direct_links_frame, height=4, wrap=tk.NONE)
        direct_links_scrollbar = ttk.Scrollbar(direct_links_frame, orient=tk.VERTICAL, command=self.direct_links_text.yview)
        self.direct_links_text.configure(yscrollcommand=direct_links_scrollbar.set)
        self.direct_links_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        direct_links_scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        ttk.Checkbutton(main, text="Фоновый режим: не показывать браузер", variable=self.headless_var).grid(
            row=9, column=0, columnspan=2, sticky="w", pady=(0, 10)
        )
        ttk.Checkbutton(main, text="Сравнить с предыдущим успешным проходом", variable=self.compare_cache_var).grid(
            row=10, column=0, columnspan=2, sticky="w", pady=(0, 10)
        )

        ttk.Label(main, text="Статус").grid(row=11, column=0, sticky="w")
        ttk.Label(main, textvariable=self.status_var, wraplength=840).grid(row=12, column=0, columnspan=2, sticky="ew", pady=(2, 10))
        ttk.Label(main, text="Текущий товар").grid(row=13, column=0, sticky="w")
        url_label = ttk.Label(main, textvariable=self.url_var, foreground="#0645ad", cursor="hand2", wraplength=840)
        url_label.grid(row=14, column=0, columnspan=2, sticky="ew", pady=(2, 12))
        url_label.bind("<Button-1>", lambda _event: self._open_current_url())

        ttk.Label(main, textvariable=self.main_progress_var).grid(row=15, column=0, columnspan=2, sticky="ew", pady=(0, 2))
        self.main_progress_bar = ttk.Progressbar(main, mode="determinate", maximum=1, value=0)
        self.main_progress_bar.grid(row=16, column=0, columnspan=2, sticky="ew", pady=(0, 12))

        buttons = ttk.Frame(main)
        buttons.grid(row=17, column=0, columnspan=2, sticky="w")
        self.start_button = ttk.Button(buttons, text="Старт", command=self._start)
        self.start_button.pack(side=tk.LEFT)
        self.stop_button = ttk.Button(buttons, text="Стоп", command=self._stop, state=tk.DISABLED)
        self.stop_button.pack(side=tk.LEFT, padx=8)
        self.authorized_button = ttk.Button(buttons, text="Я авторизован", command=self._confirm_authorized, state=tk.DISABLED)
        self.authorized_button.pack(side=tk.LEFT)
        ttk.Button(buttons, text="Открыть текущий товар", command=self._open_current_url).pack(side=tk.LEFT, padx=(8, 0))
        ttk.Button(buttons, text="Прошлые замены", command=self._open_saved_existing_queue).pack(side=tk.LEFT, padx=(8, 0))
        ttk.Button(buttons, text="Прошлые ошибки", command=self._open_saved_deferred_queue).pack(side=tk.LEFT, padx=(8, 0))

        main.columnconfigure(0, weight=1)

    def _run_worker(
        self,
        image_folder: Path,
        excel_path: Path,
        names_excel_path: Path | None,
        headless: bool,
        selected_codes: set[str] | None,
        direct_urls: list[str] | None = None,
    ) -> None:
        awaiting_manual_decision = False
        try:
            catalog = load_catalog(excel_path)
            name_catalog = self._load_optional_name_catalog(names_excel_path)
            direct_urls = direct_urls or []
            if direct_urls:
                images = apply_name_catalog(processing_items(image_folder, catalog), name_catalog)
                if not images:
                    self.status_queue.put(("Нет товаров для обработки.", ""))
                    return
                report_writer = ReportWriter(REPORT_DIR)
                self.bot = self._build_bot(report_writer)
                self.bot.replace_direct_links(direct_urls, {image.code: image for image in images})
                self.pending_deferred_uploads = list(self.bot.deferred_uploads)
                successful_codes = successful_catalog_cache_codes(report_writer, [])
                save_catalog_cache_entries(excel_path, catalog, successful_codes, name_catalog)
                not_found_report = self._write_not_found_report(report_writer)
                message = f"Точечная перезапись завершена. Отчет: {report_writer.csv_path}"
                if not_found_report is not None:
                    message += f". TXT по отсутствующим товарам: {not_found_report}"
                self.status_queue.put((message, ""))
                if self.pending_deferred_uploads:
                    append_deferred_queue(self.pending_deferred_uploads)
                    awaiting_manual_decision = True
                    self.status_queue.put(("__DEFERRED__", self.pending_deferred_uploads))  # type: ignore[arg-type]
                return

            images = apply_name_catalog(processing_items(image_folder, catalog, selected_codes), name_catalog)
            if not images:
                self.status_queue.put(("Нет товаров для обработки.", ""))
                return
            completed_keys = self._prepare_run_state(image_folder, excel_path, images)
            if completed_keys:
                image_keys = {self._image_key(image.path, image.code) for image in images}
                matching_completed = completed_keys & image_keys
                if self.active_run_state is not None:
                    self.active_run_state["completed"] = sorted(matching_completed)
                    self._save_run_state()
                images = [image for image in images if self._image_key(image.path, image.code) not in matching_completed]
                skipped_count = len(matching_completed)
                self.status_queue.put((f"Продолжение прохода: уже обработано {skipped_count}, осталось {len(images)}.", ""))
            if not images:
                self.status_queue.put(("Все товары из сохраненного прохода уже обработаны.", ""))
                self._clear_run_state()
                return

            report_writer = ReportWriter(REPORT_DIR)
            self.bot = self._build_bot(report_writer)
            existing = self.bot.run(images)
            self.pending_deferred_uploads = list(self.bot.deferred_uploads)
            self._clear_run_state()
            if selected_codes is not None:
                deferred_codes = {item.code for item in self.pending_deferred_uploads}
                successful_codes = successful_catalog_cache_codes(report_writer, existing)
                save_catalog_cache_entries(excel_path, catalog, (set(selected_codes) & successful_codes) - deferred_codes, name_catalog)
            elif self.save_catalog_cache_after_run and not self.pending_deferred_uploads:
                save_catalog_cache(excel_path, catalog, name_catalog)
            not_found_report = self._write_not_found_report(report_writer)
            message = f"Основной проход завершен. Отчет: {report_writer.csv_path}"
            if not_found_report is not None:
                message += f". TXT по отсутствующим товарам: {not_found_report}"
            self.status_queue.put((message, ""))
            if existing:
                append_existing_queue(existing)
                awaiting_manual_decision = True
                self.status_queue.put(("__EXISTING__", existing))  # type: ignore[arg-type]
                return
            if self.pending_deferred_uploads:
                append_deferred_queue(self.pending_deferred_uploads)
                awaiting_manual_decision = True
                self.status_queue.put(("__DEFERRED__", self.pending_deferred_uploads))  # type: ignore[arg-type]
                return
        except StopRequested:
            self.status_queue.put(("Процесс остановлен пользователем.", ""))
        except Exception as exc:
            self.status_queue.put((f"Ошибка: {exc}", ""))
        finally:
            if not awaiting_manual_decision:
                self.status_queue.put(("__DONE__", ""))

    def _open_current_url(self) -> None:
        if self.current_url:
            webbrowser.open(self.current_url)


if __name__ == "__main__":
    App().mainloop()
