#!/usr/bin/env python3
"""claude-profile — Claude Code subscription juggler (core).

Concepts
  profile  one Claude Code config dir (~/.claude, ~/.claude-personal, …).
           Multi-profile = several dirs, selected by cwd path rules or an
           explicit toggle.
  account  a named credential slot (a subscription). A profile holds 1..N
           accounts; N>1 = "serial": switching accounts swaps ONLY
           token/creds/auth inside that dir — sessions, memory, settings and
           per-project state persist untouched.

Config (user-edited): $XDG_CONFIG_HOME/claude-profile/config.json
State  (tool-managed): $XDG_STATE_HOME/claude-profile/state.json + accounts/

Secrets never touch disk: live credentials stay in the Claude Code Keychain
item for the profile dir; parked (inactive) account credentials are stored as
separate Keychain items ("claude-profile-parked-<account>"). Only non-secret
metadata (email, uuids) is written to the state dir.

This file is the non-interactive core; the zsh wrapper (claude-profile.zsh)
adds the `claude` launcher, fzf pickers and shell conveniences.
"""

import argparse
import datetime
import json
import os
import random
import signal
import subprocess
import sys
import tempfile
import time

HOME = os.path.expanduser("~")
CONFIG_DIR = os.path.join(
    os.environ.get("XDG_CONFIG_HOME", os.path.join(HOME, ".config")), "claude-profile"
)
STATE_DIR = os.path.join(
    os.environ.get("XDG_STATE_HOME", os.path.join(HOME, ".local", "state")),
    "claude-profile",
)
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")
STATE_PATH = os.path.join(STATE_DIR, "state.json")
ACCOUNTS_DIR = os.path.join(STATE_DIR, "accounts")

DEFAULT_CLAUDE_DIR = os.path.join(HOME, ".claude")
LIVE_SERVICE_DEFAULT = "Claude Code-credentials"
PARKED_SERVICE_PREFIX = "claude-profile-parked-"
USAGE_TTL = 60  # seconds; server-side usage cache freshness
LAUNCHD_LABEL = "com.claude-profile.refresh"

# Anthropic's OAuth client id + endpoints and Claude Code's User-Agent identify
# Claude Code's own OAuth client, not this tool — so they are deliberately NOT
# distributed here and never committed. Supply them per machine: an environment
# override wins, else the "oauth" block of config.json (see README → "OAuth
# constants"). When absent, profile/account switching still works fully; only
# the network features (refresh / usage / exhaustion auto-rotate) go inert and
# say so, rather than shipping a baked-in default.
_OAUTH_ENV = {
    "client_id": "CLAUDE_PROFILE_CLIENT_ID",
    "token_url": "CLAUDE_PROFILE_TOKEN_URL",
    "usage_url": "CLAUDE_PROFILE_USAGE_URL",
    "user_agent": "CLAUDE_PROFILE_UA",
}


def oauth_setting(key):
    """Resolve an Anthropic OAuth constant: env override → config `oauth` block
    → None. Ships no default (see the note above)."""
    v = os.environ.get(_OAUTH_ENV[key])
    if v:
        return v
    cfg = load_config(required=False)
    return (cfg.get("oauth") or {}).get(key) if cfg else None


def curl_json(url, body=None, bearer=None):
    """POST (body given) or GET a JSON endpoint via curl. Secrets never enter
    argv: a POST body travels on stdin, a bearer token travels as a header
    read from stdin (-H @-). body and bearer are mutually exclusive here.
    Returns (status:int, data:dict|None, err:str)."""
    # Cloudflare in front of the endpoints rejects python-urllib's signature
    # (error 1010); curl carrying the configured (CLI) User-Agent is accepted.
    cmd = [
        "curl", "-sS", "--connect-timeout", "5", "--max-time", "15",
        "-H", "Accept: application/json",
        "-H", "anthropic-beta: oauth-2025-04-20",
        "-w", "\n%{http_code}",
        url,
    ]
    ua = oauth_setting("user_agent")
    if ua:
        cmd += ["-H", f"User-Agent: {ua}"]
    inp = None
    if body is not None:
        inp = json.dumps(body)
        cmd += ["-X", "POST", "-H", "Content-Type: application/json", "--data-binary", "@-"]
    elif bearer:
        inp = f"Authorization: Bearer {bearer}"
        cmd += ["-H", "@-"]
    res = subprocess.run(cmd, input=inp, capture_output=True, text=True)
    if res.returncode != 0:
        return 0, None, (res.stderr.strip().splitlines() or ["curl failed"])[-1][:200]
    raw, _, code = res.stdout.rpartition("\n")
    if not code.strip().isdigit():
        return 0, None, "no HTTP status from curl"
    status = int(code)
    try:
        data = json.loads(raw) if raw.strip() else None
    except json.JSONDecodeError:
        data = None
    return status, data, raw[:200]

# Flat (non-account-keyed) caches in .claude.json that hold single-account
# values; cleared on swap so they regenerate for the incoming account.
FLAT_CACHE_KEYS = [
    "modelAccessCache",
    "additionalModelOptionsCache",
    "additionalModelCostsCache",
    "orgModelDefaultCache",
    "hasAvailableSubscription",
    "cachedExtraUsageDisabledReason",
]


def die(msg, code=1):
    print(f"claude-profile: {msg}", file=sys.stderr)
    sys.exit(code)


def expand(p):
    return os.path.abspath(os.path.expanduser(p))


def atomic_write(path, data_str):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".tmp-")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(data_str)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ── config / state ──────────────────────────────────────────────────────────

def load_config(required=True):
    try:
        with open(CONFIG_PATH) as f:
            cfg = json.load(f)
    except FileNotFoundError:
        if required:
            die(f"no config at {CONFIG_PATH} — see README for the schema")
        return None
    except json.JSONDecodeError as e:
        die(f"config parse error in {CONFIG_PATH}: {e}")
    profiles = cfg.get("profiles")
    if not isinstance(profiles, dict) or not profiles:
        die("config needs a non-empty \"profiles\" object")
    for name, p in profiles.items():
        if not isinstance(p, dict) or "dir" not in p:
            die(f"profile \"{name}\" needs a \"dir\"")
    default = cfg.get("default_profile")
    if default is not None and default not in profiles:
        die(f"default_profile \"{default}\" is not a configured profile")
    return cfg


def save_config(cfg):
    atomic_write(CONFIG_PATH, json.dumps(cfg, indent=2) + "\n")


def load_state():
    try:
        with open(STATE_PATH) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state):
    atomic_write(STATE_PATH, json.dumps(state, indent=2) + "\n")


def profile_dir(cfg, name):
    return expand(cfg["profiles"][name]["dir"])


def profile_accounts(cfg, name):
    return list(cfg["profiles"][name].get("accounts") or [])


def is_serial(cfg, name):
    return len(profile_accounts(cfg, name)) > 1


def account_keepalive(cfg, account):
    """Whether the keep-alive daemon should renew this account's refresh token.
    Default True; flip with `claude-profile keepalive <account> off`. Stored in
    the top-level `keepalive` map of config.json (account → bool)."""
    return bool((cfg.get("keepalive") or {}).get(account, True))


def profile_exhaust_credits(cfg, profile):
    """Whether auto-rotation should burn a rate-limited account's extra-usage
    (overage) credits before swapping off it. Default False (swap at the rate
    limit; don't spend credits). Per-profile `exhaust_credits` in config.json."""
    return bool(cfg["profiles"].get(profile, {}).get("exhaust_credits", False))


# ── profile resolution ──────────────────────────────────────────────────────

def resolve_profile(cfg, state, pwd):
    """cwd path rule (longest match) → explicit toggle → default_profile."""
    pwd = expand(pwd)
    best, best_len = None, -1
    for name, p in cfg["profiles"].items():
        for raw in p.get("paths") or []:
            root = expand(raw)
            if pwd == root or pwd.startswith(root + os.sep):
                if len(root) > best_len:
                    best, best_len = name, len(root)
    if best:
        return best, "path"
    active = state.get("active_profile")
    if active and active in cfg["profiles"]:
        return active, "toggle"
    default = cfg.get("default_profile") or next(iter(cfg["profiles"]))
    return default, "default"


# ── Keychain (macOS `security`) ─────────────────────────────────────────────

def _security(args, input_str=None, check=False):
    try:
        res = subprocess.run(
            ["security"] + args,
            input=input_str,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        # No macOS `security` binary (e.g. Linux). Keychain-backed features
        # (save/auth/refresh/parked-account listing) are macOS-only; degrade to
        # a clean miss so read paths (status, resolve) keep working instead of
        # crashing. Write paths pass check=True and surface a clear error.
        if check:
            raise RuntimeError("`security` not found — Keychain features are macOS-only")
        return subprocess.CompletedProcess(args, returncode=1, stdout="", stderr="security: not found")
    if check and res.returncode != 0:
        raise RuntimeError(f"security {args[0]} failed: {res.stderr.strip()}")
    return res


def dir_suffix(d):
    """Claude Code's per-config-dir Keychain suffix: sha256(abs dir)[:8]."""
    import hashlib

    return hashlib.sha256(expand(d).encode()).hexdigest()[:8]


def live_service(d):
    """Keychain service name Claude Code uses for this config dir."""
    if expand(d) == DEFAULT_CLAUDE_DIR:
        return LIVE_SERVICE_DEFAULT
    return f"{LIVE_SERVICE_DEFAULT}-{dir_suffix(d)}"


def keychain_read(service):
    res = _security(["find-generic-password", "-s", service, "-a", os.environ.get("USER", ""), "-w"])
    if res.returncode != 0:
        return None
    return res.stdout.rstrip("\n")


def keychain_write(service, blob):
    """Create/update a generic password without the secret entering argv.

    `security -i` reads commands from stdin, so the blob never appears in the
    process table. Backslashes and double quotes are escaped for security's
    lexer. -U updates in place, preserving the item's ACL (important for the
    live item Claude Code created — a delete/recreate would drop its access
    grant and trigger a Keychain prompt).
    """
    esc = blob.replace("\\", "\\\\").replace('"', '\\"')
    user = os.environ.get("USER", "")
    cmd = f'add-generic-password -U -a "{user}" -s "{service}" -w "{esc}"\n'
    res = _security(["-i"], input_str=cmd)
    if res.returncode != 0:
        raise RuntimeError(f"keychain write to '{service}' failed: {res.stderr.strip()}")


def keychain_delete(service):
    _security(["delete-generic-password", "-s", service, "-a", os.environ.get("USER", "")])


def parked_service(account):
    return f"{PARKED_SERVICE_PREFIX}{account}"


# ── .claude.json (per-dir Claude Code config) ───────────────────────────────

def claude_json_path(d):
    """Default dir keeps the legacy top-level ~/.claude.json; custom dirs use
    <dir>/.claude.json."""
    if expand(d) == DEFAULT_CLAUDE_DIR:
        return os.path.join(HOME, ".claude.json")
    return os.path.join(expand(d), ".claude.json")


def load_claude_json(d):
    try:
        with open(claude_json_path(d)) as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except json.JSONDecodeError as e:
        die(f"cannot parse {claude_json_path(d)}: {e}")


def save_claude_json(d, data):
    atomic_write(claude_json_path(d), json.dumps(data, indent=2))


# ── account metadata snapshots (non-secret) ─────────────────────────────────

def snapshot_path(account):
    return os.path.join(ACCOUNTS_DIR, f"{account}.json")


def load_snapshot(account):
    try:
        with open(snapshot_path(account)) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def save_snapshot(account, snap):
    atomic_write(snapshot_path(account), json.dumps(snap, indent=2) + "\n")


def all_parked_names():
    """Names of every parked Keychain item (claude-profile-parked-*), scraped
    from `security dump-keychain` metadata (no secrets read)."""
    res = _security(["dump-keychain"])
    names = set()
    for line in res.stdout.splitlines():
        if PARKED_SERVICE_PREFIX in line and '"svce"' in line:
            try:
                svc = line.split('="', 1)[1].rstrip('"')
            except IndexError:
                continue
            if svc.startswith(PARKED_SERVICE_PREFIX):
                names.add(svc[len(PARKED_SERVICE_PREFIX):])
    return names


# ── credential store (platform-dispatched) ──────────────────────────────────
# macOS keeps credentials in the login Keychain (the keychain_*/_security
# functions above). Linux has no Keychain, so it mirrors Claude Code's own
# scheme: the LIVE credential is the <dir>/.credentials.json file Claude Code
# reads, and PARKED credentials are mode-0600 files under the state dir. The
# blob format is identical on both platforms (JSON {"claudeAiOauth": {...}}),
# so save / swap / refresh work the same. (Trade-off on Linux: parked tokens
# sit as 0600 files on disk — same protection as Claude Code's own credential,
# weaker than the macOS Keychain.)

IS_MACOS = sys.platform == "darwin"
PARKED_DIR = os.path.join(STATE_DIR, "parked")


def live_cred_path(d):
    return os.path.join(expand(d), ".credentials.json")


def parked_cred_path(name):
    return os.path.join(PARKED_DIR, f"{name}.json")


def live_cred_desc(d):
    """Where the live credential for `d` lives — for error messages (Keychain
    service on macOS, file path on Linux)."""
    return live_service(d) if IS_MACOS else live_cred_path(d)


def _file_read(path):
    try:
        with open(path) as f:
            return f.read().rstrip("\n") or None
    except (FileNotFoundError, NotADirectoryError, IsADirectoryError, OSError):
        return None


def _file_write(path, blob):
    """Atomically write `blob` at mode 0600 — the blob holds live tokens."""
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(blob if blob.endswith("\n") else blob + "\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _file_delete(path):
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


def read_live_cred(d):
    return keychain_read(live_service(d)) if IS_MACOS else _file_read(live_cred_path(d))


def write_live_cred(d, blob):
    return keychain_write(live_service(d), blob) if IS_MACOS else _file_write(live_cred_path(d), blob)


def delete_live_cred(d):
    return keychain_delete(live_service(d)) if IS_MACOS else _file_delete(live_cred_path(d))


def read_parked_cred(name):
    return keychain_read(parked_service(name)) if IS_MACOS else _file_read(parked_cred_path(name))


def write_parked_cred(name, blob):
    return keychain_write(parked_service(name), blob) if IS_MACOS else _file_write(parked_cred_path(name), blob)


def delete_parked_cred(name):
    return keychain_delete(parked_service(name)) if IS_MACOS else _file_delete(parked_cred_path(name))


def parked_names():
    if IS_MACOS:
        return all_parked_names()
    if not os.path.isdir(PARKED_DIR):
        return set()
    return {fn[:-5] for fn in os.listdir(PARKED_DIR) if fn.endswith(".json")}


def saved_account_names():
    """Every account with stored artifacts: snapshot and/or parked credential."""
    return set(all_snapshots()) | parked_names()


def all_snapshots():
    out = {}
    if os.path.isdir(ACCOUNTS_DIR):
        for fn in os.listdir(ACCOUNTS_DIR):
            if fn.endswith(".json"):
                name = fn[: -len(".json")]
                snap = load_snapshot(name)
                if snap:
                    out[name] = snap
    return out


def current_account_of(cfg, profile):
    """Name of the account currently live in the profile's dir, matched by
    accountUuid against the snapshots. None if unknown/unsaved. When several
    snapshots share the uuid (e.g. a rename in progress), a name listed in
    the profile's accounts wins over a stray."""
    cj = load_claude_json(profile_dir(cfg, profile))
    if not cj:
        return None
    uuid = (cj.get("oauthAccount") or {}).get("accountUuid")
    if not uuid:
        return None
    matches = [n for n, s in all_snapshots().items() if s.get("accountUuid") == uuid]
    if not matches:
        return None
    listed = profile_accounts(cfg, profile)
    for n in listed:
        if n in matches:
            return n
    return matches[0]


# ── live sessions guard ─────────────────────────────────────────────────────

def live_sessions(d):
    """Claude Code sessions currently running out of this config dir.
    Session registry: <dir>/sessions/*.json with a `pid` field (the same
    source claude-mv uses). A live pid = a live session."""
    sessions_dir = os.path.join(expand(d), "sessions")
    live = []
    if not os.path.isdir(sessions_dir):
        return live
    for fn in os.listdir(sessions_dir):
        if not fn.endswith(".json"):
            continue
        try:
            with open(os.path.join(sessions_dir, fn)) as f:
                s = json.load(f)
            pid = int(s.get("pid"))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            continue
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            continue
        except PermissionError:
            pass  # exists, not ours — still counts
        live.append({"pid": pid, "cwd": s.get("cwd"), "status": s.get("status")})
    return live


def _pid_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, not ours


def kill_sessions(sessions, wait=6.0):
    """Terminate the given live sessions: SIGTERM, then SIGKILL any still alive
    after `wait` seconds. Returns the pids acted on."""
    pids = [s["pid"] for s in sessions]
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
    deadline = time.time() + wait
    alive = list(pids)
    while alive and time.time() < deadline:
        time.sleep(0.2)
        alive = [p for p in alive if _pid_alive(p)]
    for pid in alive:  # stragglers
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    return pids


def ensure_swappable(d, force):
    """Credential-swap guard for config dir `d`. No live sessions → return.
    Live sessions + not force → refuse (a swap under a running session can
    corrupt its credentials). Live sessions + force → terminate them first,
    then verify they're gone (abort if not)."""
    sessions = live_sessions(d)
    if not sessions:
        return
    pids = ", ".join(str(s["pid"]) for s in sessions)
    if not force:
        die(
            f"{len(sessions)} live Claude session(s) in {d} (pids {pids}) — "
            f"a swap under a running session can corrupt its credentials. "
            f"Close them, or use --force to terminate them first.",
            code=2,
        )
    print(f"--force: terminating {len(sessions)} live Claude session(s) (pids {pids})", file=sys.stderr)
    kill_sessions(sessions)
    still = live_sessions(d)
    if still:
        die(
            "could not terminate session(s) "
            f"(pids {', '.join(str(s['pid']) for s in still)}) — aborting swap",
            code=2,
        )


# ── usage / exhaustion ──────────────────────────────────────────────────────

def fetch_usage(token):
    url = oauth_setting("usage_url")
    if not url:
        return None  # usage endpoint not configured → usage simply "unknown"
    status, data, _ = curl_json(url, bearer=token)
    if status != 200 or not isinstance(data, dict) or data.get("error"):
        return None
    return data


def summarize_usage(data):
    """→ list of {kind, utilization(percent), resets_at} from either the
    modern limits[] shape or the legacy flat fields."""
    out = []
    for lim in data.get("limits") or []:
        if isinstance(lim, dict) and lim.get("utilization") is not None:
            out.append(
                {
                    "kind": lim.get("kind") or "?",
                    "utilization": float(lim["utilization"]),
                    "resets_at": lim.get("resets_at"),
                }
            )
    if not out:
        for k in ("five_hour", "seven_day", "seven_day_opus", "seven_day_sonnet"):
            v = data.get(k)
            if isinstance(v, dict) and v.get("utilization") is not None:
                out.append(
                    {
                        "kind": k,
                        "utilization": float(v["utilization"]),
                        "resets_at": v.get("resets_at"),
                    }
                )
    return out


def is_exhausted(limits):
    return any(l["utilization"] >= 100 for l in limits)


# Claude Code stops *before* the extra-usage cap is fully hit (it halts when
# you're close, e.g. ~$99 of a $100 limit), so treat near-cap as exhausted —
# otherwise rotation would wait for 100% that never arrives and leave you stuck
# on a stopped account.
CREDITS_EXHAUSTED_PCT = 99


def credits_available(data):
    """True iff the account has usable extra-usage (overage) credits *right now*:
    extra usage is enabled, its spend limit isn't reached, and utilization is
    below the near-cap threshold. Basis for the per-profile `exhaust_credits`
    option (burn credits before rotating off a rate-limited account).
    Disabled/absent extra_usage → False — nothing to exhaust, don't wait."""
    eu = (data or {}).get("extra_usage") or {}
    if not eu.get("is_enabled") or eu.get("spend_limit_reached"):
        return False
    util = eu.get("utilization")
    try:
        if util is not None and float(util) >= CREDITS_EXHAUSTED_PCT:
            return False
    except (TypeError, ValueError):
        pass
    return True


def token_from_blob(blob):
    try:
        d = json.loads(blob)
        oauth = d.get("claudeAiOauth") or {}
        tok = oauth.get("accessToken")
        exp = oauth.get("expiresAt")
        if tok and (not exp or exp > time.time() * 1000):
            return tok
    except (json.JSONDecodeError, AttributeError):
        pass
    return None


def refresh_expiry_ms(blob):
    """refreshTokenExpiresAt (ms epoch) from a credential blob, or None."""
    try:
        return json.loads(blob).get("claudeAiOauth", {}).get("refreshTokenExpiresAt")
    except (json.JSONDecodeError, AttributeError):
        return None


def refresh_health(blob):
    """'' | 'refresh expires in Nd' (≤14d) | 'refresh EXPIRED' for a blob."""
    exp = refresh_expiry_ms(blob)
    if not exp:
        return ""
    left = exp / 1000 - time.time()
    if left <= 0:
        return "refresh EXPIRED"
    days = left / 86400
    if days <= 14:
        return f"refresh expires in {days:.0f}d"
    return ""


def account_usage(cfg, state, profile, account, ttl=USAGE_TTL):
    """Cached usage summary for an account (live or parked). Returns
    (limits|None, age_seconds|None). None = unknown (no valid token / offline);
    unknown is treated as NOT exhausted — never block a launch on a guess."""
    cache = state.setdefault("usage", {}).get(account)
    now = time.time()
    if cache and now - cache.get("checked_at", 0) < ttl:
        return cache.get("limits"), now - cache["checked_at"]

    blob = None
    if current_account_of(cfg, profile) == account:
        blob = read_live_cred(profile_dir(cfg, profile))
    if blob is None:
        blob = read_parked_cred(account)
    token = token_from_blob(blob) if blob else None
    if not token:
        return (cache or {}).get("limits"), None

    data = fetch_usage(token)
    if data is None:
        return (cache or {}).get("limits"), None
    limits = summarize_usage(data)
    state["usage"][account] = {
        "checked_at": now,
        "limits": limits,
        "credits_available": credits_available(data),
    }
    save_state(state)
    return limits, 0


def account_credits(state, account):
    """Cached extra-usage credit availability (True/False), or None if unknown."""
    return ((state.get("usage") or {}).get(account) or {}).get("credits_available")


# ── mutation lock ───────────────────────────────────────────────────────────

import contextlib
import fcntl


@contextlib.contextmanager
def mutation_lock():
    """Serialize credential mutations (manual swaps vs the refresh daemon)."""
    os.makedirs(STATE_DIR, exist_ok=True)
    fd = os.open(os.path.join(STATE_DIR, ".lock"), os.O_CREAT | os.O_RDWR)
    try:
        for _ in range(100):  # ≤10 s
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                time.sleep(0.1)
        else:
            die("another claude-profile operation holds the lock — try again")
        yield
    finally:
        os.close(fd)


# ── swap mechanics ──────────────────────────────────────────────────────────

def park_current(cfg, profile):
    """Park the profile dir's live credential under its account name.
    Returns the account name. Refuses if the live account has never been
    `save`d (we wouldn't know which parked slot to overwrite)."""
    d = profile_dir(cfg, profile)
    name = current_account_of(cfg, profile)
    if name is None:
        die(
            f"the account currently live in {d} has no snapshot — run "
            f"`claude-profile save <name>` for it first"
        )
    blob = read_live_cred(d)
    if blob is None:
        die(f"no live credential found for {d} ({live_cred_desc(d)})")
    write_parked_cred(name, blob)
    # refresh the metadata snapshot too (profileFetchedAt etc. move on)
    cj = load_claude_json(d) or {}
    snap = load_snapshot(name) or {}
    snap.update(
        {
            "accountUuid": (cj.get("oauthAccount") or {}).get("accountUuid"),
            "oauthAccount": cj.get("oauthAccount"),
            "userID": cj.get("userID"),
            "savedAt": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
        }
    )
    save_snapshot(name, snap)
    return name


def activate_account(cfg, state, profile, target):
    """Swap the profile's live credential to `target` (already-parked).
    Caller has done the guard checks."""
    d = profile_dir(cfg, profile)
    snap = load_snapshot(target)
    if not snap:
        die(f"account \"{target}\" has no snapshot — bootstrap it with `claude-profile save {target}`")
    blob = read_parked_cred(target)
    if blob is None:
        die(f"account \"{target}\" has no parked credential — bootstrap it with `claude-profile save {target}`")

    park_current(cfg, profile)

    write_live_cred(d, blob)
    cj = load_claude_json(d) or {}
    cj["oauthAccount"] = snap.get("oauthAccount")
    if snap.get("userID"):
        cj["userID"] = snap["userID"]
    for k in FLAT_CACHE_KEYS:
        cj.pop(k, None)
    save_claude_json(d, cj)

    state.setdefault("active_account", {})[profile] = target
    save_state(state)


# ── keep-alive refresh (parked accounts) ────────────────────────────────────

def oauth_refresh_grant(refresh_token):
    """Perform the OAuth refresh grant Claude Code itself uses. Returns the
    parsed response dict, or raises RuntimeError with a terse reason."""
    token_url = oauth_setting("token_url")
    client_id = oauth_setting("client_id")
    if not token_url or not client_id:
        raise RuntimeError(
            "OAuth token_url/client_id not configured — add them to the config "
            "\"oauth\" block (see README → \"OAuth constants\")"
        )
    status, data, err = curl_json(
        token_url,
        body={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id,
        },
    )
    if status == 200 and isinstance(data, dict) and data.get("access_token"):
        return data
    if status == 200:
        raise RuntimeError(f"no access_token in response ({list(data or {})[:5]})")
    raise RuntimeError(f"HTTP {status} {err}".strip())


def refresh_gate(cfg, name, min_days_left, force):
    """Decide whether a parked account needs a network refresh grant. Pure /
    read-only (Keychain read only, no network). Returns
    (act: bool, blob: str|None, oauth: dict|None, reason: str|None):
    act=True → a grant should run (reason is None); act=False → reason is the
    human 'skipped/fresh/EXPIRED' line. Single source of truth for both the
    real refresh and the jitter 'is anything due?' pre-check."""
    live_in = [p for p in cfg["profiles"] if current_account_of(cfg, p) == name]
    if live_in and not force:
        return False, None, None, f"{name}: skipped — live in profile \"{live_in[0]}\" (live tokens refresh themselves)"
    blob = read_parked_cred(name)
    if blob is None:
        return False, None, None, f"{name}: skipped — no parked credential"
    try:
        oauth = json.loads(blob).get("claudeAiOauth") or {}
    except json.JSONDecodeError:
        return False, None, None, f"{name}: skipped — unparsable parked blob"
    if not oauth.get("refreshToken"):
        return False, blob, oauth, f"{name}: skipped — parked blob has no refresh token"
    rexp = oauth.get("refreshTokenExpiresAt")
    if rexp and rexp / 1000 <= time.time():
        return False, blob, oauth, f"{name}: refresh token already EXPIRED — run `claude-profile auth {name}`"
    if not force and rexp and (rexp / 1000 - time.time()) > min_days_left * 86400:
        days = (rexp / 1000 - time.time()) / 86400
        return False, blob, oauth, f"{name}: fresh ({days:.0f}d left) — nothing to do"
    return True, blob, oauth, None


def refresh_account(cfg, name, min_days_left, force, quiet):
    """Refresh a parked account's token pair in place. Returns a short result
    string (also printed unless quiet suppresses the boring ones)."""
    act, blob, oauth, reason = refresh_gate(cfg, name, min_days_left, force)
    if not act:
        return reason
    rt = oauth.get("refreshToken")

    try:
        resp = oauth_refresh_grant(rt)
    except RuntimeError as e:
        return f"{name}: refresh grant FAILED ({e}) — parked credential unchanged"

    now_ms = int(time.time() * 1000)
    new_oauth = dict(oauth)
    new_oauth["accessToken"] = resp["access_token"]
    if resp.get("expires_in"):
        new_oauth["expiresAt"] = now_ms + int(resp["expires_in"]) * 1000
    if resp.get("refresh_token"):
        new_oauth["refreshToken"] = resp["refresh_token"]
        if resp.get("refresh_token_expires_in"):
            new_oauth["refreshTokenExpiresAt"] = (
                now_ms + int(resp["refresh_token_expires_in"]) * 1000
            )
        # no expiry in response → keep the old (conservative: warns early)
    new_blob = json.dumps({**json.loads(blob), "claudeAiOauth": new_oauth})

    # write + read-back verify before declaring success — the new refresh
    # token must never exist only in memory
    write_parked_cred(name, new_blob)
    if read_parked_cred(name) != new_blob:
        return f"{name}: KEYCHAIN VERIFY FAILED after refresh — run `claude-profile auth {name}`"
    snap = load_snapshot(name) or {}
    snap["lastRefreshedAt"] = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
    save_snapshot(name, snap)
    nexp = new_oauth.get("refreshTokenExpiresAt")
    if nexp:
        dt = datetime.datetime.fromtimestamp(nexp / 1000).astimezone()
        days = (nexp / 1000 - time.time()) / 86400
        horizon = f", refresh token good until {dt.strftime('%Y-%m-%d')} ({days:.0f}d)"
    else:
        horizon = ""
    return f"{name}: refreshed{horizon}"


def notify_failure(cfg, fails):
    """Best-effort email on keep-alive failure via the system mailer. Opt-in:
    fires only when `notify_email` is set (config, or $CLAUDE_PROFILE_NOTIFY_EMAIL)
    and a `sendmail` exists (e.g. the homelab's msmtp-mta). Never raises."""
    to = os.environ.get("CLAUDE_PROFILE_NOTIFY_EMAIL") or (cfg or {}).get("notify_email")
    if not to or not fails:
        return
    import shutil
    import socket

    sendmail = shutil.which("sendmail") or (
        "/usr/sbin/sendmail" if os.path.exists("/usr/sbin/sendmail") else None
    )
    if not sendmail:
        print("notify_email is set but no `sendmail` was found — skipping email", file=sys.stderr)
        return
    host = socket.gethostname()
    body = (
        f"claude-profile keep-alive refresh failed on {host}:\n\n"
        + "\n".join(f"  - {r}" for r in fails)
        + "\n\nFix: run `claude-profile status`, then `claude-profile auth <name>`.\n"
    )
    msg = f"To: {to}\nSubject: [claude-profile] keep-alive failed on {host}\n\n{body}"
    try:
        subprocess.run(
            [sendmail, "-t"], input=msg, text=True, timeout=30,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        pass


def cmd_refresh(args):
    cfg = load_config()
    pre = []
    if args.name:
        # explicit account → honored regardless of its keep-alive setting
        names = [args.name]
    else:
        # all-accounts / daemon run → skip accounts with keep-alive turned off
        alln = sorted({a for p in cfg["profiles"].values() for a in (p.get("accounts") or [])})
        names = [n for n in alln if account_keepalive(cfg, n)]
        pre = [f"{n}: skipped — keep-alive disabled" for n in alln if not account_keepalive(cfg, n)]

    # Timing jitter: if (and only if) a grant is actually due, sleep a random
    # 0..jitter seconds first, so the daemon's request never lands at a fixed
    # wall-clock time. No-op runs (nothing due) skip the sleep, staying instant.
    # Slept BEFORE the mutation lock so a concurrent manual swap isn't blocked;
    # refresh_account re-runs the gate under the lock, so this is TOCTOU-safe.
    jitter = getattr(args, "jitter", 0) or 0
    if jitter > 0 and any(refresh_gate(cfg, n, args.min_days_left, args.force)[0] for n in names):
        delay = random.uniform(0, jitter)
        stamp = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
        print(f"[{stamp}] jitter: sleeping {delay:.0f}s before grant", file=sys.stderr)
        time.sleep(delay)

    results = list(pre)
    with mutation_lock():
        for name in names:
            results.append(refresh_account(cfg, name, args.min_days_left, args.force, args.quiet))
    stamp = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
    for r in results:
        boring = ": fresh (" in r or ": skipped — live in" in r or ": skipped — keep-alive" in r
        if not (args.quiet and boring):
            print(f"[{stamp}] {r}")
    fails = [r for r in results if "FAILED" in r or "EXPIRED" in r]
    if fails:
        notify_failure(cfg, fails)
        sys.exit(1)


# ── keep-alive daemon (launchd) ─────────────────────────────────────────────

def launchd_plist_path():
    return os.path.join(HOME, "Library", "LaunchAgents", f"{LAUNCHD_LABEL}.plist")


def _daemon_launchd(args):
    plist = launchd_plist_path()
    uid = os.getuid()
    if args.action == "install":
        script = os.path.abspath(__file__)
        log = os.path.join(STATE_DIR, "refresh.log")
        os.makedirs(STATE_DIR, exist_ok=True)
        content = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>{LAUNCHD_LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{sys.executable}</string>
        <string>{script}</string>
        <string>refresh</string>
        <string>--quiet</string>
        <string>--jitter</string>
        <string>{int(args.jitter)}</string>
    </array>
    <key>StartCalendarInterval</key>
    <dict><key>Hour</key><integer>12</integer><key>Minute</key><integer>17</integer></dict>
    <key>RunAtLoad</key><true/>
    <key>StandardOutPath</key><string>{log}</string>
    <key>StandardErrorPath</key><string>{log}</string>
</dict>
</plist>
"""
        os.makedirs(os.path.dirname(plist), exist_ok=True)
        atomic_write(plist, content)
        subprocess.run(
            ["launchctl", "bootout", f"gui/{uid}/{LAUNCHD_LABEL}"], capture_output=True
        )
        res = subprocess.run(
            ["launchctl", "bootstrap", f"gui/{uid}", plist], capture_output=True, text=True
        )
        if res.returncode != 0:
            die(f"launchctl bootstrap failed: {res.stderr.strip()}")
        print(
            f"daemon installed: daily refresh at 12:17 (+ on load), "
            f"up to {int(args.jitter)}s jitter before a due grant — log: {log}"
        )
    elif args.action == "uninstall":
        subprocess.run(
            ["launchctl", "bootout", f"gui/{uid}/{LAUNCHD_LABEL}"], capture_output=True
        )
        try:
            os.unlink(plist)
        except FileNotFoundError:
            pass
        print("daemon uninstalled")
    else:  # status
        res = subprocess.run(
            ["launchctl", "print", f"gui/{uid}/{LAUNCHD_LABEL}"], capture_output=True, text=True
        )
        loaded = res.returncode == 0
        print(f"plist: {plist} ({'present' if os.path.exists(plist) else 'absent'})")
        print(f"launchd: {'loaded' if loaded else 'not loaded'}")
        log = os.path.join(STATE_DIR, "refresh.log")
        if os.path.exists(log):
            with open(log) as f:
                tail = f.readlines()[-6:]
            print("recent log:")
            for line in tail:
                print(f"  {line.rstrip()}")


SYSTEMD_UNIT = "claude-profile-refresh"


def _sctl(*a):
    """systemctl --user with XDG_RUNTIME_DIR filled in (SSH sessions lack it)."""
    env = dict(os.environ)
    env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    return subprocess.run(["systemctl", "--user", *a], capture_output=True, text=True, env=env)


def _daemon_systemd(args):
    import getpass

    unit_dir = os.path.join(
        os.environ.get("XDG_CONFIG_HOME") or os.path.join(HOME, ".config"), "systemd", "user"
    )
    svc = os.path.join(unit_dir, f"{SYSTEMD_UNIT}.service")
    timer = os.path.join(unit_dir, f"{SYSTEMD_UNIT}.timer")
    log = os.path.join(STATE_DIR, "refresh.log")
    user = getpass.getuser()

    if args.action == "install":
        os.makedirs(unit_dir, exist_ok=True)
        os.makedirs(STATE_DIR, exist_ok=True)
        script = os.path.abspath(__file__)
        atomic_write(svc, f"""[Unit]
Description=claude-profile keep-alive (renew parked account refresh tokens)

[Service]
Type=oneshot
ExecStart={sys.executable} {script} refresh --quiet --jitter {int(args.jitter)}
StandardOutput=append:{log}
StandardError=append:{log}
""")
        atomic_write(timer, f"""[Unit]
Description=claude-profile keep-alive timer

[Timer]
OnCalendar=*-*-* 12:17:00
Persistent=true

[Install]
WantedBy=timers.target
""")
        # Linger first, so the user manager runs (and keeps the timer firing
        # while logged out — the point of it on a server).
        linger = subprocess.run(["loginctl", "enable-linger", user], capture_output=True, text=True)
        if linger.returncode != 0:
            linger = subprocess.run(
                ["sudo", "-n", "loginctl", "enable-linger", user], capture_output=True, text=True
            )
        _sctl("daemon-reload")
        res = _sctl("enable", "--now", f"{SYSTEMD_UNIT}.timer")
        if res.returncode != 0:
            die(
                f"systemctl --user enable failed: {(res.stderr or res.stdout).strip()} "
                f"(a bus error? run `claude-profile daemon install` from an interactive login)"
            )
        print(
            f"daemon installed: systemd --user timer '{SYSTEMD_UNIT}.timer', daily 12:17 "
            f"(Persistent catch-up), up to {int(args.jitter)}s jitter — log: {log}"
        )
        if linger.returncode != 0:
            print(
                f"  ⚠ could not enable linger — run `sudo loginctl enable-linger {user}` so the "
                f"timer fires while you're logged out",
                file=sys.stderr,
            )
    elif args.action == "uninstall":
        _sctl("disable", "--now", f"{SYSTEMD_UNIT}.timer")
        for f in (timer, svc):
            try:
                os.unlink(f)
            except FileNotFoundError:
                pass
        _sctl("daemon-reload")
        print("daemon uninstalled")
    else:  # status
        print(f"unit: {timer} ({'present' if os.path.exists(timer) else 'absent'})")
        active = _sctl("is-active", f"{SYSTEMD_UNIT}.timer").stdout.strip() or "inactive"
        enabled = _sctl("is-enabled", f"{SYSTEMD_UNIT}.timer").stdout.strip() or "disabled"
        print(f"systemd --user timer: {active} / {enabled}")
        for line in _sctl("list-timers", "--all", f"{SYSTEMD_UNIT}.timer").stdout.splitlines():
            if SYSTEMD_UNIT in line:
                print(f"  next: {line.strip()}")
        linger = subprocess.run(
            ["loginctl", "show-user", user, "-p", "Linger"], capture_output=True, text=True
        ).stdout.strip()
        print(f"  {linger or 'Linger=?'}")
        if os.path.exists(log):
            with open(log) as f:
                tail = f.readlines()[-6:]
            print("recent log:")
            for line in tail:
                print(f"  {line.rstrip()}")


def cmd_daemon(args):
    (_daemon_launchd if IS_MACOS else _daemon_systemd)(args)


# ── commands ────────────────────────────────────────────────────────────────

def pick_profile(cfg, args):
    """--profile flag, else the resolved active profile."""
    state = load_state()
    if getattr(args, "profile", None):
        if args.profile not in cfg["profiles"]:
            die(f"unknown profile \"{args.profile}\"")
        return args.profile
    name, _ = resolve_profile(cfg, state, os.getcwd())
    return name


def fmt_reset(resets_at):
    if not resets_at:
        return ""
    try:
        t = datetime.datetime.fromisoformat(str(resets_at).replace("Z", "+00:00"))
        return t.astimezone().strftime("%a %H:%M")
    except ValueError:
        return str(resets_at)


def fmt_limits(limits):
    if limits is None:
        return "usage unknown"
    if not limits:
        return "no limit data"
    parts = []
    for l in limits:
        kind = {"five_hour": "5h", "seven_day": "7d", "session": "5h", "weekly": "7d"}.get(
            l["kind"], l["kind"]
        )
        s = f"{kind} {round(l['utilization'])}%"
        if l["utilization"] >= 100 and l.get("resets_at"):
            s += f" (resets {fmt_reset(l['resets_at'])})"
        parts.append(s)
    return " · ".join(parts)


def _rel_short(ms):
    """ms-epoch → short horizon from now: 'expired', '5m', '7h', '28d'."""
    if not ms:
        return "?"
    secs = ms / 1000 - time.time()
    if secs <= 0:
        return "expired"
    if secs < 3600:
        return f"{int(secs // 60)}m"
    if secs < 86400:
        return f"{int(secs // 3600)}h"
    return f"{int(secs // 86400)}d"


def token_horizon(blob, snap):
    """Compact per-account token insight: access-token life, refresh-token life
    + absolute expiry date, and when the pair was last saved/rotated. '' if no
    readable blob."""
    try:
        o = (json.loads(blob).get("claudeAiOauth") or {}) if blob else {}
    except json.JSONDecodeError:
        o = {}
    if not o:
        return ""
    parts = [f"access {_rel_short(o.get('expiresAt'))}"]
    rexp = o.get("refreshTokenExpiresAt")
    if rexp:
        day = datetime.datetime.fromtimestamp(rexp / 1000).astimezone().strftime("%Y-%m-%d")
        parts.append(f"refresh {_rel_short(rexp)} (→ {day})")
    else:
        parts.append("refresh ?")
    snap = snap or {}
    when = snap.get("lastRefreshedAt")
    label, when = ("rotated", when) if when else ("saved", snap.get("savedAt"))
    if when:
        parts.append(f"{label} {str(when)[:10]}")
    return "token: " + " · ".join(parts)


def cmd_status(args):
    cfg = load_config()
    state = load_state()
    resolved, how = resolve_profile(cfg, state, os.getcwd())
    print(f"config: {CONFIG_PATH}")
    toggle = state.get("active_profile")
    if toggle:
        print(f"toggle: {toggle} (explicit — clear with `claude-profile use default`)")
    print()
    for name, p in cfg["profiles"].items():
        d = profile_dir(cfg, name)
        marks = []
        if name == resolved:
            marks.append(f"active:{how}")
        if name == cfg.get("default_profile"):
            marks.append("default")
        if p.get("auto") and is_serial(cfg, name):
            marks.append("auto-rotate")
        mark = f"  [{', '.join(marks)}]" if marks else ""
        bullet = "●" if name == resolved else "○"
        print(f"{bullet} {name}  {p['dir']}{mark}")
        for raw in p.get("paths") or []:
            print(f"    path: {raw}")
        accounts = profile_accounts(cfg, name)
        current = current_account_of(cfg, name) if accounts else None
        sessions = live_sessions(d)
        if sessions:
            print(f"    live sessions: {len(sessions)} (account swap blocked)")
        for acct in accounts:
            snap = load_snapshot(acct)
            is_live = acct == current
            parked_blob = read_parked_cred(acct)
            # live account's true expiry lives in the live Keychain item (Claude
            # Code refreshes it); parked accounts show their parked pair, which
            # is what the keep-alive daemon renews.
            blob = read_live_cred(d) if is_live else parked_blob
            tag = "ACTIVE " if is_live else ("parked " if parked_blob else "UNSAVED")
            email = (snap or {}).get("oauthAccount", {}) or {}
            email = email.get("emailAddress", "")
            if args.usage:
                limits, _ = account_usage(cfg, state, name, acct)
                usage = "  " + fmt_limits(limits)
            else:
                cache = (state.get("usage") or {}).get(acct)
                usage = "  " + fmt_limits(cache.get("limits")) if cache else ""
            health = refresh_health(parked_blob) if (parked_blob and not is_live) else ""
            if health:
                usage += f"  ⚠ {health} → claude-profile auth {acct}"
            print(f"    {'▸' if is_live else ' '} {acct:<10} {tag} {email}{usage}")
            th = token_horizon(blob, snap)
            if not account_keepalive(cfg, acct):
                th = (th + "  · keep-alive OFF") if th else "keep-alive OFF"
            if th:
                print(f"        {th}")
        if accounts and current is None:
            print("      (live account unrecognized — run `claude-profile save <name>`)")
    configured = {a for p in cfg["profiles"].values() for a in (p.get("accounts") or [])}
    strays = sorted(saved_account_names() - configured)
    if strays:
        print()
        print("saved but not in any profile (`claude-profile delete <name>` to remove):")
        for name in strays:
            snap = load_snapshot(name) or {}
            email = (snap.get("oauthAccount") or {}).get("emailAddress", "")
            parked = "parked" if read_parked_cred(name) is not None else "snapshot-only"
            saved_at = snap.get("savedAt", "")
            print(f"    {name:<10} {parked}  {email}  {saved_at}")
    sys.exit(0)


def cmd_resolve(args):
    cfg = load_config(required=False)
    if cfg is None:
        return  # no config → wrapper passthrough
    state = load_state()
    name, _ = resolve_profile(cfg, state, args.pwd or os.getcwd())
    d = profile_dir(cfg, name)
    auto = "1" if (cfg["profiles"][name].get("auto") and is_serial(cfg, name)) else "0"
    print(f"{name}\t{d}\t{auto}")


def cmd_dir(args):
    cfg = load_config()
    if args.name not in cfg["profiles"]:
        die(f"unknown profile \"{args.name}\"")
    print(profile_dir(cfg, args.name))


def cmd_list(args):
    cfg = load_config()
    state = load_state()
    resolved, _ = resolve_profile(cfg, state, os.getcwd())
    for name, p in cfg["profiles"].items():
        active = "active" if name == resolved else ""
        print(f"{name}\t{p['dir']}\t{active}")


def cmd_accounts(args):
    cfg = load_config()
    profile = pick_profile(cfg, args)
    current = current_account_of(cfg, profile)
    listed = profile_accounts(cfg, profile)
    for acct in listed:
        state = "active" if acct == current else (
            "parked" if read_parked_cred(acct) is not None else "unsaved"
        )
        print(f"{acct}\t{state}\t{profile}")
    configured = {a for p in cfg["profiles"].values() for a in (p.get("accounts") or [])}
    for acct in sorted(saved_account_names() - configured):
        print(f"{acct}\tsaved\t-")


def cmd_use(args):
    cfg = load_config()
    state = load_state()
    if args.name in ("default", "clear", "off"):
        state.pop("active_profile", None)
        save_state(state)
        print("profile toggle cleared — path rules / default_profile apply")
        return
    if args.name not in cfg["profiles"]:
        die(f"unknown profile \"{args.name}\" (have: {', '.join(cfg['profiles'])})")
    state["active_profile"] = args.name
    save_state(state)
    print(f"active profile → {args.name} ({cfg['profiles'][args.name]['dir']}) — all shells")
    for pname, p in cfg["profiles"].items():
        if p.get("paths"):
            print(f"note: path rules still outrank the toggle (e.g. {pname}: {', '.join(p['paths'])})")
            break


def cmd_save(args):
    cfg = load_config()
    profile = pick_profile(cfg, args)
    d = profile_dir(cfg, profile)
    blob = read_live_cred(d)
    if blob is None:
        die(f"no live credential for {d} ({live_cred_desc(d)}) — log in with `claude` → /login first")
    cj = load_claude_json(d)
    if not cj or not (cj.get("oauthAccount") or {}).get("accountUuid"):
        die(f"{claude_json_path(d)} has no oauthAccount — log in first")
    with mutation_lock():
        write_parked_cred(args.name, blob)
        save_snapshot(
            args.name,
            {
                "accountUuid": cj["oauthAccount"]["accountUuid"],
                "oauthAccount": cj["oauthAccount"],
                "userID": cj.get("userID"),
                "savedAt": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
            },
        )
        state = load_state()
        state.setdefault("active_account", {})[profile] = args.name
        save_state(state)
    email = cj["oauthAccount"].get("emailAddress", "?")
    print(f"saved live credential of {profile} ({email}) as account \"{args.name}\"")
    if args.name not in profile_accounts(cfg, profile):
        print(
            f"note: \"{args.name}\" is not listed in profile \"{profile}\"'s accounts — "
            f"add it to {CONFIG_PATH}"
        )


def cmd_account(args):
    cfg = load_config()
    profile = pick_profile(cfg, args)
    accounts = profile_accounts(cfg, profile)
    if args.name not in accounts:
        die(f"account \"{args.name}\" is not configured for profile \"{profile}\" (have: {', '.join(accounts) or 'none'})")
    d = profile_dir(cfg, profile)
    current = current_account_of(cfg, profile)
    if current == args.name:
        print(f"\"{args.name}\" is already the live account of {profile}")
        return
    ensure_swappable(d, args.force)
    state = load_state()
    with mutation_lock():
        activate_account(cfg, state, profile, args.name)
    email = (load_snapshot(args.name) or {}).get("oauthAccount", {}).get("emailAddress", "?")
    print(f"{profile}: live account → \"{args.name}\" ({email}). Restart claude to use it.")


def cmd_toggle(args):
    """Switch to the NEXT account in the profile's list (cyclic). With two
    accounts this simply flips between them."""
    cfg = load_config()
    profile = pick_profile(cfg, args)
    accounts = profile_accounts(cfg, profile)
    if len(accounts) < 2:
        die(f"profile \"{profile}\" has {len(accounts)} account(s) — toggle needs at least 2")
    current = current_account_of(cfg, profile)
    if current in accounts:
        target = accounts[(accounts.index(current) + 1) % len(accounts)]
    else:
        target = accounts[0]  # live account unrecognized → start at the first
    d = profile_dir(cfg, profile)
    ensure_swappable(d, args.force)
    state = load_state()
    with mutation_lock():
        activate_account(cfg, state, profile, target)
    email = (load_snapshot(target) or {}).get("oauthAccount", {}).get("emailAddress", "?")
    print(f"{profile}: \"{current or '?'}\" → \"{target}\" ({email}). Restart claude to use it.")


def cmd_delete(args):
    """Delete an account's stored artifacts: its parked Keychain credential
    and metadata snapshot. Reverts a `save`/`auth`. Never touches live
    credentials."""
    cfg = load_config()
    name = args.name
    # capture live-ness BEFORE deleting the snapshot (matching needs it)
    live_in = [p for p in cfg["profiles"] if current_account_of(cfg, p) == name]
    had_parked = read_parked_cred(name) is not None
    had_snap = load_snapshot(name) is not None
    if not had_parked and not had_snap:
        die(f"account \"{name}\" has nothing saved")
    with mutation_lock():
        delete_parked_cred(name)
        try:
            os.unlink(snapshot_path(name))
        except FileNotFoundError:
            pass
        state = load_state()
        for prof, acct in list((state.get("active_account") or {}).items()):
            if acct == name:
                del state["active_account"][prof]
        (state.get("usage") or {}).pop(name, None)
        save_state(state)
    print(f"deleted \"{name}\" — parked credential and snapshot removed (live logins untouched)")
    for p in live_in:
        print(
            f"note: \"{name}\" is still the live login of profile \"{p}\" — it now shows "
            f"as unrecognized/UNSAVED, and swaps off it will refuse until you `save` it again"
        )


def cmd_auth(args):
    """Re-authenticate a (possibly parked) account without touching any live
    profile: run the login flow in a throwaway scratch config dir, harvest the
    fresh credential from the scratch dir's own credential store, park it under
    the account name, then wipe the scratch dir and its credential."""
    import shutil

    load_config()  # config must exist/parse, keeps UX consistent
    name = args.name
    scratch = os.path.join(STATE_DIR, "auth-scratch")

    if not args.no_launch:
        # wipe any previous scratch login so claude forces a fresh /login
        delete_live_cred(scratch)
        shutil.rmtree(scratch, ignore_errors=True)
        os.makedirs(scratch, exist_ok=True)
        claude_bin = shutil.which("claude")
        if not claude_bin:
            die("`claude` binary not found in PATH")
        print(f"Capturing fresh credentials for \"{name}\" via a throwaway config dir.")
        print(f"  1. complete the login — the browser must be signed into the {name} account")
        print("  2. once logged in, just QUIT claude (Ctrl+C twice)")
        print()
        subprocess.call([claude_bin], env=dict(os.environ, CLAUDE_CONFIG_DIR=scratch))
        print()

    blob = read_live_cred(scratch)
    try:
        with open(os.path.join(scratch, ".claude.json")) as f:
            cj = json.load(f)
    except (OSError, json.JSONDecodeError):
        cj = {}
    oauth_acct = cj.get("oauthAccount") or {}
    if blob is None or not oauth_acct.get("accountUuid"):
        die("no login captured in the scratch dir — nothing changed")

    email = oauth_acct.get("emailAddress", "?")
    prev = load_snapshot(name)
    if (
        prev
        and prev.get("accountUuid")
        and prev["accountUuid"] != oauth_acct["accountUuid"]
        and not args.force
    ):
        die(
            f"captured login ({email}) is a different account than \"{name}\"'s "
            f"recorded one — signed into the wrong claude.ai account? "
            f"(--force to accept; scratch login kept for a --no-launch retry)"
        )

    with mutation_lock():
        write_parked_cred(name, blob)
        save_snapshot(
            name,
            {
                "accountUuid": oauth_acct["accountUuid"],
                "oauthAccount": oauth_acct,
                "userID": cj.get("userID"),
                "savedAt": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
            },
        )
    # hygiene: no credentials linger outside the parked item
    delete_live_cred(scratch)
    shutil.rmtree(scratch, ignore_errors=True)
    print(f"parked fresh credentials for \"{name}\" ({email}).")

    cfg = load_config()
    for pname in cfg["profiles"]:
        if current_account_of(cfg, pname) == name:
            print(
                f"note: \"{name}\" is currently LIVE in profile \"{pname}\" — its live "
                f"credential was not modified (live tokens refresh themselves); the "
                f"parked copy applies at the next swap back to it."
            )
    sys.exit(0)


def cmd_rotate(args):
    cfg = load_config()
    profile = pick_profile(cfg, args)
    accounts = profile_accounts(cfg, profile)
    if len(accounts) < 2:
        if not args.quiet:
            print(f"profile \"{profile}\" has {len(accounts)} account(s) — nothing to rotate")
        return
    d = profile_dir(cfg, profile)
    state = load_state()
    current = current_account_of(cfg, profile)

    # Aging-refresh-token nudge: printed even under --quiet (rare, actionable —
    # a parked account nearing refresh expiry means a forced re-login later).
    for acct in accounts:
        if acct == current:
            continue
        blob = read_parked_cred(acct)
        health = refresh_health(blob) if blob else ""
        if health:
            print(
                f"claude-profile: ⚠ account \"{acct}\": {health} — run `claude-profile auth {acct}`",
                file=sys.stderr,
            )

    if current is None:
        if not args.quiet:
            print("live account unrecognized — run `claude-profile save <name>` first")
        return

    limits, _ = account_usage(cfg, state, profile, current)
    rate_limited = limits is not None and is_exhausted(limits)
    cur_exhausted = rate_limited
    burning_credits = False
    if rate_limited and profile_exhaust_credits(cfg, profile) and account_credits(state, current):
        # rate-limited, but this profile opts to spend extra-usage credits first
        # and some remain → not "exhausted" for rotation purposes yet
        cur_exhausted = False
        burning_credits = True
    if args.if_exhausted and not cur_exhausted:
        if not args.quiet:
            if burning_credits:
                print(f"\"{current}\" rate-limited but still has extra-usage credits "
                      f"(exhaust_credits on) — staying put")
            else:
                print(f"\"{current}\" not exhausted ({fmt_limits(limits)}) — no rotation")
        return

    # next non-exhausted account in list order, wrapping past the current one
    idx = accounts.index(current) if current in accounts else -1
    target = None
    for i in range(1, len(accounts)):
        cand = accounts[(idx + i) % len(accounts)]
        if read_parked_cred(cand) is None:
            continue  # never saved — can't activate
        cand_limits, _ = account_usage(cfg, state, profile, cand)
        if cand_limits is not None and is_exhausted(cand_limits):
            continue
        target = cand
        break
    if target is None:
        if not args.quiet:
            print("no rotation target: every other account is exhausted or unsaved")
        return

    why = "exhausted" if cur_exhausted else "rotation requested"
    if args.dry_run:
        print(f"would rotate {profile}: \"{current}\" ({why}) → \"{target}\"")
        return
    sessions = live_sessions(d)
    if sessions:
        # never swap under a live session; auto mode degrades to a warning
        print(
            f"claude-profile: \"{current}\" is {why} but {len(sessions)} live session(s) "
            f"block the swap — close them and relaunch to rotate",
            file=sys.stderr,
        )
        return
    with mutation_lock():
        activate_account(cfg, state, profile, target)
    print(f"claude-profile: rotated {profile}: \"{current}\" ({why}) → \"{target}\"", file=sys.stderr)


def cmd_auto(args):
    cfg = load_config()
    profile = pick_profile(cfg, args)
    cfg["profiles"][profile]["auto"] = args.mode == "on"
    save_config(cfg)
    print(f"profile \"{profile}\": auto-rotate {args.mode}")


def cmd_keepalive(args):
    """Per-account switch for keep-alive refresh-token renewal. No mode → report
    (all, or one account); mode on|off → set and persist to config."""
    cfg = load_config()
    known = {a for p in cfg["profiles"].values() for a in (p.get("accounts") or [])}
    known |= saved_account_names()
    if args.mode is None:
        targets = sorted(known)
        if args.account:
            if args.account not in known:
                die(f"unknown account \"{args.account}\"")
            targets = [args.account]
        print("keep-alive (refresh-token renewal):")
        for a in targets:
            print(f"  {a:<12} {'on' if account_keepalive(cfg, a) else 'OFF'}")
        return
    if not args.account or args.account not in known:
        die(f"unknown account \"{args.account or ''}\" — see `claude-profile keepalive`")
    cfg.setdefault("keepalive", {})[args.account] = args.mode == "on"
    save_config(cfg)
    print(f"keep-alive for \"{args.account}\": {'on' if args.mode == 'on' else 'OFF'}")
    if args.mode == "off":
        print("  its refresh token will now age out on its own — `claude-profile auth "
              f"{args.account}` (or keepalive on) before it expires")


def cmd_usage(args):
    cfg = load_config()
    state = load_state()
    profile = pick_profile(cfg, args)
    for acct in profile_accounts(cfg, profile):
        limits, age = account_usage(cfg, state, profile, acct, ttl=0 if args.fresh else USAGE_TTL)
        print(f"{acct}: {fmt_limits(limits)}")


def main():
    ap = argparse.ArgumentParser(prog="claude-profile", description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("status", help="show config, profiles, accounts, usage")
    p.add_argument("--usage", action="store_true", help="refresh usage from the API")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("resolve", help="print resolved profile for a cwd (wrapper porcelain)")
    p.add_argument("--pwd")
    p.set_defaults(func=cmd_resolve)

    p = sub.add_parser("dir", help="print a profile's config dir")
    p.add_argument("name")
    p.set_defaults(func=cmd_dir)

    p = sub.add_parser("list", help="list profiles (porcelain)")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("accounts", help="list accounts of a profile (porcelain)")
    p.add_argument("--profile")
    p.set_defaults(func=cmd_accounts)

    p = sub.add_parser("use", help="toggle the active profile (all shells)")
    p.add_argument("name")
    p.set_defaults(func=cmd_use)

    p = sub.add_parser("save", help="park the live credential as a named account")
    p.add_argument("name")
    p.add_argument("--profile")
    p.set_defaults(func=cmd_save)

    p = sub.add_parser("account", help="swap the live account (serial)")
    p.add_argument("name")
    p.add_argument("--profile")
    p.add_argument("--force", action="store_true", help="terminate live sessions in the dir first, then swap")
    p.set_defaults(func=cmd_account)

    p = sub.add_parser("toggle", help="switch to the next account (cyclic; flips between two)")
    p.add_argument("--profile")
    p.add_argument("--force", action="store_true", help="terminate live sessions in the dir first, then swap")
    p.set_defaults(func=cmd_toggle)

    p = sub.add_parser("delete", help="delete an account's parked credential + snapshot (reverts save/auth)")
    p.add_argument("name")
    p.set_defaults(func=cmd_delete)

    p = sub.add_parser(
        "auth",
        help="re-authenticate an account via a throwaway config dir (live profiles untouched)",
    )
    p.add_argument("name")
    p.add_argument("--force", action="store_true", help="accept a login that mismatches the recorded account")
    p.add_argument(
        "--no-launch",
        action="store_true",
        help="skip launching claude; harvest an existing scratch-dir login",
    )
    p.set_defaults(func=cmd_auth)

    p = sub.add_parser("rotate", help="switch to the next non-exhausted account")
    p.add_argument("--profile")
    p.add_argument("--if-exhausted", action="store_true", help="only rotate when the live account is exhausted")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--quiet", action="store_true")
    p.set_defaults(func=cmd_rotate)

    p = sub.add_parser(
        "refresh",
        help="keep-alive: renew parked accounts' refresh tokens via the OAuth refresh grant",
    )
    p.add_argument("name", nargs="?", help="account to refresh (default: all configured)")
    p.add_argument(
        "--min-days-left",
        type=float,
        default=14,
        help="only refresh when the refresh token has fewer days left (default 14)",
    )
    p.add_argument("--force", action="store_true", help="refresh even if fresh or live (unsafe when live)")
    p.add_argument("--quiet", action="store_true", help="suppress nothing-to-do lines (daemon mode)")
    p.add_argument(
        "--jitter",
        type=float,
        default=0,
        metavar="SECONDS",
        help="when a grant is due, first sleep a random 0..SECONDS to decorrelate timing (default 0)",
    )
    p.set_defaults(func=cmd_refresh)

    p = sub.add_parser("daemon", help="manage the launchd keep-alive daemon")
    p.add_argument("action", choices=["install", "uninstall", "status"])
    p.add_argument(
        "--jitter",
        type=float,
        default=3600,
        metavar="SECONDS",
        help="install: max random pre-grant delay baked into the plist (default 3600 = 1h)",
    )
    p.set_defaults(func=cmd_daemon)

    p = sub.add_parser("auto", help="toggle auto-rotation for a profile")
    p.add_argument("mode", choices=["on", "off"])
    p.add_argument("--profile")
    p.set_defaults(func=cmd_auto)

    p = sub.add_parser(
        "keepalive",
        help="per-account: whether keep-alive renews its refresh token (no args = report)",
    )
    p.add_argument("account", nargs="?")
    p.add_argument("mode", nargs="?", choices=["on", "off"])
    p.set_defaults(func=cmd_keepalive)

    p = sub.add_parser("usage", help="show per-account usage")
    p.add_argument("--profile")
    p.add_argument("--fresh", action="store_true")
    p.set_defaults(func=cmd_usage)

    args = ap.parse_args()
    if not args.cmd:
        args.usage = False
        cmd_status(args)
        return
    args.func(args)


if __name__ == "__main__":
    main()
