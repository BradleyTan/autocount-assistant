---
name: ac-setup
description: Set up or repair the AutoCount support assistant on this PC (install the tool, check the setup, create the read-only SQL login).
disable-model-invocation: true
allowed-tools: Bash(autocount-assist doctor *) Bash(autocount-assist doctor) Bash(autocount-assist dbs) Bash(autocount-assist demo *) PowerShell(autocount-assist doctor *) PowerShell(autocount-assist doctor) PowerShell(autocount-assist dbs) PowerShell(autocount-assist demo *)
---
Help the user get `autocount-assist` working. Go one step at a time and explain each step in simple words.

1. Run `autocount-assist --version`.
   - If it is not found, install it from the folder that contains `pyproject.toml`. That folder is three levels above this skill's folder: `${CLAUDE_SKILL_DIR}/../../..`. Ask the user before installing, then run `py -m pip install "<that folder>"`. Python 3.11 or newer is needed (python.org, tick "Add python.exe to PATH").
2. Run `autocount-assist doctor`. Read each line:
   - `[PASS]` needs nothing.
   - `[WARN]` and `[FAIL]` come with a `->` line that says what to do. Explain it and help the user do it.
3. If the ODBC driver is missing, the user installs "ODBC Driver 18 for SQL Server" from Microsoft.
4. If the read-only login is missing, or the login can change data:
   - Explain that `autocount-assist setup-login --book AED_NAME` writes a SQL script with a new random password to the local data folder, and saves the login in the user's Windows environment variables. It changes nothing in SQL Server.
   - Ask for the user's yes. Then run it with `--yes`.
   - The user opens the script in SQL Server Management Studio as an administrator and presses F5. You never run the script.
   - Afterwards run `autocount-assist setup-login --cleanup`, and tell the user to open a new terminal.
5. Run `autocount-assist doctor --db AED_NAME` again until there is no `[FAIL]`.
6. Run `autocount-assist check-playbooks --db AED_NAME` and show which SQL checks work on this account book.

Never print, read aloud or ask for a password. Never run INSERT, UPDATE, DELETE or DDL.
