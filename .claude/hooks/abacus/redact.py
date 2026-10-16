#!/usr/bin/env python3
"""Abacus PostToolUse redaction: remove secrets from tool output before the model reads it.

Redacts, in every string of the tool response (structure preserved):
  * values (and URL/base64 encodings) of secret-named environment variables loaded in this session
  * well-known token shapes (GitHub, AWS key ids, Slack, npm, JWT, private-key blocks, Bitwarden
    Secrets Manager access tokens, credentials embedded in URLs, ...)

Replies with hookSpecificOutput.updatedToolOutput only when something changed. If the payload
cannot be processed, the output is withheld rather than passed through (fail closed). Each
redaction is logged to $ABACUS_HOME/state/redactions.jsonl as counts only, never values.
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import _secrets as sec  # noqa: E402

WITHHELD = "[abacus:secrets] tool output withheld: the redaction hook could not process it ({})"


def walk(node, secrets, counts):
    if isinstance(node, str):
        text, c = sec.redact_text(node, secrets)
        for k, v in c.items():
            counts[k] = counts.get(k, 0) + v
        return text
    if isinstance(node, list):
        return [walk(x, secrets, counts) for x in node]
    if isinstance(node, dict):
        return {k: walk(v, secrets, counts) for k, v in node.items()}
    return node


def log(data, counts):
    try:
        home = os.environ.get("ABACUS_HOME", os.path.expanduser("~/.abacus"))
        d = os.path.join(home, "state")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "redactions.jsonl"), "a") as f:
            f.write(json.dumps({"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                                "tool": data.get("tool_name"), "session": data.get("session_id"),
                                "cwd": data.get("cwd"), "counts": counts}) + "\n")
    except OSError:
        pass


def reply(updated):
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PostToolUse", "updatedToolOutput": updated}}))


def main():
    try:
        data = json.loads(sys.stdin.read())
        response = data.get("tool_response", data.get("tool_output"))
        root = os.environ.get("CLAUDE_PROJECT_DIR") or data.get("cwd") or os.getcwd()
        secrets = sec.loaded_secrets(extra=sec.extra_names(root))
        counts = {}
        updated = walk(response, secrets, counts)
    except Exception as e:  # never let an unprocessed response through
        reply(WITHHELD.format(type(e).__name__))
        return 0
    if counts:
        log(data, counts)
        reply(updated)
    return 0


if __name__ == "__main__":
    sys.exit(main())
