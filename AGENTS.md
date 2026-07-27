# claude-profile — agent quick reference

Claude Code subscription juggler. Two composable modes: **multi-profile**
(several config dirs, selected by cwd path rules / explicit toggle) and
**serial accounts** (one dir, several subscriptions; switching swaps only the
OAuth credential — environment persists bit-for-bit). Full docs: `README.md`.

- Config (user-edited): `~/.config/claude-profile/config.json` —
  `profiles.<name>: {dir, paths[], accounts[], auto}` + `default_profile`.
- State (tool-managed): `~/.local/state/claude-profile/` — `state.json`
  (active toggle, usage cache) + `accounts/*.json` (non-secret metadata).
  Secrets live only in the macOS Keychain (`claude-profile-parked-<name>`).

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
  profile toggle, fzf picker w/o name), `account <name>` (serial credential
  swap, fzf picker w/o name; refuses under live sessions, `--force` =
  SIGTERM/SIGKILL them first then swap — `ensure_swappable()`),
  `toggle` (switch to the NEXT account in the profile list, cyclic — flips
  between two; same `--force`), `save <name>` (park current login as a named account —
  bootstrap), `auth <name> [--email E] [--tui]` (re-authenticate via a
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
- **`claude-with <profile> [args]`** — one-shot launch against a profile.
- **`claude-switch [<name>]`** — alias for `claude-profile use`.
- **`claude-default`** — one-shot with `CLAUDE_CONFIG_DIR` unset.
- `claude-usage`/statusline follow the resolved profile via a
  mtime-guarded precmd hook (`CLAUDE_USAGE_DIR`).

Safety invariants (do not regress): secrets never on disk / never in argv;
no credential swap while sessions run in the dir; passthrough (no
`CLAUDE_CONFIG_DIR`) whenever the resolved dir is `~/.claude`; unknown usage
= not exhausted (a launch is never blocked on a guess).
