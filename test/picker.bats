#!/usr/bin/env bats
# The selector screen behind the name-less `use`/`account`/`auth`/`delete`
# forms: fzf when installed, a numbered prompt otherwise. Both branches must
# agree on one contract — the chosen key, and nothing else, on stdout.

load helpers

setup() { cp_setup; }

ROWS_PROFILES='personal\t~/.claude-personal\tactive\nwork\t~/.claude\t\n'
ROWS_ACCOUNTS='max20x\tactive\tpersonal\nmax5x\tparked\tpersonal\n'

# --- fzf branch -----------------------------------------------------------

@test "fzf: a matching query resolves to that row's key" {
  command -v fzf >/dev/null || skip "fzf not installed"
  pick_fzf "$ROWS_PROFILES" work
  [ "$status" -eq 0 ]
  [[ "$output" == *"rc=0 sel=[work]"* ]]
}

@test "fzf: only the key reaches stdout, never the display columns" {
  command -v fzf >/dev/null || skip "fzf not installed"
  pick_fzf "$ROWS_PROFILES" personal
  [[ "$output" == *"sel=[personal]"* ]]
  [[ "$output" != *"sel=[personal "* ]]      # no padding, no dir column
}

@test "fzf: a key containing spaces round-trips intact" {
  command -v fzf >/dev/null || skip "fzf not installed"
  # NB: the query itself must be space-free — FZF_DEFAULT_OPTS is split on
  # whitespace by fzf, so "my prof" would arrive as two arguments.
  pick_fzf 'my profile\t~/.claude-x\t\nwork\t~/.claude\t\n' my
  [[ "$output" == *"sel=[my profile]"* ]]
}

@test "fzf: no match cancels (rc 1, empty selection)" {
  command -v fzf >/dev/null || skip "fzf not installed"
  pick_fzf "$ROWS_PROFILES" zzzznope
  [[ "$output" == *"rc=1 sel=[]"* ]]
}

# --- numbered fallback ----------------------------------------------------

@test "menu: renders one numbered row per entry when fzf is absent" {
  pick_menu "$ROWS_PROFILES"
  [[ "$output" == *"HEADER"* ]]
  [[ "$output" == *"1) personal"* ]]
  [[ "$output" == *"2) work"* ]]
}

@test "menu: a number selects that row's key" {
  pick_menu "$ROWS_PROFILES" 2
  [[ "$output" == *"rc=0 sel=[work]"* ]]
}

@test "menu: columns are aligned and the empty 'active' column adds no trailing space" {
  pick_menu "$ROWS_PROFILES" 1
  # 'personal' is the widest key, so 'work' is padded out to match it.
  [[ "$output" == *"1) personal  ~/.claude-personal  active"* ]]
  [[ "$output" == *"2) work      ~/.claude"* ]]
  # …and the row with a blank third column ends right after the dir.
  [[ "$output" != *"~/.claude "* ]]
}

@test "menu: accounts porcelain renders its three columns too" {
  pick_menu "$ROWS_ACCOUNTS" 2
  [[ "$output" == *"1) max20x  active  personal"* ]]
  [[ "$output" == *"2) max5x   parked  personal"* ]]
  [[ "$output" == *"rc=0 sel=[max5x]"* ]]
}

@test "menu: empty answer cancels (rc 1)" {
  pick_menu "$ROWS_PROFILES" ""
  [[ "$output" == *"rc=1 sel=[]"* ]]
}

@test "menu: q cancels (rc 1)" {
  pick_menu "$ROWS_PROFILES" q
  [[ "$output" == *"rc=1 sel=[]"* ]]
}

@test "menu: a non-numeric answer re-prompts rather than selecting" {
  pick_menu "$ROWS_PROFILES" x 2
  [[ "$output" == *"enter 1-2"* ]]
  [[ "$output" == *"rc=0 sel=[work]"* ]]
}

@test "menu: an out-of-range number re-prompts" {
  pick_menu "$ROWS_PROFILES" 9 0 1
  [[ "$output" == *"enter 1-2"* ]]
  [[ "$output" == *"rc=0 sel=[personal]"* ]]
}

@test "menu: a key containing spaces round-trips intact" {
  pick_menu 'my profile\t~/.claude-x\t\nwork\t~/.claude\t\n' 1
  [[ "$output" == *"rc=0 sel=[my profile]"* ]]
}

@test "menu: the whole screen goes to stderr, so stdout carries only the key" {
  # stdout of the picker is captured into sel=[…]; if any menu line leaked
  # into it, sel would contain the header or a numbered row.
  pick_menu "$ROWS_PROFILES" 1
  [[ "$output" == *"sel=[personal]"* ]]
  [[ "$output" != *"sel=[HEADER"* ]]
}

# --- edges ----------------------------------------------------------------

@test "no rows to choose from cancels (rc 1) without prompting" {
  pick_menu ''
  [[ "$output" == *"rc=1 sel=[]"* ]]
  [[ "$output" != *"HEADER"* ]]
}

@test "without a terminal: a clear message, rc 1, and no zsh error leaks" {
  # No pty here — plain pipes, as in a script or a hook.
  run env HOME="$FIXHOME" zsh -c 'source "$1" >/dev/null 2>&1; PATH=""
    sel=$(printf "personal\t~/.claude-personal\tactive\n" \
          | _claude_profile_pick "pick> " "HEADER")
    echo "rc=$? sel=[$sel]"' _ "$CP_ZSH"
  [[ "$output" == *"no terminal for the selector"* ]]
  [[ "$output" == *"rc=1 sel=[]"* ]]
  [[ "$output" != *"device not configured"* ]]   # the /dev/tty open must not leak
}
