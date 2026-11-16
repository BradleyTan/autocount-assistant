# AutoCount Desktop Support Assistant

A **read-only** support tool for AutoCount Accounting 2.2 desktop (system 2.2.32.45, database 2.2.97, SQL Server 2022).

Ask a question or describe a customer scenario. The tool looks at the AutoCount environment on the PC (logs, config files, installed DLLs, and the account book database) and returns ranked solutions. Each solution shows its evidence. When there is no evidence, the tool says so and does not guess.

```
autocount-assist "Customer's e-Invoice submission fails since this morning" --db AED_ACME
```

```
#1 [Medium 0.70] e-Invoice client ID / secret invalid or expired
   Why: AutoCount must first get an access token from MyInvois using the client ID and secret. ...
   Where: Tools > Options > e-Invoice (client ID / client secret)  (menu path to confirm)
   Before you change anything: This changes a setting, not accounting data. ...
   Steps:
     1. Log in to the MyInvois portal as the taxpayer and check the ERP system registration is active.
     ...
   Check it worked:
     - One resubmitted document gets a validated status and a UIN from MyInvois.
   Evidence:
     - log: C:\ProgramData\AutoCount\Logs\EInvoice.log:3
         2026-11-16 09:15:40 ERROR MyInvois token request failed: 401 Unauthorized (invalid_client)
Ask the customer:
  - Does it fail for every document or only some? Since when?
```

## Safety rules (built in)

| Rule | How it is enforced |
|---|---|
| Read-only | Every SQL statement must pass `sql_guard.check_read_only()` before it is sent: one `SELECT`/`WITH` statement, and no `INSERT`, `UPDATE`, `DELETE`, `INTO`, `EXEC`, DDL or procedure calls anywhere in it. Before the first query, the tool checks the login's own rights and **refuses a login that can change data**. The recommended login is `db_datareader` plus `db_denydatawriter`. |
| Does not disturb users | Queries use a lock timeout, low deadlock priority, a query timeout and a row limit. They read without taking locks, so a result can include a posting that is in progress at that moment. Run it again to confirm an odd result. |
| Changes are only proposed | Fixes are shown as steps for you to do. `propose` can save a SQL script for review, only after you agree. Nothing ever runs it. |
| Customer data masked | Names, addresses, remarks, TIN, SST no., BRN, NRIC, bank accounts, emails, phone numbers, Windows user names and credentials become tokens such as `<TIN#3f2a>` in all output and in the knowledge base. Database rows are masked by column, and free text is hidden unless it looks like a code. Masking free text in log lines is best effort. |
| Secrets out of code and git | The SQL login and password come only from `AC_SQL_USER` / `AC_SQL_PASSWORD`. A config file that contains a password is rejected. The tool never prints the password. |
| Sensitive data stays on this PC | The knowledge base, cases, proposals and the login script live in `%LocalAppData%\autocount-assist\`. The tool refuses a data folder inside a git checkout. A test fails if a data file is committed. |
| Saved only with permission | `ask`, `scan`, `dbs`, `doctor`, `query`, `logs`, `config`, `schema`, `check-playbooks`, `cases` and `demo` never write anything. `learn`, `propose` and `setup-login` ask `[y/N]` first, or need `--yes`. |

## Setup (Windows 11)

1. Install **Python 3.11+** from python.org (tick "Add python.exe to PATH") and **ODBC Driver 18 for SQL Server** from Microsoft.
2. Install the tool from this folder:
   ```powershell
   py -m pip install -e ".[dev]"
   autocount-assist demo all
   ```
   The demo needs no database. If `autocount-assist` is not recognised, use `py -m autocount_assist` instead.
3. Check the setup. Each line says PASS, WARN or FAIL, and what to do:
   ```powershell
   autocount-assist doctor
   ```
4. Create the read-only login:
   ```powershell
   autocount-assist setup-login --book AED_YOURBOOK
   ```
   This writes a script with a new random password and saves the login in your Windows environment variables. It changes nothing in SQL Server. Open the script in SQL Server Management Studio as an administrator and press **F5**. Then delete it with `autocount-assist setup-login --cleanup` and open a new terminal.
   (To do this by hand instead, use `sql/create_readonly_login.sql`.)
5. Check again, then test the table names on your account book:
   ```powershell
   autocount-assist doctor --db AED_YOURBOOK
   autocount-assist check-playbooks --db AED_YOURBOOK
   ```
6. Optional: copy `autocount-assist.example.toml` to `%LocalAppData%\autocount-assist\autocount-assist.toml` to change the install path, the SQL instance or the log folders.

## Commands

| Command | What it does | Writes? |
|---|---|---|
| `ask "<question>" [--db NAME] [--since 7d] [--ticket-note] [--json]` | Ranked recommendations with evidence. The word `ask` is optional. | No |
| `scan [--db NAME] [--binaries] [--json]` | Versions and compatibility, DLL mismatches, recent log errors, SQL Server edition and size, login rights, key tables | No |
| `doctor [--db NAME]` | Checks the setup and says what to fix | No |
| `dbs` | Lists databases (`*` = looks like an account book) | No |
| `logs [pattern] [--since 7d] [--context 5]` | Searches AutoCount logs, masked | No |
| `config [name]` | Shows config settings, secrets masked | No |
| `schema [text] --db NAME` | Finds tables and columns (structure only) | No |
| `query "SELECT ..." --db NAME` | Runs one guarded, masked SELECT | No |
| `check-playbooks [--db NAME]` | Validates playbooks. With `--db`, tests each SQL check's table names. | No |
| `cases` | Lists confirmed cases | No |
| `demo <scenario>\|all` | Runs sample scenarios on synthetic data | No |
| `learn schema\|release-notes\|case\|error-code` | Adds to the local knowledge base | Yes, asks |
| `propose "<question>"` | Saves proposed data-fix scripts for review | Yes, asks |
| `setup-login [--book NAME]` | Writes the read-only login script and stores the login | Yes, asks |

Account book selection order: `--db`, then `AC_DB`, then `database` in the config, then an interactive pick.

## Use it from Claude Code

This repository is a Claude Code plugin. Open Claude Code in this folder, or install it:

```
/plugin marketplace add BradleyTan/autocount-assistant
/plugin install autocount-assist@autocount-tools
```

- `/ac-ask "<question>" --db AED_NAME` gathers evidence and explains the answer: cause, why, where, steps, how to check it worked, and what to ask the customer. Claude also uses it by itself when you describe an AutoCount problem.
- `/ac-scan` gives a health check of the installation.
- `/ac-learn case` saves a solved case after you confirm.
- `/ac-setup` walks through installation, `doctor` and the read-only login.

The skills only allow the read-only commands to run without asking.

## How it works

```
question -> analyser: pick playbook(s) by keywords and past cases
              |
              +-> connectors (read-only, masked): logs, config, DLLs, SQL Server, version, Windows services
              v
           score causes: base + evidence found + past confirmed cases
              v
           recommender: ranked causes with why, steps, checks, confidence and evidence (or what is missing)
```

- **Playbooks** (`src/autocount_assist/playbooks/*.yaml`) hold checks and causes. Check types: `log`, `config`, `sql`, `question` (what the customer reported), `version_mismatch`, `core_dll_mismatch`, `server_fact`, `sql_connect`, `windows_service`.
- **A cause is ruled out** when all its checks come back clear, or when a check in its `requires` list is clear. When a check cannot run, the cause stays as **Low** with the missing evidence listed.
- **Confidence**: High is 0.75 or more, Medium is 0.5 or more, otherwise Low. A cause without evidence is never above Low. A cause that rests only on the customer's words is never High.
- **Table names adapt.** A SQL check lists candidate names, and the tool uses the ones this account book has:
  ```yaml
  objects:
    tax: {table: [TaxCode, TaxType], columns: {rate: TaxRate, account: TaxAccNo}}
  query: SELECT {tax.rate} FROM {tax} WHERE {tax.account} IS NOT NULL
  ```
- **Your own playbooks** go in `%LocalAppData%\autocount-assist\playbooks\`. A file with the same `id` replaces the built-in one. A broken file is skipped and reported.
- **Version compatibility**: `compat.yaml` maps system version to database version (`2.2.32.45` to `2.2.97`). If the database version is read from the wrong table, set `db_version_query`.

### Things to confirm on a real system

These were written without access to a live AutoCount 2.2 database, and the output flags them:

- **AutoCount table names.** SQL checks are marked `verified: false`. Run `autocount-assist check-playbooks --db AED_NAME` to see which work on your account book. A check whose tables are not found is reported as missing evidence.
- **Menu paths** show "(menu path to confirm)" until `menu_verified: true` is set in the playbook.
- **Log folders and the main EXE name.** `doctor` and `scan` show what was found.

## Sample scenarios

`autocount-assist demo all` runs these on synthetic data:

| Scenario | Synthetic evidence | Top cause |
|---|---|---|
| e-Invoice submission fails | 401 `invalid_client` in the e-Invoice log | Client ID / secret invalid |
| Stock balance mismatch | Negative quantity per item and location | Negative stock |
| SST report differs from ledger | Journal entries on the SST account | Manual journal entries |
| Cannot log in after upgrade | Database 2.2.95, expected 2.2.97 | Database not upgraded |
| Database version mismatch | Database 2.2.99, newer than expected | Upgrade this PC |
| SQL Server connection fails | SQL Server Browser stopped, error 26 in the log | Browser service stopped |
| Slow or full database | Express database at 98.8% of 10 GB | Express size limit |
| Backup or restore fails | Error 3169 in the question | Backup from a newer SQL Server |

## Development

```powershell
py -m pytest -q
py -m ruff check .
py -m mypy
```

See `CHANGELOG.md` for what changed in each version.
