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
  swap, fzf picker w/o name; refuses under live sessions, `--force`
  exists), `save <name>` (park current login as a named account —
  bootstrap), `auth <name>` (re-authenticate via a throwaway scratch
  config dir — harvest login → park → wipe scratch; live profiles
  untouched; rejects a mismatched login), `delete <name>` (remove an
  account's parked credential + snapshot — reverts save/auth),
  `rotate [--if-exhausted] [--dry-run]`, `auto on|off`, `usage [--fresh]`.
  `status` warns when a parked refresh token nears/passes expiry
  (`⚠ … → claude-profile auth X`) — the same nudge also prints at every
  `claude` launch of an auto profile — and lists saved-but-unconfigured
  accounts so strays are visible. Keep-alive: `refresh [<name>]
  [--min-days-left N] [--force]` runs the OAuth refresh grant (endpoint
  `SET-CLAUDE-CODE-TOKEN-URL` + public client id, both verified
  against the shipped binary) on parked, non-live accounts and re-parks
  the new pair (write + read-back verify; mutation lock vs manual swaps;
  live accounts never touched). `daemon install|uninstall|status` =
  launchd agent (`com.claude-profile.refresh`, daily 12:17 + RunAtLoad,
  log in the state dir) running `refresh --quiet`.
- **`claude-with <profile> [args]`** — one-shot launch against a profile.
- **`claude-switch [<name>]`** — alias for `claude-profile use`.
- **`claude-default`** — one-shot with `CLAUDE_CONFIG_DIR` unset.
- `claude-usage`/statusline follow the resolved profile via a
  mtime-guarded precmd hook (`CLAUDE_USAGE_DIR`).

Safety invariants (do not regress): secrets never on disk / never in argv;
no credential swap while sessions run in the dir; passthrough (no
`CLAUDE_CONFIG_DIR`) whenever the resolved dir is `~/.claude`; unknown usage
= not exhausted (a launch is never blocked on a guess).
