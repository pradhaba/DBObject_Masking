#!/usr/bin/env python3
"""GUI for the DDL masking/unmasking tool."""

import json
import time
import tkinter as tk
from tkinter import messagebox, scrolledtext, simpledialog, ttk

from masker import (
    extract_mapping_from_text,
    mask_text,
    unmask_text,
)


def update_processing_progress(run_context, percent, status):
    """Update stage, elapsed time, and rolling ETA in the migration workspace."""
    if not run_context or run_context.get('progress_var') is None:
        return
    percent = max(0, min(100, int(percent)))
    if percent <= 3 or 'progress_started_at' not in run_context:
        run_context['progress_started_at'] = time.monotonic()
    elapsed = max(0, time.monotonic() - run_context['progress_started_at'])
    eta = (elapsed * (100 - percent) / percent) if 0 < percent < 100 else 0
    timing = f'Elapsed {elapsed:.1f}s'
    if 0 < percent < 100:
        timing += f' • estimated {eta:.1f}s remaining'
    run_context['progress_var'].set(percent)
    run_context['progress_status_var'].set(f'{status} — {percent}% — {timing}')
    widget = run_context.get('progress_widget')
    if widget is not None:
        widget.update_idletasks()

def process_action(mode_var, source_dialect_var, target_dialect_var, embed_var, source_text, target_text, mapping_text,
                   skill_text, target_routine_var, project=None, object_path_var=None, workflow_action=None, run_context=None):
    if run_context is not None:
        for button_name in ('approval_button', 'migrate_button'):
            if run_context.get(button_name) is not None:
                run_context[button_name].config(state='disabled')
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
        metadata_connection = None
        source_metadata_connection = None
        source_catalog = None
        update_processing_progress(run_context, 1, 'Starting Test Migrate')
        try:
            source_available = bool(getattr(project, 'source_available', True))
            if source_dialect == 'sybase_asa' and source_available:
                update_processing_progress(run_context, 2, 'Discovering referenced ASA objects')
                from workflow import cache_project_password, get_project_password, open_database_connection
                source_password = get_project_password(project.id, 'source') or getattr(project, 'source_password', None)
                if not source_password:
                    source_password = simpledialog.askstring('SQL Anywhere metadata', 'Source password:', show='*')
                    if source_password is None:return
                    cache_project_password(project.id, source_password, 'source')
                source_metadata_connection = open_database_connection(
                    project.source_database,
                    {'host': project.host, 'port': project.port, 'database': project.database, 'username': project.username},
                    source_password,
                )
                from source_catalog import capture_source_catalog
                source_catalog = capture_source_catalog(source_metadata_connection, project.id, ddl_text)
                update_processing_progress(run_context, 5, 'Stored ASA object definitions in SQLite')
            if target_dialect == 'postgresql':
                update_processing_progress(run_context, 2, 'Connecting to PostgreSQL metadata')
                from workflow import cache_project_password, get_project_password
                if not all((project.target_host, project.target_port, project.target_database_name, project.target_username)):
                    messagebox.showerror('PostgreSQL metadata', 'Complete the target connection details by editing this project.')
                    return
                password = get_project_password(project.id) or getattr(project, 'target_password', None)
                if not password:
                    password = simpledialog.askstring('PostgreSQL metadata', 'Target password:', show='*')
                    if password is None:return
                    cache_project_password(project.id, password)
                    project.target_password = password
                import psycopg
                metadata_connection = psycopg.connect(
                    host=project.target_host, port=project.target_port,
                    dbname=project.target_database_name, user=project.target_username,
                    password=password, connect_timeout=5,
                )
                update_processing_progress(run_context, 3, 'Connected to PostgreSQL metadata')
            migrated_text, mapping, skill = migrate_text(
                ddl_text, source_dialect, target_dialect,
                target_override=target_routine_var.get(),
                metadata_connection=metadata_connection,
                formatter_indent=project.formatter_indent,
                progress_callback=lambda percent, status: update_processing_progress(
                    run_context, percent, status
                ),
                source_catalog=source_catalog,
                source_available=source_available,
            )
        except Exception as exc:
            update_processing_progress(run_context, 100, 'Migration preview failed')
            stopped_diagnostics = [{
                'severity': 'error', 'code': 'MIGRATION_STOPPED', 'line': None,
                'column': None, 'expression': '', 'message': str(exc),
                'suggestion': 'Correct the structural source error and run migration again.',
                'migration_continued': False, 'resolved': False,
            }]
            failed_run_id = persist_processing(
                project, object_path_var, 'migrate', target_dialect, ddl_text, '', {},
                technical_status='failed', diagnostics=stopped_diagnostics,
            )
            if run_context is not None:
                run_context.update(run_id=failed_run_id, output='')
            show_migration_diagnostics(run_context, stopped_diagnostics, 'failed', 'pending_review')
            messagebox.showerror('Migration failed', str(exc))
            return
        finally:
            if metadata_connection is not None:
                metadata_connection.close()
            if source_metadata_connection is not None:
                source_metadata_connection.close()
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
            technical_status=skill.get('technical_status', 'success'),
            diagnostics=skill.get('diagnostics', []),
        )
        update_processing_progress(run_context, 100, 'Test Migrate completed')
        if run_context is not None:
            run_context.update(run_id=run_id, skill_version_id=skill['id'], output=migrated_text)
            if run_context.get('approval_button') is not None:
                run_context['approval_button'].config(state='normal')
            if run_context.get('migrate_button') is not None:
                run_context['migrate_button'].config(state='disabled')
        show_migration_diagnostics(
            run_context, skill.get('diagnostics', []), skill.get('technical_status', 'success'), 'pending_review'
        )
        if project is not None and run_context is not None and run_context.get('test_plan_callback'):
            run_context['test_plan_callback'](project, ddl_text, migrated_text)
        issue_count = len([
            item for item in skill.get('diagnostics', [])
            if not item.get('resolved') and item.get('severity') == 'error'
        ])
        if issue_count:
            messagebox.showwarning(
                'Migration needs modification',
                f"A review draft was generated with {issue_count} unresolved issue(s).\n"
                "Open the Error Review tab for the source location and suggested correction.",
            )
        else:
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
                       human_override=None, routine_analysis=None, routine_language=None,
                       technical_status='success', diagnostics=None):
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
        technical_status=technical_status,
        review_status='pending_review',
        diagnostics=diagnostics,
    )


def show_migration_diagnostics(run_context, diagnostics, technical_status, review_status):
    if not run_context or 'issue_table' not in run_context:
        return
    table = run_context['issue_table']
    table.delete(*table.get_children())
    for index, item in enumerate(diagnostics):
        table.insert('', tk.END, iid=str(index), values=(
            item.get('severity', '').upper(), item.get('code', ''),
            item.get('line') or '', item.get('message', ''),
        ), tags=(item.get('severity', 'warning'),))
    table.tag_configure('error', foreground='#b91c1c')
    table.tag_configure('warning', foreground='#9a6700')
    run_context['diagnostics'] = diagnostics
    run_context['technical_status_var'].set(f"Migration: {technical_status.replace('_', ' ').title()}")
    run_context['review_status_var'].set(f"Review: {review_status.replace('_', ' ').title()}")
    run_context['detail_tabs'].tab(
        run_context['issues_tab'], text=f"Error Review ({len(diagnostics)})"
    )
    set_readonly_text(run_context['issue_detail'], 'Select an issue to view its details.' if diagnostics else 'No unresolved migration issues.')
    if run_context.get('review_message_var') is not None:
        unresolved_errors = [
            item for item in diagnostics
            if item.get('severity') == 'error' and not item.get('resolved')
        ]
        if unresolved_errors:
            run_context['review_message_var'].set(
                f'{len(unresolved_errors)} unresolved error(s) block approval. '
                'Review the findings, choose Needs modification, correct the input, and retest.'
            )
        elif technical_status == 'not_run':
            run_context['review_message_var'].set('Run Test Migrate before choosing a review decision.')
        else:
            run_context['review_message_var'].set(
                'No unresolved errors. Enter the reviewer name, optionally add notes, then approve or reject.'
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


def build_gui(root=None, initial_files=None, initial_action='mask', initial_dialect='generic', project=None, navigation=None):
    root = root or tk.Tk()
    for child in root.winfo_children():
        child.destroy()
    window = root.winfo_toplevel()
    window.title('DDL Masker & Database Migration')
    screen_width = window.winfo_screenwidth()
    screen_height = window.winfo_screenheight()
    width = min(1350, max(760, screen_width - 80))
    height = min(760, max(520, screen_height - 120))
    window.geometry(f'{width}x{height}')
    window.minsize(min(760, width), min(520, height))

    initial_files = list(initial_files or [])
    if project is not None:
        banner = ttk.Frame(root, padding=(10, 8))
        banner.pack(fill=tk.X)
        home_command = navigation.show_projects if navigation is not None else lambda: return_to_launcher(root)
        back_command = navigation.show_files if navigation is not None else lambda: return_to_project(root, project)
        ttk.Button(banner, text='Projects', command=home_command).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(banner, text='Source Files', command=back_command).pack(side=tk.LEFT, padx=(0, 12))
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
    if navigation is not None:
        run_context['test_plan_callback'] = navigation.prepare_routine_test_plan
    def reset_run_context():
        preserved = {key: value for key, value in run_context.items()
                     if key == 'test_plan_callback' or key.endswith('_var') or key in {
                         'issue_table', 'issue_detail', 'detail_tabs', 'issues_tab',
                         'approval_button', 'migrate_button', 'review_callback',
                         'progress_widget'
                     }}
        run_context.clear()
        run_context.update(preserved)
        for button_name in ('approval_button', 'migrate_button'):
            if run_context.get(button_name) is not None:
                run_context[button_name].config(state='disabled')
        if run_context.get('reviewer_var') is not None:
            run_context['reviewer_var'].set('')
        if run_context.get('review_notes_var') is not None:
            run_context['review_notes_var'].set('')
        if run_context.get('progress_var') is not None:
            run_context['progress_var'].set(0)
        if run_context.get('progress_status_var') is not None:
            run_context['progress_status_var'].set('Ready — waiting for Test Migrate')
        show_migration_diagnostics(run_context, [], 'not_run', 'pending_review')

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
        target_routine_box.bind('<<ComboboxSelected>>', lambda _e: (reset_run_context(), clear_results(target_text, mapping_text, skill_text)))

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
        reset_run_context()
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
        reset_run_context()
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

    is_postgresql_migration = (
        project is not None and mode_var.get() == 'migrate' and project.target_database == 'PostgreSQL'
    )
    ttk.Button(button_frame, text='Test Migrate' if is_postgresql_migration else 'Process', command=lambda: process_action(
        mode_var, source_dialect_var, target_dialect_var, embed_var, source_text, target_text, mapping_text, skill_text, target_routine_var,
        project, object_var, None,
        run_context,
    )).pack(side=tk.LEFT)
    if is_postgresql_migration:
        approval_button = ttk.Button(
            button_frame, text='Test Approved', state='disabled',
            command=lambda: approve_test_migration(run_context),
        )
        approval_button.pack(side=tk.LEFT, padx=(10, 0))
        migrate_button = ttk.Button(
            button_frame, text='Migrate', state='disabled',
            command=lambda: migrate_approved_to_postgresql(project, run_context),
        )
        migrate_button.pack(side=tk.LEFT, padx=(10, 0))
        run_context.update(approval_button=approval_button, migrate_button=migrate_button)
    ttk.Button(button_frame, text='Copy output', command=lambda: copy_to_clipboard(target_text, 'Output DDL')).pack(side=tk.LEFT, padx=(10, 0))
    ttk.Button(button_frame, text='Copy mapping', command=lambda: copy_to_clipboard(mapping_text, 'Mapping JSON')).pack(side=tk.LEFT, padx=(10, 0))
    ttk.Button(button_frame, text='Copy skill trace', command=lambda: copy_to_clipboard(skill_text, 'Skill trace')).pack(side=tk.LEFT, padx=(10, 0))
    ttk.Button(button_frame, text='Show embedded mapping', command=lambda: show_mapping_text(source_text)).pack(side=tk.LEFT, padx=(10, 0))
    ttk.Button(button_frame, text='Clear output', command=lambda: clear_results(target_text, mapping_text, skill_text)).pack(side=tk.LEFT, padx=(10, 0))

    progress_frame = ttk.Frame(root, padding=(10, 0, 10, 4))
    progress_frame.pack(fill=tk.X)
    progress_var = tk.IntVar(value=0)
    progress_status_var = tk.StringVar(value='Ready — waiting for Test Migrate')
    progress_widget = ttk.Progressbar(
        progress_frame, variable=progress_var, maximum=100, mode='determinate'
    )
    progress_widget.pack(side=tk.LEFT, fill=tk.X, expand=True)
    ttk.Label(progress_frame, textvariable=progress_status_var, width=72).pack(
        side=tk.LEFT, padx=(10, 0)
    )
    run_context.update(
        progress_var=progress_var, progress_status_var=progress_status_var,
        progress_widget=progress_widget,
    )

    workspace_tabs = ttk.Notebook(root)
    workspace_tabs.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

    source_frame = ttk.Frame(workspace_tabs)
    target_frame = ttk.Frame(workspace_tabs)
    mapping_tab = ttk.Frame(workspace_tabs)
    skill_tab = ttk.Frame(workspace_tabs)
    issues_tab = ttk.Frame(workspace_tabs)
    workspace_tabs.add(source_frame, text='Input DDL')
    workspace_tabs.add(target_frame, text='Output DDL')
    workspace_tabs.add(mapping_tab, text='JSON Mapping')
    workspace_tabs.add(skill_tab, text='Skills Used')
    workspace_tabs.add(issues_tab, text='Error Review (0)')

    source_text = scrolledtext.ScrolledText(source_frame, wrap=tk.NONE)
    source_xscroll = ttk.Scrollbar(source_frame, orient=tk.HORIZONTAL, command=source_text.xview)
    source_text.configure(xscrollcommand=source_xscroll.set)
    source_xscroll.pack(side=tk.BOTTOM, fill=tk.X)
    source_text.pack(fill=tk.BOTH, expand=True)
    target_text = scrolledtext.ScrolledText(target_frame, wrap=tk.NONE, state='disabled')
    target_xscroll = ttk.Scrollbar(target_frame, orient=tk.HORIZONTAL, command=target_text.xview)
    target_text.configure(xscrollcommand=target_xscroll.set)
    target_xscroll.pack(side=tk.BOTTOM, fill=tk.X)
    target_text.pack(fill=tk.BOTH, expand=True)
    mapping_text = scrolledtext.ScrolledText(mapping_tab, wrap=tk.NONE, state='disabled')
    mapping_text.pack(fill=tk.BOTH, expand=True)
    skill_text = scrolledtext.ScrolledText(skill_tab, wrap=tk.WORD, state='disabled')
    skill_text.pack(fill=tk.BOTH, expand=True)

    status_bar = ttk.Frame(issues_tab, padding=6)
    status_bar.pack(fill=tk.X)
    technical_status_var = tk.StringVar(value='Migration: Not Run')
    review_status_var = tk.StringVar(value='Review: Pending Review')
    ttk.Label(status_bar, textvariable=technical_status_var, font=('Segoe UI', 9, 'bold')).pack(side=tk.LEFT)
    ttk.Label(status_bar, textvariable=review_status_var).pack(side=tk.LEFT, padx=16)
    issue_table = ttk.Treeview(
        issues_tab, columns=('severity', 'code', 'line', 'message'), show='headings', height=8
    )
    for key, label, width in (
        ('severity', 'Severity', 75), ('code', 'Issue code', 190),
        ('line', 'Line', 55), ('message', 'Finding', 460),
    ):
        issue_table.heading(key, text=label)
        issue_table.column(key, width=width, anchor=tk.W)
    issue_table.pack(fill=tk.BOTH, expand=True, padx=6)
    issue_detail = scrolledtext.ScrolledText(issues_tab, wrap=tk.WORD, state='disabled', height=8)
    issue_detail.pack(fill=tk.BOTH, expand=True, padx=6, pady=6)

    run_context.update(
        issue_table=issue_table, issue_detail=issue_detail, detail_tabs=workspace_tabs,
        issues_tab=issues_tab, technical_status_var=technical_status_var,
        review_status_var=review_status_var, diagnostics=[],
    )

    def load_issue_detail(_event=None):
        selected = issue_table.selection()
        if not selected:
            return
        item = run_context.get('diagnostics', [])[int(selected[0])]
        details = [
            f"Severity: {item.get('severity', '').upper()}",
            f"Code: {item.get('code', '')}",
            f"Location: line {item.get('line') or 'unknown'}, column {item.get('column') or 'unknown'}",
            f"Migration continued: {'Yes' if item.get('migration_continued') else 'No'}",
            '', f"Expression: {item.get('expression') or 'Not available'}",
            '', f"Issue: {item.get('message', '')}",
            '', f"Suggested action: {item.get('suggestion', '')}",
        ]
        set_readonly_text(issue_detail, '\n'.join(details))

    issue_table.bind('<<TreeviewSelect>>', load_issue_detail)

    review_form = ttk.Frame(issues_tab, padding=(6, 3))
    review_form.pack(fill=tk.X)
    reviewer_var = tk.StringVar()
    review_notes_var = tk.StringVar()
    review_message_var = tk.StringVar(
        value='Review the findings. Approval is available only when no unresolved errors remain.'
    )
    ttk.Label(review_form, text='Reviewer:').grid(row=0, column=0, sticky=tk.W)
    ttk.Entry(review_form, textvariable=reviewer_var, width=28).grid(
        row=0, column=1, sticky=tk.EW, padx=(6, 18)
    )
    ttk.Label(review_form, text='Review notes:').grid(row=0, column=2, sticky=tk.W)
    ttk.Entry(review_form, textvariable=review_notes_var).grid(
        row=0, column=3, sticky=tk.EW, padx=(6, 0)
    )
    review_form.columnconfigure(3, weight=1)
    ttk.Label(
        review_form, textvariable=review_message_var, foreground='#075985', wraplength=1000
    ).grid(row=1, column=0, columnspan=4, sticky=tk.W, pady=(6, 0))

    def set_review(decision):
        workspace_tabs.select(issues_tab)
        run_id = run_context.get('run_id')
        if not run_id:
            review_message_var.set('Run Test Migrate before changing the review status.')
            return False
        reviewer = reviewer_var.get().strip()
        notes = review_notes_var.get().strip()
        if decision in {'approved', 'rejected'} and not reviewer:
            review_message_var.set('Enter the reviewer name before approving or rejecting.')
            return False
        unresolved_errors = [
            item for item in run_context.get('diagnostics', [])
            if item.get('severity') == 'error' and not item.get('resolved')
        ]
        if decision == 'approved' and unresolved_errors:
            review_message_var.set(
                f'Approval blocked: {len(unresolved_errors)} unresolved error(s). '
                'Choose Needs modification, correct the Input DDL, and run Test Migrate again.'
            )
            return False
        try:
            from database import set_processing_review
            set_processing_review(run_id, decision, reviewer, notes)
        except Exception as exc:
            review_message_var.set(f'Unable to save review: {exc}')
            return False
        review_status_var.set(f"Review: {decision.replace('_', ' ').title()}")
        if run_context.get('migrate_button') is not None:
            run_context['migrate_button'].config(
                state='normal' if decision == 'approved' else 'disabled'
            )
        messages = {
            'needs_modification': 'Marked Needs modification. Correct the input and run Test Migrate again.',
            'approved': 'Test migration approved. The Migrate button is now enabled.',
            'rejected': 'Test migration rejected. Migrate remains disabled.',
        }
        review_message_var.set(messages[decision])
        return True

    review_actions = ttk.Frame(issues_tab, padding=6)
    review_actions.pack(fill=tk.X)
    ttk.Button(review_actions, text='Needs modification', command=lambda: set_review('needs_modification')).pack(side=tk.LEFT)
    ttk.Button(review_actions, text='Approve', command=lambda: set_review('approved')).pack(side=tk.LEFT, padx=6)
    ttk.Button(review_actions, text='Reject', command=lambda: set_review('rejected')).pack(side=tk.LEFT)

    run_context.update(
        reviewer_var=reviewer_var, review_notes_var=review_notes_var,
        review_message_var=review_message_var, review_callback=set_review,
    )

    def source_changed(_event=None):
        if not source_text.edit_modified():
            return
        source_text.edit_modified(False)
        reset_run_context()
        clear_results(target_text, mapping_text, skill_text)

    source_text.bind('<<Modified>>', source_changed)

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


def approve_test_migration(run_context):
    """Approve the displayed preview and unlock the committed migration step."""
    run_id = run_context.get('run_id')
    if not run_id or not run_context.get('output'):
        if run_context.get('detail_tabs') is not None and run_context.get('issues_tab') is not None:
            run_context['detail_tabs'].select(run_context['issues_tab'])
        if run_context.get('review_message_var') is not None:
            run_context['review_message_var'].set('Run Test Migrate and review its output first.')
        return
    if run_context.get('detail_tabs') is not None and run_context.get('issues_tab') is not None:
        run_context['detail_tabs'].select(run_context['issues_tab'])
    callback = run_context.get('review_callback')
    if callback is not None:
        callback('approved')


def migrate_approved_to_postgresql(project, run_context):
    """Commit an approved migration preview to its configured PostgreSQL target."""
    if not run_context.get('output') or not run_context.get('run_id'):
        messagebox.showwarning('Migrate', 'Run and approve Test Migrate first.')
        return
    from database import get_processing_run
    saved_run = get_processing_run(run_context['run_id'])
    if not saved_run or saved_run.get('review_status') != 'approved':
        if run_context.get('migrate_button') is not None:
            run_context['migrate_button'].config(state='disabled')
        messagebox.showerror('Migrate', 'This test migration is not approved. Approve it before migrating.')
        return
    if not messagebox.askyesno(
        'Confirm migration',
        f"Apply the approved DDL to {project.target_database_name} on {project.target_host}?\n\n"
        'This step commits changes to the target database.',
    ):
        return
    if not all((project.target_host, project.target_port, project.target_database_name, project.target_username)):
        messagebox.showerror('Migrate', 'Complete the target PostgreSQL connection details in the project.')
        return
    from workflow import cache_project_password, get_project_password
    password = get_project_password(project.id) or getattr(project, 'target_password', None)
    if not password:
        password = simpledialog.askstring('PostgreSQL password', 'Target database password:', show='*')
        if password is None:
            return
        cache_project_password(project.id, password)
        project.target_password = password
    try:
        from deployment import deploy_postgresql
        result = deploy_postgresql(
            project, run_context['output'], run_context['run_id'],
            run_context['skill_version_id'], password,
        )
    except Exception as exc:
        messagebox.showerror('Migration failed', str(exc))
        return
    if result['deployed']:
        if run_context.get('migrate_button') is not None:
            run_context['migrate_button'].config(state='disabled')
        messagebox.showinfo('Migration complete', 'The approved DDL was committed to PostgreSQL.')
    else:
        messagebox.showerror('Migration failed', result['error'])


def test_migration_in_postgresql(project, run_context):
    if not run_context.get('output'):
        messagebox.showwarning('PostgreSQL test', 'Run the migration before testing it.')
        return
    if not all((project.target_host, project.target_port, project.target_database_name, project.target_username)):
        messagebox.showerror('PostgreSQL test', 'Complete the target PostgreSQL connection details in the project.')
        return
    from workflow import cache_project_password, get_project_password
    password = get_project_password(project.id) or getattr(project, 'target_password', None)
    if not password:
        password = simpledialog.askstring('PostgreSQL password', 'Target database password:', show='*')
        if password is None:
            return
        cache_project_password(project.id, password)
        project.target_password = password
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
