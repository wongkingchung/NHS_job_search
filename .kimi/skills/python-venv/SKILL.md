# Python Virtual Environment Skill

Use this skill when working on Python projects in this repository.

## Rule

Before installing packages or running Python code, check whether a project-local virtual environment exists. If it does not exist, create it and install project dependencies into it.

## How to check

On Windows, look for `.venv\Scripts\python.exe` in the project root.
On macOS/Linux, look for `.venv/bin/python` in the project root.

## How to create

```bash
# Windows
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt

# macOS / Linux
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

## How to use

Always use the virtual environment's interpreter for subsequent commands:

```bash
# Windows
.venv\Scripts\python nhs_job_search.py

# macOS / Linux
.venv/bin/python nhs_job_search.py
```

This keeps project dependencies isolated and avoids affecting the system Python or other projects.
