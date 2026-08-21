"""Pre-workspace project wizard for the DDL Masker desktop application."""

from __future__ import annotations

import shutil
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from workflow import (
    SUPPORTED_DATABASES, WORKSPACE_DIR, create_project, dialect_for,
    import_sql_files, list_project_files, load_projects, safe_extract_sql_archive, save_projects,
    test_database_connection,
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
        fit_window(root, 1100, 850, 760, 520)
        shell = ttk.Frame(root)
        shell.pack(fill=tk.BOTH, expand=True)
        self.page_canvas = tk.Canvas(shell, highlightthickness=0)
        page_scroll = ttk.Scrollbar(shell, orient=tk.VERTICAL, command=self.page_canvas.yview)
        page_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.page_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.container = ttk.Frame(self.page_canvas, padding=24)
        self.page_window = self.page_canvas.create_window((0, 0), window=self.container, anchor=tk.NW)
        self.container.bind("<Configure>", lambda _e: self.page_canvas.configure(scrollregion=self.page_canvas.bbox("all")))
        self.page_canvas.bind("<Configure>", lambda e: self.page_canvas.itemconfigure(self.page_window, width=e.width))
        self.page_canvas.configure(yscrollcommand=page_scroll.set)
        root.bind("<MouseWheel>", self._scroll_page)
        self.show_projects()

    def _scroll_page(self, event):
        try:
            if self.page_canvas.winfo_exists():
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

    def show_skill_studio(self):
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
        table = ttk.Treeview(self.container, columns=("skill","version","status","test"), show="headings", height=9)
        for key, label, width in (("skill","Skill",250),("version","Base version",100),("status","Status",140),("test","Test",120)):
            table.heading(key,text=label); table.column(key,width=width)
        for proposal in proposals:
            table.insert("",tk.END,iid=str(proposal["id"]),values=(proposal["skill_name"],proposal["base_version"],proposal["status"],proposal["test_status"]))
        table.pack(fill=tk.X)
        editor=ttk.Frame(self.container); editor.pack(fill=tk.BOTH,expand=True,pady=15)
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
        ttk.Button(bar,text="Test correction",command=test_rule).pack(side=tk.LEFT,padx=8)
        ttk.Button(bar,text="Approve and activate",command=approve).pack(side=tk.LEFT)

    def show_project_form(self):
        self.clear(); self.heading("Create project", "Connection details define the source and migration target. Passwords are never saved.")
        form = ttk.Frame(self.container); form.pack(anchor=tk.NW, fill=tk.X)
        defaults = {"name":"", "operation":"migrate", "scope":"all", "source":"SQL Anywhere ASA", "target":"PostgreSQL", "host":"localhost", "port":"2638", "database":"", "username":"", "password":"", "target_host":"localhost", "target_port":"5432", "target_database":"", "target_username":""}
        vars_ = {key: tk.StringVar(value=value) for key, value in defaults.items()}
        fields = [("Project name","name"),("Purpose","operation"),("Object scope","scope"),("Source dialect","source"),("Target dialect","target"),("Source host","host"),("Source port","port"),("Source database / service","database"),("Source username","username"),("Source password","password"),("Target host","target_host"),("Target port","target_port"),("Target database","target_database"),("Target username","target_username")]
        for row, (label, key) in enumerate(fields):
            ttk.Label(form, text=label).grid(row=row, column=0, sticky=tk.W, padx=(0,18), pady=7)
            if key in {"source", "target"}:
                widget = ttk.Combobox(form, textvariable=vars_[key], values=SUPPORTED_DATABASES, state="readonly", width=42)
            elif key == "operation":
                widget = ttk.Combobox(form, textvariable=vars_[key], values=("mask", "unmask", "migrate"), state="readonly", width=42)
            elif key == "scope":
                widget = ttk.Combobox(form, textvariable=vars_[key], values=("one", "multiple", "all"), state="readonly", width=42)
            else:
                widget = ttk.Entry(form, textvariable=vars_[key], width=45, show="*" if key == "password" else "")
            widget.grid(row=row, column=1, sticky=tk.W, pady=7)
        status = ttk.Label(form, text=""); status.grid(row=14, column=0, columnspan=2, sticky=tk.W, pady=8)
        def details():
            if not all(vars_[key].get().strip() for key in ("name","host","port","database","username")):
                raise ValueError("Complete all required fields.")
            return dict(name=vars_["name"].get().strip(), default_operation=vars_["operation"].get(), object_scope=vars_["scope"].get(), source_database=vars_["source"].get(), target_database=vars_["target"].get(), host=vars_["host"].get().strip(), port=int(vars_["port"].get()), database=vars_["database"].get().strip(), username=vars_["username"].get().strip(), target_host=vars_["target_host"].get().strip(), target_port=int(vars_["target_port"].get()), target_database_name=vars_["target_database"].get().strip(), target_username=vars_["target_username"].get().strip())
        def test():
            try:
                data=details(); test_database_connection(data["source_database"], data, vars_["password"].get()); status.config(text="Connection successful", foreground="green")
            except Exception as exc: status.config(text=str(exc), foreground="red")
        def create():
            try: data=details()
            except Exception as exc: messagebox.showerror("Create project", str(exc)); return
            self.password=vars_["password"].get(); self.project=create_project(**data); self.projects.append(self.project); save_projects(self.projects); self.show_files()
        bar=ttk.Frame(self.container); bar.pack(fill=tk.X, pady=20)
        ttk.Button(bar,text="Back",command=self.show_projects).pack(side=tk.LEFT)
        ttk.Button(bar,text="Test connection",command=test).pack(side=tk.LEFT,padx=8)
        ttk.Button(bar,text="Create and continue",command=create).pack(side=tk.LEFT)

    def show_files(self):
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
        ttk.Button(archive_bar,text="Upload archive",command=upload).pack(side=tk.LEFT,padx=(8,0))
        ttk.Button(archive_bar,text="Add file(s)",command=add_files).pack(side=tk.LEFT,padx=(8,0))
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
            self.open_workspace(self.root, selected, action.get(), dialect_for(active_database), self.project)
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
