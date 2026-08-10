#!/usr/bin/env zsh
# ---------------------------------------------------------------------------
# tools/generate-readme-svg.zsh — regenerate the README SVGs.
#
# Renders the REAL claude-profile into SVG terminal windows: builds a hermetic
# sandbox (fake $HOME, a seeded config, seeded account snapshots, and a stub
# `security` on PATH so the macOS Keychain is never touched — no network, no
# credentials, no reading or writing your own config), runs the tool
# **unmodified**, and lays its output out on a terminal grid. The text in the
# images is therefore genuine output, not art. Only the window chrome around
# it — title bar, traffic lights — is drawn.
#
# Sibling of claude-usage's and claude-statusline's tools/generate-readme-svg.zsh,
# from which the grid/emitter core here is borrowed; keep the three roughly in
# sync. Two differences worth knowing:
#
#   * claude-profile emits no ANSI at all, so there is no SGR→<tspan fill>
#     conversion here. Rows are plain, with dim reserved for the chrome.
#   * the selector image shows the fzf branch, and fzf paints with cursor
#     addressing that an SVG grid cannot replay. So instead of screen-scraping
#     it, a stub `fzf` on PATH captures exactly the row list the real picker
#     hands it; those rows are genuine, and fzf's own furniture (pointer,
#     match counter, prompt) is drawn around them.
#
# Usage:  zsh tools/generate-readme-svg.zsh
#           → assets/{status,selector}-<hash>.svg, older ones deleted, README
#             <img> references rewritten (the random hash busts GitHub's camo
#             image cache). Commit all three files.
#         zsh tools/generate-readme-svg.zsh STATUS.svg SELECTOR.svg
#           → fixed paths, README untouched.
#
# Regenerate whenever the status layout, the selector, or these demo values
# change. Token horizons ("27d") are seeded stable; the absolute dates they
# render to track the day you run it — fine for a demo.
# ---------------------------------------------------------------------------
emulate -L zsh
setopt extended_glob

here=${0:a:h}
root=${here:h}

tmp=$(cd "$(mktemp -d)" && pwd -P)   # physical: see the path-rule note below
trap 'rm -rf "$tmp"' EXIT

# ---- hermetic sandbox ------------------------------------------------------
fakehome="$tmp/home"
mkdir -p "$fakehome/.claude" "$fakehome/.claude-personal" "$fakehome/code-private"

export HOME="$fakehome"
export XDG_STATE_HOME="$tmp/state"
export XDG_CONFIG_HOME="$fakehome/.config"
cfgfile="$XDG_CONFIG_HOME/claude-profile/config.json"
mkdir -p "${cfgfile:h}"
export USER=demo
unset CLAUDE_USAGE_DIR CLAUDE_CONFIG_DIR    # ambient values would leak in

cat > "$cfgfile" <<'JSON'
{
  "default_profile": "work",
  "profiles": {
    "personal": {
      "dir": "~/.claude-personal",
      "display": "Personal",
      "paths": ["~/code-private", "~/.zsh"],
      "accounts": ["max20x", "max5x"],
      "auto": true
    },
    "work": { "dir": "~/.claude" }
  }
}
JSON

# ---- seeded credentials ----------------------------------------------------
# A stub `security` on PATH stands in for the Keychain: claude-profile shells
# out to it (subprocess → PATH lookup), so the real login keychain is never
# opened, and nothing here needs an unlock or leaves a trace.
seeddir="$tmp/keychain"; mkdir -p "$seeddir" "$tmp/bin"
now=$(date +%s)
blob() {  # blob <refresh-days-left> → a credential shaped like Claude Code's
  local ms=$(( (now + $1 * 86400) * 1000 ))
  print -r -- "{\"claudeAiOauth\":{\"accessToken\":\"a\",\"refreshToken\":\"r\",\"refreshTokenExpiresAt\":${ms},\"expiresAt\":$(( (now + 3*3600 + 1800) * 1000 ))}}"
}
blob 27 > "$seeddir/live.json"                         # whatever dir is asked for
blob 27 > "$seeddir/claude-profile-parked-max5x.json"  # the parked slot

cat > "$tmp/bin/security" <<'STUB'
#!/bin/sh
# Stub Keychain: `find-generic-password -s <service> … -w` → seeded blob.
# Live services carry a sha256-of-the-dir suffix, so match on shape rather
# than on a name this script would otherwise have to recompute.
svc=""; while [ $# -gt 0 ]; do case "$1" in -s) svc="$2"; shift 2 ;; *) shift ;; esac; done
case "$svc" in
  "Claude Code-credentials"*) exec cat "$SEEDDIR/live.json" ;;
  claude-profile-parked-*)    [ -f "$SEEDDIR/$svc.json" ] && exec cat "$SEEDDIR/$svc.json" ;;
esac
echo "security: SecKeychainSearchCopyNext: The specified item could not be found." >&2
exit 44
STUB
chmod +x "$tmp/bin/security"
export SEEDDIR="$seeddir"

# Stub `fzf` for the selector image: it records exactly the row list the real
# picker hands it, then exits 130 (fzf's "cancelled") so nothing is selected.
# Written now, before PATH is exported: zsh hashes the contents of every PATH
# directory, so a binary that appears in an already-hashed dir is invisible
# until a `rehash` — creating it later silently ran the operator's real fzf.
cat > "$tmp/bin/fzf" <<'STUB'
#!/bin/sh
# Record the argv too: the prompt and header strings belong to the picker, so
# reading them back here keeps the drawn chrome from drifting away from what
# the tool actually passes (it already had: the header was stale by a word).
printf '%s\n' "$@" > "$FZF_ARGS"
cat > "$FZF_CAPTURE"
exit 130
STUB
chmod +x "$tmp/bin/fzf"
export FZF_CAPTURE="$tmp/fzf-rows" FZF_ARGS="$tmp/fzf-args"

# ---- seeded account metadata ----------------------------------------------
# Non-secret snapshots: what `status` reads to show an account's email and the
# date its credential was parked.
acctdir="$XDG_STATE_HOME/claude-profile/accounts"; mkdir -p "$acctdir"
today=$(date +%Y-%m-%d)
snap() {  # snap <name> <uuid> <email>
  print -r -- "{\"accountUuid\":\"$2\",\"oauthAccount\":{\"accountUuid\":\"$2\",\"emailAddress\":\"$3\"},\"saved_at\":\"$today\"}" \
    > "$acctdir/$1.json"
}
snap max20x 11111111-1111-1111-1111-111111111111 'you@example.com'
snap max5x  22222222-2222-2222-2222-222222222222 'you.personal@example.com'

# Which account is *live* in a dir is decided by the accountUuid in that dir's
# .claude.json, matched against the snapshots above — so seed one for personal.
print -r -- '{"oauthAccount":{"accountUuid":"11111111-1111-1111-1111-111111111111","emailAddress":"you@example.com"}}' \
  > "$fakehome/.claude-personal/.claude.json"

export PATH="$tmp/bin:$PATH"
rehash
cp="python3 $root/claude-profile.py"

# ---- capture the real output ----------------------------------------------
# Run from a path rule directory, so the status screen shows "active:path" —
# the most interesting of the three resolution reasons.
mkdir -p "$fakehome/code-private/app"
status_out=$(cd "$fakehome/code-private/app" && ${=cp} status 2>&1)
# The tool prints its config path verbatim while tilde-contracting profile
# dirs; contract the sandbox home the same way so the demo reads like a real
# machine rather than a temp dir. Display-only, and nothing else is touched.
status_out=${status_out//$fakehome/\~}

# The selector: the stub `fzf` (installed above) recorded exactly the row list
# the real picker feeds it, then exited 130 — fzf's "cancelled" — so nothing is
# actually selected and no terminal is needed.
# Deliberately NOT an `&&` chain: sourcing the wrapper ends in the precmd
# routing hook, which returns non-zero whenever it decides to leave an
# already-exported CLAUDE_USAGE_DIR alone — which would silently skip the
# capture in any shell that has one (i.e. the author's).
( cd "$fakehome/code-private/app"
  source "$root/claude-profile.zsh" >/dev/null 2>&1
  claude-profile use </dev/null >/dev/null 2>&1 )
typeset -a sel_rows
sel_rows=("${(@f)$(<"$FZF_CAPTURE")}")
sel_rows=("${(@)sel_rows%%$'\t'*}")     # drop the hidden key column fzf hides

[[ -n $status_out && ${#sel_rows} -gt 0 ]] || {
  print -u2 "generate-readme-svg: sandbox produced no output — aborting"; exit 1
}

# ---- SVG ------------------------------------------------------------------
# Catppuccin Mocha chrome, matching the sibling generators.
BG='#1e1e2e'  BAR='#181825'  FG='#cdd6f4'  DIMC='#9399b2'
DOT1='#f38ba8' DOT2='#f9e2af' DOT3='#a6e3a1'
# fzf's own furniture, in its default roles: blue prompt, red pointer, yellow
# match counter, and a lifted background on the current line.
ACC='#89dceb'   # prompt string
PTR='#f38ba8'   # current-line marker bar
INFO='#f9e2af'  # match counter
HDR='#94e2d5'   # header
RULE='#45475a'  # the solid rule fzf draws on the info line
GUT='#313244'   # the gutter bar every list row carries
HL='#313244'    # current-line background
FONT="'Cascadia Code','Fira Code',SFMono-Regular,Consolas,Menlo,monospace"
integer FS=13 LH=20 TH=30 PX=20 PY=14 SLACK=24 MINCOLS=52

# Terminal grid: every character is pinned to its own cell, so a row occupies
# exactly (columns × cw) whichever font the renderer falls back to — which is
# what keeps the columns aligned in a browser that has none of these fonts.
typeset -a XCOL
local -F cw=7.85
integer k; local v
for (( k = 0; k <= 400; k++ )); do printf -v v '%.2f' $(( PX + k * cw )); XCOL[k+1]=$v; done
xrun() { print -rn -- "${(j: :)XCOL[$1+1,$1+$2]}" }
xesc() { local s=$1; s=${s//\&/&amp;}; s=${s//</&lt;}; s=${s//>/&gt;}; print -rn -- "$s" }

# emit_svg <lines-array-name> <out-file> <title> <aria>
# Each entry: TYPE|content — b=blank, t=plain, c=dim, and the fzf frame:
# q=prompt (+cursor), i=match counter + separator rule, h=header, n=row,
# p=current row.
# Everything in the fzf frame except q sits at column 2, as fzf indents it.
emit_svg() {
  local -a _lines=("${(@P)1}")
  local out=$2 title=$3 aria=$4 entry typ body
  integer maxcols=0 n
  for entry in "${_lines[@]}"; do
    body=${entry#*|}; n=${#body}
    [[ ${entry%%\|*} == (p|n|h|i) ]] && (( n += 2 ))
    (( n > maxcols )) && maxcols=$n
  done
  (( maxcols < MINCOLS )) && maxcols=$MINCOLS
  integer W=$(( PX * 2 + maxcols * cw + 6 + SLACK ))
  integer H=$(( TH + PY + ${#_lines} * LH + PY ))
  {
    print -r -- "<svg xmlns=\"http://www.w3.org/2000/svg\" width=\"$W\" height=\"$H\" viewBox=\"0 0 $W $H\" role=\"img\" aria-label=\"$(xesc "$aria")\">"
    print -r -- "  <rect width=\"$W\" height=\"$H\" rx=\"10\" fill=\"$BG\"/>"
    print -r -- "  <rect width=\"$W\" height=\"$TH\" rx=\"10\" fill=\"$BAR\"/>"
    print -r -- "  <rect y=\"$(( TH - 6 ))\" width=\"$W\" height=\"6\" fill=\"$BAR\"/>"
    print -r -- "  <circle cx=\"18\" cy=\"$(( TH / 2 ))\" r=\"5.5\" fill=\"$DOT1\"/><circle cx=\"36\" cy=\"$(( TH / 2 ))\" r=\"5.5\" fill=\"$DOT2\"/><circle cx=\"54\" cy=\"$(( TH / 2 ))\" r=\"5.5\" fill=\"$DOT3\"/>"
    print -r -- "  <text x=\"$(( W / 2 ))\" y=\"$(( TH / 2 + 5 ))\" text-anchor=\"middle\" font-family=\"$FONT\" font-size=\"12\" fill=\"$DIMC\">$(xesc "$title")</text>"
    integer i=0 y
    local T
    for entry in "${_lines[@]}"; do
      typ=${entry%%\|*}; body=${entry#*|}
      y=$(( TH + PY + i * LH + FS ))
      T="  <text x=\"$PX\" y=\"$y\" font-family=\"$FONT\" font-size=\"$FS\" xml:space=\"preserve\""
      case $typ in
        b) ;;
        t) print -r -- "$T fill=\"$FG\"><tspan x=\"$(xrun 0 ${#body})\">$(xesc "$body")</tspan></text>" ;;
        c) print -r -- "$T fill=\"$DIMC\"><tspan x=\"$(xrun 0 ${#body})\">$(xesc "$body")</tspan></text>" ;;
        n) # every list row carries the gutter bar; only the current one is pink
           print -r -- "  <rect x=\"$PX\" y=\"$(( y - FS - 3 ))\" width=\"3\" height=\"$LH\" fill=\"$GUT\"/>"
           print -r -- "$T fill=\"$FG\"><tspan x=\"$(xrun 2 ${#body})\">$(xesc "$body")</tspan></text>" ;;
        i) # fzf's --separator draws the rule on the info line itself, from just
           # after the match counter out to the right edge.
           integer rx=$(( PX + (${#body} + 3) * cw ))
           print -r -- "  <rect x=\"$rx\" y=\"$(( y - FS / 2 + 1 ))\" width=\"$(( W - rx - PX ))\" height=\"1\" fill=\"$RULE\"/>"
           print -r -- "$T fill=\"$INFO\"><tspan x=\"$(xrun 2 ${#body})\">$(xesc "$body")</tspan></text>" ;;
        h) print -r -- "$T fill=\"$HDR\"><tspan x=\"$(xrun 2 ${#body})\">$(xesc "$body")</tspan></text>" ;;
        p) # the current line: lifted background and a marker bar in the gutter
           print -r -- "  <rect x=\"0\" y=\"$(( y - FS - 3 ))\" width=\"$W\" height=\"$LH\" fill=\"$HL\"/>"
           print -r -- "  <rect x=\"$PX\" y=\"$(( y - FS - 3 ))\" width=\"3\" height=\"$LH\" fill=\"$PTR\"/>"
           print -r -- "$T fill=\"$FG\"><tspan x=\"$(xrun 2 ${#body})\">$(xesc "$body")</tspan></text>" ;;
        q) # the prompt string sits at column 0, with the block cursor after it
           print -r -- "  <rect x=\"$XCOL[${#body}+1]\" y=\"$(( y - FS + 1 ))\" width=\"8\" height=\"$(( FS + 3 ))\" fill=\"$FG\" opacity=\"0.75\"/>"
           print -r -- "$T fill=\"$ACC\"><tspan x=\"$(xrun 0 ${#body})\">$(xesc "$body")</tspan></text>" ;;
      esac
      (( i++ ))
    done
    print -r -- "</svg>"
  } > "$out"
}

# ---- compose ---------------------------------------------------------------
typeset -a status_lines
status_lines=('t|% claude-profile' 'b|')
local ln
for ln in "${(@f)status_out}"; do
  [[ -z $ln ]] && { status_lines+=('b|'); continue }
  status_lines+=("t|$ln")
done

# Prompt and header are read back from the recorded argv, not retyped here.
typeset -a fzf_argv
fzf_argv=("${(@f)$(<"$FZF_ARGS")}")
local fzf_prompt='' fzf_header='' a
for a in "${fzf_argv[@]}"; do
  case $a in
    --prompt=*) fzf_prompt=${a#--prompt=} ;;
    --header=*) fzf_header=${a#--header=} ;;
  esac
done
fzf_prompt=${fzf_prompt%% #}          # fzf renders the trailing pad as the gap

typeset -a selector_lines
selector_lines=('t|% claude-profile use' 'b|')
integer nrows=${#sel_rows}
selector_lines+=("q|$fzf_prompt")
selector_lines+=("i|${nrows}/${nrows}")
selector_lines+=("h|$fzf_header")
# The pointer rests on the row you would actually be switching *to* — the
# first one the porcelain does not mark active.
integer cur=0 j=1
for ln in "${sel_rows[@]}"; do
  [[ $ln != *active* && $cur == 0 ]] && cur=$j
  (( j++ ))
done
(( cur )) || cur=1
j=1
for ln in "${sel_rows[@]}"; do
  (( j == cur )) && selector_lines+=("p|$ln") || selector_lines+=("n|$ln")
  (( j++ ))
done

# ---- write -----------------------------------------------------------------
STATUS_ARIA='claude-profile status: two profiles, the active one marked, its two accounts with the live one tagged ACTIVE and the parked one showing its token horizon'
SEL_ARIA='claude-profile use with no name: an fzf picker listing both profiles, the active one marked, with a prompt to filter'

if [[ -n ${1:-} ]]; then
  emit_svg status_lines   "$1" 'claude-profile' "$STATUS_ARIA"; print "wrote $1"
  [[ -n ${2:-} ]] && { emit_svg selector_lines "$2" 'claude-profile' "$SEL_ARIA"; print "wrote $2" }
else
  mkdir -p "$root/assets"
  local old
  for old in "$root"/assets/status-*.svg(N) "$root"/assets/selector-*.svg(N); do rm -f "$old"; done
  local hash; hash=$(xxd -l3 -p /dev/urandom)
  emit_svg status_lines   "$root/assets/status-${hash}.svg"   'claude-profile' "$STATUS_ARIA"
  emit_svg selector_lines "$root/assets/selector-${hash}.svg" 'claude-profile' "$SEL_ARIA"
  sed -i.bak \
    -e "s|assets/status-[^)\"]*\.svg|assets/status-${hash}.svg|" \
    -e "s|assets/selector-[^)\"]*\.svg|assets/selector-${hash}.svg|" \
    "$root/README.md" && rm -f "$root/README.md.bak"
  print "wrote assets/{status,selector}-${hash}.svg and updated README.md"
fi
