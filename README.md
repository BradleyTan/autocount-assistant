# AutoCount Desktop Support Assistant

A **read-only** support tool for AutoCount Accounting 2.2 desktop (system 2.2.32.45, database 2.2.97, SQL Server 2022).
Ask a question or describe a customer scenario. The tool looks at the AutoCount environment on the PC (logs, config files, installed DLLs, and the account book database) and returns ranked solutions. Each solution comes with its evidence. When it has no evidence, it says so.

```
autocount-assist ask "Customer's e-Invoice submission fails since this morning" --db AED_ACME
```

```
#1 [Medium 0.70] e-Invoice client ID / secret invalid or expired
   Where: Tools > Options > e-Invoice (client ID / client secret)  (menu path to confirm)
   Steps:
     1. Log in to the MyInvois portal as the taxpayer and check the ERP system registration is active.
     ...
   Evidence:
     - log: C:\ProgramData\AutoCount\Logs\EInvoice.log:3
         2026-10-06 09:15:40 ERROR MyInvois token request failed: 401 Unauthorized (invalid_client)
```

## Safety rules (built in)

| Rule | How it is enforced |
|---|---|
| Read-only | Every SQL statement passes `sql_guard.check_read_only()` (single `SELECT`/`WITH` only, no `INTO`, `EXEC`, DDL, etc.) before it is sent. The connection uses `ApplicationIntent=ReadOnly` and pyodbc `readonly=True`, and the login is `db_datareader` + `db_denydatawriter`. |
| Changes are only proposed | Fix steps are shown as instructions. `--save-proposal` writes SQL scripts to a `proposed\` folder for review, and only after you agree. Nothing ever runs them. |
| Customer data masked | Names, addresses, TIN, SST no., BRN, NRIC, bank accounts, emails, phone numbers and credentials are replaced with tokens like `<TIN#3f2a>` in all output and in the knowledge base. |
| Secrets out of code/git | SQL user and password come only from `AC_SQL_USER` / `AC_SQL_PASSWORD`. A config file containing a password is rejected. Without `AC_SQL_USER`, Windows authentication is used. |
| Sensitive data stays on this PC | The knowledge base, cases and proposals live in `%LocalAppData%\autocount-assist\`. The tool refuses to use a data folder inside a git checkout. `.gitignore` excludes all data files, and a test fails if any are committed. |
| Saved only with permission | `ask`, `scan`, `dbs`, `cases` and `demo` never write anything. Every command that saves asks `[y/N]` first, or needs `--yes`. |

## Setup (Windows 11)

1. **Python 3.11+** from python.org (tick "Add to PATH").
2. **ODBC Driver 18 for SQL Server** from Microsoft.
3. Install the tool:
   ```powershell
   git clone https://github.com/BradleyTan/autocount-assistant.git
   cd autocount-assistant
   py -m pip install -e ".[dev]"
   ```
4. **Read-only login.** Open `sql/create_readonly_login.sql` in SSMS, replace the placeholders, and run it yourself. Then set the credentials for your user only:
   ```powershell
   setx AC_SQL_USER ac_readonly
   setx AC_SQL_PASSWORD "your-password"
   ```
   (Or grant your Windows account `db_datareader` and leave these unset to use Windows authentication.)
5. **Optional config.** Copy `autocount-assist.example.toml` to `%LocalAppData%\autocount-assist\autocount-assist.toml` and adjust the install path, instance or log folders. The defaults are `C:\Program Files\AutoCount\Accounting 2.2` and `localhost\A2006`.
6. Check everything is found:
   ```powershell
   autocount-assist dbs
   autocount-assist scan
   ```

## Commands

| Command | What it does | Writes? |
|---|---|---|
| `ask "<question>" [--db NAME] [--playbook ID] [--json]` | Analyse a question and return ranked recommendations. If `--db` is not given, it lists account books and asks you to pick one. | No (offers to record a confirmed case, asks first) |
| `scan [--db NAME] [--json]` | Versions and compatibility, AutoCount DLL mismatches, recent log errors, config files, database tables | No |
| `dbs` | List databases on the instance (`*` = looks like an account book) | No |
| `learn schema --db NAME` | Save table/column/relationship structure (no row data) | Yes, asks |
| `learn release-notes [--limit N]` | Download 2.2 release notes from wiki.autocountsoft.com | Yes, asks |
| `learn case --scenario ... --cause ... --fix ... [--playbook ID --cause-id ID]` | Record a solved case. Future answers for that cause get a confidence boost. | Yes, asks |
| `learn error-code --code ... --message ... --cause ... --fix ...` | Add an error code | Yes, asks |
| `cases [--playbook ID]` | List confirmed cases | No |
| `demo <scenario>|all` | Run the sample scenarios on synthetic data (no SQL Server needed) | No |

Account book selection order: `--db`, then `AC_DB`, then `database` in config, then an interactive pick.

### Claude Code slash commands

Open Claude Code in this folder:

- `/ac-ask "<question>" --db AED_NAME` runs `ask --json` and has Claude explain the evidence-backed answer.
- `/ac-scan` summarises the environment scan.
- `/ac-learn case` saves the solved case, after you confirm.

## How it works

```
question ─► analyser: pick playbook(s) by keywords + past cases
              │
              ├─► connectors (read-only, masked): logs · config · DLLs · SQL Server · version
              ▼
           score causes: base + evidence found + past confirmed cases
              ▼
           recommender: ranked causes with steps, menu, confidence and evidence (or what's missing)
```

- **Playbooks** (`src/autocount_assist/playbooks/*.yaml`) describe checks (`log`, `config`, `sql`, `version_mismatch`, `core_dll_mismatch`) and causes. A cause is **ruled out** when all its checks come back clear, or when a check in its `requires` list is clear. If a check can't run (no DB, table not found, no logs), the cause stays as **Low** with the missing evidence listed.
- **Your own playbooks**: put YAML files in `%LocalAppData%\autocount-assist\playbooks\`. A file with the same `id` replaces the built-in one.
- **Confidence**: High ≥ 0.75, Medium ≥ 0.5, else Low. A cause without evidence is never above Low.
- **Version compatibility**: `src/autocount_assist/compat.yaml` maps system → database version (`2.2.32.45 → 2.2.97`). The system version comes from the AutoCount EXE's file version. The database version is auto-discovered from a table with "version" in its name. If that picks the wrong table, set `db_version_query`.

### Things to confirm on a real system

These were written without access to a live AutoCount 2.2 database, and the output flags them:

- **SQL checks marked `verified: false`** use table and column names such as `StockDTL`, `ItemUOM`, `GLDTL`, `TaxCode` and `Debtor.TIN`. Before running, the tool checks the names exist. If one is missing, the check is reported as missing evidence and nothing is guessed. Evidence from these checks shows `[unverified table]`.
- **Menu paths** show "(menu path to confirm)" until you set `menu_verified: true` in the playbook.
- **Log folders and the main EXE name**: `scan` shows what was found. Add folders in the config if needed.

## Sample scenarios

`autocount-assist demo all` runs all five on synthetic data (`src/autocount_assist/samples/`):

| Scenario | Synthetic evidence | Top cause |
|---|---|---|
| e-Invoice submission fails | 401 `invalid_client` in the e-Invoice log | Client ID / secret invalid |
| Stock balance mismatch | Negative `SUM(Qty)` per item/location | Negative stock |
| SST report differs from ledger | Manual JEs on the SST account | Manual journal entries |
| Cannot log in after upgrade | DB 2.2.95 vs expected 2.2.97 | Database not upgraded |
| Database version mismatch | DB 2.2.99 newer than the system expects | Upgrade this PC |

## Development

```powershell
py -m pytest -q
```

Tests cover the connectors (logs, config, DLL versions, SQL Server via a fake connection, version compatibility), the SQL guard, masking, the knowledge base, the recommendation format contract, the five scenarios end to end, the CLI's permission prompts, and repository hygiene.
