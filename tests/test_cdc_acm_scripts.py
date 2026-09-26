"""Behaviour of the root scripts (root/load-cdc-acm, root/stage-cdc-acm, root/install-cdc-acm-loader.sh)
in a sandbox: every fixed path (/var/lib/babble-tracker, /etc, /sys/module/cdc_acm, /usr/lib/modules,
the terminal) is moved into a temporary directory, and the commands that need root or a kernel
(insmod, rmmod, systemctl, stat's owner, pacman, modinfo, ...) are stubs that keep their state in
files. Nothing here needs root or a headset, only bash:

    python3 -m unittest discover -s tests -v

The tests run with bash on Linux. Elsewhere set BABBLE_TEST_BASH to a bash to use, e.g. Git for
Windows: BABBLE_TEST_BASH="C:/Program Files/Git/bin/bash.exe" (never the WSL launcher).
"""
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROOT_DIR = os.path.join(ROOT, "root")
ROOT_FILES = ("load-cdc-acm", "stage-cdc-acm", "install-cdc-acm-loader.sh", "babble-cdc-acm.service",
              "70-babble-tracker.rules", "babble-tracker.atomic-update.conf")
KVER = "6.18.0-gtest"
KPKG = "linux-618-deckard"
KPKGVER = "6.18.0+gtest-1"
KBUILD = "#1 SMP PREEMPT_DYNAMIC test build"


def find_bash():
    explicit = os.environ.get("BABBLE_TEST_BASH")
    if explicit:
        return explicit
    if sys.platform.startswith("linux"):
        return shutil.which("bash")
    return None


BASH = find_bash()


def posix(path):
    """The path as bash sees it (Git for Windows: C:\\x -> /c/x)."""
    path = os.path.abspath(path)
    if os.name == "nt":
        drive, rest = os.path.splitdrive(path)
        return "/" + drive[0].lower() + rest.replace("\\", "/")
    return path


def module_bytes(tag):
    """Just enough of an aarch64 ET_REL ELF file for stage-cdc-acm's header checks."""
    ident = b"\x7fELF\x02\x01\x01" + bytes(9)
    return ident + b"\x01\x00" + b"\xb7\x00" + bytes(44) + b"fake cdc_acm " + tag.encode()


STUB_HEAD = """#!/bin/bash
T='{t}'
KVER='{kver}'
echo "$(basename "$0") $*" >> "$T/calls"
"""

STUBS = {
    # id -u: 0 (the root scripts run with sudo), or the uid in $T/state/uid
    "id": """[ "${1:-}" = -u ] && { cat "$T/state/uid" 2>/dev/null || echo 0; exit 0; }
exec /usr/bin/id "$@"
""",
    "uname": """case "${1:-}" in
    -r) echo "$KVER" ;;
    -v) cat "$T/state/kernel-build" ;;
    *) exec /usr/bin/uname "$@" ;;
esac
""",
    # stat -c '%u %a %h %F' -- PATH: everything is root-owned and not writable by others here.
    "stat": """if [ "${1:-}" = -c ] && [ "${2:-}" = '%u %a %h %F' ]; then
    p="${!#}"
    case "$p" in /|/var|/var/lib) echo "0 755 2 directory"; exit 0 ;; esac
    if [ -L "$p" ]; then echo "0 777 1 symbolic link"
    elif [ -d "$p" ]; then echo "0 755 2 directory"
    elif [ -f "$p" ]; then echo "0 644 1 regular file"
    else exit 1
    fi
    exit 0
fi
exec /usr/bin/stat "$@"
""",
    "modinfo": """case "$2" in
    vermagic) echo "$KVER SMP preempt mod_unload aarch64" ;;
    name) echo cdc_acm ;;
    depends) echo "" ;;
esac
""",
    "pacman": """case "$1" in
    -Q) echo "$2 {kpkgver}" ;;
    -Qqo) echo {kpkg} ;;
esac
""",
    # insmod: $T/state/insmod = ok (default), oops (init crashes: the task is killed, cdc_acm stays
    # "coming") or fail (init returns an error).
    "insmod": """M="$T/sys/module/cdc_acm"
case "$(cat "$T/state/insmod" 2>/dev/null || echo ok)" in
    ok) mkdir -p "$M"; echo live > "$M/initstate"; echo 0 > "$M/refcnt" ;;
    oops) mkdir -p "$M"; echo coming > "$M/initstate"; echo 0 > "$M/refcnt"; ulimit -c 0; kill -SEGV $$ ;;
    *) echo "insmod: ERROR: could not insert module: Invalid argument" >&2; exit 1 ;;
esac
""",
    # rmmod: fails while refcnt is not 0, or always with $T/state/rmmod-busy (a port opened after
    # the scripts looked).
    "rmmod": """M="$T/sys/module/cdc_acm"
[ -d "$M" ] || exit 1
if [ -e "$T/state/rmmod-busy" ] || [ "$(cat "$M/refcnt")" != 0 ]; then
    echo "rmmod: ERROR: Module cdc_acm is in use" >&2
    exit 1
fi
rm -rf "$M"
""",
    # systemctl: enabled/active/failed are files, and enable/disable also add/remove the
    # multi-user.target.wants entry; restart runs the unit the way systemd would (conditions,
    # ExecCondition, ExecStart). is-enabled says not-found without a unit file (a wants entry whose
    # target exists counts as enabled). $T/state/disable-fails: disable fails and changes nothing
    # (e.g. a D-Bus timeout); $T/state/is-enabled-fails: is-enabled prints nothing and fails
    # (containing "once": only the next call).
    "systemctl": """S="$T/state"
W="$T/etc/systemd/system/multi-user.target.wants/babble-cdc-acm.service"
quiet=0
args=()
for a in "$@"; do
    case "$a" in --quiet) quiet=1 ;; *) args+=("$a") ;; esac
done
set -- "${args[@]}"
case "$1" in
    is-enabled)
        if [ -e "$S/is-enabled-fails" ]; then
            [ "$(cat "$S/is-enabled-fails")" != once ] || rm -f "$S/is-enabled-fails"
            echo "Failed to get unit file state for $2: Connection timed out" >&2
            exit 1
        fi
        if [ ! -e "$T/etc/systemd/system/$2" ]; then [ "$quiet" = 1 ] || echo not-found; exit 4; fi
        if [ -e "$S/enabled" ] || [ -e "$W" ]; then [ "$quiet" = 1 ] || echo enabled; exit 0; fi
        [ "$quiet" = 1 ] || echo disabled
        exit 1 ;;
    enable) touch "$S/enabled"; mkdir -p "$(dirname "$W")"; touch "$W" ;;
    disable)
        if [ -e "$S/disable-fails" ]; then echo "Failed to disable unit: Connection timed out" >&2; exit 1; fi
        if [ ! -e "$T/etc/systemd/system/$2" ]; then echo "Failed to disable unit: Unit file $2 does not exist." >&2; exit 1; fi
        rm -f "$S/enabled" "$W" ;;
    is-active) [ -e "$S/active" ] ;;
    reset-failed) rm -f "$S/failed" ;;
    restart)
        rm -f "$S/active"
        [ -d "$T/sys/module/cdc_acm" ] && exit 0
        [ -f "$T/store/load-cdc-acm" ] || exit 0
        bash "$T/store/load-cdc-acm" --check >> "$T/journal" 2>&1
        rc=$?
        if [ "$rc" = 0 ]; then
            bash "$T/store/load-cdc-acm" >> "$T/journal" 2>&1
            rc=$?
        fi
        case "$rc" in
            0) touch "$S/active" ;;
            255) touch "$S/failed"; exit 1 ;;
        esac ;;
esac
exit 0
""",
    "install": """args=()
while [ $# -gt 0 ]; do
    case "$1" in -o|-g) shift 2 ;; *) args+=("$1"); shift ;; esac
done
exec /usr/bin/install "${args[@]}"
""",
    "chown": "exit 0\n",
    "journalctl": "exit 0\n",
    "udevadm": "exit 0\n",
}


@unittest.skipUnless(BASH, "needs bash (Linux, or BABBLE_TEST_BASH)")
class Sandbox(unittest.TestCase):
    """A fake headset: T/store (/var/lib/babble-tracker), T/etc, T/sys/module, T/modules
    (/usr/lib/modules) and T/answer (what the operator types on the terminal)."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name
        self.t = posix(self.dir)
        for sub in ("stubs", "state", "store/state", "sys/module", "dev", "src/root", "etc/systemd/system",
                    "etc/udev/rules.d", "modules/%s/kernel/drivers" % KVER):
            os.makedirs(self.path(sub), exist_ok=True)
        self.write("modules/%s/kernel/drivers/ref.ko" % KVER, b"in-tree module")
        self.write("modules/%s/pkgbase" % KVER, KPKG.encode() + b"\n")
        self.write("state/kernel-build", KBUILD.encode() + b"\n")
        for name, body in STUBS.items():
            head = STUB_HEAD.format(t=self.t, kver=KVER)
            self.write("stubs/" + name, (head + body.replace("{kpkgver}", KPKGVER).replace("{kpkg}", KPKG)).encode())
            os.chmod(self.path("stubs/" + name), 0o755)
        for name in ROOT_FILES:
            with open(os.path.join(ROOT_DIR, name), "rb") as fh:
                self.write("src/root/" + name, self.sandboxed(fh.read().decode("utf-8")).encode())

    # -- helpers

    def path(self, rel):
        return os.path.join(self.dir, *rel.split("/"))

    def write(self, rel, data):
        with open(self.path(rel), "wb") as fh:
            fh.write(data)

    def read(self, rel):
        with open(self.path(rel), "rb") as fh:
            return fh.read().decode("utf-8")

    def exists(self, rel):
        return os.path.exists(self.path(rel))

    def sandboxed(self, text):
        """A root script with its fixed paths moved into the sandbox and the stubs first in PATH."""
        t = self.t
        for old, new in (("export PATH=/usr/bin LC_ALL=C", "export PATH=%s/stubs:/usr/bin:/bin LC_ALL=C" % t),
                         ("/var/lib/babble-tracker", t + "/store"),
                         ("/sys/module/cdc_acm", t + "/sys/module/cdc_acm"),
                         ("/usr/lib/modules", t + "/modules"),
                         ("/etc/", t + "/etc/"),
                         ("</dev/tty", "<%s/answer" % t),
                         ("/dev/ttyACM", t + "/dev/ttyACM"),
                         ("/dev/babble-tracker", t + "/dev/babble-tracker")):
            text = text.replace(old, new)
        return text

    def run_script(self, rel, *args, stdin=b""):
        """bash on a script in the sandbox: (exit status, stdout + stderr)."""
        result = subprocess.run([BASH, self.t + "/" + rel] + list(args), input=stdin, capture_output=True,
                                check=False)
        return result.returncode, (result.stdout + result.stderr).decode("utf-8", "replace")

    def disables(self):
        return [c for c in self.calls("systemctl") if c.startswith("systemctl disable")]

    def calls(self, command):
        if not self.exists("calls"):
            return []
        return [line for line in self.read("calls").splitlines() if line.split(" ", 1)[0] == command]

    def install_loader(self):
        """The installed state: helpers in the store, the unit in /etc (not enabled)."""
        for name in ("load-cdc-acm", "stage-cdc-acm"):
            shutil.copy(self.path("src/root/" + name), self.path("store/" + name))
            os.chmod(self.path("store/" + name), 0o755)
        shutil.copy(self.path("src/root/babble-cdc-acm.service"), self.path("etc/systemd/system/babble-cdc-acm.service"))

    def stage_module(self, tag="old", kernel_build=KBUILD):
        """A module staged earlier for KVER, as stage-cdc-acm leaves it."""
        data = module_bytes(tag)
        base = "store/by-kernel/" + KVER
        os.makedirs(self.path(base), exist_ok=True)
        self.write(base + "/cdc-acm.ko", data)
        self.write(base + "/cdc-acm.ko.sha256", ("%s  cdc-acm.ko\n" % hashlib.sha256(data).hexdigest()).encode())
        self.write(base + "/kernel-build", (kernel_build + "\n").encode())
        return data

    def staged_bytes(self):
        with open(self.path("store/by-kernel/%s/cdc-acm.ko" % KVER), "rb") as fh:
            return fh.read()

    def set_module(self, state, refcnt=0):
        """cdc_acm in the running kernel: None (absent), live, coming, ..."""
        shutil.rmtree(self.path("sys/module/cdc_acm"), ignore_errors=True)
        if state is not None:
            os.makedirs(self.path("sys/module/cdc_acm"))
            self.write("sys/module/cdc_acm/initstate", (state + "\n").encode())
            self.write("sys/module/cdc_acm/refcnt", ("%d\n" % refcnt).encode())

    def module_state(self):
        if not self.exists("sys/module/cdc_acm"):
            return None
        return self.read("sys/module/cdc_acm/initstate").strip()

    def set_state(self, name, value=""):
        self.write("state/" + name, value.encode())

    def enabled(self):
        return self.exists("state/enabled")

    WANTS = "etc/systemd/system/multi-user.target.wants/babble-cdc-acm.service"

    def wants_link(self):
        """Is there a multi-user.target.wants entry (a dangling symlink counts)?"""
        return os.path.lexists(self.path(self.WANTS))

    def marker(self):
        return self.exists("store/state/load-attempt")


class LoadScriptTests(Sandbox):
    def setUp(self):
        super().setUp()
        self.install_loader()
        self.stage_module()

    def test_a_module_that_crashes_in_init_is_refused_and_keeps_the_marker(self):
        self.set_state("insmod", "oops")
        rc, out = self.run_script("store/load-cdc-acm")
        self.assertEqual(255, rc, out)
        self.assertIn("killed by signal 11", out)
        self.assertEqual("coming", self.module_state())
        self.assertTrue(self.marker(), "the crash marker must stay")
        # the half-loaded module is not taken for a loaded one, and the next check refuses
        rc, out = self.run_script("store/load-cdc-acm", "--check")
        self.assertEqual(255, rc, out)
        self.assertIn("stuck in state 'coming'", out)
        self.set_module(None)   # next boot: the marker still refuses
        rc, out = self.run_script("store/load-cdc-acm", "--check")
        self.assertEqual(255, rc, out)
        self.assertIn("never finished", out)

    def test_a_successful_load_removes_the_marker(self):
        rc, out = self.run_script("store/load-cdc-acm")
        self.assertEqual(0, rc, out)
        self.assertIn("cdc_acm loaded", out)
        self.assertEqual("live", self.module_state())
        self.assertFalse(self.marker())
        self.assertEqual(["insmod %s/store/by-kernel/%s/cdc-acm.ko" % (self.t, KVER)], self.calls("insmod"))

    def test_an_init_that_returns_an_error_is_refused_without_a_marker(self):
        self.set_state("insmod", "fail")
        rc, out = self.run_script("store/load-cdc-acm")
        self.assertEqual(255, rc, out)
        self.assertIn("insmod exited with 1", out)
        self.assertFalse(self.marker())

    def test_check_skips_a_live_module_and_refuses_a_half_loaded_one(self):
        self.set_module("live")
        self.assertEqual(1, self.run_script("store/load-cdc-acm", "--check")[0])
        self.set_module("coming")
        self.assertEqual(255, self.run_script("store/load-cdc-acm", "--check")[0])
        self.set_module(None)
        rc, out = self.run_script("store/load-cdc-acm", "--check")
        self.assertEqual(0, rc, out)

    def test_another_kernel_build_is_skipped(self):
        self.set_state("kernel-build", "#2 SMP PREEMPT_DYNAMIC rebuilt\n")
        rc, out = self.run_script("store/load-cdc-acm", "--check")
        self.assertEqual(1, rc, out)
        self.assertEqual([], self.calls("insmod"))


class StageScriptTests(Sandbox):
    def setUp(self):
        super().setUp()
        self.install_loader()
        self.old = self.stage_module("old")

    def stage(self, data, answer=None, yes=False):
        if answer is not None:
            self.write("answer", (answer + "\n").encode())
        args = ["--headers", KPKGVER] + (["--yes"] if yes else [])
        return self.run_script("store/stage-cdc-acm", *args, stdin=data)

    def armed_and_live(self):
        """The situation on a working headset: the staged build loaded at boot, the unit armed."""
        self.set_state("enabled")
        self.set_state("active")
        self.set_module("live")

    # -- --rearm

    def test_rearm_with_the_port_in_use_changes_nothing(self):
        self.armed_and_live()
        self.set_module("live", refcnt=1)
        rc, out = self.run_script("store/stage-cdc-acm", "--rearm")
        self.assertEqual(1, rc, out)
        self.assertIn("in use", out)
        self.assertIn("The staged build and babble-cdc-acm.service were left as they were", out)
        self.assertTrue(self.enabled(), "a working, armed unit must stay armed")
        self.assertEqual([], self.calls("rmmod"))
        self.assertEqual([], [c for c in self.calls("systemctl") if c.startswith("systemctl disable")])

    def test_rearm_keeps_the_unit_armed_when_rmmod_fails_after_the_check(self):
        self.armed_and_live()
        self.set_state("rmmod-busy")
        rc, out = self.run_script("store/stage-cdc-acm", "--rearm")
        self.assertEqual(1, rc, out)
        self.assertTrue(self.enabled())
        self.assertEqual("live", self.module_state())

    def test_rearm_tests_the_staged_module_through_the_unit_and_arms_it(self):
        self.armed_and_live()
        rc, out = self.run_script("store/stage-cdc-acm", "--rearm")
        self.assertEqual(0, rc, out)
        self.assertEqual(1, len(self.calls("rmmod")))
        self.assertEqual(1, len(self.calls("insmod")))
        self.assertTrue(self.enabled())
        self.assertEqual("live", self.module_state())

    def test_a_module_that_crashes_in_init_is_never_armed(self):
        self.set_state("insmod", "oops")
        rc, out = self.run_script("store/stage-cdc-acm", "--rearm")
        self.assertEqual(1, rc, out)
        self.assertIn("not armed for boot (babble-cdc-acm.service is disabled)", out)
        self.assertIn("coming", out)
        self.assertFalse(self.enabled())
        self.assertTrue(self.marker())
        # and while it is stuck half loaded, nothing else is tried until a reboot
        rc, out = self.run_script("store/stage-cdc-acm", "--rearm")
        self.assertEqual(1, rc, out)
        self.assertIn("stuck in state 'coming'", out)
        self.assertEqual(1, len(self.calls("insmod")))

    # -- staging a new build

    def test_staging_asks_before_it_changes_anything(self):
        self.armed_and_live()
        new = module_bytes("new")
        rc, out = self.stage(new, answer="n")
        self.assertEqual(1, rc, out)
        self.assertIn(hashlib.sha256(new).hexdigest(), out)
        self.assertIn("not confirmed; the staged build and babble-cdc-acm.service were left as they were", out)
        self.assertEqual(self.old, self.staged_bytes())
        self.assertTrue(self.enabled())
        self.assertEqual([], self.calls("rmmod"))
        rc, out = self.stage(new, answer="y")
        self.assertEqual(0, rc, out)
        self.assertEqual(new, self.staged_bytes())
        self.assertTrue(self.enabled())
        self.assertEqual("live", self.module_state())

    def test_staging_without_a_terminal_needs_yes(self):
        new = module_bytes("new")
        rc, out = self.stage(new)   # no answer: the terminal cannot be read
        self.assertEqual(1, rc, out)
        self.assertEqual(self.old, self.staged_bytes())
        rc, out = self.stage(new, yes=True)
        self.assertEqual(0, rc, out)
        self.assertEqual(new, self.staged_bytes())
        self.assertTrue(self.enabled())

    def test_staging_with_the_port_in_use_changes_nothing(self):
        self.armed_and_live()
        self.set_module("live", refcnt=1)
        rc, out = self.stage(module_bytes("new"), yes=True)
        self.assertEqual(1, rc, out)
        self.assertIn("were left as they were", out)
        self.assertEqual(self.old, self.staged_bytes())
        self.assertTrue(self.enabled())

    def test_the_same_build_stays_armed_when_rmmod_fails_after_the_check(self):
        self.armed_and_live()
        self.set_state("rmmod-busy")
        rc, out = self.stage(self.old, yes=True)
        self.assertEqual(1, rc, out)
        self.assertIn("same as before and stays armed", out)
        self.assertTrue(self.enabled())

    def test_a_new_build_is_not_armed_when_rmmod_fails_after_the_check(self):
        self.armed_and_live()
        self.set_state("rmmod-busy")
        rc, out = self.stage(module_bytes("new"), yes=True)
        self.assertEqual(1, rc, out)
        self.assertIn("is disabled: it is not armed for boot", out)
        self.assertFalse(self.enabled())

    def test_a_failed_disarm_changes_nothing(self):
        # systemctl disable fails (D-Bus timeout) and the port is opened after the check: the new,
        # untested build must not end up in place while the unit is still armed.
        self.armed_and_live()
        self.set_state("disable-fails")
        self.set_state("rmmod-busy")
        rc, out = self.stage(module_bytes("new"), yes=True)
        self.assertEqual(1, rc, out)
        self.assertIn("cannot confirm that babble-cdc-acm.service is disarmed", out)
        self.assertIn("The staged build was not replaced", out)
        self.assertEqual(self.old, self.staged_bytes())
        self.assertTrue(self.enabled())
        self.assertEqual([], self.calls("rmmod"))

    def test_a_disarm_that_cannot_be_confirmed_claims_nothing_about_the_unit(self):
        # disable works, but is-enabled gives no answer: the unit is in fact disabled now, so the
        # message must not say that it stays as it was (or armed).
        self.armed_and_live()
        self.set_state("is-enabled-fails")
        rc, out = self.stage(module_bytes("new"), yes=True)
        self.assertEqual(1, rc, out)
        self.assertIn("may or may not still be armed", out)
        self.assertNotIn("stay as they were", out)
        self.assertNotIn("stays armed", out)
        self.assertEqual(self.old, self.staged_bytes())
        self.assertEqual([], self.calls("insmod"))

    def test_rearm_does_not_test_while_the_unit_cannot_be_disarmed(self):
        self.armed_and_live()
        self.set_state("disable-fails")
        rc, out = self.run_script("store/stage-cdc-acm", "--rearm")
        self.assertEqual(1, rc, out)
        self.assertIn("cannot confirm that babble-cdc-acm.service is disarmed", out)
        self.assertEqual([], self.calls("insmod"))
        self.assertIn("sudo %s/store/stage-cdc-acm --rearm" % self.t, out)

    def test_rearm_with_the_port_in_use_names_the_rearm_command(self):
        self.armed_and_live()
        self.set_module("live", refcnt=1)
        rc, out = self.run_script("store/stage-cdc-acm", "--rearm")
        self.assertEqual(1, rc, out)
        self.assertIn("then run: sudo %s/store/stage-cdc-acm --rearm" % self.t, out)
        self.assertIn("fcam_overlay.py --stop", out)    # the stop that refuses a raw-mode instance
        self.assertNotIn("kill -TERM", out)
        self.assertIn("fcam_overlay.py --start", out)   # how to get the overlay back afterwards

    def test_rearm_refuses_a_module_staged_on_another_build_of_the_kernel(self):
        # a SteamOS update rebuilt the same kernel release (new uname -v): the unit skips the old
        # copy, so --rearm cannot work and must say to build and stage again
        self.armed_and_live()
        self.set_state("kernel-build", "#2 SMP PREEMPT_DYNAMIC rebuilt\n")
        rc, out = self.run_script("store/stage-cdc-acm", "--rearm")
        self.assertEqual(1, rc, out)
        self.assertIn("another build of kernel", out)
        self.assertIn("build-cdc-acm.sh --install", out)
        self.assertEqual([], self.calls("rmmod"))
        self.assertEqual([], self.disables())
        self.assertTrue(self.enabled())


class InstallerTests(Sandbox):
    def install(self, *args, stdin=b""):
        return self.run_script("src/root/install-cdc-acm-loader.sh", *args, stdin=stdin)

    def change(self, name):
        with open(self.path("src/root/" + name), "ab") as fh:
            fh.write(b"# changed\n")

    def installed_is_new(self, name="load-cdc-acm"):
        return self.read("src/root/" + name) == self.read("store/" + name)

    def armed(self):
        """Installed, a module staged and loaded through the unit, the unit armed."""
        rc, out = self.install()
        self.assertEqual(0, rc, out)
        self.stage_module()
        rc, out = self.run_script("store/stage-cdc-acm", "--rearm")
        self.assertEqual(0, rc, out)
        self.assertTrue(self.enabled())
        os.remove(self.path("calls"))   # count only what the update does

    def test_first_install_points_to_the_build_script_of_this_copy(self):
        rc, out = self.install()
        self.assertEqual(0, rc, out)
        self.assertRegex(out, r"next, as steamos: bash /\S*/src/build-cdc-acm\.sh --install")
        self.assertNotIn("~/Babble-Bridge", out)
        self.assertFalse(self.enabled())

    def test_an_unchanged_update_keeps_the_unit_armed(self):
        self.armed()
        rc, out = self.install()
        self.assertEqual(0, rc, out)
        self.assertIn("unchanged", out)
        self.assertTrue(self.enabled())
        self.assertEqual([], self.calls("rmmod"))

    def test_a_changed_boot_path_is_tested_through_the_unit_before_the_next_boot(self):
        self.armed()
        self.change("load-cdc-acm")
        rc, out = self.install()
        self.assertEqual(0, rc, out)
        self.assertIn("boot path changes", out)
        self.assertEqual(1, len(self.calls("rmmod")))
        self.assertEqual(1, len(self.calls("insmod")))
        self.assertTrue(self.enabled())
        self.assertTrue(self.installed_is_new())
        # disarmed before the new files went in, armed again only after they loaded the module
        calls = self.read("calls").splitlines()
        disarm = calls.index("systemctl disable --quiet babble-cdc-acm.service")
        self.assertLess(disarm, calls.index("systemctl daemon-reload"))
        self.assertLess(calls.index("insmod %s/store/by-kernel/%s/cdc-acm.ko" % (self.t, KVER)),
                        calls.index("systemctl enable --quiet babble-cdc-acm.service"))

    def test_a_changed_boot_path_with_the_port_in_use_changes_nothing(self):
        self.armed()
        self.set_module("live", refcnt=1)
        self.change("babble-cdc-acm.service")
        rc, out = self.install()
        self.assertEqual(1, rc, out)
        self.assertIn("in use", out)
        self.assertIn("Nothing was changed", out)
        self.assertTrue(self.enabled(), "the old, tested files stay armed")
        self.assertNotEqual(self.read("src/root/babble-cdc-acm.service"),
                            self.read("etc/systemd/system/babble-cdc-acm.service"), "nothing was replaced")
        self.assertEqual([], self.calls("rmmod"))
        self.assertEqual([], self.disables())
        # with the port free, running the same command again tests the new files (it does not take
        # them for an unchanged, armed boot path)
        self.set_module("live", refcnt=0)
        rc, out = self.install()
        self.assertEqual(0, rc, out)
        self.assertNotIn("unchanged", out)
        self.assertEqual(1, len(self.calls("rmmod")))
        self.assertEqual(1, len(self.calls("insmod")))
        self.assertTrue(self.enabled())
        self.assertEqual(self.read("src/root/babble-cdc-acm.service"),
                         self.read("etc/systemd/system/babble-cdc-acm.service"))

    def test_a_changed_boot_path_stays_disarmed_when_rmmod_fails_after_the_check(self):
        self.armed()
        self.set_state("rmmod-busy")   # the port is opened after the installer looked
        self.change("load-cdc-acm")
        rc, out = self.install()
        self.assertEqual(1, rc, out)
        self.assertIn("not verified yet", out)
        self.assertIn("sudo %s/store/stage-cdc-acm --rearm" % self.t, out)
        self.assertFalse(self.enabled(), "untested files must not be armed")
        self.assertEqual([], self.calls("insmod"))
        # running the installer again does not report the untested files as armed
        rc, out = self.install()
        self.assertEqual(0, rc, out)
        self.assertNotIn("stay armed", out)
        self.assertFalse(self.enabled())
        self.assertIn("next: sudo %s/store/stage-cdc-acm --rearm" % self.t, out)

    def test_a_changed_boot_path_is_not_replaced_when_the_unit_cannot_be_disarmed(self):
        self.armed()
        self.set_state("disable-fails")
        self.change("load-cdc-acm")
        rc, out = self.install()
        self.assertEqual(1, rc, out)
        self.assertIn("cannot confirm that babble-cdc-acm.service is disarmed", out)
        self.assertIn("may or may not still be armed", out)
        self.assertNotIn("stay armed", out)
        self.assertTrue(self.enabled())
        self.assertFalse(self.installed_is_new())
        self.assertEqual([], self.calls("rmmod"))

    def test_an_unknown_unit_state_counts_as_armed(self):
        # systemctl is-enabled gives no answer (D-Bus timeout) while the unit is armed and the boot
        # path changes: nothing may be replaced until a disarm is confirmed.
        for disable_works in (False, True):
            with self.subTest(disable_works=disable_works):
                self.setUp()
                self.armed()
                os.remove(self.path(self.WANTS))   # only systemctl could tell that it is armed
                self.set_state("is-enabled-fails")
                if not disable_works:
                    self.set_state("disable-fails")
                self.change("load-cdc-acm")
                self.change("babble-cdc-acm.service")
                rc, out = self.install()
                self.assertEqual(1, rc, out)
                self.assertIn("cannot confirm that babble-cdc-acm.service is disarmed", out)
                self.assertIn("may or may not still be armed", out)
                self.assertFalse(self.installed_is_new(), "the boot path must not be replaced")
                self.assertNotEqual(self.read("src/root/babble-cdc-acm.service"),
                                    self.read("etc/systemd/system/babble-cdc-acm.service"))
                self.assertEqual([], self.calls("insmod"))
                self.assertEqual(1, len(self.disables()))
                # a working disable leaves it disarmed (safe); a failed one leaves it as it was
                self.assertEqual(not disable_works, self.enabled())

    def test_a_state_systemctl_could_not_tell_once_is_tested_before_it_is_armed(self):
        # The first is-enabled fails (the unit is armed): the installer must still disarm, test the
        # new files through the unit and only then arm them, not copy them over an armed unit.
        self.armed()
        os.remove(self.path(self.WANTS))   # only systemctl could tell that it is armed
        self.set_state("is-enabled-fails", "once")
        self.change("load-cdc-acm")
        rc, out = self.install()
        self.assertEqual(0, rc, out)
        self.assertTrue(self.installed_is_new())
        calls = self.read("calls").splitlines()
        disarm = calls.index("systemctl disable --quiet babble-cdc-acm.service")
        self.assertLess(disarm, calls.index("systemctl daemon-reload"))
        self.assertEqual(1, len(self.calls("insmod")))
        self.assertLess(calls.index("insmod %s/store/by-kernel/%s/cdc-acm.ko" % (self.t, KVER)),
                        calls.index("systemctl enable --quiet babble-cdc-acm.service"))
        self.assertTrue(self.enabled())

    def test_a_wants_link_left_without_its_unit_file_is_removed_before_the_unit_is_installed(self):
        # The unit file was deleted by hand, its wants link was not: installing the unit file would
        # arm the untested new files through that link.
        self.armed()
        os.remove(self.path("etc/systemd/system/babble-cdc-acm.service"))
        os.remove(self.path("state/enabled"))
        os.remove(self.path(self.WANTS))
        try:   # a real dangling link where the platform allows it, a plain file otherwise
            os.symlink("../babble-cdc-acm.service", self.path(self.WANTS))
        except (OSError, NotImplementedError):
            self.write(self.WANTS, b"")
        self.assertTrue(self.wants_link())
        self.change("load-cdc-acm")
        rc, out = self.install()
        self.assertEqual(0, rc, out)
        self.assertIn("disarmed until the new files have loaded the module once", out)
        # the new files were tested through the unit before it was armed again
        calls = self.read("calls").splitlines()
        installed_unit = next(i for i, c in enumerate(calls)
                              if c.startswith("install ") and c.endswith("/etc/systemd/system/babble-cdc-acm.service"))
        self.assertLess(calls.index("systemctl disable --quiet babble-cdc-acm.service"), installed_unit)
        self.assertEqual(1, len(self.calls("insmod")))
        self.assertLess(calls.index("insmod %s/store/by-kernel/%s/cdc-acm.ko" % (self.t, KVER)),
                        calls.index("systemctl enable --quiet babble-cdc-acm.service"))
        self.assertTrue(self.installed_is_new())
        self.assertTrue(self.enabled())

    def test_a_wants_link_that_cannot_be_confirmed_gone_stops_before_the_unit_is_installed(self):
        # The same leftover link, but systemctl gives no answer: nothing is installed.
        self.armed()
        os.remove(self.path("etc/systemd/system/babble-cdc-acm.service"))
        os.remove(self.path("state/enabled"))
        self.set_state("is-enabled-fails")
        rc, out = self.install()
        self.assertEqual(1, rc, out)
        self.assertIn("cannot confirm", out)
        self.assertFalse(self.exists("etc/systemd/system/babble-cdc-acm.service"))
        self.assertFalse(self.wants_link())
        self.assertEqual([], self.calls("insmod"))

    def test_a_loader_update_after_a_same_release_rebuild_points_to_the_build(self):
        # uname -r is the same, uname -v is new: the staged copy is skipped at boot, so the next
        # step is building and staging again, never a --rearm that cannot work.
        self.armed()
        self.set_state("kernel-build", "#2 SMP PREEMPT_DYNAMIC rebuilt\n")
        self.change("load-cdc-acm")
        rc, out = self.install()
        self.assertEqual(0, rc, out)
        self.assertIn("belongs to another build of that kernel", out)
        self.assertRegex(out, r"next, as steamos: bash /\S*/src/build-cdc-acm\.sh --install")
        self.assertNotIn("--rearm", out)
        self.assertEqual([], self.calls("insmod"))
        self.assertFalse(self.enabled())

    def test_a_declined_staging_after_a_same_release_rebuild_points_to_the_build(self):
        self.armed()
        self.set_state("kernel-build", "#2 SMP PREEMPT_DYNAMIC rebuilt\n")
        self.change("load-cdc-acm")
        self.write("answer", b"n\n")
        rc, out = self.install("--headers", KPKGVER, stdin=module_bytes("new"))
        self.assertEqual(1, rc, out)
        self.assertIn("not verified yet", out)
        hint = out[out.index("To test and arm them:"):].splitlines()[0]
        self.assertIn("build-cdc-acm.sh --install", hint)
        self.assertNotIn("--rearm", hint)
        self.assertFalse(self.enabled())

    def test_a_first_install_whose_staging_stops_says_what_it_changed(self):
        # The first --install (with the 0.1.x rule still there) installs the loader and removes the
        # rule before staging; staging then refuses (port in use): "nothing was changed" is false.
        self.write("etc/udev/rules.d/90-babble-tracker.rules", b"# 0.1.x\n")
        self.set_module("live", refcnt=1)
        rc, out = self.install("--headers", KPKGVER, "--yes", stdin=module_bytes("new"))
        self.assertEqual(1, rc, out)
        self.assertNotIn("othing was changed", out)
        self.assertIn("staging stopped, but this run already installed", out)
        self.assertIn("/store/load-cdc-acm", out)
        self.assertIn("removed %s/etc/udev/rules.d/90-babble-tracker.rules" % self.t, out)
        self.assertFalse(self.exists("etc/udev/rules.d/90-babble-tracker.rules"))
        self.assertTrue(self.installed_is_new())
        self.assertFalse(self.enabled())
        # declined at the sha256 question: the same
        self.set_module(None)
        self.change("stage-cdc-acm")
        self.write("answer", b"n\n")
        rc, out = self.install("--headers", KPKGVER, stdin=module_bytes("new"))
        self.assertEqual(1, rc, out)
        self.assertIn("not confirmed", out)
        self.assertIn("staging stopped, but this run already installed %s/store/stage-cdc-acm" % self.t, out)

    def test_a_changed_boot_path_with_nothing_staged_disarms(self):
        rc, out = self.install()
        self.assertEqual(0, rc, out)
        self.set_state("enabled")   # e.g. armed for a kernel this slot no longer runs
        self.change("load-cdc-acm")
        rc, out = self.install()
        self.assertEqual(0, rc, out)
        self.assertFalse(self.enabled())
        self.assertIn("stays disarmed (disabled)", out)
        self.assertIn("--install", out)

    # -- --headers (what build-cdc-acm.sh --install runs)

    def test_headers_with_a_changed_boot_path_tests_the_new_files(self):
        self.armed()
        self.change("load-cdc-acm")
        new = module_bytes("new")
        rc, out = self.install("--headers", KPKGVER, "--yes", stdin=new)
        self.assertEqual(0, rc, out)
        self.assertEqual(new, self.staged_bytes())
        self.assertTrue(self.installed_is_new())
        self.assertEqual(1, len(self.calls("insmod")))
        self.assertTrue(self.enabled())

    def test_headers_with_a_changed_boot_path_and_the_port_in_use_changes_nothing(self):
        self.armed()
        old = self.staged_bytes()
        self.set_module("live", refcnt=1)
        self.change("load-cdc-acm")
        rc, out = self.install("--headers", KPKGVER, "--yes", stdin=module_bytes("new"))
        self.assertEqual(1, rc, out)
        self.assertIn("Nothing was changed", out)
        self.assertTrue(self.enabled())
        self.assertFalse(self.installed_is_new())
        self.assertEqual(old, self.staged_bytes())
        self.assertEqual([], self.disables())

    def test_a_declined_staging_does_not_leave_new_files_armed(self):
        # The operator answers N at the sha256 question after the boot path changed: the new loader
        # files are in place, so the unit must not stay armed with them untested.
        self.armed()
        old = self.staged_bytes()
        self.change("load-cdc-acm")
        self.write("answer", b"n\n")
        rc, out = self.install("--headers", KPKGVER, stdin=module_bytes("new"))
        self.assertEqual(1, rc, out)
        self.assertIn("not confirmed", out)
        self.assertIn("not verified yet", out)
        self.assertFalse(self.enabled())
        self.assertEqual(old, self.staged_bytes())
        self.assertEqual([], self.calls("insmod"))
        # the printed next step tests the new files with the staged module and arms them
        self.assertIn("sudo %s/store/stage-cdc-acm --rearm" % self.t, out)
        rc, out = self.run_script("store/stage-cdc-acm", "--rearm")
        self.assertEqual(0, rc, out)
        self.assertTrue(self.enabled())
        self.assertEqual(1, len(self.calls("insmod")))

    def test_headers_with_an_unchanged_boot_path_leaves_the_arming_to_staging(self):
        self.armed()
        self.write("answer", b"n\n")
        rc, out = self.install("--headers", KPKGVER, stdin=module_bytes("new"))
        self.assertEqual(1, rc, out)
        self.assertIn("not confirmed; the staged build and babble-cdc-acm.service were left as they were", out)
        self.assertNotIn("this run already", out)   # the installed files were already these
        self.assertTrue(self.enabled(), "a declined staging keeps the tested build armed")
        self.assertEqual([], self.disables())

    # -- --uninstall

    def test_uninstall_reprocesses_existing_ports_without_the_rule(self):
        self.armed()
        rc, out = self.install("--uninstall")
        self.assertEqual(0, rc, out)
        self.assertFalse(self.exists("etc/udev/rules.d/70-babble-tracker.rules"))
        self.assertFalse(self.exists("store"))
        udevadm = self.calls("udevadm")
        self.assertEqual("udevadm control --reload", udevadm[0])
        self.assertIn("udevadm trigger --action=change --subsystem-match=tty --sysname-match=ttyACM*", udevadm[1:])
        self.assertIn("keeps the access it was already granted", out)


class BuildScriptTests(Sandbox):
    """build-cdc-acm.sh as steamos, in the sandbox: --check, and the refusal to build while the 0.1.x
    udev rule is installed."""

    def setUp(self):
        super().setUp()
        with open(os.path.join(ROOT, "build-cdc-acm.sh"), "rb") as fh:
            text = self.sandboxed(fh.read().decode("utf-8"))
        text = text.replace("$HOME/", self.t + "/home/")
        text = text.replace("set -euo pipefail\n", "set -euo pipefail\nexport PATH=%s/stubs:$PATH\n" % self.t, 1)
        self.write("src/build-cdc-acm.sh", text.encode())
        self.set_state("uid", "1000")
        os.makedirs(self.path("home/cdc-acm"))

    def healthy(self):
        """Built, staged, loaded and armed: --check has nothing to report."""
        self.install_loader()
        self.stage_module()
        self.write("home/cdc-acm/cdc-acm.ko", module_bytes("built"))
        self.write("home/cdc-acm/cdc-acm.headers", (KPKGVER + "\n").encode())
        self.set_state("enabled")
        self.set_state("active")
        self.set_module("live")

    def old_rule(self):
        self.write("etc/udev/rules.d/90-babble-tracker.rules", b"# 0.1.x\n")

    def test_check_passes_on_a_healthy_headset(self):
        self.healthy()
        rc, out = self.run_script("src/build-cdc-acm.sh", "--check")
        self.assertEqual(0, rc, out)
        self.assertNotIn("90-babble-tracker.rules", out)

    def test_check_fails_while_the_old_rule_is_installed(self):
        self.healthy()
        self.old_rule()
        rc, out = self.run_script("src/build-cdc-acm.sh", "--check")
        self.assertEqual(1, rc, out)
        self.assertIn("90-babble-tracker.rules", out)
        self.assertIn("root/install-cdc-acm-loader.sh", out)

    def test_no_build_while_the_old_rule_is_installed(self):
        self.old_rule()
        rc, out = self.run_script("src/build-cdc-acm.sh")
        self.assertEqual(1, rc, out)
        self.assertIn("not building", out)
        self.assertFalse(self.exists("home/cdc-acm/cdc-acm.ko"))


if __name__ == "__main__":
    unittest.main()
