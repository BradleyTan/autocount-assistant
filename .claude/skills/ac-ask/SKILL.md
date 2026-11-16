---
name: ac-ask
description: Diagnose an AutoCount Accounting desktop support question or customer scenario with evidence from this PC. Use when the user asks about an AutoCount problem such as e-Invoice or MyInvois submission failing, stock balance not matching, SST report differing from the ledger, login failing after an upgrade, database version mismatch, SQL Server connection errors, slow or full database, or backup and restore errors.
argument-hint: "<question>" [--db AED_NAME]
allowed-tools: Bash(autocount-assist ask *) Bash(autocount-assist ask) PowerShell(autocount-assist ask *) PowerShell(autocount-assist ask) Bash(autocount-assist scan *) Bash(autocount-assist scan) PowerShell(autocount-assist scan *) PowerShell(autocount-assist scan) Bash(autocount-assist dbs *) Bash(autocount-assist dbs) PowerShell(autocount-assist dbs *) PowerShell(autocount-assist dbs) Bash(autocount-assist doctor *) Bash(autocount-assist doctor) PowerShell(autocount-assist doctor *) PowerShell(autocount-assist doctor) Bash(autocount-assist query *) Bash(autocount-assist query) PowerShell(autocount-assist query *) PowerShell(autocount-assist query) Bash(autocount-assist logs *) Bash(autocount-assist logs) PowerShell(autocount-assist logs *) PowerShell(autocount-assist logs) Bash(autocount-assist config *) Bash(autocount-assist config) PowerShell(autocount-assist config *) PowerShell(autocount-assist config) Bash(autocount-assist schema *) Bash(autocount-assist schema) PowerShell(autocount-assist schema *) PowerShell(autocount-assist schema) Bash(autocount-assist check-playbooks *) Bash(autocount-assist check-playbooks) PowerShell(autocount-assist check-playbooks *) PowerShell(autocount-assist check-playbooks) Bash(autocount-assist cases *) Bash(autocount-assist cases) PowerShell(autocount-assist cases *) PowerShell(autocount-assist cases) Bash(autocount-assist demo *) Bash(autocount-assist demo) PowerShell(autocount-assist demo *) PowerShell(autocount-assist demo)
---
The user's question or customer scenario: $ARGUMENTS

You are helping an AutoCount support engineer. Everything here is read-only.

## 1. Collect evidence

Run this command yourself (do not ask the user to run it):

`autocount-assist ask "<the question>" --json` and add `--db AED_NAME` when the user named an account book.

- If the output's `notes` say no account book was selected, run `autocount-assist dbs`, show the list, and ask which one to use. Then run `ask` again with `--db`.
- If the command is not found or fails to start, tell the user to run `/ac-setup` and stop.
- If `recommendations` is empty, say no playbook matched, show `related_knowledge`, and offer to look with `logs`, `config`, `schema` or `query` (section 3).

## 2. Answer

Use only the command output as evidence. Never invent AutoCount menu paths, table names or evidence.

For each recommendation, in rank order:
- **Cause and confidence** (the label and score from the output).
- **Why** (the `why` text; this is the accounting or system reasoning).
- **Where** (the `menu`). When `menu_verified` is false, add "(menu path to confirm)".
- **Before changing anything** (the `safety` text, when present).
- **Steps** (numbered).
- **Check it worked** (the `verify` list).
- **Evidence** (each entry's ref and detail). When `verified` is false, say the table name is not yet confirmed on this database.
- **Missing evidence** and the `caveat`, stated plainly. Do not fill gaps by guessing.

Then add:
- **Known error codes** from `error_codes`, if any.
- **Questions to ask the customer** from `questions_for_customer`.
- **Ruled out** causes, in one line.
- Any `notes` (for example a login that can change data, or skipped database checks).

Values like `<COMPANY#1a2b>` are masked customer data. Keep them as they are; never try to recover the real value.

## 3. Dig deeper (optional, read-only)

When the user wants more detail, these commands are safe. They never change anything and they mask customer data:
- `autocount-assist logs "<text or regex>" --since 7d --context 5`
- `autocount-assist config "<setting name>"`
- `autocount-assist schema "<part of a table or column name>" --db AED_NAME`
- `autocount-assist query "SELECT ..." --db AED_NAME` (one SELECT only; anything else is refused)
- `autocount-assist scan --db AED_NAME`

## Rules

- Never run INSERT, UPDATE, DELETE, DDL or any command that changes AutoCount data or settings. Changes are only proposed as steps for the user.
- Do not run `learn`, `propose` or `setup-login` from this skill; they save files and need the user's explicit yes.
- At the end, ask whether one of the solutions worked, so it can be saved with `/ac-learn`.
