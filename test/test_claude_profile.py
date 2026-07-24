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

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@contextlib.contextmanager
def muted():
    """Swallow a command's normal stdout/stderr chatter during a test."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        yield


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
        cp.current_account_of = lambda cfg, prof: "a"  # "a" is the live account

    def tearDown(self):
        cp.current_account_of, cp.read_parked_cred = self._caf, self._rpc

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


# ── toggle target selection (cyclic next account) ───────────────────────────
class Toggle(unittest.TestCase):
    def setUp(self):
        self._st = cp.STATE_DIR
        cp.STATE_DIR = tempfile.mkdtemp()
        self.saved = {}
        self._orig = {n: getattr(cp, n) for n in
                      ("load_config", "pick_profile", "profile_dir", "load_state",
                       "save_state", "current_account_of", "ensure_swappable",
                       "load_snapshot", "activate_account")}
        cp.load_config = lambda **k: {"profiles": {"p": {"dir": "~/.claude", "accounts": ["max20x", "max5x"]}}}
        cp.pick_profile = lambda cfg, a: "p"
        cp.profile_dir = lambda cfg, prof: "/x"
        cp.load_state = lambda: {}
        cp.save_state = lambda s: None
        cp.ensure_swappable = lambda d, f: None
        cp.load_snapshot = lambda n: {"oauthAccount": {"emailAddress": n + "@x"}}
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
