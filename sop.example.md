# Ticket standards (example)

Copy this to `sop.md` (or put it in a Notion page and set NOTION_SOP_PAGE) and edit it.
The bot reads it before drafting every proposal. Plain English is fine.

- Titles: imperative, max ~70 characters, start with a verb ("Add…", "Fix…", "Remove…").
- Bugs: the description must say what happens, what should happen, and where (page/endpoint).
- Every ticket gets 1-5 acceptance criteria that QA can check.
- Priority: P0 = production is broken, P1 = needed this sprint, P2 = everything else. Default to P2.
- Labels: use `frontend`, `backend` or `infra` based on where the work happens.
- One ticket = something one engineer can finish in at most ~3 days. Split bigger work.
- Never assign anyone unless the message names them as the owner.
