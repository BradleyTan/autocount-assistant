---
description: Scan the AutoCount environment (versions, logs, plugins, database)
argument-hint: [--db AED_NAME]
allowed-tools: Bash(autocount-assist scan:*)
---
!`autocount-assist scan $ARGUMENTS --json`

Summarise the scan for a support engineer: system vs database version and compatibility, AutoCount DLL version mismatches, the most recent errors (with file:line), and anything that could not be read. Flag problems first. Do not change anything.
