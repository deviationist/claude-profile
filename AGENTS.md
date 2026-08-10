# claude-profile — agent quick reference

Claude Code subscription juggler. Two composable modes: **multi-profile**
(several config dirs, selected by cwd path rules / explicit toggle) and
**serial accounts** (one dir, several subscriptions; switching swaps only the
OAuth credential — environment persists bit-for-bit). Full docs: `README.md`.

- Config (user-edited): `~/.config/claude-profile/config.json`
  (`$CLAUDE_PROFILE_CONFIG` relocates it; claude-usage watches this file's
  mtime to expire a cached seat label, so both sides read the same knob) —
  `profiles.<name>: {dir, paths[], accounts[], auto}` + `default_profile`.
- State (tool-managed): `~/.local/state/claude-profile/` — `state.json`
  (active toggle, usage cache) + `accounts/*.json` (non-secret metadata, via
  `accounts_dir()` — derived from `STATE_DIR` per call, never frozen, so a
  redirected `STATE_DIR` can't resolve back onto real state).
  Secrets live only in the macOS Keychain (`claude-profile-parked-<name>`).
  A saved account = **both** artifacts (parked credential + snapshot); a swap
  needs the pair, which is what `ensure_account_ready()` checks.

Commands (zsh layer sources `claude-profile.zsh`; core is `claude-profile.py`,
python3 stdlib):

- **`claude`** (wrapped) — resolves profile per launch: explicit
  `CLAUDE_CONFIG_DIR` > cwd path rule > toggle > `default_profile`. For the
  default `~/.claude` dir (or with no config file) it is a transparent
  passthrough — `CLAUDE_CONFIG_DIR` never set. With `auto: true` + >1
  accounts it runs `rotate --if-exhausted` first (never blocks a launch;
  never swaps under live sessions). macOS launches run under `caffeinate`
  (knobs in `./.env`). Bypass: `\claude`.
- **`claude-profile`** — status (default), `use <name>|default` (persistent
  profile toggle, picker w/o name), `account <name>` (serial credential
  swap, picker w/o name; refuses under live sessions, `--force` =
  SIGTERM/SIGKILL them first then swap — `ensure_swappable()`; the refusal
  lists each blocking pid+cwd *and* the blocked swap via `swap_context()`
  — profile, current account, target, both labelled with emails by
  `account_label()`, since the dir name alone doesn't say which seats were
  involved; `rotate`'s own session-blocked line carries the same labels),
  `toggle` (switch to the NEXT account in the profile list, cyclic — flips
  between two; same `--force`). Both swaps run `ensure_account_ready()` **before**
  `ensure_swappable()` — a target with no parked credential is the never-captured /
  post-`delete` case, so an interactive run offers the `auth` flow inline and
  continues (auth needs no session guard, so the fixable half lands even when the
  swap is still blocked); non-interactive dies with the `auth` line. Because a
  first capture has no recorded uuid for `cmd_auth`'s mismatch guard to compare
  against, the preflight rejects (and, if nothing pre-existed, discards) a capture
  whose uuid equals the account being swapped *away* from. Interactivity goes
  through the `interactive()` seam; `save <name>` (park current login as a named
  account — bootstrap), `auth <name> [--email E] [--tui]` (re-authenticate via a
  throwaway scratch dir; drives `claude auth login --claudeai` = focused
  URL+paste-code sign-in, no first-run TUI, headless/SSH-friendly with no
  localhost callback; `--email` prefills, `--tui` falls back to the full
  client → harvest login → park → wipe scratch; live profiles untouched;
  rejects a mismatched login), `delete <name>` (remove an
  account's parked credential + snapshot — reverts save/auth),
  `rotate [--if-exhausted] [--dry-run]`, `auto on|off`, `usage [--fresh]`,
  `usage-json [--all|--profile P|--account A]` (porcelain for
  `claude-usage --all`: emits `<account>\t<compact-raw-usage-json>` per line,
  empty field = unavailable; `account_usage_raw()` refreshes an expired *parked*
  access token in place first — under the mutation lock, via `refresh_account` —
  so parked accounts stay renderable; **never touches a live credential**, so a
  live account with an idle-expired token comes back empty until Claude Code
  refreshes it).
  `anchor-window [--all|--profile P|--account A] [--model ID] [--prompt T]
  [--max-tokens N]` anchors a 5-hour usage window per account by firing ONE
  POST /v1/messages with that account's token (Claude Code identity spoof +
  oauth-2025-04-20 beta header), no session launch and no live swap — the only way
  to anchor a *parked* serial account's window. Token resolution reuses
  `account_access_token()` (extracted from `account_usage_raw`: live cred, else
  parked, refresh-parked-under-lock); the token rides on curl stdin (never argv/
  disk). Fires unconditionally (gating belongs to the orchestrator,
  claude-auto-window). Porcelain: `<account>\t<anchored|error>\t<http>\t<live|
  parked>\t<detail>` per line, exit 0 iff all anchored. Unofficial spoof — may be
  rejected if Anthropic changes the rule.
  Per-profile **`exhaust_credits`** (config, default false): when true, a
  rate-limited account isn't "exhausted" for rotation while it still has
  extra-usage credits (`credits_available()` off `usage.extra_usage`; near-cap
  threshold `CREDITS_EXHAUSTED_PCT=99`, since Claude stops just before the cap)
  — so auto mode burns credits first, then swaps.
  `status` prints a per-account **token line** (access-token life, refresh-
  token life + absolute expiry date, and `saved`/`rotated <date>` — the
  `rotated` label confirms the keep-alive ran; live account reads the live
  Keychain item, parked accounts their parked pair) and warns when a parked
  refresh token nears/passes expiry
  (`⚠ … → claude-profile auth X`) — the same nudge also prints at every
  `claude` launch of an auto profile — and lists saved-but-unconfigured
  accounts so strays are visible. Keep-alive: `refresh [<name>]
  [--min-days-left N] [--force] [--jitter SECONDS]` runs the OAuth refresh
  grant (Claude Code's OAuth client id + endpoints + User-Agent, supplied
  per machine via the config `oauth` block / `CLAUDE_PROFILE_*` env — never
  committed, see `oauth_setting()`; transport is curl w/ the configured UA,
  since urllib is CF-1010-blocked) on parked, non-live accounts and re-parks
  the new pair (write + read-back verify; mutation lock vs manual swaps; live
  accounts never touched). `refresh_gate()` is the single source of truth
  for "is a grant due?" — shared by the real refresh and the `--jitter`
  pre-check, which sleeps a random `0..SECONDS` (pre-lock) only when a grant
  is actually due. Keep-alive is **per-account opt-out**: `keepalive
  [<account>] [on|off]` (no args = report) writes the config `keepalive` map
  (account → bool, default true, `account_keepalive()`); the daemon sweep
  (`refresh` with no name) skips off accounts, but an explicit `refresh
  <account>` ignores the toggle. `status` shows `keep-alive OFF`.
  `daemon install [--jitter N]|uninstall|status` = keep-alive scheduler,
  daily 12:17, running `refresh --quiet --jitter <N>` (default 3600), logging
  to the state dir. **macOS** = launchd agent (`com.claude-profile.refresh`,
  RunAtLoad). **Linux** = systemd `--user` timer (`claude-profile-refresh`,
  Persistent catch-up) + `loginctl enable-linger` so it fires while logged
  out; `_sctl()` injects `XDG_RUNTIME_DIR` for SSH sessions. Dispatched by
  `IS_MACOS`.
- **`resolve`** — which profile applies here. Default output is the wrapper
  porcelain `<name>\t<dir>\t<auto>`. **`--json`** is the single-call contract
  for display consumers (`claude-usage --show-profile` → the statusline):
  `{schema, active, profile, display, dir, account, account_display, serial,
  auto, source, label}`, plus `accounts[]` (name/profile/display/label/live per
  configured account) under `--accounts`. **`--dir PATH`** is a reverse lookup
  (which profile owns this config dir) — the question a statusline must ask,
  since it knows the session's dir but has no meaningful cwd; ties go to the
  profile listing the live account. `label` is composed HERE and rendered
  verbatim downstream: `display` / `account_display` from config, no
  title-casing heuristics, and each half included only when it disambiguates
  (profile named only if >1 profile exists, account only if the profile is
  serial; neither → **empty label**, a valid answer rather than a failure), so a
  single-profile host renders `Max 20x` rather than `personal (max20x)`, and a
  one-profile-one-account host renders nothing at all. `{"schema":1,"active":false}` at exit 0 = no config / unclaimed dir —
  an ordinary outcome, not an error. Invariants: **never touch the Keychain**
  on this path (`.claude.json` + snapshots only; it runs on a render loop), and
  don't change the plain output shape — the zsh wrapper parses it.
- **`_claude_exec`** — the single choke point every launch path funnels through
  (`claude` incl. all its early returns, `claude-with`, `claude-default`).
  Besides the macOS `caffeinate` wrap it **warms claude-usage's caches** for the
  seat being launched (`claude-usage --dir "${CLAUDE_CONFIG_DIR:-~/.claude}"
  --show-profile`, backgrounded, gated on the function existing). Reason: the
  statusline never blocks, and Claude Code paints it ONCE at startup — with no
  `refreshInterval` set it doesn't paint again until the user acts, so a cold
  cache reads as a missing segment rather than a late one. We're the only
  process that knows the seat *before* Claude starts. Put launch-time
  side effects here, not in `claude()` — that function returns early in three
  places and each one would have to repeat them.
- **`claude-with <profile> [args]`** — one-shot launch against a profile.
- **`claude-switch [<name>]`** — alias for `claude-profile use`.
- **`claude-default`** — one-shot with `CLAUDE_CONFIG_DIR` unset.
- `claude-usage`/statusline follow the resolved profile via a
  mtime-guarded precmd hook (`CLAUDE_USAGE_DIR`), and label the seat via
  `resolve --json --dir` (above).

Safety invariants (do not regress): secrets never on disk / never in argv;
no credential swap while sessions run in the dir; passthrough (no
`CLAUDE_CONFIG_DIR`) whenever the resolved dir is `~/.claude`; a caller-set
`CLAUDE_CONFIG_DIR` honored verbatim (ccfind resumes through it); unknown usage
= not exhausted (a launch is never blocked on a guess).

Colour: the human-facing commands emit SGR — `color_enabled()` gates
on isatty, honours `NO_COLOR`, and takes `$CLAUDE_PROFILE_COLOR=always|never`
(`always` wins over `NO_COLOR`, as `ls --color=always` does; the README-SVG
generator relies on it). **Invariant: no porcelain may ever emit an escape** —
`list`/`accounts`/`resolve --json`/`usage-json` are parsed by field by the zsh
layer and claude-usage; there are tests for this at both levels. `c()` colours,
callers pad first (escapes count toward str width, not toward drawn width);
`c(..., stream=sys.stderr)` gates on stderr for messages that go there.
`die(msg, style=)` colours only the message's FIRST line — multi-line refusals
(the live-session guard) style their own body, so the complaint, the evidence
and the way out stay distinguishable.

Selector: the name-less forms of `use`/`account`/`auth`/`delete` open
`_claude_profile_pick` — fzf when installed, a numbered `/dev/tty` prompt
otherwise. fzf is optional and documented as such; the fallback prints a
one-time nudge (`_claude_profile_fzf_hint`, silenced by
`CLAUDE_PROFILE_NO_FZF_HINT`) which lives in `claude-profile()` rather than the
picker because the picker runs inside `$( … )`, where a flag could not persist.

Assets: `zsh tools/generate-readme-svg.zsh` regenerates the two README SVGs
from a hermetic sandbox (fake `$HOME`, seeded config/snapshots, stub `security`
so the Keychain is never opened, stub `fzf` that captures the picker's row
list) and rewrites the README `<img>` refs. The text in both images is real
output; only the window chrome is drawn.

Tests: `test/run.sh` runs both suites — `test/test_claude_profile.py` (python
core, stdlib unittest) and `test/{picker,wrapper}.bats` (zsh layer: the selector
screen's fzf + numbered branches, the wrapper's resolution order, and the
argv-shaping). The zsh suite needs `bats`; the numbered fallback is driven
through a real pty by `test/pty_run.py` because it reads `/dev/tty`. Harness
notes in `test/README.md`. Every invariant above has a test — add one alongside
any change here.
