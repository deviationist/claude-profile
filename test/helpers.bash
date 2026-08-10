#!/usr/bin/env bash
# Shared helpers for the claude-profile zsh-layer bats suite.
#
# claude-profile.zsh is zsh-only (${(@f)}/${(r:N:)}/${(@ps:\t:)} flags, assoc
# arrays, $+commands), so — like ccfind's suite — we never source it under
# bash. Each test runs it inside an isolated `zsh -c` subprocess with a fixture
# HOME. All state lives in $BATS_TEST_TMPDIR, created and removed per test.
#
# The .py is copied alongside the .zsh so $_CLAUDE_PROFILE_HOME resolves inside
# the tmpdir: the wrapper stays functional while the real ./.env next to the
# source is never sourced (it carries the caffeinate knobs).

cp_setup() {
  export CP_HOME="$BATS_TEST_TMPDIR/cp"
  mkdir -p "$CP_HOME"
  cp "$BATS_TEST_DIRNAME/../claude-profile.zsh" \
     "$BATS_TEST_DIRNAME/../claude-profile.py" "$CP_HOME/"
  export CP_ZSH="$CP_HOME/claude-profile.zsh"

  # Physical path: cwd path rules are matched as a string prefix against
  # python's os.getcwd(), which is always resolved. On macOS $BATS_TEST_TMPDIR
  # lives under the /var -> /private/var symlink, so a logical FIXHOME would
  # never prefix-match and the path-rule test would fail on the fixture alone.
  export FIXHOME="$(cd "$BATS_TEST_TMPDIR" && pwd -P)/home"
  mkdir -p "$FIXHOME/.claude" "$FIXHOME/.claude-personal"

  # Redirect every path the tool persists to (config via its documented
  # $CLAUDE_PROFILE_CONFIG escape hatch, state + parked creds via XDG).
  export CLAUDE_PROFILE_CONFIG="$BATS_TEST_TMPDIR/config.json"
  export XDG_CONFIG_HOME="$BATS_TEST_TMPDIR/xdg-config"
  export XDG_STATE_HOME="$BATS_TEST_TMPDIR/xdg-state"
  export CLAUDE_CAFFEINATE=0            # no caffeinate around the stub

  export STUBBIN="$BATS_TEST_TMPDIR/bin"
  mkdir -p "$STUBBIN"
  unset CLAUDE_CONFIG_DIR FZF_DEFAULT_OPTS
}

# write_config <json> — install a fixture config at $CLAUDE_PROFILE_CONFIG.
# `~` inside it expands against the fixture HOME, not yours.
write_config() { printf '%s\n' "$1" > "$CLAUDE_PROFILE_CONFIG"; }

# install_claude_stub — shadow the `claude` binary. The wrapper invokes it as
# `command claude`, which honors PATH. It reports the two things every wrapper
# assertion is about: the argv it received and the config dir it inherited.
install_claude_stub() {
  cat > "$STUBBIN/claude" <<'STUB'
#!/bin/sh
echo "CLAUDE argv=[$*] dir=[${CLAUDE_CONFIG_DIR-<unset>}]"
STUB
  chmod +x "$STUBBIN/claude"
}

# run_zsh <snippet> — source the wrapper in an isolated zsh and eval <snippet>.
# The stub bin comes first on PATH; python3/zsh keep resolving normally.
run_zsh() {
  run env HOME="$FIXHOME" PATH="$STUBBIN:$PATH" \
    zsh -c 'source "$1" >/dev/null 2>&1; eval "$2"' _ "$CP_ZSH" "$1"
}

# --- picker drivers -------------------------------------------------------
# $1 is a printf format producing the porcelain rows (\t between columns).

# pick_fzf <rows> <query> — exercise the fzf branch headlessly. --select-1 +
# --exit-0 make a query that matches exactly one row resolve without a TTY,
# and a query that matches none exit non-zero (fzf's own "cancelled").
pick_fzf() {
  run env HOME="$FIXHOME" FZF_DEFAULT_OPTS="--select-1 --exit-0 --query=$2" \
    zsh -c 'source "$1" >/dev/null 2>&1
            sel=$(printf "$2" | _claude_profile_pick "pick> " "HEADER")
            echo "rc=$? sel=[$sel]"' _ "$CP_ZSH" "$1"
}

# pick_menu <rows> [answers...] — exercise the numbered fallback. PATH is
# emptied *after* sourcing so fzf is unfindable (the picker shells out to
# nothing else); pty_run.py supplies the terminal the prompt requires and
# types one answer per prompt.
pick_menu() {
  local rows="$1"; shift
  local -a sends=()
  local a; for a in "$@"; do sends+=(--send "$a"); done
  run env HOME="$FIXHOME" python3 "$BATS_TEST_DIRNAME/pty_run.py" \
    --prompt-re 'pick> ' "${sends[@]}" -- \
    zsh -c 'source "$1" >/dev/null 2>&1; PATH=""
            sel=$(printf "$2" | _claude_profile_pick "pick> " "HEADER")
            echo "rc=$? sel=[$sel]"' _ "$CP_ZSH" "$rows"
}

# run_cli_menu <snippet> [answers...] — like run_zsh, but under a pty with an
# empty PATH, for snippets that reach the numbered selector.
run_cli_menu() {
  local snippet="$1" prompt="$2"; shift 2
  local -a sends=()
  local a; for a in "$@"; do sends+=(--send "$a"); done
  run env HOME="$FIXHOME" python3 "$BATS_TEST_DIRNAME/pty_run.py" \
    --prompt-re "$prompt" "${sends[@]}" -- \
    zsh -c 'source "$1" >/dev/null 2>&1; PATH=""; eval "$2"' _ "$CP_ZSH" "$snippet"
}
