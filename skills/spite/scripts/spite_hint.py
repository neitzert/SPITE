"""Shows the line "type /spite help for help" to the person at the keyboard.

Installed as a hook by `spite hint on`. It runs when a session starts and on each prompt, and
prints the reminder at the start and then every Nth prompt. The text goes to the user only; the
model never sees it. It must never get in the way: whatever goes wrong, it stays silent and exits 0,
because a hook that fails on a prompt would block that prompt.
"""
import json
import os
import sys
import tempfile

HINT = "SPITE is set up here. Type {command} help for help."


def main():
    every = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 10
    command = sys.argv[2] if len(sys.argv) > 2 and sys.argv[2].startswith("/") else "/spite"
    hint = HINT.format(command=command)
    event = json.load(sys.stdin)
    kind = event.get("hook_event_name", "")
    if kind == "SessionStart":
        print(json.dumps({"systemMessage": hint}))
        return
    if kind != "UserPromptSubmit" or every <= 0:
        return
    if str(event.get("prompt", "")).strip().lower().startswith(command + " help"):
        return
    session = "".join(c for c in str(event.get("session_id", "x")) if c.isalnum() or c == "-")[:64]
    path = os.path.join(tempfile.gettempdir(), f"spite-hint-{session}.count")
    count = 0
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            count = int(fh.read().strip() or 0)
    count += 1
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(str(count))
    if count % every == 0:
        print(json.dumps({"systemMessage": hint}))


try:
    main()
except BaseException:  # never block a prompt
    pass
sys.exit(0)
