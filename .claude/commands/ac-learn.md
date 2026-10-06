---
description: Save a solved case (or schema / release notes) to the local AutoCount knowledge base
argument-hint: case | schema --db AED_NAME | release-notes
allowed-tools: Bash(autocount-assist learn:*), Bash(autocount-assist cases:*)
---
The user wants to add knowledge: $ARGUMENTS

- For a solved case: collect the scenario, cause, fix, playbook id and cause id from the conversation (from the last /ac-ask output when available). Show them to the user and ask for explicit permission before saving. Only then run:
  `autocount-assist learn case --scenario "..." --cause "..." --fix "..." --playbook <id> --cause-id <id> --yes`
- For `schema` or `release-notes`: confirm with the user, then run `autocount-assist learn <what> ... --yes`.
- Data is saved only on this PC (masked) in the local knowledge base, never in git. Do not save without the user's yes.
