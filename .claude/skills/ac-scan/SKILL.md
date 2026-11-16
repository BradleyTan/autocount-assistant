---
name: ac-scan
description: Scan the AutoCount desktop environment on this PC (versions and compatibility, recent log errors, DLL mismatches, SQL Server edition and size, login rights). Use when the user asks for a health check or overview of an AutoCount installation.
argument-hint: [--db AED_NAME]
allowed-tools: Bash(autocount-assist scan *) Bash(autocount-assist scan) PowerShell(autocount-assist scan *) PowerShell(autocount-assist scan) Bash(autocount-assist dbs *) Bash(autocount-assist dbs) PowerShell(autocount-assist dbs *) PowerShell(autocount-assist dbs) Bash(autocount-assist doctor *) Bash(autocount-assist doctor) PowerShell(autocount-assist doctor *) PowerShell(autocount-assist doctor)
---
Arguments from the user: $ARGUMENTS

Run `autocount-assist scan --json` yourself, adding the user's arguments. If no account book was selected, run `autocount-assist dbs`, show the list and ask which to use.
If the command is not found, tell the user to run `/ac-setup`.

Summarise for a support engineer, problems first:
1. System version, database version, and whether they are compatible.
2. AutoCount DLL version mismatches.
3. SQL Server edition, database size (and the share of the 10 GB limit on Express), recovery model, and whether the login is read-only.
4. The most recent errors with file:line and time.
5. Anything that could not be read, and why.

Everything shown is already masked. Do not change anything.
