# claude-profile — TODO / roadmap

## Test coverage

A committed stdlib `unittest` suite now lives at `test/test_claude_profile.py`
(47 tests, run via `python3 test/test_claude_profile.py`), wired to CI in
`.github/workflows/test.yml`. It stubs the network + Keychain and exercises the
Linux file backend directly. **Covered so far:** credential store (file backend
round-trip / 0600 / listing / delete), `refresh_gate` decisions, `credits_
available` + `exhaust_credits` rotation, `toggle` selection, `ensure_swappable`
guard + `--force` kill, `oauth_setting` resolution, `claude_json_path`,
`is_exhausted`/`summarize_usage`, and the `security`-absent degrade.

### Still to add

- **Full integration round-trips** against a mock OAuth endpoint: `save` →
  `account` swap (assert `.claude.json` accountUuid flips, live cred updated,
  outgoing re-parked), `refresh` grant (parked pair renewed + read-back), `auth`
  harvest from a scratch dir.
- **Daemon unit-file generation** (launchd plist / systemd unit content).
- **Wrapper resolution** (path rule > toggle > default; passthrough invariant) —
  currently only covered by the ad-hoc zsh checks, not the committed suite.

### Original unit-test wishlist (mostly covered above; kept for reference)

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

## Parked accounts' 5-hour windows go unkept

Keep-alive renews parked accounts' **refresh tokens**, but nothing keeps their
**5-hour usage windows** open. Only the live account's window gets anchored, by
whatever runs against the profile dir — e.g.
[claude-auto-window](https://github.com/deviationist/claude-auto-window), whose
profile model is one config dir ⇒ one account, so parked accounts are invisible
to it. Net effect: swap to a parked account after a quiet stretch and you land on
a closed window, having forfeited hours of allowance you were entitled to. The
serial model is what creates the gap — the two tools are each correct alone.

No swap is needed to fix it: a window is a property of the **account**, not the
config dir, and `account_usage()` already proves a parked credential works
headlessly. Sketch of a `keepwindow <account>` subcommand:

1. take `mutation_lock()`
2. materialize the parked blob into a scratch config dir — the parked file is
   already byte-compatible with `.credentials.json`
3. run the window-opener against it (`claude-auto-window --run --config-dir
   <scratch>`). Reuse it rather than reimplementing the starter: opening a window
   requires a **real interactive session**, `claude -p` does not open one
4. **re-park the rotated credential**, then remove the scratch dir

Step 4 is load-bearing. `refresh_account()` shows the refresh token rotates on
use, so a real launch against the scratch dir rotates the pair — skip the
write-back and the parked copy is dead, and the next `account` swap installs a
stale token. `claude-profile` must remain the single writer.

Open questions: driven by its own timer or by the opener's daemon loop; whether
to consult a balance gate per account before spending; whether `rotate`/`auto`
should factor "window currently open" into the target it picks.

## Auto-rotate fails silently under concurrent sessions

Launch-time auto-rotate (`rotate --if-exhausted --quiet` in the wrapper) refuses
to swap while **any** live session runs out of the config dir — the correct
"never swap the OAuth credential out from under a running session" invariant
(`cmd_rotate` → `live_sessions(d)` guard). But for anyone who runs concurrent
sessions in `~/.claude`, this means: the live account exhausts, you relaunch,
`live_sessions()` still sees your *other* open session, and the swap is skipped.
The relaunched session comes back up on the still-exhausted account. The block
prints one stderr line, but Claude Code's TUI clears the screen immediately, so
it's invisible — it just looks like nothing happened, and you rotate by hand
(observed 2026-07-25: max5x at 5h 100%, max20x at 4%, 2 live sessions, restart
didn't rotate, manual swap needed).

Fix: make the blocked rotation **self-heal** instead of silently dropping it.

1. When `cmd_rotate` detects exhaustion but bails on the live-session guard,
   persist a **"rotation pending"** marker to state (target account + reason +
   timestamp), rather than only warning to stderr.
2. On every subsequent launch, the wrapper's rotate step checks the marker
   first: if a pending rotation exists **and** `live_sessions(d)` is now empty,
   apply it under `mutation_lock()` and clear the marker — so it fires the moment
   the last blocking session closes, with no operator action.
3. Clear/refresh the marker if the pending target itself has since exhausted, or
   the once-exhausted account recovered (window reset) — don't act on a stale
   decision.

Secondary (cheap, do alongside): surface the pending/blocked state where it's
actually visible — a `status` line and/or a statusline flag — since the stderr
warning is eaten by the TUI.

Open questions: whether a pending marker should also be honored by an explicit
`claude-profile rotate` (probably yes) vs. only the launch path; interaction with
`--force` (force should just clear any marker after swapping).

## Done

- **Linux credential backend** — serial accounts / keep-alive work on Linux
  (live = `<dir>/.credentials.json`, parked = 0600 files under the state dir),
  dispatched by `IS_MACOS`. Add Linux tests to the suite above.
- **Linux keep-alive daemon** — systemd `--user` timer (+ linger) alongside the
  macOS launchd agent.

## Other

- Extend the test suite to cover the Linux backend (file store, systemd daemon
  unit generation) — not just the macOS Keychain path.
