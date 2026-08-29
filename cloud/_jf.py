"""Read JSON on stdin, print one value named by a dotted path, or an empty line.

Exists as a FILE rather than an inline heredoc because of a bug that cost a smoke test:

    api GET /ssh-keys | "$PY" - <<'PYK' ... PYK

reads nothing. A heredoc IS stdin, so it replaces the pipe -- the JSON never arrives and the
handler prints its empty fallback, which reads exactly like "the account has no SSH key".
Passing the script as a file leaves stdin free for the pipe.

Indexes work as path segments, so `ssh_keys.0.id` walks a list.
"""
import json
import sys

try:
    node = json.load(sys.stdin)
    for part in sys.argv[1].split("."):
        node = node[int(part)] if part.isdigit() else node[part]
    print(node)
except Exception:
    print("")
