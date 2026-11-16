---
name: ac-learn
description: Save a solved AutoCount case, the database structure, release notes or an error code to the local knowledge base on this PC.
argument-hint: case | schema --db AED_NAME | release-notes | error-code
disable-model-invocation: true
allowed-tools: Bash(autocount-assist cases *) Bash(autocount-assist cases) PowerShell(autocount-assist cases *) PowerShell(autocount-assist cases)
---
The user wants to add knowledge: $ARGUMENTS

Data is saved only on this PC, masked, in the local knowledge base. It never goes to git. Never save without the user's explicit yes in this conversation.

- **Solved case**: collect the scenario, cause and fix from the conversation, plus the playbook id and cause id from the last `/ac-ask` output when available. Show them to the user and ask for permission. Only after a yes, run:
  `autocount-assist learn case --scenario "..." --cause "..." --fix "..." --playbook <id> --cause-id <id> --yes`
- **schema**: after a yes, run `autocount-assist learn schema --db AED_NAME --yes`. It saves table and column names only, no row data.
- **release-notes**: after a yes, run `autocount-assist learn release-notes --yes`. It downloads from wiki.autocountsoft.com.
- **error-code**: collect code, message, cause and fix; after a yes, run `autocount-assist learn error-code --code "..." --message "..." --cause "..." --fix "..." --yes`.

Do not put real customer names, tax numbers or passwords in the text you pass. Describe the case in general terms.
