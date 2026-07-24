# claude-profile — TODO / roadmap

## Test coverage (priority)

The credential-swap path mutates real, hard-to-recover state — macOS Keychain
items **and** `.claude.json` — so it must have committed tests. Dev-time
scaffolding lived in a scratchpad and was never committed; rebuild it under
`test/` with a `test/run.zsh` entry point (mirror the `claude-usage` layout) and
wire CI.

### Unit tests (mock `security`, temp `CLAUDE_CONFIG_DIR`, mock OAuth endpoints)

- **`activate_account` swaps the account identity correctly — the linchpin of
  same-dir subscription switching:** `.oauthAccount` is replaced *in full*
  (crucially `accountUuid`, plus email etc.), `userID` updated, and the flat
  single-account caches (`FLAT_CACHE_KEYS`) cleared. Assert the new
  `accountUuid` is written and the old one is gone. (A stale `accountUuid`
  silently attributes the wrong subscription — usage, limits, and any
  account-keyed cache would point at the previous account.)
- `park_current` re-parks the outgoing account byte-exact (fresh refresh token).
- `refresh_gate` decision matrix: live / fresh / expired / keepalive-off / force.
- `current_account_of` matches by `accountUuid`, prefers a profile-listed name
  over a stray on UUID collisions.
- Keychain blob round-trip through quoting / backslash / newline hazards.
- `oauth_setting` resolution order (env → config `oauth` → None) + graceful
  degrade when unset.
- `_security` FileNotFoundError degrade on Linux (no `security` binary).
- `claude_json_path`: default dir → `$HOME/.claude.json`; custom dir →
  `<dir>/.claude.json`.

### Integration tests (end-to-end against a throwaway config dir)

- `save` → `account` swap round-trip: assert `.claude.json` `accountUuid`
  flipped, live Keychain item updated, outgoing account re-parked; the
  live-session guard blocks the swap and `--force` overrides it.
- `refresh` grant round-trip (mock token endpoint): parked pair renewed +
  read-back verified; `--jitter` sleeps only when a grant is actually due;
  `keepalive off` excludes an account from the sweep.
- `rotate` auto-rotation on faked exhaustion (and the at-launch path).
- Wrapper resolution: path rule > toggle > default, and the passthrough
  invariant (no `CLAUDE_CONFIG_DIR` when resolving to `~/.claude`).
- **Cross-tool:** after an `account` swap, the claude-usage cache reflects the
  new account — both mechanisms: the account-keyed cache path (keyed on
  `accountUuid`) and the swap-time `claude-usage --fresh` trigger in the
  `claude-profile` wrapper.

## Done

- **Linux credential backend** — serial accounts / keep-alive work on Linux
  (live = `<dir>/.credentials.json`, parked = 0600 files under the state dir),
  dispatched by `IS_MACOS`. Add Linux tests to the suite above.
- **Linux keep-alive daemon** — systemd `--user` timer (+ linger) alongside the
  macOS launchd agent.

## Other

- Extend the test suite to cover the Linux backend (file store, systemd daemon
  unit generation) — not just the macOS Keychain path.
