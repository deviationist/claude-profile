# claude-profile

Juggle multiple Claude subscriptions in [Claude Code](https://claude.com/claude-code) — without ever
feeling the switch.

Two composable modes:

- **Multi-profile** — several Claude Code config dirs (e.g. `~/.claude` for
  work, `~/.claude-personal` for personal), selected automatically by cwd
  path rules or toggled explicitly. Each dir is a fully separate identity:
  its own login, history, settings.
- **Serial accounts** — one config dir holding several *accounts*
  (subscriptions). Switching accounts swaps **only** the OAuth
  token/credentials — sessions, memory, settings, MCP servers, per-project
  trust all persist bit-for-bit. Exhaust one Max subscription, flip to the
  other, `claude --resume`, keep working. An **auto mode** rotates to the
  next non-exhausted account at launch.

The modes nest: a work machine can run a `work` profile plus a `personal`
profile whose two Max subscriptions rotate serially inside it.

> macOS-first: the credential store is the macOS Keychain. Linux support
> (`<dir>/.credentials.json`) is a planned follow-up.

## Install

```sh
git clone https://github.com/deviationist/claude-profile ~/.zsh/claude-profile
echo 'source ~/.zsh/claude-profile/claude-profile.zsh' >> ~/.zshrc
cp ~/.zsh/claude-profile/.env.example ~/.zsh/claude-profile/.env   # optional knobs
```

Requirements: zsh, python3 (stdlib only), macOS `security`. Optional: `fzf`
(interactive pickers), [`claude-usage`](https://github.com/deviationist/claude-statusline)
(statusline integration — auto-routed per profile).

## Configure

`~/.config/claude-profile/config.json` (`$XDG_CONFIG_HOME` respected) — you
edit this by hand; the CLI reads it:

```jsonc
{
  "default_profile": "personal",
  "profiles": {
    "personal": {
      "dir": "~/.claude",           // the Claude Code config dir
      "paths": [],                  // cwd prefixes that auto-select this profile
      "accounts": ["max20x", "max5x"],  // credential slots (order = rotation order)
      "auto": true                  // rotate off an exhausted account at launch
    }
  },
  "oauth": {                        // OPTIONAL — only for refresh/usage; see "OAuth constants"
    "client_id":  "<claude-code-oauth-client-id>",
    "token_url":  "<claude-code-token-endpoint-url>",
    "usage_url":  "<claude-code-usage-endpoint-url>",
    "user_agent": "<claude-code-user-agent>"
  }
}
```

A dual-mode example — work machine with a personal profile running two Max
subscriptions serially:

```jsonc
{
  "default_profile": "work",
  "profiles": {
    "work":     { "dir": "~/.claude" },
    "personal": {
      "dir": "~/.claude-personal",
      "paths": ["~/code-private"],
      "accounts": ["max20x", "max5x"],
      "auto": true
    }
  }
}
```

Runtime state (active toggle, live account per profile, usage cache,
non-secret account metadata) lives in `~/.local/state/claude-profile/`
(`$XDG_STATE_HOME` respected). **Credentials are never written to disk** —
parked accounts are Keychain items (`claude-profile-parked-<name>`).

### OAuth constants

The keep-alive `refresh` and per-account `usage` features talk to Claude
Code's OAuth endpoints, which require Claude Code's own OAuth **client id**,
token/usage **URLs**, and **User-Agent**. Those identify the official client
rather than this tool, so they are **not distributed here** — supply them per
machine in the `oauth` block above (or via the `CLAUDE_PROFILE_CLIENT_ID`,
`CLAUDE_PROFILE_TOKEN_URL`, `CLAUDE_PROFILE_USAGE_URL`, `CLAUDE_PROFILE_UA`
environment variables, which take precedence). Read the values from your own
Claude Code installation.

Everything else works without them: profile routing and account swapping are
fully functional; only `refresh`, `usage`, and exhaustion-based auto-rotation
go inert, and they fail with a clear message rather than silently.

## Use

### Launching

`claude` (the wrapped launcher) resolves the profile per launch:

1. caller-set `CLAUDE_CONFIG_DIR` — always honored, wrapper steps aside
2. cwd path rule (`paths`)
3. explicit toggle (`claude-profile use <name>`) — persists across shells
4. `default_profile`

When the resolved dir is `~/.claude` the wrapper sets nothing — behavior is
byte-identical to the bare binary (and with no config file the wrapper is a
transparent passthrough everywhere). `\claude` bypasses it entirely.

### CLI

```
claude-profile                     status: profiles, accounts, token expiry, usage, toggles
                                   (also lists saved-but-unconfigured accounts)
claude-profile status --usage      …with fresh usage from the API
claude-profile use [<name>|default]   toggle the active profile (fzf picker w/o name)
claude-profile account [<name>]    swap the live account (serial; fzf picker w/o name)
claude-profile save <name>         park the dir's current login as account <name>
claude-profile auth [<name>]       re-authenticate an account via a throwaway
                                   config dir — live profiles untouched
claude-profile delete [<name>]     delete an account's parked credential +
                                   snapshot (reverts save/auth; live untouched)
claude-profile rotate [--dry-run]  switch to the next non-exhausted account
claude-profile auto on|off         toggle launch-time auto-rotation
claude-profile usage [--fresh]     per-account usage (5h/7d windows, resets)
claude-profile refresh [<name>] [--jitter N]   keep-alive: renew aging parked tokens
claude-profile daemon install [--jitter N]|uninstall|status   launchd keep-alive
claude-with <profile> [args]       one-shot launch against a profile
claude-switch [<name>]             alias for `claude-profile use`
claude-default                     one-shot launch with CLAUDE_CONFIG_DIR unset
```

### Bootstrapping serial accounts

Each account must be *saved* once while it is the dir's live login:

```sh
claude-profile save max20x    # park the currently logged-in subscription
claude                        # → /login, sign into the second subscription
claude-profile save max5x     # park that one too
claude-profile account max20x # …and flip back
```

From then on: `claude-profile account <name>` any time (restart Claude
after), or let auto mode handle it.

### Re-authenticating a parked account

Access tokens are short-lived and refresh silently; the clock that matters
is the **refresh token** (~weeks). Every swap off an account re-parks its
freshest pair, so regular rotation keeps parked accounts alive by itself —
only an account left untouched for weeks can age out.

Two lines of defence when one does age:

**1. Keep-alive (`refresh` + daemon).** `claude-profile refresh` performs
the same OAuth refresh grant Claude Code uses (endpoint + public client id
as shipped in the binary) on parked accounts whose refresh token has fewer
than 14 days left (`--min-days-left`), and re-parks the new pair. Safety
ordering: live accounts are never touched (their tokens refresh
themselves), the new pair is written **and read back** from the Keychain
before success is declared, and a mutation lock serializes it against
manual swaps. `claude-profile daemon install` registers a launchd agent
running it daily (`daemon status` / `daemon uninstall` to inspect/remove).

The daemon fires at a fixed time (12:17) + login, but only ~once per
14-day window does it actually make a network call. To keep that call off a
predictable wall-clock beat, `refresh --jitter SECONDS` sleeps a random
`0..SECONDS` **only when a grant is due** (no-op runs stay instant, before
the lock so a concurrent swap isn't blocked). `daemon install [--jitter N]`
bakes this into the plist (default `3600` = grants land anywhere in a 1h
window; `--jitter 0` to disable).

**2. Warnings + painless re-auth.** If a token still ages out (machine off
for weeks, revocation), you're warned in `status` **and at every `claude`
launch** of an auto profile (`⚠ refresh expires in Nd` / `refresh
EXPIRED`). Fixing it does **not** require swapping the account live:

```sh
claude-profile auth max5x
```

This launches Claude Code in a **throwaway scratch config dir** (under the
state dir), where you complete a normal login for that account and quit.
The fresh credential is harvested from the scratch dir's own Keychain item,
parked under the account name, and the scratch dir + its Keychain item are
wiped. Your live profile is never touched. A login that doesn't match the
account's recorded identity is rejected (`--force` overrides;
`--no-launch` re-harvests the kept scratch login after a mismatch).

## How serial switching works

A Claude Code subscription lives in exactly two places:

1. a Keychain item (`Claude Code-credentials`, or a per-dir suffixed variant
   for custom `CLAUDE_CONFIG_DIR`s) holding the OAuth access/refresh tokens,
   and
2. the `oauthAccount` + `userID` block in the dir's `.claude.json`.

Nothing else in the config dir is account-bound. A swap:

1. **refuses if live sessions** are running out of the dir (session registry
   + pid liveness) — swapping under a running process risks the old session
   clobbering the new tokens on refresh (`--force` exists; don't),
2. re-parks the outgoing account's current blob (so its refresh token stays
   fresh — rotation only happens on use),
3. writes the incoming account's parked blob into the live Keychain item
   (update-in-place, preserving the item's ACL),
4. patches `.claude.json`: `oauthAccount`, `userID`, and drops a handful of
   single-account caches (`modelAccessCache` etc.) so they regenerate.

Auto mode runs the equivalent of `rotate --if-exhausted` before each launch:
exhaustion is read from Claude's own OAuth usage endpoint (any rate-limit
window at ≥100%), cached for 60 s. Unknown usage (offline, stale token) is
treated as *not* exhausted — a launch is never blocked on a guess.

## Safety guarantees

- **Secrets never touch disk** and never appear in process argv (Keychain
  writes go through `security -i` on stdin). State files hold only
  metadata: email, account UUIDs, timestamps, usage numbers.
- **No swap under live sessions** — enforced in `account` and `rotate`;
  auto mode degrades to a warning and launches on the exhausted account.
- **Transparent when idle** — no config file, or the resolved profile is the
  default `~/.claude`: the wrapper adds nothing but (macOS) a `caffeinate`
  wrap, and `CLAUDE_CONFIG_DIR` is untouched.
- **Recoverable by design** — worst case (Claude Code changes its internals)
  is a normal `/login`; nothing in the config dir is at risk.

## Caveats

- Restart Claude Code after a swap — a running session keeps its old tokens.
- The first Keychain access after an update-in-place may pop a one-time
  macOS "Always Allow" prompt.
- Relies on observed (unofficial) Claude Code internals: the Keychain item
  format, `.claude.json` fields, and the OAuth usage endpoint. Pinned
  behaviors are documented in the source; breakage is benign (see above).

## License

MIT
