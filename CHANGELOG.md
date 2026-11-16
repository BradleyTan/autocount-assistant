# Changelog

## 0.2.0

### Safety
- The read-only SQL guard now reads a statement in one pass, the way SQL Server does. Version 0.1 could be
  fooled by a comment marker or a quote hidden inside a string or a bracketed name.
- Database checks are refused when the login can change data, unless `allow_privileged_login = true`.
- Queries no longer wait on, or block, users: lock timeout, low deadlock priority, query timeout.
- Database rows are masked strictly: free text becomes a token unless it looks like a code.
- Windows user names in file paths are masked. Connection-string values cannot inject extra settings.

### Diagnosis
- SQL checks name their tables and columns as candidates and adapt to the account book's real names.
  `check-playbooks --db` shows which checks work and suggests similar table names for those that do not.
- Log evidence is limited to a time window (30 days by default, `--since` to change it).
- Each recommendation now carries why, how to check the fix worked, and what to do before changing data.
  Reports list questions to ask the customer and known error codes seen in the question or the logs.
- What the customer reports counts as weaker evidence than what the system shows, and never gives High confidence.
- Three new playbooks: SQL Server connection fails, slow or full database, backup or restore fails.
- `scan` shows the SQL Server edition, database and log size, Express 10 GB usage, recovery model,
  login rights, largest tables, and option, audit, user and version tables.

### Commands
- New read-only commands: `doctor`, `query`, `logs`, `config`, `schema`, `check-playbooks`.
- `setup-login` writes the read-only login script with a random password and stores the login in your
  Windows environment variables. It never runs the script and never prints the password.
- `autocount-assist "<question>"` works without the word `ask`. `ask --ticket-note` adds a note for the ticket.
- `ask --save-proposal` moved to its own command, `propose`, so `ask` can never write a file.

### Claude Code
- The repository is now a Claude Code plugin with four skills: `ac-ask`, `ac-scan`, `ac-learn`, `ac-setup`.

### Engineering
- ruff and mypy run in CI. 187 tests.
