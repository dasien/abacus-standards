"""What counts as a secret, for the `secrets` guard (PreToolUse) and redact.py (PostToolUse).

Stdlib only, Python 3.9+. Three sources:
  1. environment variables whose NAME looks secret (and whose value is long enough to matter)
  2. well-known credential FILES (dotenv, keys, CLI credential stores, Bitwarden CLI data)
  3. well-known token SHAPES (GitHub, AWS, Slack, npm, JWTs, private-key blocks, Bitwarden
     Secrets Manager access tokens...), redacted from output even when not in the environment

A repository can add names and paths in .abacus/config.json:
  "secret_env_names": ["MY_VENDOR_KEY"], "secret_paths": ["config/prod.json"]
"""
import base64
import fnmatch
import json
import os
import re
import urllib.parse

SECRET_NAME = re.compile(
    r"(TOKEN|SECRET|PASSWORD|PASSWD|PASSPHRASE|CREDENTIAL|PRIVATE_?KEY|API_?KEY|ACCESS_?KEY|CLIENT_?KEY|"
    r"SIGNING_?KEY|ENCRYPTION_?KEY|MASTER_?KEY|AUTH|COOKIE|SESSION|CONNECTION_?STRING|DSN$|DATABASE_URL|"
    r"(^|_)PAT($|_)|WEBHOOK|LICENSE_?KEY|BW_|BWS_)", re.I)
# Harmless variables whose names merely look secret.
BENIGN = {
    "SSH_AUTH_SOCK", "TERM_SESSION_ID", "ITERM_SESSION_ID", "SECURITYSESSIONID", "XPC_SERVICE_NAME",
    "CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID", "GPG_AGENT_INFO", "DBUS_SESSION_BUS_ADDRESS",
    "XDG_SESSION_ID", "XDG_SESSION_TYPE", "XDG_SESSION_CLASS", "XDG_SESSION_DESKTOP", "DESKTOP_SESSION",
    "SESSION_MANAGER", "TMUX_PANE", "BW_NON_INTERACTIVE",
}
MIN_SECRET_LEN = 8

# Basename patterns of credential files (reads are refused; *.pub and templates are fine).
SECRET_BASENAMES = (
    ".env", ".env.*", "*.env", "*.pem", "*.key", "*.p12", "*.pfx", "*.jks", "*.keystore", "*.p8",
    "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519", "id_ecdsa_sk", "id_ed25519_sk",
    ".npmrc", ".pypirc", ".netrc", "_netrc", ".git-credentials", ".htpasswd", ".pgpass", ".my.cnf",
    "credentials", "credentials.json", "client_secret*.json", "service-account*.json", "secrets.json",
    "secrets.yml", "secrets.yaml", "*.tfvars", "*.tfstate", "*.tfstate.backup",
    ".bash_history", ".zsh_history", ".python_history", ".psql_history", ".node_repl_history",
)
NOT_SECRET_BASENAMES = (".env.example", ".env.sample", ".env.template", ".env.dist", ".env.defaults",
                        "*.example", "*.sample", "*.pub", "known_hosts", "known_hosts.old")
# Path fragments (after ~ expansion) of credential stores.
SECRET_PATH_PARTS = (
    "/.ssh/", "/.aws/", "/.gnupg/", "/.kube/config", "/.docker/config.json", "/.config/gh/hosts.yml",
    "/.microsoft/usersecrets/", "/Library/Application Support/Bitwarden CLI/", "/.config/Bitwarden CLI/",
    "/Library/Keychains/", "/.config/op/", "/.vault-token", "/.terraform.d/credentials", "/.azure/",
    "/.config/gcloud/", "/.netrc", "/.git-credentials",
)
SAFE_IN_SSH = ("config", "known_hosts", "known_hosts.old", "authorized_keys")

TOKEN_SHAPES = [
    ("private-key", re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z0-9 ]*PRIVATE KEY-----")),
    ("github-token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,255}|github_pat_[A-Za-z0-9_]{22,255})\b")),
    ("aws-access-key-id", re.compile(r"\b(?:AKIA|ASIA|AGPA|AIDA|AROA|ANPA)[0-9A-Z]{16}\b")),
    ("slack-token", re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}\b")),
    ("npm-token", re.compile(r"\bnpm_[A-Za-z0-9]{36}\b")),
    ("anthropic-key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}\b")),
    ("openai-key", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{32,}\b")),
    ("stripe-key", re.compile(r"\b[rs]k_(?:live|test)_[A-Za-z0-9]{16,}\b")),
    ("google-api-key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("bitwarden-sm-access-token", re.compile(r"\b0\.[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
                                             r"\.[A-Za-z0-9]{20,}:[A-Za-z0-9+/=]{16,}")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")),
    ("azure-storage-key", re.compile(r"AccountKey=[A-Za-z0-9+/=]{40,}")),
    ("url-credentials", re.compile(r"(?<=://)[^\s/:@]+:[^\s/@]{6,}(?=@)")),
    ("bearer-token", re.compile(r"(?i)(?<=authorization: bearer )[A-Za-z0-9._~+/=-]{16,}")),
]


def repo_config(root):
    try:
        return json.load(open(os.path.join(root, ".abacus", "config.json")))
    except (OSError, ValueError, TypeError):
        return {}


def extra_names(root):
    return set(repo_config(root).get("secret_env_names") or [])


def is_secret_name(name, extra=()):
    if name in extra:
        return True
    return name not in BENIGN and bool(SECRET_NAME.search(name))


def loaded_secrets(env=None, extra=()):
    """{name: value} for secret-looking variables with values long enough to matter."""
    env = os.environ if env is None else env
    out = {}
    for k, v in env.items():
        if not v or len(v) < MIN_SECRET_LEN or not is_secret_name(k, extra):
            continue
        if v.startswith("/") and os.path.exists(v):
            continue  # a filesystem path (e.g. a socket), not a secret value
        out[k] = v
    return out


def is_secret_path(path, root=None):
    """True when path names a credential file. `path` may be relative; root is the repo."""
    p = os.path.expanduser(path)
    base = os.path.basename(p.rstrip("/"))
    if any(fnmatch.fnmatch(base, pat) for pat in NOT_SECRET_BASENAMES):
        return False
    full = os.path.normpath(p if os.path.isabs(p) else os.path.join(root or os.getcwd(), p))
    if "/.ssh/" in full + "/" and base in SAFE_IN_SSH:
        return False
    if fnmatch.fnmatch(full, "/proc/*/environ"):
        return True
    if any(fnmatch.fnmatch(base, pat) for pat in SECRET_BASENAMES):
        return True
    if any(part in full + ("/" if os.path.isdir(full) else "") for part in SECRET_PATH_PARTS):
        return True
    for pat in repo_config(root or os.getcwd()).get("secret_paths") or []:
        if fnmatch.fnmatch(os.path.relpath(full, root or os.getcwd()), pat) or fnmatch.fnmatch(base, pat):
            return True
    return False


def variants(value):
    """The value plus common encodings of it, so encoded leaks are redacted too."""
    out = {value}
    out.add(urllib.parse.quote(value, safe=""))
    out.add(urllib.parse.quote_plus(value))
    try:
        out.add(base64.b64encode(value.encode()).decode())
    except (UnicodeError, ValueError):
        pass
    return [v for v in out if len(v) >= MIN_SECRET_LEN]


def redact_text(text, secrets):
    """Return (redacted text, {kind: count}). Never returns any part of a secret."""
    counts = {}
    for name, value in sorted(secrets.items(), key=lambda kv: -len(kv[1])):
        for v in variants(value):
            if v in text:
                n = text.count(v)
                text = text.replace(v, f"[REDACTED:{name}]")
                counts[name] = counts.get(name, 0) + n
    for kind, rx in TOKEN_SHAPES:
        text, n = rx.subn(f"[REDACTED:{kind}]", text)
        if n:
            counts[kind] = counts.get(kind, 0) + n
    return text, counts
