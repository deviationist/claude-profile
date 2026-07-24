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
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

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
USAGE_URL = "https://SET-CLAUDE-CODE-USAGE-URL"
USAGE_TTL = 60  # seconds; server-side usage cache freshness

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
    res = subprocess.run(
        ["security"] + args,
        input=input_str,
        capture_output=True,
        text=True,
    )
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


def saved_account_names():
    """Every account with stored artifacts: snapshot and/or parked item."""
    return set(all_snapshots()) | all_parked_names()


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


# ── usage / exhaustion ──────────────────────────────────────────────────────

def fetch_usage(token):
    req = urllib.request.Request(
        USAGE_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "anthropic-beta": "oauth-2025-04-20",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=6) as r:
            data = json.load(r)
    except (urllib.error.URLError, OSError, json.JSONDecodeError, TimeoutError):
        return None
    if not isinstance(data, dict) or data.get("error"):
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
        blob = keychain_read(live_service(profile_dir(cfg, profile)))
    if blob is None:
        blob = keychain_read(parked_service(account))
    token = token_from_blob(blob) if blob else None
    if not token:
        return (cache or {}).get("limits"), None

    data = fetch_usage(token)
    if data is None:
        return (cache or {}).get("limits"), None
    limits = summarize_usage(data)
    state["usage"][account] = {"checked_at": now, "limits": limits}
    save_state(state)
    return limits, 0


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
    blob = keychain_read(live_service(d))
    if blob is None:
        die(f"no live credential found in Keychain for {d} ({live_service(d)})")
    keychain_write(parked_service(name), blob)
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
    blob = keychain_read(parked_service(target))
    if blob is None:
        die(f"account \"{target}\" has no parked credential — bootstrap it with `claude-profile save {target}`")

    park_current(cfg, profile)

    keychain_write(live_service(d), blob)
    cj = load_claude_json(d) or {}
    cj["oauthAccount"] = snap.get("oauthAccount")
    if snap.get("userID"):
        cj["userID"] = snap["userID"]
    for k in FLAT_CACHE_KEYS:
        cj.pop(k, None)
    save_claude_json(d, cj)

    state.setdefault("active_account", {})[profile] = target
    save_state(state)


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
            parked_blob = keychain_read(parked_service(acct))
            tag = "ACTIVE " if acct == current else ("parked " if parked_blob else "UNSAVED")
            email = (snap or {}).get("oauthAccount", {}) or {}
            email = email.get("emailAddress", "")
            if args.usage:
                limits, _ = account_usage(cfg, state, name, acct)
                usage = "  " + fmt_limits(limits)
            else:
                cache = (state.get("usage") or {}).get(acct)
                usage = "  " + fmt_limits(cache.get("limits")) if cache else ""
            health = refresh_health(parked_blob) if (parked_blob and acct != current) else ""
            if health:
                usage += f"  ⚠ {health} → claude-profile auth {acct}"
            print(f"    {'▸' if acct == current else ' '} {acct:<10} {tag} {email}{usage}")
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
            parked = "parked" if keychain_read(parked_service(name)) is not None else "snapshot-only"
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
            "parked" if keychain_read(parked_service(acct)) is not None else "unsaved"
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
    blob = keychain_read(live_service(d))
    if blob is None:
        die(f"no live credential in Keychain for {d} ({live_service(d)}) — log in with `claude` → /login first")
    cj = load_claude_json(d)
    if not cj or not (cj.get("oauthAccount") or {}).get("accountUuid"):
        die(f"{claude_json_path(d)} has no oauthAccount — log in first")
    keychain_write(parked_service(args.name), blob)
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
    sessions = live_sessions(d)
    if sessions and not args.force:
        pids = ", ".join(str(s["pid"]) for s in sessions)
        die(
            f"{len(sessions)} live Claude session(s) in {d} (pids {pids}) — "
            f"a swap under a running session can corrupt its credentials. "
            f"Close them or use --force",
            code=2,
        )
    state = load_state()
    activate_account(cfg, state, profile, args.name)
    email = (load_snapshot(args.name) or {}).get("oauthAccount", {}).get("emailAddress", "?")
    print(f"{profile}: live account → \"{args.name}\" ({email}). Restart claude to use it.")


def cmd_delete(args):
    """Delete an account's stored artifacts: its parked Keychain credential
    and metadata snapshot. Reverts a `save`/`auth`. Never touches live
    credentials."""
    cfg = load_config()
    name = args.name
    # capture live-ness BEFORE deleting the snapshot (matching needs it)
    live_in = [p for p in cfg["profiles"] if current_account_of(cfg, p) == name]
    had_parked = keychain_read(parked_service(name)) is not None
    had_snap = load_snapshot(name) is not None
    if not had_parked and not had_snap:
        die(f"account \"{name}\" has nothing saved")
    keychain_delete(parked_service(name))
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
    fresh credential from the scratch dir's own Keychain item, park it under
    the account name, then wipe the scratch dir and its Keychain item."""
    import shutil

    load_config()  # config must exist/parse, keeps UX consistent
    name = args.name
    scratch = os.path.join(STATE_DIR, "auth-scratch")
    svc = live_service(scratch)

    if not args.no_launch:
        # wipe any previous scratch login so claude forces a fresh /login
        keychain_delete(svc)
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

    blob = keychain_read(svc)
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

    keychain_write(parked_service(name), blob)
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
    keychain_delete(svc)
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
    if current is None:
        if not args.quiet:
            print("live account unrecognized — run `claude-profile save <name>` first")
        return

    limits, _ = account_usage(cfg, state, profile, current)
    cur_exhausted = limits is not None and is_exhausted(limits)
    if args.if_exhausted and not cur_exhausted:
        if not args.quiet:
            print(f"\"{current}\" not exhausted ({fmt_limits(limits)}) — no rotation")
        return

    # next non-exhausted account in list order, wrapping past the current one
    idx = accounts.index(current) if current in accounts else -1
    target = None
    for i in range(1, len(accounts)):
        cand = accounts[(idx + i) % len(accounts)]
        if keychain_read(parked_service(cand)) is None:
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
    activate_account(cfg, state, profile, target)
    print(f"claude-profile: rotated {profile}: \"{current}\" ({why}) → \"{target}\"", file=sys.stderr)


def cmd_auto(args):
    cfg = load_config()
    profile = pick_profile(cfg, args)
    cfg["profiles"][profile]["auto"] = args.mode == "on"
    save_config(cfg)
    print(f"profile \"{profile}\": auto-rotate {args.mode}")


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
    p.add_argument("--force", action="store_true", help="swap even with live sessions (unsafe)")
    p.set_defaults(func=cmd_account)

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

    p = sub.add_parser("auto", help="toggle auto-rotation for a profile")
    p.add_argument("mode", choices=["on", "off"])
    p.add_argument("--profile")
    p.set_defaults(func=cmd_auto)

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
