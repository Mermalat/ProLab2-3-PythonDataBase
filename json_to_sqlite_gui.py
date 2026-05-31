import json
import os
import re
import sqlite3
import tkinter as tk
import unicodedata
from dataclasses import dataclass, field
from tkinter import filedialog, messagebox, ttk
from typing import Optional


TYPE_PRIORITY = {
    "NULL": 0,
    "INTEGER": 1,
    "REAL": 2,
    "VARCHAR": 3,
}


def normalize_identifier(value, fallback="field"):
    """Convert arbitrary JSON keys into safe, ASCII SQLite identifiers."""
    text = str(value).strip()
    replacements = str.maketrans(
        {
            "ç": "c",
            "Ç": "C",
            "ğ": "g",
            "Ğ": "G",
            "ı": "i",
            "İ": "I",
            "ö": "o",
            "Ö": "O",
            "ş": "s",
            "Ş": "S",
            "ü": "u",
            "Ü": "U",
        }
    )
    text = text.translate(replacements)
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^0-9a-zA-Z_]+", "_", text.lower())
    text = re.sub(r"_+", "_", text).strip("_")

    if not text:
        text = fallback
    if text[0].isdigit():
        text = f"{fallback}_{text}"
    return text


def quote_identifier(identifier):
    return f'"{identifier.replace(chr(34), chr(34) * 2)}"'


def table_name_from_file(path):
    file_name = os.path.splitext(os.path.basename(path))[0]
    return normalize_identifier(file_name, "root")


def infer_sql_type(value):
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "INTEGER"
    if isinstance(value, int):
        return "INTEGER"
    if isinstance(value, float):
        return "REAL"
    return "VARCHAR"


def merge_sql_types(current, incoming):
    if current is None:
        return incoming
    if incoming == "NULL":
        return current
    if current == "NULL":
        return incoming
    return current if TYPE_PRIORITY[current] >= TYPE_PRIORITY[incoming] else incoming


def adapt_value(value):
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float, str)):
        return value
    return json.dumps(value, ensure_ascii=False)


@dataclass
class TableSchema:
    name: str
    parent_table: Optional[str] = None
    parent_column: Optional[str] = None
    columns: dict = field(default_factory=dict)
    row_count: int = 0
    json_id_count: int = 0
    json_id_values: set = field(default_factory=set)
    json_id_usable_as_pk: bool = True

    def register_row(self):
        self.row_count += 1

    def register_json_id(self, value):
        self.json_id_count += 1
        if type(value) is not int or value in self.json_id_values:
            self.json_id_usable_as_pk = False
            return
        self.json_id_values.add(value)

    def can_use_json_id_as_primary_key(self):
        return (
            self.row_count > 0
            and self.json_id_count == self.row_count
            and self.json_id_usable_as_pk
            and len(self.json_id_values) == self.row_count
        )

    def add_column(self, name, sql_type):
        if name == "id":
            name = "json_id"
        self.columns[name] = merge_sql_types(self.columns.get(name), sql_type)


class JsonToSQLiteConverter:
    """Generic JSON normalizer that dynamically creates SQLite tables."""

    def __init__(self, db_path, root_table="kisiler"):
        self.db_path = db_path
        self.root_table = normalize_identifier(root_table, "root")
        self.schemas = {}
        self.conn = None

    def convert(self, data):
        self.schemas.clear()

        root_items = data if isinstance(data, list) else [data]
        for item in root_items:
            if isinstance(item, dict):
                self._collect_dict(self.root_table, item)
            else:
                self._collect_primitive(self.root_table, item)

        self._finalize_schemas()

        self.conn = sqlite3.connect(self.db_path)
        self.conn.execute("PRAGMA foreign_keys = ON")
        self._drop_all_tables()
        self._create_tables()

        for item in root_items:
            if isinstance(item, dict):
                self._insert_dict(self.root_table, item, None, None)
            else:
                self._insert_primitive(self.root_table, item, None, None)

        self.conn.commit()
        return self.get_table_names()

    def reset_database(self):
        self.conn = sqlite3.connect(self.db_path)
        self.conn.execute("PRAGMA foreign_keys = OFF")
        self._drop_all_tables()
        self.conn.commit()
        self.conn.close()
        self.conn = None
        self.schemas.clear()

    def get_table_names(self):
        if not self.conn:
            if not os.path.exists(self.db_path):
                return []
            self.conn = sqlite3.connect(self.db_path)
        query = "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        return [row[0] for row in self.conn.execute(query).fetchall()]

    def fetch_table(self, table_name):
        if not self.conn:
            self.conn = sqlite3.connect(self.db_path)
        table = quote_identifier(table_name)
        cursor = self.conn.execute(f"SELECT * FROM {table}")
        columns = [description[0] for description in cursor.description]
        rows = cursor.fetchall()
        return columns, rows

    def close(self):
        if self.conn:
            self.conn.close()
            self.conn = None

    def _ensure_schema(self, table_name, parent_table=None):
        if table_name not in self.schemas:
            parent_column = f"{parent_table}_id" if parent_table else None
            self.schemas[table_name] = TableSchema(
                name=table_name,
                parent_table=parent_table,
                parent_column=parent_column,
            )
        return self.schemas[table_name]

    def _child_table_name(self, parent_table, key):
        return normalize_identifier(f"{parent_table}_{key}", "table")

    def _collect_primitive(self, table_name, value, parent_table=None):
        schema = self._ensure_schema(table_name, parent_table)
        schema.register_row()
        schema.add_column("value", infer_sql_type(value))

    def _collect_dict(self, table_name, data, parent_table=None, prefix=""):
        schema = self._ensure_schema(table_name, parent_table)
        if not prefix:
            schema.register_row()

        for raw_key, value in data.items():
            key = normalize_identifier(raw_key, "field")
            column_name = f"{prefix}_{key}" if prefix else key

            if isinstance(value, dict):
                self._collect_dict(table_name, value, parent_table, column_name)
            elif isinstance(value, list):
                child_table = self._child_table_name(table_name, column_name)
                self._collect_list(child_table, value, table_name)
            else:
                if column_name == "id":
                    schema.register_json_id(value)
                schema.add_column(column_name, infer_sql_type(value))

    def _collect_list(self, table_name, values, parent_table):
        schema = self._ensure_schema(table_name, parent_table)
        if not values:
            return

        for item in values:
            if isinstance(item, dict):
                self._collect_dict(table_name, item, parent_table)
            elif isinstance(item, list):
                schema.register_row()
                nested_table = self._child_table_name(table_name, "items")
                self._collect_list(nested_table, item, table_name)
            else:
                self._collect_primitive(table_name, item, parent_table)

    def _finalize_schemas(self):
        for schema in self.schemas.values():
            if schema.can_use_json_id_as_primary_key():
                schema.columns.pop("json_id", None)

    def _insert_dict(self, table_name, data, parent_table, parent_id):
        row = {}
        child_arrays = []
        self._fill_row_and_children(table_name, data, row, child_arrays)
        row_id = self._insert_row(table_name, row, parent_table, parent_id)

        for child_table, values in child_arrays:
            self._insert_list(child_table, values, table_name, row_id)

        return row_id

    def _insert_primitive(self, table_name, value, parent_table, parent_id):
        return self._insert_row(table_name, {"value": adapt_value(value)}, parent_table, parent_id)

    def _insert_list(self, table_name, values, parent_table, parent_id):
        for item in values:
            if isinstance(item, dict):
                self._insert_dict(table_name, item, parent_table, parent_id)
            elif isinstance(item, list):
                row_id = self._insert_row(table_name, {}, parent_table, parent_id)
                nested_table = self._child_table_name(table_name, "items")
                self._insert_list(nested_table, item, table_name, row_id)
            else:
                self._insert_primitive(table_name, item, parent_table, parent_id)

    def _fill_row_and_children(self, table_name, data, row, child_arrays, prefix=""):
        schema = self.schemas[table_name]
        for raw_key, value in data.items():
            key = normalize_identifier(raw_key, "field")
            column_name = f"{prefix}_{key}" if prefix else key

            if isinstance(value, dict):
                self._fill_row_and_children(table_name, value, row, child_arrays, column_name)
            elif isinstance(value, list):
                child_table = self._child_table_name(table_name, column_name)
                child_arrays.append((child_table, value))
            else:
                if column_name == "id":
                    if schema.can_use_json_id_as_primary_key():
                        row["id"] = value
                    elif "json_id" in schema.columns:
                        row["json_id"] = adapt_value(value)
                else:
                    row[column_name] = adapt_value(value)

    def _drop_all_tables(self):
        if not self.conn:
            return
        self.conn.execute("PRAGMA foreign_keys = OFF")
        tables = [
            row[0]
            for row in self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        ]
        for table in tables:
            self.conn.execute(f"DROP TABLE IF EXISTS {quote_identifier(table)}")
        self.conn.execute("PRAGMA foreign_keys = ON")

    def _create_tables(self):
        for table_name in self._ordered_table_names():
            self._create_table(table_name)

    def _ordered_table_names(self):
        ordered = []
        visited = set()

        def visit(table_name):
            if table_name in visited:
                return
            schema = self.schemas[table_name]
            if schema.parent_table and schema.parent_table in self.schemas:
                visit(schema.parent_table)
            visited.add(table_name)
            ordered.append(table_name)

        for table_name in self.schemas:
            visit(table_name)
        return ordered

    def _create_table(self, table_name):
        schema = self.schemas[table_name]
        definitions = ["id INTEGER PRIMARY KEY AUTOINCREMENT"]

        if schema.parent_table and schema.parent_column:
            definitions.append(f"{quote_identifier(schema.parent_column)} INTEGER")

        for column_name, column_type in schema.columns.items():
            sql_type = "VARCHAR" if column_type == "NULL" else column_type
            definitions.append(f"{quote_identifier(column_name)} {sql_type}")

        if schema.parent_table and schema.parent_column:
            definitions.append(
                f"FOREIGN KEY ({quote_identifier(schema.parent_column)}) "
                f"REFERENCES {quote_identifier(schema.parent_table)}(id) ON DELETE CASCADE"
            )

        sql = f"CREATE TABLE IF NOT EXISTS {quote_identifier(table_name)} ({', '.join(definitions)})"
        self.conn.execute(sql)

    def _insert_row(self, table_name, values, parent_table, parent_id):
        schema = self.schemas[table_name]
        insert_values = {}

        if "id" in values:
            insert_values["id"] = values["id"]

        if parent_table and schema.parent_column:
            insert_values[schema.parent_column] = parent_id

        for column_name in schema.columns:
            insert_values[column_name] = values.get(column_name)

        if not insert_values:
            cursor = self.conn.execute(f"INSERT INTO {quote_identifier(table_name)} DEFAULT VALUES")
            return cursor.lastrowid

        columns = list(insert_values.keys())
        placeholders = ", ".join("?" for _ in columns)
        quoted_columns = ", ".join(quote_identifier(column) for column in columns)
        params = [insert_values[column] for column in columns]
        sql = f"INSERT INTO {quote_identifier(table_name)} ({quoted_columns}) VALUES ({placeholders})"
        cursor = self.conn.execute(sql, params)
        return cursor.lastrowid


class JsonSQLiteApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("JSON -> SQLite Normalizasyon Araci")
        self.geometry("1120x720")
        self.minsize(900, 560)

        self.loaded_json = None
        self.loaded_path = None
        app_dir = os.path.dirname(os.path.abspath(__file__))
        self.db_path = os.path.join(app_dir, "json_normalized.sqlite")
        self.converter = JsonToSQLiteConverter(self.db_path, root_table="root")

        self.status_var = tk.StringVar(value="JSON dosyasi secin.")
        self.table_var = tk.StringVar()
        self.root_table_var = tk.StringVar(value="")

        self._build_ui()

    def _build_ui(self):
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        toolbar = ttk.Frame(self, padding=(10, 8))
        toolbar.grid(row=0, column=0, sticky="ew")
        toolbar.columnconfigure(6, weight=1)

        ttk.Button(toolbar, text="JSON Sec", command=self.load_json).grid(row=0, column=0, padx=(0, 8))
        ttk.Label(toolbar, text="Ana tablo:").grid(row=0, column=1, padx=(0, 4))
        ttk.Entry(toolbar, textvariable=self.root_table_var, width=18).grid(row=0, column=2, padx=(0, 8))
        ttk.Button(toolbar, text="Donustur", command=self.convert_json).grid(row=0, column=3, padx=(0, 8))
        ttk.Button(toolbar, text="Reset", command=self.reset_all).grid(row=0, column=4, padx=(0, 8))
        ttk.Label(toolbar, textvariable=self.status_var).grid(row=0, column=6, sticky="e")

        paned = ttk.PanedWindow(self, orient=tk.HORIZONTAL)
        paned.grid(row=1, column=0, sticky="nsew", padx=10, pady=(0, 10))

        left = ttk.Frame(paned)
        right = ttk.Frame(paned)
        paned.add(left, weight=1)
        paned.add(right, weight=1)

        self._build_json_panel(left)
        self._build_database_panel(right)

    def _build_json_panel(self, parent):
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(1, weight=1)

        ttk.Label(parent, text="JSON Onizleme").grid(row=0, column=0, sticky="w", pady=(0, 6))

        frame = ttk.Frame(parent)
        frame.grid(row=1, column=0, sticky="nsew")
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)

        self.json_tree = ttk.Treeview(frame, columns=("type", "value"), show="tree headings")
        self.json_tree.heading("#0", text="Key")
        self.json_tree.heading("type", text="Tip")
        self.json_tree.heading("value", text="Deger")
        self.json_tree.column("#0", width=220, stretch=True)
        self.json_tree.column("type", width=90, anchor="center")
        self.json_tree.column("value", width=280, stretch=True)

        y_scroll = ttk.Scrollbar(frame, orient=tk.VERTICAL, command=self.json_tree.yview)
        x_scroll = ttk.Scrollbar(frame, orient=tk.HORIZONTAL, command=self.json_tree.xview)
        self.json_tree.configure(yscrollcommand=y_scroll.set, xscrollcommand=x_scroll.set)

        self.json_tree.grid(row=0, column=0, sticky="nsew")
        y_scroll.grid(row=0, column=1, sticky="ns")
        x_scroll.grid(row=1, column=0, sticky="ew")

    def _build_database_panel(self, parent):
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(2, weight=1)

        ttk.Label(parent, text="SQLite Tablolari").grid(row=0, column=0, sticky="w", pady=(0, 6))

        self.table_combo = ttk.Combobox(parent, textvariable=self.table_var, state="readonly")
        self.table_combo.grid(row=1, column=0, sticky="ew", pady=(0, 8))
        self.table_combo.bind("<<ComboboxSelected>>", lambda _event: self.show_selected_table())

        frame = ttk.Frame(parent)
        frame.grid(row=2, column=0, sticky="nsew")
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)

        self.data_grid = ttk.Treeview(frame, show="headings")
        y_scroll = ttk.Scrollbar(frame, orient=tk.VERTICAL, command=self.data_grid.yview)
        x_scroll = ttk.Scrollbar(frame, orient=tk.HORIZONTAL, command=self.data_grid.xview)
        self.data_grid.configure(yscrollcommand=y_scroll.set, xscrollcommand=x_scroll.set)

        self.data_grid.grid(row=0, column=0, sticky="nsew")
        y_scroll.grid(row=0, column=1, sticky="ns")
        x_scroll.grid(row=1, column=0, sticky="ew")

    def load_json(self):
        path = filedialog.askopenfilename(
            title="JSON dosyasi sec",
            filetypes=(("JSON dosyalari", "*.json"), ("Tum dosyalar", "*.*")),
        )
        if not path:
            return

        try:
            if os.path.getsize(path) == 0:
                raise ValueError("Secilen dosya bos.")
            with open(path, "r", encoding="utf-8") as file:
                data = json.load(file)
        except json.JSONDecodeError as exc:
            messagebox.showerror("Hatali JSON", f"JSON okunamadi:\n{exc}")
            self.status_var.set("Hatali JSON dosyasi.")
            return
        except Exception as exc:
            messagebox.showerror("Dosya hatasi", str(exc))
            self.status_var.set("Dosya yuklenemedi.")
            return

        self.loaded_json = data
        self.loaded_path = path
        self.root_table_var.set(table_name_from_file(path))
        self._populate_json_tree(data)
        self.status_var.set(f"Yuklendi: {os.path.basename(path)}")

    def convert_json(self):
        if self.loaded_json is None:
            messagebox.showwarning("JSON yok", "Once bir JSON dosyasi secin.")
            return

        if self.root_table_var.get().strip():
            root_table = normalize_identifier(self.root_table_var.get(), "root")
        elif self.loaded_path:
            root_table = table_name_from_file(self.loaded_path)
            self.root_table_var.set(root_table)
        else:
            root_table = "root"

        if not root_table:
            messagebox.showwarning("Ana tablo", "Ana tablo adi bos olamaz.")
            return

        try:
            self.status_var.set("Tablo semasi cikariliyor...")
            self.update_idletasks()

            self.converter.close()
            self.converter = JsonToSQLiteConverter(self.db_path, root_table=root_table)

            self.status_var.set("Tablolar olusturuluyor ve veri yaziliyor...")
            self.update_idletasks()
            tables = self.converter.convert(self.loaded_json)

            self._refresh_table_list(tables)
            self.status_var.set(f"Donusum tamamlandi. Veritabani: {self.db_path}")
        except Exception as exc:
            messagebox.showerror("Donusum hatasi", str(exc))
            self.status_var.set("Donusum basarisiz.")

    def reset_all(self):
        if not messagebox.askyesno("Reset", "Veritabanindaki tum tablolar silinsin mi?"):
            return

        try:
            self.converter.reset_database()
            self._clear_tree(self.json_tree)
            self._clear_data_grid()
            self.table_combo["values"] = []
            self.table_var.set("")
            self.loaded_json = None
            self.loaded_path = None
            self.status_var.set("Sifirlandi.")
        except Exception as exc:
            messagebox.showerror("Reset hatasi", str(exc))

    def show_selected_table(self):
        table_name = self.table_var.get()
        if not table_name:
            return

        try:
            columns, rows = self.converter.fetch_table(table_name)
        except Exception as exc:
            messagebox.showerror("Tablo okunamadi", str(exc))
            return

        self._clear_data_grid()
        self.data_grid["columns"] = columns
        for column in columns:
            self.data_grid.heading(column, text=column)
            self.data_grid.column(column, width=max(100, len(column) * 12), stretch=True)

        for row in rows:
            self.data_grid.insert("", tk.END, values=row)

        self.status_var.set(f"{table_name}: {len(rows)} satir")

    def _refresh_table_list(self, tables):
        self.table_combo["values"] = tables
        if tables:
            self.table_var.set(tables[0])
            self.show_selected_table()
        else:
            self.table_var.set("")
            self._clear_data_grid()

    def _populate_json_tree(self, data):
        self._clear_tree(self.json_tree)
        self._insert_json_node("", "root", data)

    def _insert_json_node(self, parent, key, value):
        if isinstance(value, dict):
            node = self.json_tree.insert(parent, tk.END, text=key, values=("object", f"{len(value)} alan"))
            for child_key, child_value in value.items():
                self._insert_json_node(node, child_key, child_value)
        elif isinstance(value, list):
            node = self.json_tree.insert(parent, tk.END, text=key, values=("array", f"{len(value)} eleman"))
            for index, item in enumerate(value):
                self._insert_json_node(node, f"[{index}]", item)
        else:
            value_type = type(value).__name__ if value is not None else "null"
            display = "" if value is None else str(value)
            self.json_tree.insert(parent, tk.END, text=key, values=(value_type, display))

    def _clear_tree(self, tree):
        for item in tree.get_children():
            tree.delete(item)

    def _clear_data_grid(self):
        for item in self.data_grid.get_children():
            self.data_grid.delete(item)
        self.data_grid["columns"] = []

    def on_close(self):
        self.converter.close()
        self.destroy()


def main():
    app = JsonSQLiteApp()
    app.protocol("WM_DELETE_WINDOW", app.on_close)
    app.mainloop()


if __name__ == "__main__":
    main()
