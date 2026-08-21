# Embedded application database

`ddl_masker.sqlite3` is the application's embedded SQLite database. It is created
and upgraded automatically on first startup from the schema in `database.py`.

The database stores projects, non-secret connection details, upload history,
discovered object files, selections, and processing results. Database passwords
are deliberately never persisted.
