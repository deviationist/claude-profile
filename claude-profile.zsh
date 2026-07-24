# claude-profile — Claude Code subscription juggler (shell layer).
#
# Wraps the `claude` launcher with config-driven profile resolution and
# launch-time account auto-rotation, and exposes the `claude-profile` CLI
# (core logic in claude-profile.py next to this file — see README.md).
#
# Resolution order for a `claude` launch:
#   1. caller-set CLAUDE_CONFIG_DIR        (always honored, wrapper steps aside)
#   2. cwd path rule from config.json      (profile "paths" entries)
#   3. explicit toggle (claude-profile use) persisted in the state file
#   4. default_profile
# Passthrough invariant: when the resolved dir is ~/.claude (Claude's built-in
# default) CLAUDE_CONFIG_DIR is NOT set — behavior is byte-identical to
# running the binary with no wrapper installed. With no config file at all the
# wrapper is a transparent passthrough everywhere.
#
# Auto mode: if the resolved profile has "auto": true and >1 accounts, each
# launch first runs `claude-profile rotate --if-exhausted` — an exhausted
# account is swapped for the next non-exhausted one before claude starts.
# Rotation self-guards: it never swaps while live sessions run in that dir
# (it warns and launches on the exhausted account instead).
#
# Conveniences:
#   claude-profile            status (no args); `use`/`account` without a name
#                             open an fzf picker when fzf is installed
#   claude-with NAME [args]   one-shot launch against profile NAME
#   claude-switch [NAME]      alias for `claude-profile use`
#   claude-default            one-shot launch with CLAUDE_CONFIG_DIR unset
#   \claude                   bypass the wrapper entirely
#
# Caffeinate: on macOS every launch through the wrapper runs under
# `caffeinate` so the Mac can't idle-sleep mid-session. Knobs in ./.env
# (see .env.example): CLAUDE_CAFFEINATE=0 opts out, CLAUDE_CAFFEINATE_FLAGS
# overrides the default `-ims`. No-op on Linux. Clamshell sleep is NOT
# prevented on Apple Silicon — keep the lid open.

typeset -g _CLAUDE_PROFILE_HOME="${${(%):-%x}:A:h}"

_claude_profile_py() {
  python3 "$_CLAUDE_PROFILE_HOME/claude-profile.py" "$@"
}

# ── launcher ────────────────────────────────────────────────────────────────

# Launch `command claude`, caffeinated on macOS unless opted out. Knobs live
# in ./.env (sourced function-locally); a same-invocation env assignment
# (CLAUDE_CAFFEINATE=0 claude) wins over .env.
_claude_exec() {
  if [[ "$OSTYPE" == darwin* ]]; then
    local _on="${CLAUDE_CAFFEINATE-}" _flags="${CLAUDE_CAFFEINATE_FLAGS-}"
    local CLAUDE_CAFFEINATE CLAUDE_CAFFEINATE_FLAGS
    source "$_CLAUDE_PROFILE_HOME/.env" 2>/dev/null
    _on="${_on:-${CLAUDE_CAFFEINATE:-1}}"
    _flags="${_flags:-${CLAUDE_CAFFEINATE_FLAGS:--ims}}"
    if [[ "$_on" != 0 ]]; then
      caffeinate ${=_flags} command claude "$@"
      return
    fi
  fi
  command claude "$@"
}

claude() {
  # Caller explicitly set CLAUDE_CONFIG_DIR → honor it as-is.
  if [[ -n "$CLAUDE_CONFIG_DIR" ]]; then
    _claude_exec "$@"
    return
  fi

  local line profile dir auto
  line="$(_claude_profile_py resolve 2>/dev/null)"
  if [[ -z "$line" ]]; then
    # No config → transparent passthrough.
    _claude_exec "$@"
    return
  fi
  profile="${line%%$'\t'*}"
  dir="${${line#*$'\t'}%%$'\t'*}"
  auto="${line##*$'\t'}"

  # Launch-time auto-rotation (never blocks or fails the launch).
  if [[ "$auto" == 1 ]]; then
    _claude_profile_py rotate --profile "$profile" --if-exhausted --quiet || true
  fi

  if [[ "$dir" == "$HOME/.claude" ]]; then
    # Claude's built-in default dir: do NOT touch CLAUDE_CONFIG_DIR.
    _claude_exec "$@"
  else
    CLAUDE_CONFIG_DIR="$dir" _claude_exec "$@"
  fi
}

# One-shot launch against a named profile (ignores toggle and path rules).
claude-with() {
  local dir
  dir="$(_claude_profile_py dir "$1")" || return 1
  shift
  if [[ "$dir" == "$HOME/.claude" ]]; then
    ( unset CLAUDE_CONFIG_DIR; _claude_exec "$@" )
  else
    CLAUDE_CONFIG_DIR="$dir" _claude_exec "$@"
  fi
}

# One-shot with CLAUDE_CONFIG_DIR unset — bit-for-bit no-wrapper behavior.
claude-default() { ( unset CLAUDE_CONFIG_DIR; _claude_exec "$@" ); }

# ── CLI (with fzf pickers) ──────────────────────────────────────────────────

claude-profile() {
  # fzf pickers when a selection command is given without a name
  if (( $+commands[fzf] )); then
    case "$1" in
      use)
        if [[ -z "$2" ]]; then
          local sel
          sel=$(_claude_profile_py list 2>/dev/null \
                | fzf --height=~10 --reverse --prompt='profile> ' \
                      --with-nth=1,2,3 --delimiter=$'\t' \
                      --header='select active profile (esc to cancel)') || return
          set -- use "${sel%%$'\t'*}"
        fi
        ;;
      account|auth|delete)
        if [[ -z "$2" || "$2" == --* ]]; then
          local _cmd="$1" sel
          sel=$(_claude_profile_py accounts 2>/dev/null \
                | fzf --height=~10 --reverse --prompt="${_cmd}> " \
                      --with-nth=1,2 --delimiter=$'\t' \
                      --header="select account for '${_cmd}' (esc to cancel)") || return
          set -- "$_cmd" "${sel%%$'\t'*}" "${@:2}"
        fi
        ;;
    esac
  fi
  _claude_profile_py "$@"
  local _rc=$?
  # A serial account swap changes the credential under the SAME config dir, so
  # claude-usage's dir-keyed cache keeps serving the previous account's numbers
  # until its TTL lapses. On a successful swap, force an immediate background
  # refresh so the statusline catches up within ~a second. (Only one account is
  # live per dir, so the dir-keyed cache is correct once refreshed — no need to
  # pay a per-tick .claude.json read to key the cache per account.)
  if (( _rc == 0 )) && [[ "$1" == (account|toggle|rotate) && "$*" != *--dry-run* ]] \
     && (( ${+functions[claude-usage]} )); then
    ( claude-usage --fresh >/dev/null 2>&1 & )
  fi
  return $_rc
}

# claude-switch survives as muscle-memory alias for the profile toggle.
claude-switch() { claude-profile use "$@" }

# ── claude-usage routing ────────────────────────────────────────────────────
# Point the standalone `claude-usage` CLI (and thus the statusline) at the
# profile `claude` would launch here, via CLAUDE_USAGE_DIR. Hooked on precmd
# so toggles made in other terminals are picked up at the next prompt, but
# python only runs when something relevant changed (cwd, config or state
# mtime) — otherwise the hook is two zstat calls. Only ever overwrites a
# value it set itself; a user-exported CLAUDE_USAGE_DIR is left alone.
zmodload -F zsh/stat b:zstat 2>/dev/null
autoload -Uz add-zsh-hook
typeset -g _claude_profile_route_sig _claude_profile_route_set
_claude_profile_route() {
  local cfg="${XDG_CONFIG_HOME:-$HOME/.config}/claude-profile/config.json"
  local st="${XDG_STATE_HOME:-$HOME/.local/state}/claude-profile/state.json"
  local m1 m2
  m1=$(zstat +mtime "$cfg" 2>/dev/null) || m1=0
  m2=$(zstat +mtime "$st" 2>/dev/null) || m2=0
  local sig="$PWD|$m1|$m2"
  [[ "$sig" == "$_claude_profile_route_sig" ]] && return
  _claude_profile_route_sig="$sig"

  local line dir=""
  line="$(_claude_profile_py resolve 2>/dev/null)"
  [[ -n "$line" ]] && dir="${${line#*$'\t'}%%$'\t'*}"
  if [[ -n "$dir" && "$dir" != "$HOME/.claude" ]]; then
    if [[ -z "$CLAUDE_USAGE_DIR" || "$CLAUDE_USAGE_DIR" == "$_claude_profile_route_set" ]]; then
      CLAUDE_USAGE_DIR="$dir"
      _claude_profile_route_set="$dir"
    fi
  else
    if [[ -n "$_claude_profile_route_set" && "$CLAUDE_USAGE_DIR" == "$_claude_profile_route_set" ]]; then
      unset CLAUDE_USAGE_DIR
    fi
    _claude_profile_route_set=
  fi
}
add-zsh-hook precmd _claude_profile_route
_claude_profile_route
