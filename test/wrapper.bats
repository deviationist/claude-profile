#!/usr/bin/env bats
# The `claude` wrapper's resolution order and its passthrough invariant, plus
# the argv-shaping the selector performs for `claude-profile`.
#
# The first group is also the contract ccfind depends on: ccfind resumes a
# session as `CLAUDE_CONFIG_DIR=<hit's profile dir> claude --resume <id>`, which
# lands on this wrapper. If the wrapper ever "helpfully" re-resolved that, every
# cross-profile resume from ccfind would open the wrong seat.

load helpers

setup() {
  cp_setup
  install_claude_stub
  write_config '{
    "default_profile": "work",
    "profiles": {
      "work":     { "dir": "~/.claude" },
      "personal": { "dir": "~/.claude-personal", "paths": ["~/code-private"] }
    }
  }'
}

# --- caller-set CLAUDE_CONFIG_DIR wins (the ccfind resume handoff) ---------

@test "ccfind handoff: a caller-set CLAUDE_CONFIG_DIR is passed through verbatim" {
  run_zsh 'CLAUDE_CONFIG_DIR=$HOME/.claude-personal claude --resume ABC123'
  [ "$status" -eq 0 ]
  [[ "$output" == *"argv=[--resume ABC123]"* ]]
  [[ "$output" == *"dir=[$FIXHOME/.claude-personal]"* ]]
}

@test "ccfind handoff: holds even when the toggle names a different profile" {
  run_zsh '_claude_profile_py use personal >/dev/null
           CLAUDE_CONFIG_DIR=$HOME/.claude claude --resume DEF456'
  [[ "$output" == *"dir=[$FIXHOME/.claude]"* ]]
}

# --- resolution order ------------------------------------------------------

@test "default_profile pointing at ~/.claude leaves CLAUDE_CONFIG_DIR unset" {
  run_zsh 'claude --version'
  [[ "$output" == *"dir=[<unset>]"* ]]
}

@test "toggle selects a profile and exports its dir" {
  run_zsh '_claude_profile_py use personal >/dev/null; claude --version'
  [[ "$output" == *"dir=[$FIXHOME/.claude-personal]"* ]]
}

@test "a cwd path rule outranks default_profile" {
  mkdir -p "$FIXHOME/code-private/proj"
  run_zsh 'cd $HOME/code-private/proj && claude --version'
  [[ "$output" == *"dir=[$FIXHOME/.claude-personal]"* ]]
}

@test "no config at all is a transparent passthrough" {
  rm -f "$CLAUDE_PROFILE_CONFIG"
  run_zsh 'claude --version'
  [ "$status" -eq 0 ]
  [[ "$output" == *"dir=[<unset>]"* ]]
}

@test "claude-with targets a profile for one launch" {
  run_zsh 'claude-with personal --resume X'
  [[ "$output" == *"argv=[--resume X]"* ]]
  [[ "$output" == *"dir=[$FIXHOME/.claude-personal]"* ]]
}

@test "claude-with a ~/.claude profile unsets the dir rather than exporting it" {
  run_zsh '_claude_profile_py use personal >/dev/null; claude-with work --version'
  [[ "$output" == *"dir=[<unset>]"* ]]
}

@test "claude-default always unsets the dir" {
  run_zsh '_claude_profile_py use personal >/dev/null; claude-default --version'
  [[ "$output" == *"dir=[<unset>]"* ]]
}

# --- selector argv-shaping -------------------------------------------------
# `_claude_profile_py` is shadowed after sourcing: it feeds the picker fixture
# porcelain and echoes whatever argv the wrapper finally hands to python.

STUB_PY='_claude_profile_py() {
  case "$1" in
    list)     printf "personal\t~/.claude-personal\tactive\nwork\t~/.claude\t\n" ;;
    accounts) printf "max20x\tactive\tpersonal\nmax5x\tparked\tpersonal\n" ;;
    *)        echo "PY argv=[$*]" ;;
  esac
}'

@test "use without a name runs the selector and forwards the choice" {
  run_cli_menu "$STUB_PY; claude-profile use" 'profile> ' 2
  [[ "$output" == *"PY argv=[use work]"* ]]
}

@test "use with a name never opens the selector" {
  run_zsh "$STUB_PY; claude-profile use personal"
  [[ "$output" == *"PY argv=[use personal]"* ]]
  [[ "$output" != *"1) personal"* ]]
}

@test "cancelling the selector runs nothing" {
  run_cli_menu "$STUB_PY; claude-profile use" 'profile> ' q
  [[ "$output" != *"PY argv=[use"* ]]
}

@test "account without a name runs the selector" {
  run_cli_menu "$STUB_PY; claude-profile account" 'account> ' 1
  [[ "$output" == *"PY argv=[account max20x]"* ]]
}

@test "account --force selects a name and keeps the flag" {
  run_cli_menu "$STUB_PY; claude-profile account --force" 'account> ' 2
  [[ "$output" == *"PY argv=[account max5x --force]"* ]]
}

@test "auth and delete get the account selector too" {
  run_cli_menu "$STUB_PY; claude-profile auth" 'auth> ' 1
  [[ "$output" == *"PY argv=[auth max20x]"* ]]
  run_cli_menu "$STUB_PY; claude-profile delete" 'delete> ' 2
  [[ "$output" == *"PY argv=[delete max5x]"* ]]
}

@test "a non-selection command is passed straight through" {
  run_zsh "$STUB_PY; claude-profile status --usage"
  [[ "$output" == *"PY argv=[status --usage]"* ]]
}

# --- what "stepping aside" costs -------------------------------------------
# A caller-set dir short-circuits claude() before the auto-rotation step. That
# is the documented intent, but it has a consequence worth pinning: sessions
# launched through a preset dir (every ccfind resume, `claude-with`, and any
# hand-set CLAUDE_CONFIG_DIR) never auto-rotate off an exhausted account.

ROTATE_SPY='_claude_profile_py() {
  case "$1" in
    resolve) printf "personal\t$HOME/.claude-personal\t1\n" ;;
    rotate)  echo "ROTATE argv=[$*]" ;;
  esac
}'

@test "auto-rotation runs when the wrapper resolves the profile itself" {
  run_zsh "$ROTATE_SPY; claude --version"
  [[ "$output" == *"ROTATE argv=[rotate --profile personal --if-exhausted --quiet]"* ]]
  [[ "$output" == *"dir=[$FIXHOME/.claude-personal]"* ]]
}

@test "a caller-set dir skips auto-rotation (the ccfind-resume path)" {
  run_zsh "$ROTATE_SPY; CLAUDE_CONFIG_DIR=\$HOME/.claude-personal claude --resume ABC"
  [[ "$output" != *"ROTATE"* ]]
  [[ "$output" == *"dir=[$FIXHOME/.claude-personal]"* ]]
}
