#!/usr/bin/env python3
"""Unit tests for claude-profile.

Pure stdlib `unittest`. No network and no real Keychain — both are stubbed, so
this runs identically on macOS and Linux and in CI. The Linux (file) credential
backend is exercised directly by forcing IS_MACOS=False.

Run:  python3 test/test_claude_profile.py         (or: python3 -m unittest -v)
"""
import contextlib
import importlib.util
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@contextlib.contextmanager
def muted():
    """Swallow a command's normal stdout/stderr chatter during a test."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        yield buf


def load_module():
    spec = importlib.util.spec_from_file_location(
        "claude_profile", os.path.join(ROOT, "claude-profile.py")
    )
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


cp = load_module()


def ns(**kw):
    return types.SimpleNamespace(**kw)


def oauth_blob(days_left=30, refresh="r", access="a"):
    """A credential blob with a refresh token expiring `days_left` from now."""
    exp = int((cp.time.time() + days_left * 86400) * 1000)
    o = {"accessToken": access, "refreshToken": refresh, "refreshTokenExpiresAt": exp}
    return json.dumps({"claudeAiOauth": o})


def expired_blob(refresh="r"):
    """A blob whose ACCESS token has already expired (refresh token still good) —
    the parked-account state account_usage_raw refreshes past."""
    now = int(cp.time.time() * 1000)
    o = {"accessToken": "old", "expiresAt": now - 1000,
         "refreshToken": refresh, "refreshTokenExpiresAt": now + 30 * 86400 * 1000}
    return json.dumps({"claudeAiOauth": o})


# ── Linux (file) credential backend ─────────────────────────────────────────
class LinuxCredentialStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._mac, self._pd = cp.IS_MACOS, cp.PARKED_DIR
        cp.IS_MACOS = False
        cp.PARKED_DIR = os.path.join(self.tmp, "parked")

    def tearDown(self):
        cp.IS_MACOS, cp.PARKED_DIR = self._mac, self._pd
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_parked_roundtrip_preserves_quotes_and_backslashes(self):
        blob = json.dumps({"claudeAiOauth": {"accessToken": 'a"b\\c/d', "refreshToken": "r"}})
        cp.write_parked_cred("acc", blob)
        self.assertEqual(cp.read_parked_cred("acc"), blob)

    def test_parked_file_is_0600(self):
        cp.write_parked_cred("acc", "{}")
        mode = stat.S_IMODE(os.stat(cp.parked_cred_path("acc")).st_mode)
        self.assertEqual(mode, 0o600)

    def test_parked_names_and_delete(self):
        cp.write_parked_cred("a", "{}")
        cp.write_parked_cred("b", "{}")
        self.assertEqual(cp.parked_names(), {"a", "b"})
        cp.delete_parked_cred("a")
        self.assertEqual(cp.parked_names(), {"b"})
        self.assertIsNone(cp.read_parked_cred("a"))

    def test_live_cred_custom_dir_roundtrip_and_perms(self):
        d = os.path.join(self.tmp, "cfg")
        os.makedirs(d)
        cp.write_live_cred(d, "blob")
        p = os.path.join(d, ".credentials.json")
        self.assertTrue(os.path.exists(p))
        self.assertEqual(cp.read_live_cred(d), "blob")
        self.assertEqual(stat.S_IMODE(os.stat(p).st_mode), 0o600)

    def test_missing_returns_none(self):
        self.assertIsNone(cp.read_parked_cred("nope"))
        self.assertIsNone(cp.read_live_cred(os.path.join(self.tmp, "nope")))

    def test_delete_missing_is_noop(self):
        cp.delete_parked_cred("nope")  # must not raise


# ── path resolution ─────────────────────────────────────────────────────────
class ClaudeJsonPath(unittest.TestCase):
    def test_default_dir_uses_home(self):
        self.assertEqual(cp.claude_json_path("~/.claude"), os.path.join(cp.HOME, ".claude.json"))

    def test_custom_dir_is_inside(self):
        self.assertEqual(cp.claude_json_path("/tmp/foo"), "/tmp/foo/.claude.json")

    def test_live_cred_path_default(self):
        self.assertTrue(cp.live_cred_path("~/.claude").endswith("/.claude/.credentials.json"))


# ── extra-usage credits + exhaustion ────────────────────────────────────────
class CreditsAvailable(unittest.TestCase):
    def eu(self, **kw):
        return {"extra_usage": {"is_enabled": True, "spend_limit_reached": False, **kw}}

    def test_absent_extra_usage(self):
        self.assertFalse(cp.credits_available({}))

    def test_disabled(self):
        self.assertFalse(cp.credits_available({"extra_usage": {"is_enabled": False}}))

    def test_has_room(self):
        self.assertTrue(cp.credits_available(self.eu(utilization=40)))

    def test_below_near_cap(self):
        self.assertTrue(cp.credits_available(self.eu(utilization=98)))

    def test_at_near_cap_threshold(self):
        self.assertFalse(cp.credits_available(self.eu(utilization=99)))

    def test_over_cap(self):
        self.assertFalse(cp.credits_available(self.eu(utilization=100)))

    def test_spend_limit_reached(self):
        self.assertFalse(cp.credits_available(self.eu(spend_limit_reached=True, utilization=1)))

    def test_threshold_is_99(self):
        self.assertEqual(cp.CREDITS_EXHAUSTED_PCT, 99)


class ExhaustionAndUsage(unittest.TestCase):
    def test_is_exhausted(self):
        self.assertTrue(cp.is_exhausted([{"utilization": 100}]))
        self.assertTrue(cp.is_exhausted([{"utilization": 50}, {"utilization": 101}]))
        self.assertFalse(cp.is_exhausted([{"utilization": 99}]))

    def test_summarize_modern_limits(self):
        out = cp.summarize_usage({"limits": [{"kind": "session", "utilization": 50, "resets_at": "x"}]})
        self.assertEqual(out[0]["utilization"], 50.0)
        self.assertEqual(out[0]["kind"], "session")

    def test_summarize_legacy_fields(self):
        out = cp.summarize_usage({"five_hour": {"utilization": 30, "resets_at": "x"}})
        self.assertEqual(out[0]["kind"], "five_hour")

    def test_summarize_empty(self):
        self.assertEqual(cp.summarize_usage({}), [])


# ── config-driven per-profile / per-account flags ───────────────────────────
class ProfileFlags(unittest.TestCase):
    def test_exhaust_credits_default_false(self):
        cfg = {"profiles": {"p": {}, "q": {"exhaust_credits": True}}}
        self.assertFalse(cp.profile_exhaust_credits(cfg, "p"))
        self.assertTrue(cp.profile_exhaust_credits(cfg, "q"))

    def test_keepalive_default_true(self):
        self.assertTrue(cp.account_keepalive({}, "x"))
        self.assertTrue(cp.account_keepalive({"keepalive": {"x": True}}, "x"))
        self.assertFalse(cp.account_keepalive({"keepalive": {"x": False}}, "x"))


# ── display labels + `resolve --json` (the single-call contract) ────────────
# claude-usage (and through it the statusline) asks exactly one question —
# "which seat is this?" — and renders `label` verbatim. These tests pin the
# shape it depends on.
class DisplayLabels(unittest.TestCase):
    CFG = {
        "profiles": {
            "personal": {"dir": "~/.claude-personal", "accounts": ["max20x", "max5x"],
                         "display": "Personal"},
            "work": {"dir": "~/.claude"},
            "pm-me": {"dir": "~/.claude-pm", "accounts": ["solo"]},
        },
        "account_display": {"max5x": "Max 5x", "max20x": "Max 20x"},
    }

    def test_display_falls_back_to_raw_name(self):
        # No title-case heuristic: an unconfigured name renders as written.
        self.assertEqual(cp.profile_display(self.CFG, "pm-me"), "pm-me")
        self.assertEqual(cp.account_display(self.CFG, "solo"), "solo")
        self.assertEqual(cp.profile_display(self.CFG, "personal"), "Personal")
        self.assertEqual(cp.account_display(self.CFG, "max5x"), "Max 5x")

    def test_account_named_only_when_serial(self):
        self.assertEqual(cp.compose_label(self.CFG, "personal", "max5x"), "Personal (Max 5x)")
        # One account (or none): the profile name already identifies the seat.
        self.assertEqual(cp.compose_label(self.CFG, "pm-me", "solo"), "pm-me")
        self.assertEqual(cp.compose_label(self.CFG, "work", None), "work")

    def test_profile_dropped_when_it_is_the_only_one(self):
        # A single-profile host: the profile name is a constant, so the label
        # is the account alone — "personal (max20x)" spends most of its width
        # saying nothing.
        solo = {
            "profiles": {"personal": {"dir": "~/.claude",
                                      "accounts": ["max20x", "max5x"],
                                      "display": "Personal"}},
            "account_display": {"max20x": "Max 20x"},
        }
        self.assertEqual(cp.compose_label(solo, "personal", "max20x"), "Max 20x")

    def test_single_profile_single_account_has_no_label(self):
        # Nothing varies, so there is nothing to say — an empty label is a
        # valid answer, not a failure.
        solo = {"profiles": {"personal": {"dir": "~/.claude", "accounts": ["only"],
                                          "display": "Personal"}}}
        self.assertEqual(cp.compose_label(solo, "personal", "only"), "")
        self.assertEqual(cp.compose_label(solo, "personal", None), "")


class ResolveJson(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.pdir = os.path.join(self.tmp, "personal")
        os.makedirs(self.pdir)
        self.cfg = {
            "profiles": {
                "personal": {"dir": self.pdir, "accounts": ["max20x", "max5x"],
                             "display": "Personal"},
                "work": {"dir": os.path.join(self.tmp, "work")},
            },
            "account_display": {"max5x": "Max 5x", "max20x": "Max 20x"},
        }
        self._load, self._state, self._cur = cp.load_config, cp.load_state, cp.current_account_of
        cp.load_config = lambda required=True: self.cfg
        cp.load_state = lambda: {}
        cp.current_account_of = lambda cfg, p: "max5x" if p == "personal" else None

    def tearDown(self):
        cp.load_config, cp.load_state, cp.current_account_of = self._load, self._state, self._cur
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_resolve(self, **kw):
        args = ns(**{"pwd": None, "dir": None, "json": True, "accounts": False, **kw})
        with muted() as buf:
            cp.cmd_resolve(args)
        return json.loads(buf.getvalue())

    def test_by_dir_reverse_lookup(self):
        # The statusline knows the config dir, not a meaningful cwd.
        out = self.run_resolve(dir=self.pdir)
        self.assertTrue(out["active"])
        self.assertEqual(out["profile"], "personal")
        self.assertEqual(out["source"], "dir")
        self.assertEqual(out["label"], "Personal (Max 5x)")
        self.assertEqual(out["schema"], cp.RESOLVE_SCHEMA)

    def test_unclaimed_dir_is_inactive_not_an_error(self):
        out = self.run_resolve(dir=os.path.join(self.tmp, "nope"))
        self.assertEqual(out, {"schema": cp.RESOLVE_SCHEMA, "active": False})

    def test_no_config_is_inactive(self):
        cp.load_config = lambda required=True: None
        self.assertEqual(self.run_resolve(),
                         {"schema": cp.RESOLVE_SCHEMA, "active": False})

    def test_accounts_map_in_one_call(self):
        out = self.run_resolve(dir=self.pdir, accounts=True)
        by_name = {a["name"]: a for a in out["accounts"]}
        self.assertEqual(by_name["max20x"]["profile"], "personal")
        self.assertEqual(by_name["max20x"]["label"], "Personal (Max 20x)")
        self.assertFalse(by_name["max20x"]["live"])
        self.assertTrue(by_name["max5x"]["live"])

    def test_plain_output_unchanged(self):
        args = ns(pwd=self.tmp, dir=None, json=False, accounts=False)
        with muted() as buf:
            cp.cmd_resolve(args)
        # `<profile>\t<dir>\t<auto>` — the wrapper porcelain, untouched.
        self.assertEqual(len(buf.getvalue().strip().split("\t")), 3)


# ── colour (status only, never the porcelain) ───────────────────────────────
class Colour(unittest.TestCase):
    """Colour is a display concern for `status`. The invariant that matters is
    that no porcelain can ever grow an escape sequence — the zsh layer,
    claude-usage and these tests all parse those streams by field."""

    def setUp(self):
        self._env = dict(os.environ)
        for k in ("CLAUDE_PROFILE_COLOR", "NO_COLOR"):
            os.environ.pop(k, None)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)

    def test_off_when_not_a_tty(self):
        # stdout here is a StringIO, i.e. exactly the piped case.
        with muted():
            self.assertFalse(cp.color_enabled())

    def test_always_and_never_force_it(self):
        os.environ["CLAUDE_PROFILE_COLOR"] = "always"
        self.assertTrue(cp.color_enabled())
        os.environ["CLAUDE_PROFILE_COLOR"] = "never"
        self.assertFalse(cp.color_enabled())

    def test_no_color_beats_auto(self):
        os.environ["NO_COLOR"] = "1"
        self.assertFalse(cp.color_enabled())

    def test_no_color_is_honoured_when_empty(self):
        # The NO_COLOR convention is presence-based, not value-based.
        os.environ["NO_COLOR"] = ""
        self.assertFalse(cp.color_enabled())

    def test_explicit_always_outranks_no_color(self):
        # Same precedence as `ls --color=always`: an explicit request wins.
        os.environ["NO_COLOR"] = "1"
        os.environ["CLAUDE_PROFILE_COLOR"] = "always"
        self.assertTrue(cp.color_enabled())

    def test_c_is_a_noop_when_disabled(self):
        os.environ["CLAUDE_PROFILE_COLOR"] = "never"
        self.assertEqual(cp.c("hi", "bold", "green"), "hi")

    def test_c_wraps_and_resets_when_enabled(self):
        os.environ["CLAUDE_PROFILE_COLOR"] = "always"
        self.assertEqual(cp.c("hi", "bold", "green"), "\033[1;32mhi\033[0m")

    def test_c_leaves_empty_text_alone(self):
        # Padding/alignment relies on c("") staying empty rather than becoming
        # a bare reset sequence.
        os.environ["CLAUDE_PROFILE_COLOR"] = "always"
        self.assertEqual(cp.c("", "bold"), "")


# ── OAuth constant resolution (env → config → None) ─────────────────────────
class OAuthSetting(unittest.TestCase):
    def setUp(self):
        self._env = {k: os.environ.get(k) for k in
                     ("CLAUDE_PROFILE_CLIENT_ID", "CLAUDE_PROFILE_USAGE_URL")}
        for k in self._env:
            os.environ.pop(k, None)
        self._load = cp.load_config

    def tearDown(self):
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        cp.load_config = self._load

    def test_env_wins(self):
        os.environ["CLAUDE_PROFILE_CLIENT_ID"] = "envid"
        cp.load_config = lambda **k: {"oauth": {"client_id": "cfgid"}, "profiles": {}}
        self.assertEqual(cp.oauth_setting("client_id"), "envid")

    def test_config_fallback(self):
        cp.load_config = lambda **k: {"oauth": {"client_id": "cfgid"}, "profiles": {}}
        self.assertEqual(cp.oauth_setting("client_id"), "cfgid")

    def test_none_when_unset(self):
        cp.load_config = lambda **k: {"profiles": {}}
        self.assertIsNone(cp.oauth_setting("usage_url"))


# ── refresh gate (the keep-alive "is a grant due?" decision) ────────────────
class RefreshGate(unittest.TestCase):
    def setUp(self):
        self.cfg = {"profiles": {"p": {"dir": "~/.claude", "accounts": ["a", "b"]}}}
        self._caf, self._rpc = cp.current_account_of, cp.read_parked_cred
        self._ls = cp.load_snapshot
        cp.current_account_of = lambda cfg, prof: "a"  # "a" is the live account
        cp.load_snapshot = lambda n: {}                # no cap latched by default

    def tearDown(self):
        cp.current_account_of, cp.read_parked_cred = self._caf, self._rpc
        cp.load_snapshot = self._ls

    def test_live_account_skipped(self):
        act, _b, _o, reason = cp.refresh_gate(self.cfg, "a", 14, False)
        self.assertFalse(act)
        self.assertIn("live", reason)

    def test_fresh_parked_skipped(self):
        cp.read_parked_cred = lambda n: oauth_blob(days_left=30)
        act, _b, _o, reason = cp.refresh_gate(self.cfg, "b", 14, False)
        self.assertFalse(act)
        self.assertIn("fresh", reason)

    def test_due_when_near_expiry(self):
        cp.read_parked_cred = lambda n: oauth_blob(days_left=5)
        act, blob, oauth, reason = cp.refresh_gate(self.cfg, "b", 14, False)
        self.assertTrue(act)
        self.assertIsNone(reason)
        self.assertEqual(oauth["refreshToken"], "r")

    def test_expired_reported(self):
        cp.read_parked_cred = lambda n: oauth_blob(days_left=-1)
        act, _b, _o, reason = cp.refresh_gate(self.cfg, "b", 14, False)
        self.assertFalse(act)
        self.assertIn("EXPIRED", reason)

    def test_force_overrides_fresh(self):
        cp.read_parked_cred = lambda n: oauth_blob(days_left=30)
        act, *_ = cp.refresh_gate(self.cfg, "b", 14, True)
        self.assertTrue(act)

    def test_no_parked_credential(self):
        cp.read_parked_cred = lambda n: None
        act, _b, _o, reason = cp.refresh_gate(self.cfg, "b", 14, False)
        self.assertFalse(act)
        self.assertIn("no parked", reason)

    def _capped(self, days_left=5):
        """A parked blob plus a latch recording that its deadline is capped."""
        blob = oauth_blob(days_left=days_left)
        rexp = json.loads(blob)["claudeAiOauth"]["refreshTokenExpiresAt"]
        cp.read_parked_cred = lambda n: blob
        cp.load_snapshot = lambda n: {"horizonStalledExp": rexp}
        return rexp

    def test_capped_chain_stops_burning_grants(self):
        self._capped()
        act, _b, _o, reason = cp.refresh_gate(self.cfg, "b", 14, False)
        self.assertFalse(act)
        self.assertIn("capped", reason)
        self.assertIn("auth b", reason)

    def test_force_still_grants_on_a_capped_chain(self):
        """The swap-in path forces a grant to make a stale access token usable —
        a capped refresh deadline must not block that."""
        self._capped()
        act, *_ = cp.refresh_gate(self.cfg, "b", 14, True)
        self.assertTrue(act)

    def test_latch_from_a_previous_chain_does_not_stick(self):
        """`auth` mints a chain with a new deadline; the stale latch must stop
        matching so keep-alive resumes on its own."""
        self._capped()
        stale = cp.load_snapshot("b")["horizonStalledExp"]
        cp.read_parked_cred = lambda n: oauth_blob(days_left=10)   # re-authed
        cp.load_snapshot = lambda n: {"horizonStalledExp": stale}
        act, _b, _o, reason = cp.refresh_gate(self.cfg, "b", 14, False)
        self.assertTrue(act)
        self.assertIsNone(reason)

    def test_expired_beats_capped(self):
        """An already-dead token reports as expired, not merely capped."""
        self._capped(days_left=-1)
        act, _b, _o, reason = cp.refresh_gate(self.cfg, "b", 14, False)
        self.assertFalse(act)
        self.assertIn("EXPIRED", reason)


# ── refresh-deadline ledger (can keep-alive actually gain time?) ────────────
class HorizonAdvanced(unittest.TestCase):
    def test_rolling_chain_advances(self):
        now = int(cp.time.time() * 1000)
        self.assertTrue(cp.horizon_advanced(now, now + 86400 * 1000))

    def test_capped_chain_does_not(self):
        ceiling = int(cp.time.time() * 1000) + 86400 * 1000
        self.assertFalse(cp.horizon_advanced(ceiling, ceiling))

    def test_seconds_of_drift_still_counts_as_capped(self):
        """A capped chain recomputes the deadline as now + what's left, so two
        grants land seconds apart — that must not read as progress."""
        ceiling = int(cp.time.time() * 1000) + 86400 * 1000
        self.assertFalse(cp.horizon_advanced(ceiling, ceiling + 7000))

    def test_not_comparable(self):
        self.assertIsNone(cp.horizon_advanced(None, 123))
        self.assertIsNone(cp.horizon_advanced(123, None))


class RecordHorizon(unittest.TestCase):
    def test_appends_with_gain(self):
        snap = {}
        cp.record_horizon(snap, 2_000_000, "grant", prev=1_000_000, live=False)
        (e,) = snap["horizonHistory"]
        self.assertEqual(e["kind"], "grant")
        self.assertEqual(e["exp"], 2_000_000)
        self.assertEqual(e["gainedSeconds"], 1000)
        self.assertFalse(e["live"])

    def test_observed_collapses_unchanged_deadline(self):
        snap = {}
        self.assertTrue(cp.record_horizon(snap, 5, "observed"))
        self.assertFalse(cp.record_horizon(snap, 5, "observed"))
        self.assertTrue(cp.record_horizon(snap, 6, "observed"))
        self.assertEqual(len(snap["horizonHistory"]), 2)

    def test_grant_never_collapses(self):
        """Two capped grants are two rows — the repetition IS the evidence."""
        snap = {}
        cp.record_horizon(snap, 5, "grant")
        cp.record_horizon(snap, 5, "grant")
        self.assertEqual(len(snap["horizonHistory"]), 2)

    def test_ledger_is_bounded(self):
        snap = {}
        for i in range(cp.HORIZON_HISTORY_MAX + 25):
            cp.record_horizon(snap, i, "grant")
        self.assertEqual(len(snap["horizonHistory"]), cp.HORIZON_HISTORY_MAX)
        self.assertEqual(snap["horizonHistory"][-1]["exp"], cp.HORIZON_HISTORY_MAX + 24)


class RefreshAccountHorizon(unittest.TestCase):
    """refresh_account must tell a grant that bought time apart from one that
    returned 200 and bought nothing."""

    def setUp(self):
        self.cfg = {"profiles": {"p": {"dir": "~/.claude", "accounts": ["b"]}}}
        self.saved = {}
        self.store = {"b": oauth_blob(days_left=5)}
        self._orig = {n: getattr(cp, n) for n in (
            "current_account_of", "read_parked_cred", "write_parked_cred",
            "oauth_refresh_grant", "load_snapshot", "save_snapshot", "mutation_lock")}
        cp.current_account_of = lambda cfg, prof: None       # nothing live
        cp.read_parked_cred = lambda n: self.store.get(n)
        cp.write_parked_cred = lambda n, blob: self.store.__setitem__(n, blob)
        cp.load_snapshot = lambda n: dict(self.saved)
        # replace, don't merge — the real save_snapshot rewrites the whole file,
        # so a key the code popped must actually disappear
        cp.save_snapshot = lambda n, snap: (self.saved.clear(), self.saved.update(snap))

    def tearDown(self):
        for n, v in self._orig.items():
            setattr(cp, n, v)

    def _server(self, refresh_expires_in):
        cp.oauth_refresh_grant = lambda rt: {
            "access_token": "new", "expires_in": 28800,
            "refresh_token": "r2", "refresh_token_expires_in": refresh_expires_in,
        }

    def test_rolling_reports_plain_refresh(self):
        self._server(30 * 86400)
        msg = cp.refresh_account(self.cfg, "b", 14, False, True)
        self.assertIn("refreshed", msg)
        self.assertNotIn(cp.HORIZON_STALLED_MARK, msg)
        self.assertNotIn("horizonStalledExp", self.saved)

    def test_capped_chain_is_escalated_then_quiet(self):
        # A ceiling that stays put: each grant is told what little is left of it.
        left = [5 * 86400]
        cp.oauth_refresh_grant = lambda rt: {
            "access_token": "new", "expires_in": 28800,
            "refresh_token": "r2", "refresh_token_expires_in": left[0],
        }
        first = cp.refresh_account(self.cfg, "b", 14, True, True)
        self.assertIn(cp.HORIZON_STALLED_MARK, first)
        self.assertIn("auth b", first)

        left[0] -= 5           # same instant, a few seconds of drift later
        second = cp.refresh_account(self.cfg, "b", 14, True, True)
        self.assertIn(cp.HORIZON_CAPPED_MARK, second)
        self.assertNotIn(cp.HORIZON_STALLED_MARK, second)

    def test_recovery_clears_the_stall(self):
        self._server(5 * 86400)
        cp.refresh_account(self.cfg, "b", 14, True, True)
        self.assertIn("horizonStalledSince", self.saved)
        self._server(30 * 86400)          # e.g. after `auth` opened a new window
        msg = cp.refresh_account(self.cfg, "b", 14, True, True)
        self.assertNotIn(cp.HORIZON_STALLED_MARK, msg)
        self.assertNotIn("horizonStalledSince", self.saved)

    def test_ledger_records_every_grant(self):
        self._server(30 * 86400)
        cp.refresh_account(self.cfg, "b", 14, True, True)
        cp.refresh_account(self.cfg, "b", 14, True, True)
        self.assertEqual(len(self.saved["horizonHistory"]), 2)
        self.assertTrue(all(e["kind"] == "grant" for e in self.saved["horizonHistory"]))


class ObserveHorizons(unittest.TestCase):
    """The read-only comparison arm: samples the live account too, and must
    never make a network call or touch a credential."""

    def setUp(self):
        self.cfg = {"profiles": {"p": {"dir": "~/.claude", "accounts": ["a", "b"]}}}
        self.saved = {}
        self._orig = {n: getattr(cp, n) for n in (
            "current_account_of", "read_parked_cred", "read_live_cred",
            "write_parked_cred", "write_live_cred", "oauth_refresh_grant",
            "load_snapshot", "save_snapshot")}
        cp.current_account_of = lambda cfg, prof: "a"        # "a" is live
        cp.read_live_cred = lambda d: oauth_blob(days_left=30)
        cp.read_parked_cred = lambda n: oauth_blob(days_left=1)
        cp.load_snapshot = lambda n: dict(self.saved.get(n, {}))
        cp.save_snapshot = lambda n, snap: self.saved.__setitem__(n, snap)
        for n in ("write_parked_cred", "write_live_cred", "oauth_refresh_grant"):
            setattr(cp, n, self._boom)

    def tearDown(self):
        for n, v in self._orig.items():
            setattr(cp, n, v)

    @staticmethod
    def _boom(*a, **k):
        raise AssertionError("observe_horizons must not write or hit the network")

    def test_samples_both_arms_and_labels_them(self):
        cp.observe_horizons(self.cfg)
        self.assertTrue(self.saved["a"]["horizonHistory"][-1]["live"])
        self.assertFalse(self.saved["b"]["horizonHistory"][-1]["live"])

    def test_second_sweep_adds_nothing_when_nothing_moved(self):
        cp.observe_horizons(self.cfg)
        cp.observe_horizons(self.cfg)
        self.assertEqual(len(self.saved["a"]["horizonHistory"]), 1)

    def test_missing_credential_is_skipped_not_fatal(self):
        cp.read_parked_cred = lambda n: None
        cp.observe_horizons(self.cfg)
        self.assertNotIn("b", self.saved)


# ── live-session swap guard (+ --force kills) ───────────────────────────────
class EnsureSwappable(unittest.TestCase):
    def setUp(self):
        self._ls, self._ks = cp.live_sessions, cp.kill_sessions

    def tearDown(self):
        cp.live_sessions, cp.kill_sessions = self._ls, self._ks

    def test_no_sessions_ok(self):
        cp.live_sessions = lambda d: []
        cp.ensure_swappable("/x", False)  # must not raise

    def test_refuses_without_force(self):
        cp.live_sessions = lambda d: [{"pid": 5}]
        with self.assertRaises(SystemExit):
            cp.ensure_swappable("/x", False)

    def test_force_kills_then_ok(self):
        seq = [[{"pid": 999}], []]  # sessions present, then gone after kill
        cp.live_sessions = lambda d: (seq.pop(0) if seq else [])
        killed = {}
        cp.kill_sessions = lambda s, **k: killed.setdefault("pids", [x["pid"] for x in s])
        cp.ensure_swappable("/x", True)
        self.assertEqual(killed["pids"], [999])

    def test_force_aborts_if_survivors(self):
        cp.live_sessions = lambda d: [{"pid": 7}]  # never clears
        cp.kill_sessions = lambda s, **k: None
        with self.assertRaises(SystemExit):
            cp.ensure_swappable("/x", True)

    def test_refusal_reports_the_blocked_swap(self):
        """The refusal must say which account you're on and where you'd go —
        the session list alone doesn't answer 'is this worth quitting for?'."""
        cp.live_sessions = lambda d: [{"pid": 5, "cwd": "/w"}]
        with muted() as buf, self.assertRaises(SystemExit):
            cp.ensure_swappable("/x", False, "nothing changed — profile p ...")
        self.assertIn("nothing changed — profile p ...", buf.getvalue())


class SwapContext(unittest.TestCase):
    def setUp(self):
        self._ls = cp.load_snapshot
        cp.load_snapshot = lambda n: {"oauthAccount": {"emailAddress": n + "@x"}}

    def tearDown(self):
        cp.load_snapshot = self._ls

    def test_names_both_ends_with_emails(self):
        s = cp.swap_context("personal", "max20x", "max5x")
        self.assertIn('stays on "max20x" (max20x@x)', s)
        self.assertIn('would go to "max5x" (max5x@x)', s)
        self.assertIn("personal", s)

    def test_unrecognized_current(self):
        s = cp.swap_context("personal", None, "max5x")
        self.assertIn("no recognized live account", s)
        self.assertIn('"max5x"', s)

    def test_label_without_snapshot(self):
        cp.load_snapshot = lambda n: None
        self.assertEqual(cp.account_label("max5x"), '"max5x"')


# ── toggle target selection (cyclic next account) ───────────────────────────
class Toggle(unittest.TestCase):
    def setUp(self):
        self._st = cp.STATE_DIR
        cp.STATE_DIR = tempfile.mkdtemp()
        self.saved = {}
        self._orig = {n: getattr(cp, n) for n in
                      ("load_config", "pick_profile", "profile_dir", "load_state",
                       "save_state", "current_account_of", "ensure_swappable",
                       "load_snapshot", "activate_account", "read_parked_cred")}
        cp.load_config = lambda **k: {"profiles": {"p": {"dir": "~/.claude", "accounts": ["max20x", "max5x"]}}}
        cp.pick_profile = lambda cfg, a: "p"
        cp.profile_dir = lambda cfg, prof: "/x"
        cp.load_state = lambda: {}
        cp.save_state = lambda s: None
        cp.ensure_swappable = lambda d, f, ctx=None: None
        cp.load_snapshot = lambda n: {"oauthAccount": {"emailAddress": n + "@x"}}
        cp.read_parked_cred = lambda n: oauth_blob()
        cp.activate_account = lambda cfg, s, prof, target: self.saved.__setitem__("target", target)

    def tearDown(self):
        shutil.rmtree(cp.STATE_DIR, ignore_errors=True)
        cp.STATE_DIR = self._st
        for n, v in self._orig.items():
            setattr(cp, n, v)

    def _toggle_from(self, current):
        cp.current_account_of = lambda cfg, prof: current
        self.saved.clear()
        with muted():
            cp.cmd_toggle(ns(profile=None, force=False))
        return self.saved.get("target")

    def test_flips_20_to_5(self):
        self.assertEqual(self._toggle_from("max20x"), "max5x")

    def test_flips_5_to_20(self):
        self.assertEqual(self._toggle_from("max5x"), "max20x")

    def test_unknown_current_starts_at_first(self):
        self.assertEqual(self._toggle_from(None), "max20x")


# ── swap preflight: unparked target offers the auth flow ────────────────────
class SwapPreflight(unittest.TestCase):
    """`toggle`/`account` onto an account that was never authenticated."""

    CFG = {"profiles": {"p": {"dir": "~/.claude", "accounts": ["max20x", "max5x"]}}}

    def setUp(self):
        self._st = cp.STATE_DIR
        cp.STATE_DIR = tempfile.mkdtemp()
        # Canary: the redirect must actually cover snapshot paths. A frozen
        # ACCOUNTS_DIR once let this class unlink the operator's real snapshot.
        assert cp.snapshot_path("probe").startswith(cp.STATE_DIR), \
            "STATE_DIR redirect does not cover snapshot_path — refusing to run"
        self.calls = {}
        self._orig = {n: getattr(cp, n) for n in
                      ("load_config", "pick_profile", "profile_dir", "load_state",
                       "save_state", "current_account_of", "ensure_swappable",
                       "load_snapshot", "read_parked_cred", "delete_parked_cred",
                       "activate_account", "auth_account", "interactive")}
        cp.load_config = lambda **k: self.CFG
        cp.pick_profile = lambda cfg, a: "p"
        cp.profile_dir = lambda cfg, prof: "/x"
        cp.load_state = lambda: {}
        cp.save_state = lambda s: None
        cp.current_account_of = lambda cfg, prof: "max20x"
        # the session guard fires only if the preflight wrongly runs first
        cp.ensure_swappable = lambda d, f, ctx=None: self.calls.__setitem__("guard", True)
        cp.delete_parked_cred = lambda n: self.calls.__setitem__("deleted", n)
        cp.activate_account = lambda cfg, s, prof, t: self.calls.__setitem__("target", t)
        # max20x is live and parked; max5x has never been captured
        self.snaps = {"max20x": {"accountUuid": "uuid-20x",
                                 "oauthAccount": {"emailAddress": "a@x"}}}
        cp.load_snapshot = lambda n: self.snaps.get(n)
        cp.read_parked_cred = lambda n: oauth_blob() if n in self.snaps else None
        cp.auth_account = self._fake_auth
        self.auth_captures = "uuid-5x"

    def _fake_auth(self, name, **kw):
        """Stand-in for the login flow: parks whatever uuid the test dictates."""
        self.calls["authed"] = name
        self.snaps[name] = {"accountUuid": self.auth_captures,
                            "oauthAccount": {"emailAddress": name + "@x"}}
        return name + "@x"

    def tearDown(self):
        shutil.rmtree(cp.STATE_DIR, ignore_errors=True)
        cp.STATE_DIR = self._st
        for n, v in self._orig.items():
            setattr(cp, n, v)

    def _toggle(self, answer=None):
        """Run `toggle`; `answer` None = non-interactive, else the typed reply."""
        cp.interactive = lambda: answer is not None
        with mock.patch("builtins.input", lambda *a: answer or ""):
            with muted() as buf:
                try:
                    cp.cmd_toggle(ns(profile=None, force=False))
                    return None, buf.getvalue()
                except SystemExit as e:
                    return e.code, buf.getvalue()

    def test_non_interactive_dies_with_auth_hint(self):
        code, out = self._toggle()
        self.assertEqual(code, 1)
        self.assertIn("claude-profile auth max5x", out)
        self.assertNotIn("target", self.calls)

    def test_preflight_runs_before_the_session_guard(self):
        # the readiness problem must surface without first demanding the
        # operator close every session for a swap that cannot happen
        self._toggle()
        self.assertNotIn("guard", self.calls)

    def test_declining_the_prompt_changes_nothing(self):
        code, out = self._toggle(answer="n")
        self.assertEqual(code, 1)
        self.assertNotIn("authed", self.calls)
        self.assertNotIn("target", self.calls)

    def test_accepting_auths_then_swaps(self):
        code, _ = self._toggle(answer="")  # bare Enter = default yes
        self.assertIsNone(code)
        self.assertEqual(self.calls.get("authed"), "max5x")
        self.assertEqual(self.calls.get("target"), "max5x")
        self.assertTrue(self.calls.get("guard"))  # guard still ran, after

    def test_auth_then_swap_handoff_is_announced(self):
        _, out = self._toggle(answer="")
        self.assertIn("continuing with the swap", out)

    def test_wrong_account_capture_is_rejected_and_discarded(self):
        self.auth_captures = "uuid-20x"  # signed into the live account again
        code, out = self._toggle(answer="y")
        self.assertEqual(code, 1)
        self.assertIn("same account", out)
        self.assertEqual(self.calls.get("deleted"), "max5x")
        self.assertNotIn("target", self.calls)

    def test_ready_account_skips_the_preflight_entirely(self):
        self.snaps["max5x"] = {"accountUuid": "uuid-5x",
                               "oauthAccount": {"emailAddress": "b@x"}}
        code, _ = self._toggle()
        self.assertIsNone(code)
        self.assertNotIn("authed", self.calls)
        self.assertEqual(self.calls.get("target"), "max5x")


# ── auto-rotation with exhaust_credits ──────────────────────────────────────
class RotateExhaustCredits(unittest.TestCase):
    EXH = [{"kind": "5h", "utilization": 100.0, "resets_at": None}]

    def setUp(self):
        self._st = cp.STATE_DIR
        cp.STATE_DIR = tempfile.mkdtemp()
        self.rotated = {}
        self._orig = {n: getattr(cp, n) for n in
                      ("load_config", "pick_profile", "profile_dir", "load_state",
                       "save_state", "current_account_of", "read_parked_cred",
                       "refresh_health", "account_usage", "account_credits",
                       "activate_account")}
        cp.pick_profile = lambda cfg, a: "p"
        cp.profile_dir = lambda cfg, prof: "/x"
        cp.load_state = lambda: {}
        cp.save_state = lambda s: None
        cp.current_account_of = lambda cfg, prof: "a"
        cp.read_parked_cred = lambda n: "{}"  # candidate is parked
        cp.refresh_health = lambda b: ""
        cp.account_usage = lambda cfg, st, pr, ac, ttl=0: (self.EXH if ac == "a" else [], 0)
        cp.activate_account = lambda *a, **k: self.rotated.__setitem__("did", True)

    def tearDown(self):
        shutil.rmtree(cp.STATE_DIR, ignore_errors=True)
        cp.STATE_DIR = self._st
        for n, v in self._orig.items():
            setattr(cp, n, v)

    def _run(self, exhaust, credits):
        cp.load_config = lambda **k: {"profiles": {"p": {"dir": "~/.claude", "accounts": ["a", "b"], "exhaust_credits": exhaust}}}
        cp.account_credits = lambda st, ac: credits
        self.rotated.clear()
        with muted():
            cp.cmd_rotate(ns(profile="p", quiet=True, if_exhausted=True, dry_run=False))
        return self.rotated.get("did", False)

    def test_flag_off_rotates_at_limit(self):
        self.assertTrue(self._run(exhaust=False, credits=True))

    def test_flag_on_with_credits_stays_put(self):
        self.assertFalse(self._run(exhaust=True, credits=True))

    def test_flag_on_without_credits_rotates(self):
        self.assertTrue(self._run(exhaust=True, credits=False))


# ── platform safety: missing `security` binary must degrade, not crash ──────
class SecurityDegrade(unittest.TestCase):
    def setUp(self):
        self._run = cp.subprocess.run

    def tearDown(self):
        cp.subprocess.run = self._run

    def test_missing_security_read_degrades(self):
        def boom(*a, **k):
            raise FileNotFoundError(2, "No such file", "security")
        cp.subprocess.run = boom
        res = cp._security(["find-generic-password"])  # check=False
        self.assertNotEqual(res.returncode, 0)  # clean miss, no raise

    def test_missing_security_write_raises(self):
        def boom(*a, **k):
            raise FileNotFoundError(2, "No such file", "security")
        cp.subprocess.run = boom
        with self.assertRaises(RuntimeError):
            cp._security(["add-generic-password"], check=True)


# ── refresh-token health string ─────────────────────────────────────────────
class RefreshHealth(unittest.TestCase):
    def test_healthy_is_blank(self):
        self.assertEqual(cp.refresh_health(oauth_blob(days_left=25)), "")

    def test_near_expiry_warns(self):
        self.assertIn("expires in", cp.refresh_health(oauth_blob(days_left=5)))

    def test_expired(self):
        self.assertIn("EXPIRED", cp.refresh_health(oauth_blob(days_left=-1)))


# ── account_usage_raw (raw usage JSON, parked-token refresh) ────────────────
class AccountUsageRaw(unittest.TestCase):
    def setUp(self):
        self.cfg = {"profiles": {"p": {"dir": "~/.claude", "accounts": ["live", "parked"]}}}
        self._st = cp.STATE_DIR
        cp.STATE_DIR = tempfile.mkdtemp()                  # isolate the raw-usage cache
        self._orig = {n: getattr(cp, n) for n in
                      ("current_account_of", "read_live_cred", "read_parked_cred",
                       "fetch_usage", "refresh_account", "mutation_lock", "profile_dir")}
        cp.current_account_of = lambda cfg, prof: "live"   # "live" is live, "parked" isn't
        cp.profile_dir = lambda cfg, prof: "/x"
        cp.fetch_usage = lambda tok: {"ok": tok}           # echo the token actually used
        cp.mutation_lock = lambda: contextlib.nullcontext()

    def tearDown(self):
        shutil.rmtree(cp.STATE_DIR, ignore_errors=True)
        cp.STATE_DIR = self._st
        for n, v in self._orig.items():
            setattr(cp, n, v)

    def test_live_valid_token_fetches_without_refresh(self):
        cp.read_live_cred = lambda d: oauth_blob(access="A")
        cp.read_parked_cred = lambda n: None
        seen = {"refresh": 0}
        cp.refresh_account = lambda *a, **k: seen.__setitem__("refresh", seen["refresh"] + 1)
        self.assertEqual(cp.account_usage_raw(self.cfg, "live"), {"ok": "A"})
        self.assertEqual(seen["refresh"], 0)               # a live account is never refreshed

    def test_parked_expired_refreshes_then_fetches(self):
        st = {"blob": expired_blob()}
        cp.read_live_cred = lambda d: None
        cp.read_parked_cred = lambda n: st["blob"]
        def fake_refresh(cfg, name, min_days_left, force, quiet):
            st["blob"] = oauth_blob(access="FRESH")        # simulate a successful grant
        cp.refresh_account = fake_refresh
        self.assertEqual(cp.account_usage_raw(self.cfg, "parked"), {"ok": "FRESH"})

    def test_parked_refresh_fails_returns_none(self):
        cp.read_live_cred = lambda d: None
        cp.read_parked_cred = lambda n: expired_blob()     # stays expired even after refresh
        cp.refresh_account = lambda *a, **k: None
        self.assertIsNone(cp.account_usage_raw(self.cfg, "parked"))

    def test_live_expired_returns_none_never_refreshes(self):
        cp.read_live_cred = lambda d: expired_blob()
        cp.read_parked_cred = lambda n: None
        seen = {"refresh": 0}
        cp.refresh_account = lambda *a, **k: seen.__setitem__("refresh", seen["refresh"] + 1)
        self.assertIsNone(cp.account_usage_raw(self.cfg, "live"))
        self.assertEqual(seen["refresh"], 0)               # invariant: live creds untouched

    def test_stale_cache_served_on_fetch_failure(self):
        # 1st call succeeds and populates the cache.
        cp.read_live_cred = lambda d: oauth_blob(access="A")
        cp.read_parked_cred = lambda n: None
        self.assertEqual(cp.account_usage_raw(self.cfg, "live", ttl=0), {"ok": "A"})
        # Now the fetch breaks (rate limit / token lapse); ttl=0 forces a re-fetch
        # attempt, which fails → the last-known-good must be served, not None.
        cp.fetch_usage = lambda tok: None
        self.assertEqual(cp.account_usage_raw(self.cfg, "live", ttl=0), {"ok": "A"})

    def test_fresh_cache_short_circuits_fetch(self):
        cp.read_live_cred = lambda d: oauth_blob(access="A")
        cp.read_parked_cred = lambda n: None
        self.assertEqual(cp.account_usage_raw(self.cfg, "live"), {"ok": "A"})
        # Within ttl, a second call serves cache without touching the token path.
        cp.read_live_cred = lambda d: (_ for _ in ()).throw(AssertionError("refetched"))
        self.assertEqual(cp.account_usage_raw(self.cfg, "live"), {"ok": "A"})


# ── usage-json porcelain (consumed by `claude-usage --all`) ─────────────────
class CmdUsageJson(unittest.TestCase):
    def setUp(self):
        self._orig = {n: getattr(cp, n) for n in ("load_config", "account_usage_raw", "pick_profile")}
        cp.load_config = lambda **k: {"profiles": {
            "p1": {"dir": "~/.claude", "accounts": ["a", "b"]},
            "p2": {"dir": "~/x", "accounts": ["c"]}}}

    def tearDown(self):
        for n, v in self._orig.items():
            setattr(cp, n, v)

    def _run(self, **kw):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
            cp.cmd_usage_json(ns(**{"account": None, "all": False, "profile": None, "fresh": False, **kw}))
        return buf.getvalue().rstrip("\n").split("\n")

    def test_all_sorted_with_gap(self):
        cp.account_usage_raw = lambda cfg, name, ttl=None: ({"who": name} if name != "b" else None)
        lines = self._run(all=True)
        self.assertEqual(lines[0], 'a\t{"who":"a"}')       # compact JSON, no spaces
        self.assertEqual(lines[1], 'b\t')                   # unavailable → empty field
        self.assertEqual(lines[2], 'c\t{"who":"c"}')       # every profile's accounts, sorted

    def test_single_account(self):
        cp.account_usage_raw = lambda cfg, name, ttl=None: {"who": name}
        self.assertEqual(self._run(account="c"), ['c\t{"who":"c"}'])

    def test_default_profile_scope(self):
        cp.pick_profile = lambda cfg, args: "p1"
        cp.account_usage_raw = lambda cfg, name, ttl=None: {"n": name}
        names = [l.split("\t")[0] for l in self._run()]
        self.assertEqual(names, ["a", "b"])                 # just the resolved profile


if __name__ == "__main__":
    unittest.main(verbosity=2)
