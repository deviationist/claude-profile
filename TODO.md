# claude-profile — TODO / roadmap

## Test coverage

A committed stdlib `unittest` suite now lives at `test/test_claude_profile.py`
(63 tests, run via `python3 test/test_claude_profile.py`), wired to CI in
`.github/workflows/test.yml`. It stubs the network + Keychain and exercises the
Linux file backend directly. **Covered so far:** credential store (file backend
round-trip / 0600 / listing / delete), `refresh_gate` decisions, `credits_
available` + `exhaust_credits` rotation, `toggle` selection, `ensure_swappable`
guard + `--force` kill, `oauth_setting` resolution, `claude_json_path`,
`is_exhausted`/`summarize_usage`, the swap readiness preflight
(`ensure_account_ready`: non-interactive hint, prompt accept/decline, ordering
vs. the session guard, wrong-account capture discard), and the
`security`-absent degrade.

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

## ~~Parked accounts' 5-hour windows go unkept~~ — DONE (2026-07-27)

**Solved by the `anchor-window` subcommand** (+ its orchestration in
[claude-auto-window](https://github.com/deviationist/claude-auto-window) v1.3.0,
which discovers accounts from this config and delegates the per-account anchor
here). Parked accounts' 5-hour windows are now kept open continuously.

The problem was real — keep-alive renews **refresh tokens**, but nothing kept the
**5-hour usage windows** open, and claude-auto-window's one-dir-⇒-one-account
model left parked accounts invisible, so swapping to one after a quiet stretch
landed on a closed window.

The fix turned out **cleaner than the sketched `keepwindow`** (scratch-dir +
interactive `claude --run` + re-park the rotated pair): `anchor-window` instead
fires a single `POST /v1/messages` with the account's **access** token (Claude
Code identity spoof), which anchors the window with **no session, no scratch dir,
no swap, and no refresh-token rotation** in the common case — so the load-bearing
"step 4 re-park" concern mostly evaporates. `account_access_token()` (extracted
from `account_usage_raw`) resolves the token — live cred, else parked, refreshing
an expired *parked* token in place under `mutation_lock()` (via `refresh_account`,
re-parked in place); the token rides on curl stdin, never argv/disk. So
**`claude-profile` remains the single writer** of the credential store — the
invariant this section insisted on is preserved.

The open questions resolved as: driven by **the opener's daemon loop** (not a
timer here) — claude-auto-window owns cadence, balance-gating, breaker, and the
"window already open?" check per account; `anchor-window` fires unconditionally
when asked. `rotate`/`auto` were left as-is (window-keeping is now independent of
which account is live).

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
