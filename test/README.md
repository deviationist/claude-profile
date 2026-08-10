# claude-profile tests

Two suites, one runner:

```sh
test/run.sh                       # both
python3 test/test_claude_profile.py   # python core only
bats test/picker.bats test/wrapper.bats   # zsh layer only
```

Requirements: `python3` (core, stdlib only). The zsh layer additionally needs
`bats` and `zsh`; `run.sh` skips it with a note when they're absent. `fzf` is
optional — the fzf-branch tests `skip` without it, and the numbered-fallback
tests deliberately run with it unavailable.

## Python core — `test_claude_profile.py`

Pure stdlib `unittest` over `claude-profile.py`. No network and no real
Keychain (both stubbed), so it behaves identically on macOS and Linux; the
Linux file credential backend is exercised directly by forcing `IS_MACOS=False`.

## Zsh layer — `picker.bats`, `wrapper.bats`

`claude-profile.zsh` is zsh-only (`${(@f)}`, `${(r:N:)}`, `${(@ps:\t:)}`,
`$+commands`, assoc arrays), so — as in ccfind's suite — it is never sourced
under bash. Each test runs it in an isolated `zsh -c` subprocess.

Every test is hermetic:

- All state is under `$BATS_TEST_TMPDIR` (bats creates + removes it per test).
- `claude-profile.zsh` **and** `.py` are copied into the tmpdir before sourcing,
  so `$_CLAUDE_PROFILE_HOME` resolves there: the wrapper stays functional while
  the real `./.env` beside the source (caffeinate knobs) is never sourced.
- A fixture `HOME`, plus `$CLAUDE_PROFILE_CONFIG` and `$XDG_{CONFIG,STATE}_HOME`,
  keep your real config, state and parked credentials untouched.
- `claude` itself is a stub on `PATH` that echoes its argv and the config dir it
  inherited — the wrapper invokes it as `command claude`, which honors `PATH`.

**`picker.bats`** — the selector screen. The fzf branch is driven headlessly
via `FZF_DEFAULT_OPTS=--select-1 --exit-0 --query=…`. The numbered fallback is
driven by `pty_run.py`, because it reads `/dev/tty` and only engages when
stderr is a terminal; the driver gives it a real pty and releases one scripted
answer per prompt occurrence, so re-prompts are deterministic and there are no
sleeps. `PATH` is emptied after sourcing to make fzf unfindable — the picker
shells out to nothing else, which is why it slurps stdin with `read` instead of
`cat`.

**`wrapper.bats`** — the `claude` wrapper's resolution order (path rule →
toggle → default), the passthrough invariant (a profile at `~/.claude` must
leave `CLAUDE_CONFIG_DIR` unset), and the argv-shaping the selector does for
`claude-profile`. Its first group is the **ccfind contract**: ccfind resumes a
session as `CLAUDE_CONFIG_DIR=<hit's profile dir> claude --resume <id>`, which
lands on this wrapper, so a caller-set dir must be honored verbatim. ccfind
tests the emitting half in its own suite (`multi-profile: resume carries
CLAUDE_CONFIG_DIR of the hit's profile`); this is the receiving half.

Not covered: the interactive fzf UI itself (query editing, preview), real
credential backends, and network usage calls.
