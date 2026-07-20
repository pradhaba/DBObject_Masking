#!/usr/bin/env python3
"""GUI for the DDL masking/unmasking tool."""

import json
import os
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk

from masker import extract_mapping_from_text, load_mapping, mask_text, unmask_text

SUPPORTED_DIALECTS = ['generic', 'sybase_asa', 'postgresql']


def select_mapping_file(mapping_path_var):
    file_path = filedialog.askopenfilename(
        title='Select mapping JSON file',
        filetypes=[('JSON files', '*.json'), ('All files', '*.*')],
    )
    if file_path:
        mapping_path_var.set(file_path)


def save_mapping_file(mapping, mapping_path_var):
    path = mapping_path_var.get().strip()
    if not path:
        path = filedialog.asksaveasfilename(
            title='Save mapping JSON file',
            defaultextension='.json',
            filetypes=[('JSON files', '*.json'), ('All files', '*.*')],
        )
        if not path:
            return None
        mapping_path_var.set(path)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(mapping, f, indent=2, sort_keys=True)
    return path


def process_action(mode_var, dialect_var, embed_var, mapping_path_var, source_text, target_text):
    ddl_text = source_text.get('1.0', tk.END).strip()
    if not ddl_text:
        messagebox.showwarning('DDL Masker', 'Please paste or type DDL text in the input pane.')
        return

    dialect = dialect_var.get()
    if mode_var.get() == 'mask':
        masked_text, mapping = mask_text(ddl_text, dialect)
        if embed_var.get():
            masked_text = masked_text
        mapping_path = mapping_path_var.get().strip()
        if mapping_path:
            try:
                save_mapping_file(mapping, mapping_path_var)
            except Exception as exc:
                messagebox.showerror('Saving mapping failed', str(exc))
                return
        target_text.config(state='normal')
        target_text.delete('1.0', tk.END)
        target_text.insert(tk.END, masked_text)
        target_text.config(state='disabled')
        messagebox.showinfo('DDL Masker', 'Masking complete.')
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
    mapping_path_var = tk.StringVar()

    ttk.Label(control_frame, text='Mode:').grid(row=0, column=0, sticky=tk.W)
    ttk.Radiobutton(control_frame, text='Mask', variable=mode_var, value='mask').grid(row=0, column=1, sticky=tk.W)
    ttk.Radiobutton(control_frame, text='Unmask', variable=mode_var, value='unmask').grid(row=0, column=2, sticky=tk.W)

    ttk.Label(control_frame, text='Dialect:').grid(row=0, column=3, sticky=tk.W, padx=(20, 0))
    dialect_box = ttk.Combobox(control_frame, textvariable=dialect_var, values=SUPPORTED_DIALECTS, state='readonly', width=14)
    dialect_box.grid(row=0, column=4, sticky=tk.W)

    ttk.Checkbutton(control_frame, text='Embed mapping', variable=embed_var).grid(row=0, column=5, sticky=tk.W, padx=(20, 0))

    ttk.Label(control_frame, text='Mapping file:').grid(row=1, column=0, sticky=tk.W, pady=(10, 0))
    mapping_entry = ttk.Entry(control_frame, textvariable=mapping_path_var, width=70)
    mapping_entry.grid(row=1, column=1, columnspan=4, sticky=tk.W+tk.E, pady=(10, 0))
    ttk.Button(control_frame, text='Browse', command=lambda: select_mapping_file(mapping_path_var)).grid(row=1, column=5, sticky=tk.W, padx=(10, 0), pady=(10, 0))

    button_frame = ttk.Frame(root, padding='10')
    button_frame.pack(fill=tk.X)

    ttk.Button(button_frame, text='Process', command=lambda: process_action(mode_var, dialect_var, embed_var, mapping_path_var, source_text, target_text)).pack(side=tk.LEFT)
    ttk.Button(button_frame, text='Show embedded mapping', command=lambda: show_mapping_text(source_text)).pack(side=tk.LEFT, padx=(10, 0))
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


if __name__ == '__main__':
    root = build_gui()
    root.mainloop()
