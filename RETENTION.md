# Transcript retention across profiles — a gap claude-profile creates and should close

**Status:** open, unsolved. Written up 2026-10-09 from measurements on the
personal MacBook. No code written yet.

## The problem in one sentence

Claude Code's transcript retention is **per-config-dir and launch-triggered**, so
a profile that stops being selected stops being cleaned — and the longer you
leave a profile alone, the longer its transcripts persist.

## Mechanism

Claude Code deletes session transcripts older than `cleanupPeriodDays` (default
**30**, min 1, `0` fails validation) as a side effect of starting a session,
against the config dir it was launched with. There is no `claude purge`
subcommand.

It is **throttled by a per-config-dir marker**, not run on every launch:

```
~/.claude/.last-cleanup              2026-08-24T07:54:27.711Z
~/.claude-personal/.last-cleanup     2026-10-08T10:31:21.545Z
```

A 24-byte file holding an ISO-8601 UTC instant, one per config dir. This is the
hard evidence that retention is **per-profile**: each dir tracks its own last
sweep, and nothing reconciles across dirs.

Those two values are the whole bug in two lines. `personal` was swept yesterday.
`work` was swept **2026-08-24** — four days before the last write to any of its
projects. It has not been swept in the 46 days since, because no launch has
resolved to it.

`claude-profile` resolves a config dir per launch — explicit `CLAUDE_CONFIG_DIR`
> cwd path rule > toggle > `default_profile`. That is the whole point of the
tool, and it is also what breaks retention: **Claude Code only ever sees one dir
per launch, and sweeps only that one.** Any profile not selected for longer than
the retention period keeps everything, indefinitely.

The failure is silent and inverted: the profiles you use least — the ones most
likely to hold material you have forgotten about — are the ones that retain it
longest.

## Measured evidence (2026-10-09, personal MacBook)

Config: `personal` → `~/.claude-personal` (`paths: [~/code-private, ~/.zsh]`,
auto, 2 accounts); `work` → `~/.claude` (fallback, `default_profile`, passthrough
with `CLAUDE_CONFIG_DIR` unset).

```
.claude            >30d:  61 transcripts  25.7 MB    <=30d:  0
.claude-personal   >30d:   0 transcripts   0.0 MB    <=30d:  1
```

Every transcript in `~/.claude` is older than the 30-day default and **none have
been deleted**. Last write there was 42 days ago; all work since has been in
`~/code-private` and `~/.zsh`, which the path rules route to `personal`. So no
`work` launch has occurred to trigger its cleanup. `~/.claude/file-history/`
shows the same (106 files, oldest 2026-08-10, unpruned).

Cause is inferred from the measurement plus documented behaviour, not from a
changelog — but a 61/0 split is hard to explain otherwise.

## What I could not verify (do not assume these)

Two attempts to observe a sweep in action both produced null results, so the
following are **open, not established**:

- **Whether a sweep covers the whole config dir or only the launched cwd's
  project dir.** The marker living at config-dir level hints at dir-wide, but
  that is inference, not observation. A sweeper must not be designed around the
  assumption until someone confirms it.
- **The throttle interval.** A marker exists; its period is unknown.

### What was tried, so it is not repeated

1. Fresh throwaway config dir, two synthetic 60-day-old transcripts in unrelated
   project dirs, launched with `CLAUDE_CONFIG_DIR` pointed at it. Aborted at
   `Not logged in` — credentials live in the macOS Keychain, so a scratch config
   dir cannot authenticate. It still created a project dir for the cwd before
   exiting.
2. Real authenticated `~/.claude-personal`, three synthetic 60-day-old files —
   one in the launched cwd's own project dir, two in unrelated dirs — then
   `claude -p`. **Nothing deleted.** Repeated with `.last-cleanup` backdated to
   `2026-01-01` to defeat the throttle: still nothing deleted, **and the marker
   was not rewritten**, which means the sweep never ran at all.

The most likely explanation for (2) is that **`-p` print mode skips the
maintenance step**. A conclusive test probably needs an interactive TUI launch,
and/or synthetic files that carry real session metadata — the stub files used
here held a single JSON line with no `timestamp` or `sessionId`, so an
age check reading metadata rather than mtime would skip them.

(Test artifacts were removed and `.last-cleanup` restored to its original value.)

## Why this belongs to claude-profile and not to Claude Code

`claude-profile` holds the only complete list of config dirs, in
`~/.config/claude-profile/config.json` → `profiles.<name>.dir`. Claude Code
cannot clean a dir it is never started with, and has no way to learn such a dir
exists. The component that fragmented retention is the component that has to
reunify it.

## Two distinct jobs, worth separating

**1. Stale transcripts in dormant profiles.** Apply the retention policy across
*every* profile, not just the one being launched. The policy value should come
from the same place Claude Code reads it — `cleanupPeriodDays` in that profile's
`settings.json`, falling back to 30 — so the sweep never disagrees with what
Claude Code would have done itself.

**2. Orphaned project dirs.** `projects/<encoded-cwd>/` whose cwd no longer
exists: deleted worktrees, renamed repos, scratch dirs. Pure dead weight, and
unlike (1) there is no retention window to respect — if the cwd is gone, the
transcripts are unreachable by `--resume` from that path.

### Implementation trap: the encoded dir name is lossy

`projects/` subdirectory names are the cwd with separators flattened to `-`.
**`/`, `-` and `_` all become `-`**, so decoding a name back to a path is
ambiguous and must not be attempted. Illustrative case (the real one observed
had the same shape):

```
dir: -Users-jane-doe-code-acme-ABC-1234-bulk-actions
cwd: /Users/jane.doe/code/acme_ABC-1234-bulk-actions
```

`code-private` vs `code/private` vs `code_private` collapse identically.

**Always read the `cwd` field from inside the JSONL** (first entry carrying it).
That is authoritative and unambiguous. A dir with no readable transcript has no
recoverable cwd — treat as unknown, never guess, and only offer it for deletion
on age.

## Third job, arguably the most valuable: triage, not just deletion

Deleting by age is blunt. The question actually worth answering is *which
transcripts contain credentials*. Transcripts accumulate bearer tokens, cookies,
JWTs, `*_token` assignments, API keys — that is Anthropic's own stated rationale
for the 30-day default.

A scan mode that reports which sessions contain credential-shaped strings turns
this from a disk-space chore into a security control, and lets you purge the
three sessions that matter instead of a year of history. Regex set that worked
well in practice (two layers — exact known secrets, plus *shapes*, so an
accidental header dump is caught even when the value was never registered):

```
(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}
(?i)\b(set-)?cookie\s*[:=]\s*[^\s,;]+
(?i)\b(authorization|x-api-key|x-auth-token)\s*[:=]\s*[^\s,;]+
(?i)\b(access|refresh|id)_token\W{0,6}[A-Za-z0-9._~+/=-]{8,}
\beyJ[A-Za-z0-9._-]{10,}                     # JWT-ish
```

Caveat learned the hard way: a non-recursive redactor misses **nested** JSON
(e.g. identity claims inside an `idTokenClaims` object). Recurse, or you will
report a transcript as clean when it still carries personal identity claims.
Note also that token-shaped secrets expire on their own; identity claims do
not, so age is not a proxy for harm.

## Proposed surface

A `sweep` subcommand, dry-run by default, consistent with the repo's existing
"refuse under live sessions" posture:

```
claude-profile sweep                        # report: per profile, stale / orphaned / empty, sizes
claude-profile sweep --secrets              # which transcripts contain credential-shaped strings
claude-profile sweep --orphans --apply      # delete only transcripts whose cwd is gone
claude-profile sweep --stale --apply        # apply each profile's own cleanupPeriodDays
claude-profile sweep --profile work --apply
```

Design constraints:

- **Reuse `live_sessions(d)`.** A transcript belonging to a running session must
  never be deleted, exactly as `ensure_swappable()` guards credential swaps. The
  same invariant, a different resource.
- **Dry-run default, explicit `--apply`.** Same shape as `sync-identities`
  elsewhere in the fleet: the diff is the thing to confirm.
- **Side stores too**, and they need their own rules rather than being swept
  along: `projects/<dir>/tool-results/` (fetched binaries, persisted large tool
  output), `file-history/`, `paste-cache/`, `shell-snapshots/`. These are not
  transcripts and may not share the transcript lifecycle.
- **Per-profile policy, never a single global number.** Read each profile's
  `settings.json`; honour managed settings if present, since those win over user
  settings and an operator-set retention must not be silently widened.
- **Report bytes reclaimable before touching anything.** The number is what makes
  the case for running it.

## Cheap interim workaround

Trigger the dormant profile's own startup cleanup by launching it once:

```zsh
CLAUDE_CONFIG_DIR=~/.claude claude -p 'exit' >/dev/null
```

Non-destructive, uses Claude Code's own policy, no new code. Worth considering as
a stopgap *inside* the wrapper too: on a profile switch, fire a no-op launch
against the profile being switched **away** from. That would close most of the
gap without implementing a sweeper at all — though it inherits whatever Claude
Code's cleanup does and cannot address orphans or offer triage.

## Open questions

- Should `sweep --stale` run automatically on profile switch, or stay manual?
  Automatic matches the 30-day intent; manual avoids surprising deletions of
  history someone was deliberately keeping. Possibly `auto: true` per profile,
  mirroring the existing rotate knob.
- Does `cleanupPeriodDays` cover the side stores, or only `projects/*.jsonl`?
  Unverified. `file-history/` being 2 months deep in `~/.claude` suggests it is
  either not covered or also gated behind a launch.
- Does a sweep remove the now-empty `projects/<dir>/` shells, or only the
  `.jsonl` files inside them? Both config dirs carry empty project dirs aged
  56–142 days, which suggests the directories are left behind.
- Can `.last-cleanup` be used as the integration point — i.e. should `sweep`
  write it after running, so Claude Code and claude-profile share one notion of
  when a dir was last swept? Attractive, but it means writing a file owned by
  another tool; a format change would break us silently.
- `ccfind` searches both profiles' transcripts. Deleting history removes results
  it would otherwise find — worth a note in its docs, and an argument for
  `--secrets` triage over blanket age deletion.
- Is there value in an *export before delete* path (sanitized summary retained,
  raw transcript dropped)? That is the pattern that actually fits: keep the
  findings, discard the container.

## Provenance

Surfaced 2026-10-09 while winding down an abandoned project. Its repo and working
directory were deleted, but its transcripts would have outlived both, in a
profile that had not been cleaned in six weeks. That asymmetry is the whole
problem.
