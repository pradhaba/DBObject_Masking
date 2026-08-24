"""Pre-workspace project wizard for the DDL Masker desktop application."""

from __future__ import annotations

import shutil
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from workflow import (
    SUPPORTED_DATABASES, WORKSPACE_DIR, clear_project_files, create_project, dialect_for,
    import_sql_files, list_project_files, load_projects, safe_extract_sql_archive, save_projects,
    test_database_connection, remove_project, cache_project_password,
)
from database import (
    add_custom_skill_rule, approve_change_proposal, approve_skill_version, get_skill_version_rules,
    list_change_proposals, list_skill_versions, record_upload, save_object_selection,
    review_skill_rule, update_proposal_rule, update_skill_rule,
)


class Launcher:
    def __init__(self, root: tk.Tk, open_workspace):
        self.root = root
        self.open_workspace = open_workspace
        for child in root.winfo_children():
            child.destroy()
        self.projects = load_projects()
        self.project = None
        self.password = ""
        root.title("DDL Masker & Database Migration")
        fit_window(root, 1250, 850, 760, 520)
        self.notebook = ttk.Notebook(root)
        self.notebook.pack(fill=tk.BOTH, expand=True)
        self.pages = {}
        for key, title in (
            ("projects", "Projects"), ("settings", "Project Settings"),
            ("files", "Source Files"), ("migration", "Migration"),
            ("routine_test", "Routine Test Plan"), ("skills", "Skill Studio"),
        ):
            page = ttk.Frame(self.notebook)
            self.notebook.add(page, text=title)
            self.pages[key] = page
        self.page_canvases = {}
        self.page_containers = {}
        for key in ("projects", "settings", "files", "routine_test", "skills"):
            self._prepare_scrolled_page(key)
        self.container = self.page_containers["projects"]
        self.page_canvas = self.page_canvases["projects"]
        root.bind("<MouseWheel>", self._scroll_page)
        self.notebook.bind("<<NotebookTabChanged>>", self._tab_changed)
        self.show_projects()

    def _prepare_scrolled_page(self, key):
        page = self.pages[key]
        shell = ttk.Frame(page)
        shell.pack(fill=tk.BOTH, expand=True)
        canvas = tk.Canvas(shell, highlightthickness=0)
        page_scroll = ttk.Scrollbar(shell, orient=tk.VERTICAL, command=canvas.yview)
        page_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        container = ttk.Frame(canvas, padding=24)
        page_window = canvas.create_window((0, 0), window=container, anchor=tk.NW)
        container.bind("<Configure>", lambda _e, item=canvas: item.configure(scrollregion=item.bbox("all")))
        canvas.bind("<Configure>", lambda e, item=canvas, window=page_window: item.itemconfigure(window, width=e.width))
        canvas.configure(yscrollcommand=page_scroll.set)
        self.page_canvases[key] = canvas
        self.page_containers[key] = container

    def _activate(self, key):
        self.container = self.page_containers.get(key, self.pages[key])
        self.page_canvas = self.page_canvases.get(key)
        self.notebook.select(self.pages[key])

    def _tab_changed(self, _event=None):
        selected = self.notebook.select()
        for key, page in self.pages.items():
            if str(page) == selected:
                self.container = self.page_containers.get(key, page)
                self.page_canvas = self.page_canvases.get(key)
                if key == "settings" and not self.container.winfo_children():
                    self.root.after_idle(lambda: self.show_project_form(self.project))
                elif key == "files" and not self.container.winfo_children():
                    if self.project is not None:
                        self.root.after_idle(self.show_files)
                    else:
                        self._empty_page("Source Files", "Select or create a project before adding source files.")
                elif key == "skills" and not self.container.winfo_children():
                    self.root.after_idle(self.show_skill_studio)
                elif key == "migration" and not page.winfo_children():
                    ttk.Label(
                        page,
                        text="Select source files and choose Open workspace to start migration.",
                        padding=30,
                    ).pack(anchor=tk.NW)
                elif key == "routine_test" and not self.container.winfo_children():
                    self._empty_page("Routine Test Plan", "Migrate a procedure or function to generate its parameter test plan.")
                break

    def _empty_page(self, title, message):
        self.clear()
        self.heading(title, message)
        ttk.Button(self.container, text="Go to Projects", command=self.show_projects).pack(anchor=tk.W)

    def _open_migration(self, selected, action, dialect, project):
        self.notebook.select(self.pages["migration"])
        self.open_workspace(
            self.pages["migration"], selected, action, dialect, project,
            navigation=self,
        )

    def prepare_routine_test_plan(self, project, source_sql, target_sql):
        from routine_test_planner import build_routine_test_plan
        self.routine_test_plan = build_routine_test_plan(source_sql, target_sql)
        self._render_routine_test_plan(project)
        self.notebook.tab(self.pages["routine_test"], text="Routine Test Plan *")
        self.notebook.select(self.pages["migration"])

    def _render_routine_test_plan(self, project):
        from tkinter import simpledialog
        from routine_test_planner import collect_data_findings, generate_invocation_sql
        from workflow import cache_project_password, get_project_password, open_database_connection

        self._activate("routine_test")
        self.clear()
        plan = self.routine_test_plan
        self.heading("Routine Test Plan", f"Review generated inputs and data findings for {plan['routine_name']} before execution.")
        mode=tk.StringVar(value=plan.get("validation_mode","compare_both"))
        mode_frame=ttk.Labelframe(self.container,text="Validation mode",padding=10);mode_frame.pack(fill=tk.X)
        ttk.Radiobutton(mode_frame,text="Compare source and destination results",variable=mode,value="compare_both").pack(anchor=tk.W)
        ttk.Radiobutton(mode_frame,text="Destination only — datasets already verified equivalent",variable=mode,value="destination_only").pack(anchor=tk.W)
        ttk.Label(mode_frame,text="Destination-only validates execution/results but does not prove source-to-target behavioral equivalence.",foreground="#9a6700").pack(anchor=tk.W,pady=(5,0))

        ttk.Label(self.container,text="Parameters and suggested test values",font=("Segoe UI",11,"bold")).pack(anchor=tk.W,pady=(16,4))
        suggestion_table=ttk.Treeview(self.container,columns=("parameter","kind","condition","values","source"),show="headings",height=8)
        for key,label,width in (("parameter","Parameter",130),("kind","Finding",130),("condition","Condition",280),("values","Suggested values",190),("source","Value source",190)):
            suggestion_table.heading(key,text=label);suggestion_table.column(key,width=width)
        def refresh_suggestions():
            for row in suggestion_table.get_children():suggestion_table.delete(row)
            for index,item in enumerate(plan["suggestions"]):
                values=", ".join("NULL" if value is None else str(value) for value in item.get("candidates",[])) or "Manual value required"
                suggestion_table.insert("",tk.END,iid=str(index),values=(item["parameter"],item["kind"],item["condition"],values,item["source"]))
        refresh_suggestions();suggestion_table.pack(fill=tk.X)
        ttk.Label(self.container,text="Suggested PostgreSQL calls",font=("Segoe UI",11,"bold")).pack(anchor=tk.W,pady=(12,4))
        invocation_text=tk.Text(self.container,height=6,wrap=tk.NONE)
        invocation_text.pack(fill=tk.X)
        def refresh_invocations():
            invocation_text.config(state="normal");invocation_text.delete("1.0",tk.END)
            invocation_text.insert("1.0","\n".join(generate_invocation_sql(plan)))
            invocation_text.config(state="disabled")
        refresh_invocations()
        manual=ttk.Frame(self.container);manual.pack(fill=tk.X,pady=(5,10));manual_value=tk.StringVar()
        ttk.Label(manual,text="Value for selected parameter:").pack(side=tk.LEFT)
        ttk.Entry(manual,textvariable=manual_value,width=35).pack(side=tk.LEFT,padx=8)
        def set_value():
            chosen=suggestion_table.selection()
            if not chosen:messagebox.showwarning("Routine Test Plan","Select a parameter suggestion first.");return
            value=manual_value.get().strip()
            if not value:messagebox.showwarning("Routine Test Plan","Enter a test value.");return
            plan["suggestions"][int(chosen[0])]["candidates"]=[value];refresh_suggestions();refresh_invocations()
        ttk.Button(manual,text="Set/override value",command=set_value).pack(side=tk.LEFT)

        ttk.Label(self.container,text="Table data findings",font=("Segoe UI",11,"bold")).pack(anchor=tk.W,pady=(8,4))
        table_tree=ttk.Treeview(self.container,columns=("table","source","target","status"),show="headings",height=7)
        for key,label,width in (("table","Table",300),("source","Source data",120),("target","Target data",120),("status","Finding",160)):
            table_tree.heading(key,text=label);table_tree.column(key,width=width)
        def data_text(value):return "Not checked" if value is None else ("Empty" if value==0 else "Available")
        def refresh_tables():
            for row in table_tree.get_children():table_tree.delete(row)
            for index,item in enumerate(plan["tables"]):
                findings=[value for value in (item.get("source_status"),item.get("target_status")) if value]
                table_tree.insert("",tk.END,iid=str(index),values=(item["name"],data_text(item.get("source_rows")),data_text(item.get("target_rows"))," / ".join(findings) or "not_checked"))
        refresh_tables();table_tree.pack(fill=tk.X)
        status=tk.StringVar(value="Database data has not been checked.");ttk.Label(self.container,textvariable=status,foreground="#555").pack(anchor=tk.W,pady=8)
        results=ttk.Labelframe(self.container,text="Execution and comparison results",padding=10);results.pack(fill=tk.X,pady=(4,8))
        result_status=tk.StringVar(value="Awaiting test-plan approval. No routine has been executed.")
        ttk.Label(results,textvariable=result_status,foreground="#555").pack(anchor=tk.W)

        def password_for(side):
            password=get_project_password(project.id,side) or getattr(project,f"{side}_password",None)
            if not password:
                password=simpledialog.askstring("Database password",f"{side.title()} database password:",show="*",parent=self.root)
                if password:cache_project_password(project.id,password,side)
            return password
        def check_data():
            plan["validation_mode"]=mode.get();connections=[]
            try:
                sides=["target"] if mode.get()=="destination_only" else ["source","target"]
                for side in sides:
                    password=password_for(side)
                    if password is None:return
                    if side=="source":
                        details={"host":project.host,"port":project.port,"database":project.database,"username":project.username};database_type=project.source_database
                    else:
                        details={"host":project.target_host,"port":project.target_port,"database":project.target_database_name,"username":project.target_username};database_type=project.target_database
                    connection=open_database_connection(database_type,details,password);connections.append(connection)
                    collect_data_findings(connection,plan,side,database_type)
                refresh_tables();refresh_suggestions();refresh_invocations()
                required=("target_rows",) if mode.get()=="destination_only" else ("source_rows","target_rows")
                empty=[item["name"] for item in plan["tables"] if any(item.get(field) in {None,0} for field in required)]
                status.set("Data check complete." if not empty else "Sample data required for: "+", ".join(empty))
            except Exception as exc:messagebox.showerror("Routine Test Plan",str(exc))
            finally:
                for connection in connections:connection.close()
        def approve():
            plan["validation_mode"]=mode.get()
            required=("target_rows",) if mode.get()=="destination_only" else ("source_rows","target_rows")
            unavailable=[item["name"] for item in plan["tables"] if any(item.get(field) in {None,0} for field in required)]
            missing_values=[item["parameter"] for item in plan["suggestions"] if not item.get("candidates")]
            if unavailable:
                messagebox.showerror("Routine Test Plan","Cannot approve: missing or empty table data for "+", ".join(unavailable));return
            if missing_values:
                messagebox.showerror("Routine Test Plan","Provide test values for: "+", ".join(sorted(set(missing_values))));return
            plan["approved"]=True;self.notebook.tab(self.pages["routine_test"],text="Routine Test Plan")
            status.set("Approved for routine execution. Parameter plan and data prerequisites passed.")
            result_status.set("Approved and ready for the execution-and-comparison stage. No routine has been executed yet.")
            messagebox.showinfo("Routine Test Plan","Test plan approved. It is ready for the execution-and-comparison stage.")
        actions=ttk.Frame(self.container);actions.pack(fill=tk.X,pady=10)
        ttk.Button(actions,text="Check table data and derive values",command=check_data).pack(side=tk.LEFT)
        ttk.Button(actions,text="Approve test plan",command=approve).pack(side=tk.LEFT,padx=8)

    def _scroll_page(self, event):
        try:
            if self.page_canvas is not None and self.page_canvas.winfo_exists():
                self.page_canvas.yview_scroll(int(-event.delta / 120), "units")
        except tk.TclError:
            pass

    def clear(self):
        for child in self.container.winfo_children():
            child.destroy()
        self.page_canvas.yview_moveto(0)

    def heading(self, title, subtitle):
        ttk.Label(self.container, text=title, font=("Segoe UI", 22, "bold")).pack(anchor=tk.W)
        ttk.Label(self.container, text=subtitle, foreground="#555").pack(anchor=tk.W, pady=(4, 22))

    def show_projects(self):
        self._activate("projects")
        self.clear()
        self.heading("Projects", "Create a migration project or continue with an existing one.")
        table = ttk.Treeview(self.container, columns=("operation", "source", "target", "scope"), show="headings", height=15)
        for key, label, width in (("operation", "Operation", 130), ("source", "Source", 150), ("target", "Target", 150), ("scope", "Object scope", 130)):
            table.heading(key, text=label); table.column(key, width=width)
        for project in self.projects:
            table.insert("", tk.END, iid=project.id, values=(project.default_operation, project.source_database, project.target_database, project.object_scope))
        table.pack(fill=tk.BOTH, expand=True)
        buttons = ttk.Frame(self.container); buttons.pack(fill=tk.X, pady=(18, 0))
        ttk.Button(buttons, text="Create project", command=self.show_project_form).pack(side=tk.LEFT)
        ttk.Button(buttons, text="Skill Studio", command=self.show_skill_studio).pack(side=tk.LEFT, padx=8)
        def resume():
            selection = table.selection()
            if not selection:
                messagebox.showwarning("Projects", "Select a project first."); return
            self.project = next(p for p in self.projects if p.id == selection[0]); self.show_files()
        ttk.Button(buttons, text="Open selected", command=resume).pack(side=tk.LEFT, padx=8)
        def edit():
            selection = table.selection()
            if not selection:
                messagebox.showwarning("Projects", "Select a project first."); return
            self.project = next(p for p in self.projects if p.id == selection[0]); self.show_project_form(self.project)
        def remove():
            selection = table.selection()
            if not selection:
                messagebox.showwarning("Projects", "Select a project first."); return
            project = next(p for p in self.projects if p.id == selection[0])
            if not messagebox.askyesno("Remove project", f"Remove project '{project.name}' and its imported file copies?\n\nOriginal source files will not be deleted."):
                return
            try: remove_project(project)
            except Exception as exc: messagebox.showerror("Remove project failed", str(exc)); return
            self.projects = [item for item in self.projects if item.id != project.id]
            self.project = None
            self.show_projects()
        ttk.Button(buttons, text="Edit selected", command=edit).pack(side=tk.LEFT, padx=8)
        ttk.Button(buttons, text="Remove selected", command=remove).pack(side=tk.LEFT)

    def show_skill_studio(self):
        self._activate("skills")
        self.clear()
        self.heading("Skill Studio", "Test correction rules and approve immutable skill versions.")
        top_actions=ttk.Frame(self.container)
        top_actions.pack(fill=tk.X,pady=(0,14))
        versions = list_skill_versions()
        skill_header=ttk.Frame(self.container);skill_header.pack(fill=tk.X)
        ttk.Label(skill_header,text="Migration skill versions",font=("Segoe UI",11,"bold")).pack(side=tk.LEFT)
        skill_reviewer=tk.StringVar()
        ttk.Label(skill_header,text="Approver:").pack(side=tk.LEFT,padx=(25,6))
        ttk.Entry(skill_header,textvariable=skill_reviewer,width=24).pack(side=tk.LEFT)
        skill_table = ttk.Treeview(self.container, columns=("pair","version","rules","status","approved"), show="headings", height=6)
        for key,label,width in (("pair","Dialect pair",260),("version","Version",70),("rules","Rules",60),("status","Status",150),("approved","Approved by",120)):
            skill_table.heading(key,text=label); skill_table.column(key,width=width)
        for version in versions:
            pair=f"{version['source_dialect']} → {version['target_dialect']}"
            skill_table.insert("",tk.END,iid=str(version["id"]),values=(pair,version["version"],version["rule_count"],version["status"],version["approved_by"] or ""))
        skill_table.pack(fill=tk.X,pady=(4,12))
        skill_details=tk.Text(self.container,height=7,wrap=tk.WORD)
        skill_details.pack(fill=tk.X)
        def selected_skill():
            chosen=skill_table.selection()
            if not chosen:raise ValueError("Select a skill version.")
            return next(item for item in versions if item["id"]==int(chosen[0]))
        def load_skill(_event=None):
            try:item=selected_skill()
            except ValueError:return
            rules=get_skill_version_rules(item["id"])
            content=[f"{item['name']} v{item['version']} — {item['status']}",item["instructions"],""]
            content.extend(f"{rule['priority']:>3}  {rule['rule_code']}: /{rule['pattern']}/ → {rule['replacement']}" for rule in rules)
            skill_details.delete("1.0",tk.END);skill_details.insert("1.0","\n".join(content))
        skill_table.bind("<<TreeviewSelect>>",load_skill)
        if versions:
            skill_table.selection_set(str(versions[0]["id"]));skill_table.focus(str(versions[0]["id"]));load_skill()

        def edit_rules():
            try:version=selected_skill()
            except Exception as exc:messagebox.showerror("Skill Studio",str(exc));return
            window=tk.Toplevel(self.root);window.title(f"Edit {version['name']} v{version['version']}");fit_window(window,1050,820,720,500)
            outer=ttk.Frame(window);outer.pack(fill=tk.BOTH,expand=True)
            canvas=tk.Canvas(outer,highlightthickness=0);scroll=ttk.Scrollbar(outer,orient=tk.VERTICAL,command=canvas.yview)
            scroll.pack(side=tk.RIGHT,fill=tk.Y);canvas.pack(side=tk.LEFT,fill=tk.BOTH,expand=True)
            frame=ttk.Frame(canvas,padding=16);frame_window=canvas.create_window((0,0),window=frame,anchor=tk.NW)
            frame.bind("<Configure>",lambda _e:canvas.configure(scrollregion=canvas.bbox("all")))
            canvas.bind("<Configure>",lambda e:canvas.itemconfigure(frame_window,width=e.width));canvas.configure(yscrollcommand=scroll.set)
            window.bind("<MouseWheel>",lambda e:canvas.yview_scroll(int(-e.delta/120),"units"))
            ttk.Label(frame,text=f"Status: {version['status']}  •  Active/superseded versions are read-only.",foreground="#555").pack(anchor=tk.W,pady=(0,10))
            rules=get_skill_version_rules(version["id"])
            rule_table=ttk.Treeview(frame,columns=("priority","code","category","risk","review"),show="headings",height=8)
            for key,label,width in (("priority","Priority",70),("code","Rule code",220),("category","Category",170),("risk","Risk",80),("review","Review status",140)):
                rule_table.heading(key,text=label);rule_table.column(key,width=width)
            for rule in rules:rule_table.insert("",tk.END,iid=str(rule["id"]),values=(rule["priority"],rule["rule_code"],rule["category"],rule["risk_level"],rule["review_status"]))
            rule_table.pack(fill=tk.X)
            edit=ttk.Frame(frame);edit.pack(fill=tk.BOTH,expand=True,pady=12);edit.columnconfigure(1,weight=1)
            rule_code=tk.StringVar();priority=tk.StringVar();pattern=tk.StringVar();replacement=tk.StringVar();description=tk.StringVar();source_example=tk.StringVar();target_example=tk.StringVar();risk=tk.StringVar();category=tk.StringVar();review_notes=tk.StringVar();enabled=tk.BooleanVar()
            field_specs=(
                ("Rule code",rule_code,"Unique stable name, for example asa-pg-custom-dateadd."),
                ("Priority",priority,"Lower numbers run first."),
                ("Match pattern",pattern,"Python regular expression matched against each masked SQL line."),
                ("Replacement",replacement,"PostgreSQL text replacing every match; may be empty for removal."),
                ("Description",description,"Explain the ASA construct and why this conversion is safe."),
                ("ASA example",source_example,"A short representative source expression or statement."),
                ("PostgreSQL example",target_example,"The expected result after applying this rule."),
            )
            for row,(label,var,tip) in enumerate(field_specs):
                ttk.Label(edit,text=label).grid(row=row*2,column=0,sticky=tk.W,padx=(0,12),pady=(5,0))
                ttk.Entry(edit,textvariable=var,width=90).grid(row=row*2,column=1,sticky=tk.EW,pady=(5,0))
                ttk.Label(edit,text=tip,foreground="#666").grid(row=row*2+1,column=1,sticky=tk.W)
            category_row=len(field_specs)*2
            ttk.Label(edit,text="Category").grid(row=category_row,column=0,sticky=tk.W,pady=6)
            categories=("datatypes","date_time","null_handling","string_functions","numeric_functions","conditional_control_flow","parameters_variables","transactions","error_handling","cursors","dynamic_sql","procedure_structure","schema_qualification","table_aliasing","custom")
            ttk.Combobox(edit,textvariable=category,values=categories,state="readonly",width=28).grid(row=category_row,column=1,sticky=tk.W)
            ttk.Label(edit,text="Groups related rules for filtering, review, and approval.",foreground="#666").grid(row=category_row+1,column=1,sticky=tk.W)
            ttk.Label(edit,text="Risk level").grid(row=category_row+2,column=0,sticky=tk.W,pady=6)
            ttk.Combobox(edit,textvariable=risk,values=("low","medium","high"),state="readonly",width=15).grid(row=category_row+2,column=1,sticky=tk.W)
            ttk.Label(edit,text="Low: direct equivalent; Medium: review semantics; High: requires targeted tests.",foreground="#666").grid(row=category_row+3,column=1,sticky=tk.W)
            ttk.Label(edit,text="Review notes").grid(row=category_row+4,column=0,sticky=tk.W,pady=6)
            ttk.Entry(edit,textvariable=review_notes,width=90).grid(row=category_row+4,column=1,sticky=tk.EW)
            ttk.Label(edit,text="Record why the rule was approved, rejected, or changed.",foreground="#666").grid(row=category_row+5,column=1,sticky=tk.W)
            ttk.Checkbutton(edit,text="Enabled for this skill version",variable=enabled).grid(row=category_row+6,column=1,sticky=tk.W,pady=6)
            def selected_rule():
                chosen=rule_table.selection()
                if not chosen:raise ValueError("Select a rule.")
                return next(item for item in rules if item["id"]==int(chosen[0]))
            def load_rule(_event=None):
                try:item=selected_rule()
                except ValueError:return
                rule_code.set(item["rule_code"]);priority.set(item["priority"]);pattern.set(item["pattern"]);replacement.set(item["replacement"]);description.set(item["description"]);source_example.set(item["source_example"]);target_example.set(item["target_example"]);risk.set(item["risk_level"]);category.set(item["category"]);review_notes.set(item["review_notes"]);enabled.set(bool(item["enabled"]))
            rule_table.bind("<<TreeviewSelect>>",load_rule)
            if rules:rule_table.selection_set(str(rules[0]["id"]));load_rule()
            def refresh_rules(selected_id=None):
                nonlocal rules
                rules=get_skill_version_rules(version["id"])
                for row_id in rule_table.get_children():rule_table.delete(row_id)
                for rule in rules:rule_table.insert("",tk.END,iid=str(rule["id"]),values=(rule["priority"],rule["rule_code"],rule["category"],rule["risk_level"],rule["review_status"]))
                if rules:
                    target_id=selected_id if any(rule["id"]==selected_id for rule in rules) else rules[0]["id"]
                    rule_table.selection_set(str(target_id));rule_table.focus(str(target_id));rule_table.see(str(target_id));load_rule()
            def save_rule():
                try:
                    item=selected_rule();update_skill_rule(item["id"],int(priority.get()),pattern.get(),replacement.get(),description.get(),source_example.get(),target_example.get(),risk.get(),enabled.get(),category.get(),review_notes.get())
                    messagebox.showinfo("Skill rule","Rule saved. It remains unavailable until the skill version is approved.",parent=window);refresh_rules(item["id"])
                except Exception as exc:messagebox.showerror("Skill rule",str(exc),parent=window)
            actions=ttk.Frame(frame);actions.pack(fill=tk.X)
            def decide(decision):
                try:item=selected_rule();review_skill_rule(item["id"],decision,review_notes.get());messagebox.showinfo("Skill rule",f"Rule {decision}.",parent=window);refresh_rules(item["id"])
                except Exception as exc:messagebox.showerror("Skill rule",str(exc),parent=window)
            def add_custom():
                try:new_id=add_custom_skill_rule(version["id"],rule_code.get(),category.get() or "custom",int(priority.get()),pattern.get(),replacement.get(),description.get(),source_example.get(),target_example.get(),risk.get());messagebox.showinfo("Skill rule","Custom rule added for review.",parent=window);refresh_rules(new_id)
                except Exception as exc:messagebox.showerror("Skill rule",str(exc),parent=window)
            ttk.Button(actions,text="Save changes",command=save_rule).pack(side=tk.RIGHT)
            ttk.Button(actions,text="Approve rule",command=lambda:decide("approved")).pack(side=tk.RIGHT,padx=6)
            ttk.Button(actions,text="Reject rule",command=lambda:decide("rejected")).pack(side=tk.RIGHT)
            ttk.Button(actions,text="Add as custom rule",command=add_custom).pack(side=tk.LEFT)
            ttk.Button(actions,text="Close",command=lambda:(window.destroy(),self.show_skill_studio())).pack(side=tk.LEFT,padx=8)

        ttk.Separator(self.container,orient=tk.HORIZONTAL).pack(fill=tk.X,pady=12)
        ttk.Label(self.container,text="PostgreSQL error correction proposals",font=("Segoe UI",11,"bold")).pack(anchor=tk.W)
        proposals = list_change_proposals()
        proposal_list=ttk.Frame(self.container);proposal_list.pack(fill=tk.X,pady=(6,0))
        table = ttk.Treeview(proposal_list, columns=("skill","version","status","test"), show="headings", height=7)
        for key, label, width in (("skill","Skill",250),("version","Base version",100),("status","Status",140),("test","Test",120)):
            table.heading(key,text=label); table.column(key,width=width)
        for proposal in proposals:
            table.insert("",tk.END,iid=str(proposal["id"]),values=(proposal["skill_name"],proposal["base_version"],proposal["status"],proposal["test_status"]))
        proposal_scroll=ttk.Scrollbar(proposal_list,orient=tk.VERTICAL,command=table.yview)
        proposal_xscroll=ttk.Scrollbar(proposal_list,orient=tk.HORIZONTAL,command=table.xview)
        table.configure(yscrollcommand=proposal_scroll.set,xscrollcommand=proposal_xscroll.set)
        table.bind("<MouseWheel>",lambda event:(table.yview_scroll(int(-event.delta/120),"units"),"break")[1])
        table.grid(row=0,column=0,sticky=tk.NSEW);proposal_scroll.grid(row=0,column=1,sticky=tk.NS)
        proposal_xscroll.grid(row=1,column=0,sticky=tk.EW);proposal_list.columnconfigure(0,weight=1)
        if not proposals:
            ttk.Label(
                self.container,
                text="No PostgreSQL error correction proposal currently requires action.",
                foreground="#555",
            ).pack(anchor=tk.W,pady=(8,0))
        editor=ttk.Labelframe(self.container,text="Selected proposal details",padding=12)
        title=tk.StringVar(); reviewer=tk.StringVar(); pattern=tk.StringVar(); replacement=tk.StringVar()
        fields=(("Proposal",title),("Reviewer",reviewer),("Regex pattern",pattern),("Replacement",replacement))
        for row,(label,var) in enumerate(fields):
            ttk.Label(editor,text=label).grid(row=row,column=0,sticky=tk.W,pady=6,padx=(0,12))
            ttk.Entry(editor,textvariable=var,width=85,state="readonly" if row==0 else "normal").grid(row=row,column=1,sticky=tk.EW,pady=6)
        rationale=tk.Text(editor,height=6,wrap=tk.WORD); rationale.grid(row=4,column=0,columnspan=2,sticky=tk.NSEW,pady=8)
        editor.columnconfigure(1,weight=1); editor.rowconfigure(4,weight=1)
        def selected():
            chosen=table.selection()
            if not chosen: raise ValueError("Select a correction proposal.")
            return next(item for item in proposals if item["id"]==int(chosen[0]))
        def load(_event=None):
            try:item=selected()
            except ValueError:return
            if not editor.winfo_manager():
                editor.pack(fill=tk.BOTH,expand=True,pady=15)
            title.set(item["title"]); pattern.set(item["pattern"]); replacement.set(item["replacement"])
            rationale.delete("1.0",tk.END); rationale.insert("1.0",item["rationale"])
        table.bind("<<TreeviewSelect>>",load)
        def test_rule():
            try:
                item=selected()
                if not reviewer.get().strip():raise ValueError("Enter the reviewer name.")
                if not pattern.get():raise ValueError("Enter a correction regex pattern.")
                update_proposal_rule(item["id"],pattern.get(),replacement.get(),reviewer.get().strip())
                messagebox.showinfo("Skill Studio","Correction rule applied successfully and is awaiting approval.")
                self.show_skill_studio()
            except Exception as exc:messagebox.showerror("Skill Studio",str(exc))
        def approve():
            try:
                item=selected()
                if not reviewer.get().strip():raise ValueError("Enter the approver name.")
                version_id=approve_change_proposal(item["id"],reviewer.get().strip())
                messagebox.showinfo("Skill Studio",f"Approved and activated skill version record #{version_id}.")
                self.show_skill_studio()
            except Exception as exc:messagebox.showerror("Skill Studio",str(exc))
        proposal_actions=ttk.Frame(editor);proposal_actions.grid(row=5,column=0,columnspan=2,sticky=tk.EW,pady=(8,0))
        ttk.Button(proposal_actions,text="Test correction",command=test_rule).pack(side=tk.LEFT)
        ttk.Button(proposal_actions,text="Approve and activate",command=approve).pack(side=tk.LEFT,padx=8)
        def approve_candidate():
            try:
                item=selected_skill()
                if not skill_reviewer.get().strip():raise ValueError("Enter the approver name.")
                approve_skill_version(item["id"],skill_reviewer.get().strip())
                messagebox.showinfo("Skill Studio",f"Approved and activated {item['name']} v{item['version']}.")
                self.show_skill_studio()
            except Exception as exc:messagebox.showerror("Skill Studio",str(exc))
        bar=top_actions
        ttk.Button(bar,text="Home",command=self.show_projects).pack(side=tk.LEFT)
        ttk.Button(bar,text="Edit selected skill rules",command=edit_rules).pack(side=tk.LEFT,padx=8)
        ttk.Button(bar,text="Approve selected skill",command=approve_candidate).pack(side=tk.LEFT,padx=8)

    def show_project_form(self, project=None):
        self._activate("settings")
        self.clear(); self.heading("Edit project" if project else "Create project", "Connection details define the source and migration target. Passwords are never saved.")
        form = ttk.Frame(self.container); form.pack(anchor=tk.NW, fill=tk.X)
        defaults = {"name":"", "operation":"migrate", "scope":"all", "source":"SQL Anywhere ASA", "target":"PostgreSQL", "host":"localhost", "port":"2638", "database":"", "username":"", "password":"", "target_host":"localhost", "target_port":"5432", "target_database":"postgres", "target_username":"", "target_password":"", "formatter_indent":"4 spaces"}
        if project is not None:
            defaults.update({"name":project.name,"operation":project.default_operation,"scope":project.object_scope,"source":project.source_database,"target":project.target_database,"host":project.host,"port":str(project.port),"database":project.database,"username":project.username,"target_host":project.target_host or "localhost","target_port":str(project.target_port or 5432),"target_database":project.target_database_name or "postgres","target_username":project.target_username,"target_password":getattr(project,"target_password",""),"formatter_indent":project.formatter_indent or "4 spaces"})
        vars_ = {key: tk.StringVar(value=value) for key, value in defaults.items()}
        fields = [("Project name","name"),("Purpose","operation"),("Object scope","scope"),("Source dialect","source"),("Target dialect","target"),("Source host","host"),("Source port","port"),("Source database / service","database"),("Source username","username"),("Source password","password"),("Target host","target_host"),("Target port","target_port"),("Target database","target_database"),("Target username","target_username"),("Target password","target_password"),("PostgreSQL indentation","formatter_indent")]
        for row, (label, key) in enumerate(fields):
            ttk.Label(form, text=label).grid(row=row, column=0, sticky=tk.W, padx=(0,18), pady=7)
            if key in {"source", "target"}:
                widget = ttk.Combobox(form, textvariable=vars_[key], values=SUPPORTED_DATABASES, state="readonly", width=42)
            elif key == "operation":
                widget = ttk.Combobox(form, textvariable=vars_[key], values=("mask", "unmask", "migrate"), state="readonly", width=42)
            elif key == "scope":
                widget = ttk.Combobox(form, textvariable=vars_[key], values=("one", "multiple", "all"), state="readonly", width=42)
            elif key == "formatter_indent":
                widget = ttk.Combobox(form, textvariable=vars_[key], values=("2 spaces", "4 spaces", "Tabs"), state="readonly", width=42)
            else:
                widget = ttk.Entry(form, textvariable=vars_[key], width=45, show="*" if key in {"password", "target_password"} else "")
            widget.grid(row=row, column=1, sticky=tk.W, pady=7)
        status = ttk.Label(form, text=""); status.grid(row=len(fields), column=0, columnspan=2, sticky=tk.W, pady=8)
        def details():
            if not all(vars_[key].get().strip() for key in ("name","host","port","database","username")):
                raise ValueError("Complete all required fields.")
            return dict(name=vars_["name"].get().strip(), default_operation=vars_["operation"].get(), object_scope=vars_["scope"].get(), source_database=vars_["source"].get(), target_database=vars_["target"].get(), host=vars_["host"].get().strip(), port=int(vars_["port"].get()), database=vars_["database"].get().strip(), username=vars_["username"].get().strip(), target_host=vars_["target_host"].get().strip(), target_port=int(vars_["target_port"].get()), target_database_name=vars_["target_database"].get().strip(), target_username=vars_["target_username"].get().strip(), formatter_indent=vars_["formatter_indent"].get())
        def test():
            try:
                data=details(); test_database_connection(data["source_database"], data, vars_["password"].get()); status.config(text="Connection successful", foreground="green")
            except Exception as exc: status.config(text=str(exc), foreground="red")
        def test_target():
            try:
                data=details()
                target_details={"host":data["target_host"], "port":data["target_port"], "database":data["target_database_name"], "username":data["target_username"]}
                test_database_connection(data["target_database"], target_details, vars_["target_password"].get())
                status.config(text="Target connection successful", foreground="green")
            except Exception as exc: status.config(text=str(exc), foreground="red")
        def create():
            try: data=details()
            except Exception as exc: messagebox.showerror("Create project", str(exc)); return
            if data["target_database"] == "PostgreSQL" and not all((data["target_host"], data["target_database_name"], data["target_username"])):
                messagebox.showerror("Create project", "Target host, database, and username are required for PostgreSQL migration."); return
            self.password=vars_["password"].get(); self.target_password=vars_["target_password"].get()
            if project is None:
                self.project=create_project(**data); self.projects.append(self.project)
            else:
                for key,value in data.items():setattr(project,key,value)
                self.project=project
            if self.password:
                cache_project_password(self.project.id, self.password, "source")
            if self.target_password:
                cache_project_password(self.project.id, self.target_password, "target")
                self.project.target_password=self.target_password
            save_projects(self.projects); self.show_projects() if project is not None else self.show_files()
        bar=ttk.Frame(self.container); bar.pack(fill=tk.X, pady=20)
        ttk.Button(bar,text="Back",command=self.show_projects).pack(side=tk.LEFT)
        ttk.Button(bar,text="Test connection",command=test).pack(side=tk.LEFT,padx=8)
        ttk.Button(bar,text="Test target connection",command=test_target).pack(side=tk.LEFT,padx=8)
        ttk.Button(bar,text="Save changes" if project else "Create and continue",command=create).pack(side=tk.LEFT)

    def show_files(self):
        self._activate("files")
        self.clear(); self.heading(self.project.name, f"{self.project.source_database} → {self.project.target_database}  •  Select database objects to process")
        archive_bar=ttk.Frame(self.container); archive_bar.pack(fill=tk.X)
        archive_var=tk.StringVar(value=self.project.archive_path)
        ttk.Entry(archive_bar,textvariable=archive_var,state="readonly").pack(side=tk.LEFT,fill=tk.X,expand=True)
        def upload():
            chosen=filedialog.askopenfilename(title="Select object archive",filetypes=[("SQL archives","*.zip *.tar *.tar.gz *.tgz"),("All files","*.*")])
            if not chosen:return
            workspace=WORKSPACE_DIR/self.project.id
            if workspace.exists(): shutil.rmtree(workspace)
            try: files=safe_extract_sql_archive(Path(chosen),workspace)
            except Exception as exc: messagebox.showerror("Upload failed",str(exc)); return
            if not files: messagebox.showwarning("Upload", "The ZIP contains no .sql, .ddl, or .txt files."); return
            self.project.archive_path=chosen; self.project.workspace=str(workspace); self.project.input_type="archive"; save_projects(self.projects); record_upload(self.project, files); self.show_files()
        def add_files():
            chosen=filedialog.askopenfilenames(title="Select one or more SQL objects",filetypes=[("SQL/DDL files","*.sql *.ddl *.txt"),("All files","*.*")])
            if not chosen:return
            workspace=WORKSPACE_DIR/self.project.id
            try: files=import_sql_files([Path(item) for item in chosen],workspace)
            except Exception as exc: messagebox.showerror("Import failed",str(exc)); return
            if not files:return
            self.project.archive_path=";".join(chosen); self.project.workspace=str(workspace); self.project.input_type="files"; save_projects(self.projects); record_upload(self.project, list_project_files(self.project)); self.show_files()
        def clear_files():
            if not list_project_files(self.project):return
            if not messagebox.askyesno("Clear project files", "Remove all imported file copies from this project?\n\nThe original source files will not be deleted."):return
            try: clear_project_files(self.project)
            except Exception as exc: messagebox.showerror("Clear failed",str(exc)); return
            self.project.archive_path=""; self.project.workspace=""; self.project.input_type=""; save_projects(self.projects); record_upload(self.project, []); self.show_files()
        ttk.Button(archive_bar,text="Upload archive",command=upload).pack(side=tk.LEFT,padx=(8,0))
        ttk.Button(archive_bar,text="Add file(s)",command=add_files).pack(side=tk.LEFT,padx=(8,0))
        ttk.Button(archive_bar,text="Clear project files",command=clear_files).pack(side=tk.LEFT,padx=(8,0))
        files=list_project_files(self.project)
        list_frame=ttk.Frame(self.container); list_frame.pack(fill=tk.BOTH,expand=True,pady=18)
        canvas=tk.Canvas(list_frame,highlightthickness=0); scroll=ttk.Scrollbar(list_frame,orient=tk.VERTICAL,command=canvas.yview)
        inner=ttk.Frame(canvas); inner.bind("<Configure>",lambda e:canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0,0),window=inner,anchor="nw"); canvas.configure(yscrollcommand=scroll.set); canvas.pack(side=tk.LEFT,fill=tk.BOTH,expand=True); scroll.pack(side=tk.RIGHT,fill=tk.Y)
        choices=[]
        for path in files:
            var=tk.BooleanVar(value=False); choices.append((path,var)); ttk.Checkbutton(inner,text=str(path.relative_to(Path(self.project.workspace))),variable=var).pack(anchor=tk.W,pady=3)
        footer=ttk.Frame(self.container); footer.pack(fill=tk.X)
        def set_all(value):
            for _,var in choices:var.set(value)
        ttk.Button(footer,text="Select all",command=lambda:set_all(True)).pack(side=tk.LEFT)
        ttk.Button(footer,text="Clear all",command=lambda:set_all(False)).pack(side=tk.LEFT,padx=7)
        action=tk.StringVar(value=self.project.default_operation)
        ttk.Radiobutton(footer,text="Mask objects",variable=action,value="mask").pack(side=tk.LEFT,padx=(25,4))
        ttk.Radiobutton(footer,text="Unmask objects",variable=action,value="unmask").pack(side=tk.LEFT)
        ttk.Radiobutton(footer,text=f"Migrate to {self.project.target_database}",variable=action,value="migrate").pack(side=tk.LEFT)
        def start():
            selected=[path for path,var in choices if var.get()]
            if not selected: messagebox.showwarning("Objects", "Select at least one object file."); return
            self.project.default_operation=action.get(); self.project.object_scope="one" if len(selected)==1 else ("all" if len(selected)==len(files) else "multiple"); save_projects(self.projects)
            save_object_selection(self.project.id, selected)
            active_database=self.project.target_database if action.get()=="unmask" else self.project.source_database
            self._open_migration(selected, action.get(), dialect_for(active_database), self.project)
        ttk.Button(footer,text="Open workspace",command=start).pack(side=tk.RIGHT)
        ttk.Button(footer,text="Projects",command=self.show_projects).pack(side=tk.RIGHT,padx=8)


def run(open_workspace):
    root=tk.Tk(); Launcher(root,open_workspace); root.mainloop()


def fit_window(window, desired_width, desired_height, minimum_width=700, minimum_height=480):
    """Fit a window inside the usable screen while retaining a practical minimum."""
    screen_width = window.winfo_screenwidth()
    screen_height = window.winfo_screenheight()
    width = min(desired_width, max(minimum_width, screen_width - 80))
    height = min(desired_height, max(minimum_height, screen_height - 120))
    x = max(0, (screen_width - width) // 2)
    y = max(0, (screen_height - height) // 3)
    window.geometry(f"{width}x{height}+{x}+{y}")
    window.minsize(min(minimum_width, width), min(minimum_height, height))
