---
description: Ask the AutoCount support assistant about a question or customer scenario
argument-hint: "<question>" [--db AED_NAME]
allowed-tools: Bash(autocount-assist ask:*), Bash(autocount-assist dbs:*)
---
Run this read-only command and use only its output as evidence:

!`autocount-assist ask $ARGUMENTS --json`

Then answer as an AutoCount support engineer:
- List the recommendations in rank order with confidence, the AutoCount menu/screen, the steps, and the evidence (file:line, table/field or config key) each one rests on.
- Mark menu paths shown as unverified "(to confirm)".
- If `notes` or `missing_evidence` say something could not be checked, say so plainly; do not fill gaps by guessing.
- If no account book was selected, show the output of `autocount-assist dbs` and ask which one to use.
- Never run INSERT/UPDATE/DELETE or any command that changes data. Propose changes only.
- At the end, ask whether one of the solutions worked so it can be saved with /ac-learn.
