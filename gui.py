#!/usr/bin/env python3
"""GUI for the DDL masking/unmasking tool."""

import json
import tkinter as tk
from tkinter import messagebox, scrolledtext, simpledialog, ttk

from masker import (
    extract_mapping_from_text,
    mask_text,
    unmask_text,
)

<<<<<<< HEAD
SUPPORTED_DIALECTS = ['generic', 'sybase_asa', 'postgresql', 'MySQL', 'Oracle', 'MSSQL']


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


def process_action(mode_var, dialect_var, embed_var, mapping_path_var, source_text, target_text, mapping_text):
=======
def process_action(mode_var, source_dialect_var, target_dialect_var, embed_var, source_text, target_text, mapping_text,
                   skill_text, target_routine_var, project=None, object_path_var=None, workflow_action=None, run_context=None):
>>>>>>> 8230d46acbc34fe9f9da10c8dcc9c443f5d47f9c
    ddl_text = source_text.get('1.0', tk.END).strip()
    if not ddl_text:
        messagebox.showwarning('DDL Masker', 'Please select a SQL file or paste DDL text in the input pane.')
        return

    from workflow import dialect_for
    source_dialect = dialect_for(source_dialect_var.get())
    target_dialect = dialect_for(target_dialect_var.get())
    selected_action = workflow_action or mode_var.get()
    dialect = target_dialect if selected_action == 'unmask' else source_dialect
    if selected_action == 'migrate' and project is not None:
        from migration_engine import migrate_text
        try:
            migrated_text, mapping, skill = migrate_text(ddl_text, source_dialect, target_dialect, target_override=target_routine_var.get())
        except Exception as exc:
            messagebox.showerror('Migration failed', str(exc))
            return
        set_readonly_text(target_text, migrated_text)
        set_readonly_text(mapping_text, json.dumps(mapping, indent=2, sort_keys=True))
        set_readonly_text(skill_text, format_skill_trace(skill))
        run_id = persist_processing(
            project, object_path_var, 'migrate', target_dialect, ddl_text, migrated_text, mapping,
            migration_skill_id=skill['skill_id'], skill_version_id=skill['id'],
            target_object_type=skill['target_object_type'], classification_reason=skill['classification_reason'],
            skill_trace=skill['trace'],
            classification_rule=skill['classification_rule'], human_override=skill['human_override'],
            routine_analysis=skill['analysis'],
            routine_language=skill['routine_language'],
        )
        if run_context is not None:
            run_context.update(run_id=run_id, skill_version_id=skill['id'], output=migrated_text)
        messagebox.showinfo(
            'Migration',
            f"Migration completed with skill: {skill['name']} v{skill['version']}\n"
            f"PostgreSQL target: {skill['target_object_type']} ({skill['classification_reason']})\n"
            "Mapping saved to the project database.",
        )
    elif selected_action == 'mask':
        masked_text, mapping = mask_text(ddl_text, dialect, embed_mapping=embed_var.get())
        set_readonly_text(target_text, masked_text)
        set_readonly_text(mapping_text, json.dumps(mapping, indent=2, sort_keys=True))
        set_readonly_text(skill_text, 'Masking used the approved SQLite masking_rules registry.\nNo migration skill was applied.')
        persist_processing(project, object_path_var, 'mask', dialect, ddl_text, masked_text, mapping)
        messagebox.showinfo('DDL Masker', 'Masking complete.\nMapping saved to the project database.')
    else:
        mapping = None
        mapping = extract_mapping_from_text(ddl_text)
        if mapping is None and project is not None:
            from database import latest_mapping
            object_path = object_path_var.get() if object_path_var is not None else ''
            mapping = latest_mapping(project.id, object_path)
        if mapping is None:
            messagebox.showerror('DDL Masker', 'No mapping found in the project database or embedded SQL metadata.')
            return
        try:
            unmasked = unmask_text(ddl_text, mapping, dialect)
        except Exception as exc:
            messagebox.showerror('Unmask failed', str(exc))
            return
        set_readonly_text(target_text, unmasked)
        set_readonly_text(mapping_text, json.dumps(mapping, indent=2, sort_keys=True))
        set_readonly_text(skill_text, 'Unmasking used the SQLite unmasking_rules registry and stored object mapping.\nNo migration skill was applied.')
        persist_processing(project, object_path_var, 'unmask', dialect, ddl_text, unmasked, mapping)
        messagebox.showinfo('DDL Masker', 'Unmasking complete.')


def persist_processing(project, object_path_var, operation, dialect, input_ddl, output_ddl, mapping,
                       migration_skill_id=None, skill_version_id=None, target_object_type=None,
                       classification_reason=None, skill_trace=None, classification_rule=None,
                       human_override=None, routine_analysis=None, routine_language=None):
    """Save a successful workspace operation without coupling the masker to SQLite."""
    if project is None:
        return None
    from database import record_processing
    from workflow import dialect_for

    object_path = object_path_var.get() if object_path_var is not None else ''
    return record_processing(
        project.id, object_path, operation, dialect, input_ddl, output_ddl, mapping,
        source_dialect=dialect_for(getattr(project, 'source_database', '')),
        target_dialect=dialect_for(getattr(project, 'target_database', '')),
        migration_skill_id=migration_skill_id,
        skill_version_id=skill_version_id,
        target_object_type=target_object_type,
        classification_reason=classification_reason,
        skill_trace=skill_trace,
        classification_rule=classification_rule,
        human_override=human_override,
        routine_analysis=routine_analysis,
        routine_language=routine_language,
    )


def format_skill_trace(skill):
    lines = [
        f"Skill: {skill['name']}",
        f"Version: {skill['version']}",
        f"Source: {skill['source_dialect']}",
        f"Destination: {skill['target_dialect']}",
        f"PostgreSQL object: {skill['target_object_type']}",
        f"Reason: {skill['classification_reason']}",
        f"Classification rule: {skill['classification_rule']}",
        f"Human override: {skill['human_override'] or 'None (Auto)'}",
        f"Routine language: {skill['routine_language'] or 'Not applicable'}",
        "",
        "Line-by-line rule trace",
        "=" * 72,
    ]
    for item in skill['trace']:
        rules = ', '.join(f"{rule['rule_code']} (priority {rule['priority']})" for rule in item['rules'])
        lines.append(f"Line {item['line']}: {rules or 'No migration rule applied'}")
        lines.append(f"  Source: {item['source']}")
        lines.append(f"  Output: {item['output']}")
    return '\n'.join(lines)


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


def build_gui(root=None, initial_files=None, initial_action='mask', initial_dialect='generic', project=None):
    root = root or tk.Tk()
    for child in root.winfo_children():
        child.destroy()
    root.title('DDL Masker GUI')
    screen_width = root.winfo_screenwidth()
    screen_height = root.winfo_screenheight()
    width = min(1350, max(760, screen_width - 80))
    height = min(760, max(520, screen_height - 120))
    root.geometry(f'{width}x{height}')
    root.minsize(min(760, width), min(520, height))

    initial_files = list(initial_files or [])
    if project is not None:
        banner = ttk.Frame(root, padding=(10, 8))
        banner.pack(fill=tk.X)
        ttk.Button(banner, text='Home', command=lambda: return_to_launcher(root)).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(banner, text='Back', command=lambda: return_to_project(root, project)).pack(side=tk.LEFT, padx=(0, 12))
        ttk.Label(banner, text=project.name, font=('Segoe UI', 13, 'bold')).pack(side=tk.LEFT)
        workflow = {'migrate': 'Migration preparation', 'unmask': 'Unmasking'}.get(initial_action, 'Masking')
        ttk.Label(
            banner,
            text=f'  •  {workflow}: {project.source_database} → {project.target_database}  •  {len(initial_files)} selected object(s)',
        ).pack(side=tk.LEFT)
        if initial_action == 'migrate':
            from database import get_migration_skill
            from workflow import dialect_for
            skill = get_migration_skill(dialect_for(project.source_database), dialect_for(project.target_database))
            ttk.Label(
                banner,
                text=f"  •  Active skill: {skill['name'] if skill else 'No enabled skill'}",
                foreground='#075985' if skill else '#b91c1c',
            ).pack(side=tk.LEFT)

    control_frame = ttk.Frame(root, padding='10')
    control_frame.pack(fill=tk.X)

    project_action = getattr(project, 'default_operation', '') if project is not None else ''
    mode_var = tk.StringVar(value=project_action or initial_action)
    from workflow import SUPPORTED_DATABASES
    source_name = getattr(project, 'source_database', 'SQL Anywhere ASA') if project is not None else 'SQL Anywhere ASA'
    target_name = getattr(project, 'target_database', 'PostgreSQL') if project is not None else 'PostgreSQL'
    source_name = 'SQL Anywhere ASA' if source_name == 'SAP ASA' else source_name
    target_name = 'SQL Anywhere ASA' if target_name == 'SAP ASA' else target_name
    source_dialect_var = tk.StringVar(value=source_name)
    target_dialect_var = tk.StringVar(value=target_name)
    embed_var = tk.BooleanVar(value=False)
    object_var = None
    target_routine_var = tk.StringVar(value='auto')
    run_context = {}

    if initial_files:
        ttk.Label(control_frame, text='Selected object:').grid(row=1, column=0, sticky=tk.W, pady=(10, 0))
        object_var = tk.StringVar(value=str(initial_files[0]))
        object_display_var = tk.StringVar(value=initial_files[0].name)
        object_box = ttk.Combobox(
            control_frame, textvariable=object_display_var, values=[p.name for p in initial_files], state='readonly', width=70
        )
        object_box.grid(row=1, column=1, columnspan=4, sticky=tk.W+tk.E, pady=(10, 0))
        object_box.current(0)

        def load_selected_object(_event=None):
            index = object_box.current()
            if index < 0:
                index = 0
                object_box.current(0)
            path = str(initial_files[index])
            object_var.set(path)
            try:
                with open(path, 'r', encoding='utf-8-sig') as sql_file:
                    ddl_text = sql_file.read()
            except (OSError, UnicodeError) as exc:
                messagebox.showerror('Loading SQL failed', str(exc))
                return
            source_text.delete('1.0', tk.END)
            source_text.insert('1.0', ddl_text)
            clear_results(target_text, mapping_text, skill_text)

        object_box.bind('<<ComboboxSelected>>', load_selected_object)
        ttk.Label(control_frame, text='PostgreSQL routine:').grid(row=1, column=5, sticky=tk.W, padx=(20, 0), pady=(10, 0))
        target_routine_box = ttk.Combobox(
            control_frame, textvariable=target_routine_var,
            values=('auto', 'function', 'procedure'), state='readonly', width=14,
        )
        target_routine_box.grid(row=1, column=6, sticky=tk.W, pady=(10, 0))
        target_routine_box.bind('<<ComboboxSelected>>', lambda _e: (run_context.clear(), clear_results(target_text, mapping_text, skill_text)))

    ttk.Label(control_frame, text='Mode:').grid(row=0, column=0, sticky=tk.W)
    mode_box = ttk.Combobox(
        control_frame, textvariable=mode_var, values=('mask', 'unmask', 'migrate'),
        state='readonly', width=16,
    )
    mode_box.grid(row=0, column=1, columnspan=2, sticky=tk.W)

    def mode_changed(_event=None):
        if project is None:
            return
        from workflow import save_projects, load_projects
        project.default_operation = mode_var.get()
        run_context.clear()
        clear_results(target_text, mapping_text, skill_text)
        projects = load_projects()
        for index, item in enumerate(projects):
            if item.id == project.id:
                projects[index] = project
                break
        save_projects(projects)

    mode_box.bind('<<ComboboxSelected>>', mode_changed)

    ttk.Label(control_frame, text='Source dialect:').grid(row=0, column=3, sticky=tk.W, padx=(20, 0))
    source_dialect_box = ttk.Combobox(control_frame, textvariable=source_dialect_var, values=SUPPORTED_DATABASES, state='readonly', width=20)
    source_dialect_box.grid(row=0, column=4, sticky=tk.W)
    ttk.Label(control_frame, text='Destination dialect:').grid(row=0, column=5, sticky=tk.W, padx=(20, 0))
    target_dialect_box = ttk.Combobox(control_frame, textvariable=target_dialect_var, values=SUPPORTED_DATABASES, state='readonly', width=20)
    target_dialect_box.grid(row=0, column=6, sticky=tk.W)

    def dialects_changed(_event=None):
        if project is None:
            return
        from workflow import load_projects, save_projects
        project.source_database = source_dialect_var.get()
        project.target_database = target_dialect_var.get()
        run_context.clear()
        clear_results(target_text, mapping_text, skill_text)
        projects = load_projects()
        for index, item in enumerate(projects):
            if item.id == project.id:
                projects[index] = project
                break
        save_projects(projects)

    source_dialect_box.bind('<<ComboboxSelected>>', dialects_changed)
    target_dialect_box.bind('<<ComboboxSelected>>', dialects_changed)

    button_frame = ttk.Frame(root, padding='10')
    button_frame.pack(fill=tk.X)

    ttk.Button(button_frame, text='Process', command=lambda: process_action(
        mode_var, source_dialect_var, target_dialect_var, embed_var, source_text, target_text, mapping_text, skill_text, target_routine_var,
        project, object_var, None,
        run_context,
    )).pack(side=tk.LEFT)
    if project is not None and project.target_database == 'PostgreSQL':
        ttk.Button(
            button_frame, text='Test in PostgreSQL',
            command=lambda: test_migration_in_postgresql(project, run_context),
        ).pack(side=tk.LEFT, padx=(10, 0))
    ttk.Button(button_frame, text='Copy output', command=lambda: copy_to_clipboard(target_text, 'Output DDL')).pack(side=tk.LEFT, padx=(10, 0))
    ttk.Button(button_frame, text='Copy mapping', command=lambda: copy_to_clipboard(mapping_text, 'Mapping JSON')).pack(side=tk.LEFT, padx=(10, 0))
    ttk.Button(button_frame, text='Copy skill trace', command=lambda: copy_to_clipboard(skill_text, 'Skill trace')).pack(side=tk.LEFT, padx=(10, 0))
    ttk.Button(button_frame, text='Show embedded mapping', command=lambda: show_mapping_text(source_text)).pack(side=tk.LEFT, padx=(10, 0))
    ttk.Button(button_frame, text='Clear output', command=lambda: clear_results(target_text, mapping_text, skill_text)).pack(side=tk.LEFT, padx=(10, 0))

    pane = ttk.PanedWindow(root, orient=tk.HORIZONTAL)
    pane.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

    source_frame = ttk.Labelframe(pane, text='Input DDL')
    target_frame = ttk.Labelframe(pane, text='Output DDL')
    mapping_frame = ttk.Labelframe(pane, text='Mapping and Skill Details')

    source_text = scrolledtext.ScrolledText(source_frame, wrap=tk.WORD)
    source_text.pack(fill=tk.BOTH, expand=True)
    target_text = scrolledtext.ScrolledText(target_frame, wrap=tk.WORD, state='disabled')
    target_text.pack(fill=tk.BOTH, expand=True)
    detail_tabs = ttk.Notebook(mapping_frame)
    detail_tabs.pack(fill=tk.BOTH, expand=True)
    mapping_tab = ttk.Frame(detail_tabs)
    skill_tab = ttk.Frame(detail_tabs)
    detail_tabs.add(mapping_tab, text='JSON Mapping')
    detail_tabs.add(skill_tab, text='Skill Used')
    mapping_text = scrolledtext.ScrolledText(mapping_tab, wrap=tk.NONE, state='disabled', width=35)
    mapping_text.pack(fill=tk.BOTH, expand=True)
    skill_text = scrolledtext.ScrolledText(skill_tab, wrap=tk.WORD, state='disabled', width=45)
    skill_text.pack(fill=tk.BOTH, expand=True)

    pane.add(source_frame, weight=1)
    pane.add(target_frame, weight=1)
    pane.add(mapping_frame, weight=1)

    if initial_files:
        root.after_idle(load_selected_object)

    return root


def return_to_launcher(root):
    """Return from the processing workspace to the projects home screen."""
    from launcher import Launcher

    Launcher(root, build_gui)


def return_to_project(root, project):
    """Return to object selection for the active project."""
    from launcher import Launcher

    launcher = Launcher(root, build_gui)
    launcher.project = next((item for item in launcher.projects if item.id == project.id), project)
    launcher.show_files()


def test_migration_in_postgresql(project, run_context):
    if not run_context.get('output'):
        messagebox.showwarning('PostgreSQL test', 'Run the migration before testing it.')
        return
    if not all((project.target_host, project.target_port, project.target_database_name, project.target_username)):
        messagebox.showerror('PostgreSQL test', 'Complete the target PostgreSQL connection details in the project.')
        return
    password = simpledialog.askstring('PostgreSQL password', 'Target database password:', show='*')
    if password is None:
        return
    from deployment import test_postgresql_deployment

    result = test_postgresql_deployment(
        project, run_context['output'], run_context['run_id'], run_context['skill_version_id'], password,
    )
    if result['passed']:
        messagebox.showinfo('PostgreSQL test', 'Compilation succeeded. The test transaction was rolled back.')
    else:
        messagebox.showerror(
            'PostgreSQL test',
            f"Compilation failed and correction proposal #{result['proposal_id']} was created.\n\n{result['error']}",
        )


def clear_text(target_text):
    target_text.config(state='normal')
    target_text.delete('1.0', tk.END)
    target_text.config(state='disabled')


def set_readonly_text(text_widget, value):
    text_widget.config(state='normal')
    text_widget.delete('1.0', tk.END)
    text_widget.insert('1.0', value)
    text_widget.config(state='disabled')


def clear_results(target_text, mapping_text, skill_text=None):
    clear_text(target_text)
    clear_text(mapping_text)
    if skill_text is not None:
        clear_text(skill_text)


def copy_to_clipboard(text_widget, label):
    value = text_widget.get('1.0', tk.END).rstrip('\n')
    if not value:
        messagebox.showwarning('DDL Masker', f'{label} is empty.')
        return
    text_widget.clipboard_clear()
    text_widget.clipboard_append(value)
    text_widget.update_idletasks()
    messagebox.showinfo('DDL Masker', f'{label} copied to clipboard.')


if __name__ == '__main__':
    from launcher import run

    run(build_gui)
