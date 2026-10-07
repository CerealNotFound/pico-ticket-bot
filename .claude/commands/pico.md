---
description: Start the Pico Pumble→Notion ticket bot and keep an eye on it
---
Start the Pico bot for this project:

1. Run `uv run pico doctor --skip-llm`. If anything fails, show me the failing lines and how to fix them, then stop.
2. Otherwise start `uv run pico watch` in the background (Bash with run_in_background) so it keeps running while we keep working.
3. Tell me it is live and which channel/board it's connected to (from its startup log line).
4. If I ask how it's going later, read the background output and summarise new proposals, approvals, created ticket links, and any warnings/errors.
