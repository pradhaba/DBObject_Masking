#!/usr/bin/env python3
"""GUI for the DDL masking/unmasking tool."""

import json
import os
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk

from masker import (
    extract_mapping_from_text,
    load_mapping,
    mask_text,
    suggest_mapping_filename,
    unmask_text,
)

SUPPORTED_DIALECTS = ['generic', 'sybase_asa', 'postgresql']


def select_sql_file(sql_path_var, source_text):
    file_path = filedialog.askopenfilename(
        title='Select SQL file',
        filetypes=[('SQL files', '*.sql'), ('Text files', '*.txt'), ('All files', '*.*')],
    )
    if not file_path:
        return
    try:
        with open(file_path, 'r', encoding='utf-8-sig') as sql_file:
            ddl_text = sql_file.read()
    except (OSError, UnicodeError) as exc:
        messagebox.showerror('Loading SQL failed', str(exc))
        return

    sql_path_var.set(file_path)
    source_text.delete('1.0', tk.END)
    source_text.insert('1.0', ddl_text)


def select_mapping_file(mapping_path_var):
    file_path = filedialog.askopenfilename(
        title='Select mapping JSON file',
        filetypes=[('JSON files', '*.json'), ('All files', '*.*')],
    )
    if file_path:
        mapping_path_var.set(file_path)


def select_mapping_location(mapping_path_var):
    directory = filedialog.askdirectory(title='Select mapping save location')
    if directory:
        mapping_path_var.set(directory)


def save_mapping_file(mapping, mapping_path_var, ddl_text):
    path = mapping_path_var.get().strip()
    if not path:
        directory = filedialog.askdirectory(title='Select mapping save location')
        if not directory:
            return None
        path = directory
    if os.path.isdir(path):
        path = os.path.join(path, suggest_mapping_filename(ddl_text))
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(mapping, f, indent=2, sort_keys=True)
    mapping_path_var.set(path)
    return path


def process_action(mode_var, dialect_var, embed_var, mapping_path_var, source_text, target_text):
    ddl_text = source_text.get('1.0', tk.END).strip()
    if not ddl_text:
        messagebox.showwarning('DDL Masker', 'Please select a SQL file or paste DDL text in the input pane.')
        return

    dialect = dialect_var.get()
    if mode_var.get() == 'mask':
        masked_text, mapping = mask_text(ddl_text, dialect, embed_mapping=embed_var.get())
        try:
            mapping_path = save_mapping_file(mapping, mapping_path_var, ddl_text)
        except Exception as exc:
            messagebox.showerror('Saving mapping failed', str(exc))
            return
        if mapping_path is None:
            return
        target_text.config(state='normal')
        target_text.delete('1.0', tk.END)
        target_text.insert(tk.END, masked_text)
        target_text.config(state='disabled')
        messagebox.showinfo('DDL Masker', f'Masking complete.\nMapping saved to:\n{mapping_path}')
    else:
        mapping = None
        mapping_path = mapping_path_var.get().strip()
        if mapping_path and os.path.exists(mapping_path):
            try:
                mapping = load_mapping(mapping_path)
            except Exception as exc:
                messagebox.showerror('Loading mapping failed', str(exc))
                return
        if mapping is None:
            mapping = extract_mapping_from_text(ddl_text)
        if mapping is None:
            messagebox.showerror('DDL Masker', 'No mapping JSON found. Provide a mapping file or include an embedded mapping comment.')
            return
        try:
            unmasked = unmask_text(ddl_text, mapping)
        except Exception as exc:
            messagebox.showerror('Unmask failed', str(exc))
            return
        target_text.config(state='normal')
        target_text.delete('1.0', tk.END)
        target_text.insert(tk.END, unmasked)
        target_text.config(state='disabled')
        messagebox.showinfo('DDL Masker', 'Unmasking complete.')


def show_mapping_text(source_text):
    ddl_text = source_text.get('1.0', tk.END)
    mapping = extract_mapping_from_text(ddl_text)
    if mapping is None:
        messagebox.showinfo('Mapping Viewer', 'No embedded mapping comment found in the input text.')
        return

    window = tk.Toplevel()
    window.title('Embedded Mapping JSON')
    window.geometry('600x400')
    text = scrolledtext.ScrolledText(window, wrap=tk.WORD)
    text.pack(fill=tk.BOTH, expand=True)
    text.insert(tk.END, json.dumps(mapping, indent=2, sort_keys=True))
    text.config(state='disabled')


def build_gui():
    root = tk.Tk()
    root.title('DDL Masker GUI')
    root.geometry('1100x700')

    control_frame = ttk.Frame(root, padding='10')
    control_frame.pack(fill=tk.X)

    mode_var = tk.StringVar(value='mask')
    dialect_var = tk.StringVar(value='generic')
    embed_var = tk.BooleanVar(value=True)
    sql_path_var = tk.StringVar()
    mapping_path_var = tk.StringVar()

    ttk.Label(control_frame, text='Mode:').grid(row=0, column=0, sticky=tk.W)
    ttk.Radiobutton(control_frame, text='Mask', variable=mode_var, value='mask').grid(row=0, column=1, sticky=tk.W)
    ttk.Radiobutton(control_frame, text='Unmask', variable=mode_var, value='unmask').grid(row=0, column=2, sticky=tk.W)

    ttk.Label(control_frame, text='Dialect:').grid(row=0, column=3, sticky=tk.W, padx=(20, 0))
    dialect_box = ttk.Combobox(control_frame, textvariable=dialect_var, values=SUPPORTED_DIALECTS, state='readonly', width=14)
    dialect_box.grid(row=0, column=4, sticky=tk.W)

    ttk.Checkbutton(control_frame, text='Embed mapping', variable=embed_var).grid(row=0, column=5, sticky=tk.W, padx=(20, 0))

    ttk.Label(control_frame, text='SQL file:').grid(row=1, column=0, sticky=tk.W, pady=(10, 0))
    sql_entry = ttk.Entry(control_frame, textvariable=sql_path_var, width=70, state='readonly')
    sql_entry.grid(row=1, column=1, columnspan=4, sticky=tk.W+tk.E, pady=(10, 0))
    ttk.Button(
        control_frame,
        text='Browse SQL',
        command=lambda: select_sql_file(sql_path_var, source_text),
    ).grid(row=1, column=5, sticky=tk.W, padx=(10, 0), pady=(10, 0))

    ttk.Label(control_frame, text='Mapping path:').grid(row=2, column=0, sticky=tk.W, pady=(10, 0))
    mapping_entry = ttk.Entry(control_frame, textvariable=mapping_path_var, width=70)
    mapping_entry.grid(row=2, column=1, columnspan=4, sticky=tk.W+tk.E, pady=(10, 0))
    mapping_buttons = ttk.Frame(control_frame)
    mapping_buttons.grid(row=2, column=5, sticky=tk.W, padx=(10, 0), pady=(10, 0))
    ttk.Button(mapping_buttons, text='Save location', command=lambda: select_mapping_location(mapping_path_var)).pack(side=tk.LEFT)
    ttk.Button(mapping_buttons, text='JSON file', command=lambda: select_mapping_file(mapping_path_var)).pack(side=tk.LEFT, padx=(5, 0))

    button_frame = ttk.Frame(root, padding='10')
    button_frame.pack(fill=tk.X)

    ttk.Button(button_frame, text='Process', command=lambda: process_action(mode_var, dialect_var, embed_var, mapping_path_var, source_text, target_text)).pack(side=tk.LEFT)
    ttk.Button(button_frame, text='Show embedded mapping', command=lambda: show_mapping_text(source_text)).pack(side=tk.LEFT, padx=(10, 0))
    ttk.Button(button_frame, text='Clear input', command=lambda: clear_input(source_text, sql_path_var)).pack(side=tk.LEFT, padx=(10, 0))
    ttk.Button(button_frame, text='Clear output', command=lambda: clear_text(target_text)).pack(side=tk.LEFT, padx=(10, 0))

    pane = ttk.PanedWindow(root, orient=tk.HORIZONTAL)
    pane.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

    source_frame = ttk.Labelframe(pane, text='Input DDL')
    target_frame = ttk.Labelframe(pane, text='Output DDL')

    source_text = scrolledtext.ScrolledText(source_frame, wrap=tk.WORD)
    source_text.pack(fill=tk.BOTH, expand=True)
    target_text = scrolledtext.ScrolledText(target_frame, wrap=tk.WORD, state='disabled')
    target_text.pack(fill=tk.BOTH, expand=True)

    pane.add(source_frame, weight=1)
    pane.add(target_frame, weight=1)

    return root


def clear_text(target_text):
    target_text.config(state='normal')
    target_text.delete('1.0', tk.END)
    target_text.config(state='disabled')


def clear_input(source_text, sql_path_var):
    source_text.delete('1.0', tk.END)
    sql_path_var.set('')


if __name__ == '__main__':
    root = build_gui()
    root.mainloop()
