"""Shared plumbing for Abacus guards: payload, shell parsing, paths, refusal log.

Stdlib only, Python 3.9+. Guards fail closed: if a call cannot be parsed, it
is refused with a reason the agent can read.

The parser is a careful heuristic, not a shell. Its contract is the negative
controls in tests/test_guards.py; add a test before changing it.
"""
import json
import os
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path

RESERVED = {"if", "then", "else", "elif", "fi", "do", "done", "while", "until", "for", "in",
            "case", "esac", "!", "{", "}", "function", "select", "time", "coproc"}
# wrapper -> (flags that take a value, number of leading positionals before the command)
WRAPPERS = {
    "env": ({"-u", "--unset", "-C", "--chdir"}, 0),
    "sudo": ({"-u", "-g", "-C", "-D", "-h", "-p", "-r", "-t", "-U", "-T"}, 0),
    "timeout": ({"-s", "--signal", "-k", "--kill-after"}, 1),
    "nice": ({"-n", "--adjustment"}, 0),
    "nohup": (set(), 0), "command": (set(), 0), "exec": ({"-a"}, 0), "builtin": (set(), 0),
    "caffeinate": ({"-t", "-w"}, 0), "stdbuf": ({"-i", "-o", "-e"}, 0), "time": ({"-o", "-f"}, 0),
    "xargs": ({"-I", "-i", "-n", "-P", "-L", "-l", "-s", "-d", "-E", "-e", "-a"}, 0),
    "doas": ({"-u", "-C"}, 0), "chronic": (set(), 0), "watch": ({"-n", "-d"}, 0),
}
SHELLS = {"bash", "sh", "zsh", "dash", "ksh", "fish"}
REDIRECTS = {">", ">>", "&>", "&>>", ">|", "<>"}
_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_SUBST = re.compile(r"\$\(|`|\$\{?[A-Za-z_]")


class Unparseable(Exception):
    pass


class Segment:
    """One simple command: effective cwd, environment assignments in force, argv, redirect targets."""

    def __init__(self, cwd, env, argv, redirects):
        self.cwd, self.env, self.argv, self.redirects = cwd, env, argv, redirects

    @property
    def assigns(self):
        return self.env

    @property
    def cmd(self):
        return os.path.basename(self.argv[0]) if self.argv else ""

    def __repr__(self):
        return f"Segment(cwd={self.cwd!r}, argv={self.argv!r}, redirects={self.redirects!r})"


# ------------------------------------------------------------------ scanner

def preprocess(script):
    """Quote-aware pass: drop comments and heredoc bodies; refuse unterminated constructs.

    A `#` starts a comment only at a word boundary outside quotes. `<<` / `<<-`
    (never `<<<`) outside quotes opens a heredoc whose body is skipped up to its
    terminator; a missing terminator fails closed.
    """
    out, i, n = [], 0, len(script)
    quote = None          # "'", '"', or "$'"
    pending = []          # heredoc terminators opened on the current line
    while i < n:
        c = script[i]
        if quote:
            out.append(c)
            if quote == "'" and c == "'":
                quote = None
            elif quote == "$'" and c == "\\" and i + 1 < n:
                out.append(script[i + 1]); i += 1
            elif quote == "$'" and c == "'":
                quote = None
            elif quote == '"' and c == "\\" and i + 1 < n:
                out.append(script[i + 1]); i += 1
            elif quote == '"' and c == '"':
                quote = None
            i += 1
            continue
        if c == "\\" and i + 1 < n:
            out.append(c); out.append(script[i + 1]); i += 2
            continue
        if c == "$" and i + 1 < n and script[i + 1] == "'":
            quote = "$'"; out.append("$'"); i += 2
            continue
        if c in "'\"":
            quote = c; out.append(c); i += 1
            continue
        if c == "#" and (i == 0 or script[i - 1] in " \t\n;&|()"):
            while i < n and script[i] != "\n":
                i += 1
            continue
        if script.startswith("<<", i) and not script.startswith("<<<", i):
            j = i + 2
            if j < n and script[j] == "-":
                j += 1
            while j < n and script[j] in " \t":
                j += 1
            m = re.match(r"""(['"]?)([A-Za-z_][A-Za-z0-9_]*)\1""", script[j:])
            if not m:
                raise Unparseable("heredoc without a recognizable terminator")
            pending.append(m.group(2))
            out.append(" ")  # the heredoc operator and tag are not arguments
            i = j + m.end()
            continue
        if c == "\n" and pending:
            out.append("\n")
            i += 1
            for tag in pending:
                while True:
                    end = script.find("\n", i)
                    line = script[i:] if end == -1 else script[i:end]
                    if end == -1 and line.strip() != tag:
                        raise Unparseable(f"heredoc '{tag}' is never terminated")
                    i = n if end == -1 else end + 1
                    if line.strip() == tag:
                        break
            pending = []
            continue
        out.append(c)
        i += 1
    if quote:
        raise Unparseable("unterminated quote")
    if pending:
        raise Unparseable(f"heredoc '{pending[0]}' has no body or terminator")
    return "".join(out)


def extract_substitutions(text):
    """Replace $(...) and `...` (outside single quotes) with a marker; return (text, inner scripts)."""
    out, inner, i, n, quote = [], [], 0, len(text), None
    while i < n:
        c = text[i]
        if quote == "'":
            out.append(c)
            if c == "'":
                quote = None
            i += 1
            continue
        if c == "\\" and i + 1 < n:
            out.append(text[i:i + 2]); i += 2
            continue
        if c == "'" and quote is None:
            quote = "'"; out.append(c); i += 1
            continue
        if c == '"':
            quote = None if quote == '"' else '"'
            out.append(c); i += 1
            continue
        if text.startswith("$(", i) and not text.startswith("$((", i):
            depth, j = 1, i + 2
            while j < n and depth:
                depth += {"(": 1, ")": -1}.get(text[j], 0)
                j += 1
            if depth:
                raise Unparseable("unbalanced $( ... )")
            inner.append(text[i + 2:j - 1])
            out.append("$__ABACUS_SUBST__")
            i = j
            continue
        if c == "`":
            j = text.find("`", i + 1)
            if j == -1:
                raise Unparseable("unbalanced backtick")
            inner.append(text[i + 1:j])
            out.append("$__ABACUS_SUBST__")
            i = j + 1
            continue
        out.append(c)
        i += 1
    return "".join(out), inner


def _tokens(script, inner=None):
    text, subs = extract_substitutions(preprocess(script))
    if inner is not None:
        inner.extend(subs)
    text = text.replace("\n", " ; ")
    lex = shlex.shlex(text, posix=True, punctuation_chars=";&|()<>")
    lex.whitespace_split = True
    lex.commenters = ""
    try:
        return list(lex)
    except ValueError as e:
        raise Unparseable(f"could not parse shell command ({e})")


def _expand(path, cwd, env=None):
    p = os.path.expanduser(path)
    for k, v in (env or {}).items():
        p = p.replace("${" + k + "}", v).replace("$" + k, v)
    p = os.path.expandvars(p)
    return os.path.normpath(p if os.path.isabs(p) else os.path.join(cwd, p))


def parse(script, cwd, env=None, depth=0):
    """Split a Bash command into Segments, tracking cd/pushd, subshells and exported variables."""
    if depth > 3:
        raise Unparseable("nested shell commands too deep")
    env = dict(env or {})
    segments, cur, stack, dirstack, inner = [], [], [], [], []
    for t in _tokens(script, inner) + [";"]:
        is_op = bool(t) and set(t) <= set(";&|()")
        if not is_op:
            cur.append(t)
            continue
        if cur:
            seg = _segment(cur, cwd, env)
            if seg.argv and seg.cmd in ("cd", "pushd"):
                target = next((a for a in seg.argv[1:] if not a.startswith("-")), "~")
                if seg.cmd == "pushd":
                    dirstack.append(cwd)
                if target != "-":
                    cwd = _expand(target, cwd, env)
            elif seg.argv and seg.cmd == "popd" and dirstack:
                cwd = dirstack.pop()
            elif seg.argv and seg.cmd in ("export", "declare", "typeset", "local", "readonly"):
                for a in seg.argv[1:]:
                    if _ASSIGN.match(a):
                        k, v = a.split("=", 1)
                        env[k] = v
            elif not seg.argv and seg.own_assigns:
                env.update(seg.own_assigns)  # `X=1; cmd` -- the assignment persists in the shell
            segments.append(seg)
            segments.extend(_nested(seg, depth))
            cur = []
        for ch in t:  # the lexer may merge operators, e.g. ");" or "&&("
            if ch == "(":
                stack.append((cwd, dict(env), list(dirstack)))
            elif ch == ")" and stack:
                cwd, env, dirstack = stack.pop()
    for sub in inner:
        segments.extend(parse(sub, cwd, env, depth + 1))
    return segments


def _nested(seg, depth):
    """Re-parse command strings handed to shells or eval, so `bash -c "git push -f"` is seen."""
    argv = seg.argv
    if seg.cmd in SHELLS and "-c" in argv[1:]:
        i = argv.index("-c", 1)
        if i + 1 < len(argv):
            return parse(argv[i + 1], seg.cwd, seg.env, depth + 1)
    if seg.cmd == "eval" and len(argv) > 1:
        return parse(" ".join(argv[1:]), seg.cwd, seg.env, depth + 1)
    if seg.cmd == "env" and any(a in ("-S", "--split-string") or a.startswith("--split-string=") for a in argv):
        raise Unparseable("`env -S` is not supported by the guards; run the command directly")
    return []


def _segment(tokens, cwd, env):
    redirects, inputs, clean, i = [], [], [], 0
    while i < len(tokens):
        t = tokens[i]
        if t in REDIRECTS or (t.endswith(">") and t.rstrip(">").isdigit()):
            if i + 1 < len(tokens):
                tgt = tokens[i + 1]
                if tgt.startswith("&"):  # >&2
                    i += 2; continue
                redirects.append(_expand(tgt, cwd, env))
            i += 2
            continue
        if t == "<":
            if i + 1 < len(tokens):
                inputs.append(_expand(tokens[i + 1], cwd, env))
            i += 2
            continue
        if t == "<<<":
            i += 2
            continue
        if re.fullmatch(r"\d?>&\d?-?", t) or t == ">&":
            i += 1 if t != ">&" else 2
            continue
        clean.append(t)
        i += 1
    own, argv, j = {}, [], 0
    while j < len(clean):
        t = clean[j]
        base = os.path.basename(t)
        if not argv and _ASSIGN.match(t):
            k, v = t.split("=", 1)
            own[k] = v
        elif not argv and t in RESERVED:
            pass
        elif not argv and base in WRAPPERS:
            valued, positionals = WRAPPERS[base]
            j += 1
            while j < len(clean) and clean[j].startswith("-") and clean[j] != "--":
                flag = clean[j].split("=", 1)[0]
                j += 2 if (flag in valued and "=" not in clean[j]) else 1
            if j < len(clean) and clean[j] == "--":
                j += 1
            while j < len(clean) and base == "env" and _ASSIGN.match(clean[j]):
                k, v = clean[j].split("=", 1)
                own[k] = v
                j += 1
            j += positionals
            continue
        else:
            argv.append(t)
        j += 1
    seg = Segment(cwd, dict(env, **own), argv, redirects)
    seg.own_assigns = own
    seg.words = clean      # before wrapper skipping: a bare `env` has words but no argv
    seg.inputs = inputs    # `< file` input redirects
    return seg


# ------------------------------------------------------------------ context

def payload():
    raw = sys.stdin.read()
    try:
        data = json.loads(raw)
        if not isinstance(data, dict) or "tool_name" not in data:
            raise ValueError("no tool_name")
        return data
    except ValueError as e:
        refuse("abacus-guards", f"could not read the hook payload ({e}); refusing (fail closed)", {})


def abacus_home():
    return Path(os.environ.get("ABACUS_HOME", Path.home() / ".abacus")).expanduser()


def _config():
    try:
        return json.loads((abacus_home() / "config.json").read_text())
    except (OSError, ValueError):
        return {}


def workspace_roots():
    """Every folder whose repositories the fence protects. ABACUS_WORKSPACE may list several (os.pathsep)."""
    env = os.environ.get("ABACUS_WORKSPACE")
    if env:
        roots = [r for r in env.split(os.pathsep) if r]
    else:
        cfg = _config()
        roots = cfg.get("workspace_roots") or ([cfg["workspace_root"]] if cfg.get("workspace_root") else [])
    return [os.path.realpath(os.path.expanduser(r)) for r in roots]


def fleet_repos():
    """Every repository registered in the deployed fleet, wherever it lives (fenced even outside the roots)."""
    try:
        workers = json.loads((abacus_home() / "agents" / "fleet.json").read_text()).get("workers", {})
    except (OSError, ValueError):
        return []
    return [os.path.realpath(os.path.expanduser(w["repo"])) for w in workers.values()
            if isinstance(w, dict) and w.get("repo")]


def toplevel(path):
    """The git toplevel containing path (or path itself when not in a repository)."""
    d = path
    while d and not os.path.isdir(d):
        d = os.path.dirname(d)
    p = subprocess.run(["git", "-C", d or "/", "rev-parse", "--show-toplevel"], capture_output=True, text=True)
    return os.path.realpath(p.stdout.strip()) if p.returncode == 0 else os.path.realpath(path)


def repo_identity(path):
    """The main checkout of the repository containing path. Every worktree of a repository shares it."""
    d = path
    while d and not os.path.isdir(d):
        d = os.path.dirname(d)
    p = subprocess.run(["git", "-C", d or "/", "rev-parse", "--path-format=absolute", "--git-common-dir"],
                       capture_output=True, text=True)
    if p.returncode != 0:
        return os.path.realpath(path)
    common = os.path.realpath(p.stdout.strip())
    return os.path.dirname(common) if os.path.basename(common) == ".git" else common


def worktrees_dir(main):
    return os.path.join(main, ".claude", "worktrees")


def session_unit(cwd):
    """Where this session may write: its own worktree when it has isolated into one, else its repository."""
    top = toplevel(cwd)
    main = repo_identity(cwd)
    if top != main and inside(top, worktrees_dir(main)):
        return top
    return repo_root(cwd)


def repo_root(cwd):
    """This session's repository: CLAUDE_PROJECT_DIR when set, else the cwd's toplevel."""
    env = os.environ.get("CLAUDE_PROJECT_DIR")
    return os.path.realpath(env) if env else toplevel(cwd)


def _fold(p):
    # macOS filesystems are case-insensitive by default: compare casefolded so `.CLAUDE` is `.claude`.
    return os.path.realpath(p).casefold()


def inside(path, root):
    path, root = _fold(path), _fold(root)
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def has_substitution(word):
    return bool(_SUBST.search(word))


def role():
    return os.environ.get("ABACUS_ROLE", "worker")


# ------------------------------------------------------------------ refusal

def log_refusal(guard, reason, data):
    try:
        d = abacus_home() / "state"
        d.mkdir(parents=True, exist_ok=True)
        ti = data.get("tool_input") or {}
        rec = {"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "guard": guard, "reason": reason,
               "tool": data.get("tool_name"), "input": (ti.get("command") or ti.get("file_path") or "")[:300],
               "cwd": data.get("cwd"), "session": data.get("session_id"), "agent_type": data.get("agent_type"),
               "role": role(), "classification": None}
        with (d / "refusals.jsonl").open("a") as f:
            f.write(json.dumps(rec) + "\n")
    except OSError:
        pass


def refuse(guard, reason, data):
    log_refusal(guard, reason, data)
    print(f"[abacus:{guard}] REFUSED: {reason}", file=sys.stderr)
    sys.exit(2)
