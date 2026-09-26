"""Static checks for the root side of the cdc-acm loader (root/, build-cdc-acm.sh, CI, line endings).
Standard library only; nothing here needs root, a headset or bash:

    python3 -m unittest discover -s tests -v

The loader's security rests on a few properties that are easy to lose in an edit: the unit keeps
CAP_SYS_MODULE and hides /home, nothing loads a module from a user-writable path, the root scripts
pin PATH, and every script is checked out with LF endings (core.autocrlf=true on Windows).
"""
import os
import re
import shutil
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROOT_DIR = os.path.join(ROOT, "root")
# bash for the syntax check: on Linux the one in PATH; elsewhere only an explicit BABBLE_TEST_BASH
# (for example Git for Windows' bash; "bash" in PATH on Windows may be the WSL launcher).
BASH = os.environ.get("BABBLE_TEST_BASH") or (shutil.which("bash") if sys.platform.startswith("linux") else None)
UNIT_PATH = os.path.join(ROOT_DIR, "babble-cdc-acm.service")
LOADER = "/var/lib/babble-tracker/load-cdc-acm"

SKIP_DIRS = {".git", "__pycache__"}
SKIP_SUFFIXES = (".png", ".pyc", ".log", ".log.old", ".lock")
OLD_RULE = "/etc/udev/rules.d/90-babble-tracker.rules"


def read_text(path):
    with open(path, encoding="utf-8", newline="") as f:
        return f.read()


def read_bytes(path):
    with open(path, "rb") as f:
        return f.read()


def same_path(a, b):
    return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))


def git_files():
    """The repository's files as git sees them (tracked, plus untracked ones .gitignore does not
    exclude), or None outside a git checkout of this repository (a release tarball, no git)."""
    try:
        top = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=ROOT, capture_output=True,
                             text=True, check=False)
        if top.returncode != 0 or not same_path(top.stdout.strip(), ROOT):
            return None
        listed = subprocess.run(["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
                                cwd=ROOT, capture_output=True, check=False)
    except OSError:
        return None
    if listed.returncode != 0:
        return None
    names = {name.decode("utf-8") for name in listed.stdout.split(b"\0") if name}
    # --cached still lists a tracked file that was deleted in the working tree
    return sorted(name for name in names if os.path.isfile(os.path.join(ROOT, *name.split("/"))))


def walk_files():
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        for name in sorted(filenames):
            yield os.path.relpath(os.path.join(dirpath, name), ROOT).replace(os.sep, "/")


def repo_files():
    """Every file of the repository that could be a text file (no .git, caches or binaries). In a git
    checkout that is what git tracks or would add: build output such as dist/ or a local venv is
    not part of the repository."""
    names = git_files()
    for rel in walk_files() if names is None else names:
        parts = rel.split("/")
        if SKIP_DIRS.intersection(parts[:-1]) or rel.endswith(SKIP_SUFFIXES):
            continue
        data = read_bytes(os.path.join(ROOT, *parts))
        if b"\0" in data:
            continue
        yield rel, data


def is_shell_script(rel, data):
    if rel.endswith(".sh"):
        return True
    first = data.split(b"\n", 1)[0]
    return re.match(rb"#!\s*\S*(?:/|\s)(?:ba)?sh\b", first) is not None


def code_lines(text):
    """Shell or rule lines without full-line comments."""
    return [line for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")]


def parse_unit(path):
    """systemd unit file -> {section: [(key, value), ...]} (keys may repeat)."""
    sections, current = {}, None
    for line in read_text(path).splitlines():
        line = line.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("[") and line.endswith("]"):
            current = sections.setdefault(line[1:-1], [])
            continue
        if line.endswith("\\"):
            raise ValueError("line continuation not expected: %r" % line)
        key, sep, value = line.partition("=")
        if not sep or current is None:
            raise ValueError("not a unit file line: %r" % line)
        current.append((key.strip(), value.strip()))
    return sections


def values(section, key):
    return [v for k, v in section if k == key]


def shell_function_body(text, name):
    match = re.search(r"^%s\(\)\s*\{(.*?)^\}" % re.escape(name), text, re.M | re.S)
    if match is None:
        raise AssertionError("function %s() not found" % name)
    return match.group(1)


def without_heredocs(text):
    """Shell text with the bodies of here-documents removed (they are data, not commands)."""
    out, end = [], None
    for line in text.splitlines():
        if end is not None:
            if line.strip() == end:
                end = None
            continue
        out.append(line)
        match = re.search(r"<<-?\s*(['\"]?)(\w+)\1", line)
        if match:
            end = match.group(2)
    return "\n".join(out)


def mask_quotes(line):
    """The line with the contents of quoted strings replaced by '_' (same length, same offsets)."""
    out, quote = [], None
    for ch in line:
        if quote:
            out.append(ch if ch == quote else "_")
            if ch == quote:
                quote = None
        else:
            out.append(ch)
            if ch in "'\"":
                quote = ch
    return "".join(out)


# sudo in command position: at the start, after ; & | ( ! or a keyword, optionally as a path.
SUDO_CALL = re.compile(r"(?:^|[;&|(!]|\b(?:exec|then|do|else|time|command))\s*((?:\S*/)?sudo)\b")


def sudo_calls(text):
    """(line, text from the sudo word on) for every sudo that a shell script would run."""
    calls = []
    for line in code_lines(without_heredocs(text)):
        masked = mask_quotes(line)
        for match in SUDO_CALL.finditer(masked):
            calls.append((line.strip(), line[match.start(1):]))
    return calls


class UnitFileTests(unittest.TestCase):
    def setUp(self):
        self.unit = parse_unit(UNIT_PATH)
        self.service = self.unit["Service"]

    def single(self, key, section="Service"):
        found = values(self.unit[section], key)
        self.assertEqual(1, len(found), "%s= in [%s]: %r" % (key, section, found))
        return found[0]

    def test_sections(self):
        self.assertEqual({"Unit", "Service", "Install"}, set(self.unit))

    def test_oneshot_that_stays_active(self):
        self.assertEqual("oneshot", self.single("Type"))
        self.assertEqual("yes", self.single("RemainAfterExit"))
        self.assertEqual("multi-user.target", self.single("WantedBy", "Install"))

    def test_runs_the_root_owned_loader_behind_an_exec_condition(self):
        self.assertEqual(LOADER + " --check", self.single("ExecCondition"))
        self.assertEqual(LOADER, self.single("ExecStart"))
        conditions = values(self.unit["Unit"], "ConditionPathExists")
        self.assertIn("!/sys/module/cdc_acm", conditions)
        self.assertIn(LOADER, conditions)
        self.assertIn("/var/lib/babble-tracker", values(self.unit["Unit"], "RequiresMountsFor"))

    def test_runs_as_root_with_only_cap_sys_module(self):
        self.assertEqual([], values(self.service, "User"))
        self.assertEqual([], values(self.service, "DynamicUser"))
        self.assertEqual("CAP_SYS_MODULE", self.single("CapabilityBoundingSet"))
        self.assertEqual([], values(self.service, "AmbientCapabilities"))
        self.assertEqual("yes", self.single("NoNewPrivileges"))

    def test_protect_kernel_modules_is_not_set(self):
        # ProtectKernelModules=yes drops CAP_SYS_MODULE, and the unit could never load anything.
        for name, section in self.unit.items():
            self.assertEqual([], values(section, "ProtectKernelModules"), "[%s]" % name)

    def test_home_is_hidden_and_the_system_read_only(self):
        self.assertEqual("yes", self.single("ProtectHome"))
        self.assertEqual("strict", self.single("ProtectSystem"))
        self.assertEqual([], values(self.service, "ReadWritePaths"))
        self.assertEqual([], values(self.service, "BindPaths"))
        self.assertEqual("babble-tracker/state", self.single("StateDirectory"))
        self.assertEqual("0700", self.single("StateDirectoryMode"))

    def test_syscall_filter_allows_module_loading(self):
        groups = self.single("SystemCallFilter").split()
        self.assertIn("@module", groups)
        self.assertNotIn("~@module", groups)

    def test_no_user_writable_path_anywhere_in_the_unit(self):
        for name, section in self.unit.items():
            for key, value in section:
                self.assertNotRegex(value, r"/home|~/|/tmp|/run/user", "%s= in [%s]" % (key, name))


class ModuleLoadPathTests(unittest.TestCase):
    # insmod in command position (not "echo ... ran insmod on ..."), with or without a path.
    INSMOD = re.compile(r"(?:^|[;&|(]|\b(?:exec|sudo|then|do|else))\s*(?:/usr)?(?:/s?bin/)?insmod\s+(\S+)")

    def test_no_insmod_of_the_user_build(self):
        user_build = "/home/" + "steamos/cdc-acm"   # split so this file does not match itself
        user_path = re.compile(r"""\binsmod\s+["']?(?:/home/|~/|\$\{?HOME\b|\$\{?OUT\b)""")
        for rel, data in repo_files():
            text = data.decode("utf-8", "replace")
            for line in text.splitlines():
                self.assertNotRegex(line, user_path, rel)
                if "insmod" in line:
                    self.assertNotIn(user_build, line, rel)

    def test_the_only_insmod_is_the_loaders_fixed_path(self):
        calls = []
        for rel, data in repo_files():
            if rel.endswith(".md") or not (is_shell_script(rel, data) or rel.startswith("root/")):
                continue
            for line in code_lines(data.decode("utf-8")):
                calls += [(rel, m.group(1)) for m in self.INSMOD.finditer(line)]
        self.assertEqual([("root/load-cdc-acm", '"$KO"')], calls)
        loader = read_text(os.path.join(ROOT_DIR, "load-cdc-acm"))
        self.assertRegex(loader, r'(?m)^STORE=/var/lib/babble-tracker$')
        self.assertRegex(loader, r'(?m)^DIR="\$STORE/by-kernel/\$KVER"$')
        self.assertRegex(loader, r'(?m)^KO="\$DIR/cdc-acm\.ko"$')

    def test_udev_rules_run_nothing(self):
        rules = [rel for rel, _ in repo_files() if rel.endswith(".rules")]
        self.assertEqual(["root/70-babble-tracker.rules"], rules)
        for rel in rules:
            for line in code_lines(read_text(os.path.join(ROOT, rel))):
                self.assertNotRegex(line, r"\b(RUN|PROGRAM|IMPORT\{program\})\b", rel)

    def test_udev_rule_tags_uaccess_before_seat_late(self):
        # 73-seat-late.rules applies the ACL; a uaccess tag set by a later file has no effect.
        name = "70-babble-tracker.rules"
        self.assertLess(int(name.split("-", 1)[0]), 73)
        rule = " ".join(code_lines(read_text(os.path.join(ROOT_DIR, name))))
        self.assertIn('TAG+="uaccess"', rule)
        self.assertIn('SUBSYSTEM=="tty"', rule)
        self.assertIn('ATTRS{idVendor}=="303a"', rule)

    def test_old_rule_is_gone(self):
        # Only ever named by its installed path, to detect and remove it; nothing installs it.
        self.assertFalse(os.path.exists(os.path.join(ROOT, "90-babble-tracker.rules")))
        name = "90-babble-tracker.rules"
        for rel, data in repo_files():
            if rel.startswith("tests/"):
                continue
            text = data.decode("utf-8", "replace")
            for match in re.finditer(re.escape(name), text):
                start = match.start()
                self.assertTrue(text[:start].endswith("/etc/udev/rules.d/") or
                                text[:start].endswith("$OLD_RULE") or
                                text[:start].rstrip().endswith("OLD_RULE=/etc/udev/rules.d/"),
                                "%s: %r" % (rel, text[max(0, start - 60):start + len(name)]))
            for line in text.splitlines():
                self.assertNotRegex(line, r"\b(?:install|cp|ln)\b\s[^#]*" + re.escape(name), rel)

    def test_every_installer_warns_about_the_old_rule(self):
        # 0.1.x told operators to install it; an upgrade that never runs the loader's installer
        # (the port keeps working) must still say that it lets steamos programs load kernel code.
        for rel in ("build-cdc-acm.sh", "install-headset.sh", "overlay/install-overlay.sh"):
            text = read_text(os.path.join(ROOT, rel))
            self.assertRegex(text, r"(?m)^\s*OLD_RULE=%s\b" % re.escape(OLD_RULE), rel)
            self.assertIn('[ -e "$OLD_RULE" ] || [ -L "$OLD_RULE" ]', text, rel)
            self.assertIn("install-cdc-acm-loader.sh", text, rel)
            self.assertIn("sudo rm $OLD_RULE && sudo udevadm control --reload", text, rel)
        build = read_text(os.path.join(ROOT, "build-cdc-acm.sh"))
        check = build[build.index('if [ "$CHECK" = 1 ]; then'):build.index('exit "$rc"')]
        self.assertRegex(check, r"if old_rule_present; then\n[^\n]*\n\s*rc=1")   # --check fails
        # --install goes through the installer (which removes the rule) while the rule is there
        self.assertIn('if [ -x "$STORE/stage-cdc-acm" ] && ! old_rule_present; then', build)


class RootScriptTests(unittest.TestCase):
    SCRIPTS = ("load-cdc-acm", "stage-cdc-acm", "install-cdc-acm-loader.sh")

    def test_root_scripts_are_bash_and_pin_path(self):
        for name in self.SCRIPTS:
            text = read_text(os.path.join(ROOT_DIR, name))
            self.assertTrue(text.startswith("#!/bin/bash\n"), name)
            self.assertRegex(text, r"(?m)^export PATH=/usr/bin LC_ALL=C$", name)
            self.assertRegex(text, r"(?m)^set -(e?)uo pipefail$", name)

    def test_loader_exit_codes_match_exec_condition(self):
        # ExecCondition: 1..254 skips the unit, 255 fails it.
        text = read_text(os.path.join(ROOT_DIR, "load-cdc-acm"))
        self.assertIn("exit 255", shell_function_body(text, "refuse"))
        skip = shell_function_body(text, "skip")
        self.assertRegex(skip, r'if \[ "\$CHECK" = 1 \]; then exit 1; fi')
        self.assertNotRegex(text, r"(?m)^set -e")   # exit codes are chosen, never left to set -e

    def test_stage_arms_only_after_a_live_load_through_the_unit(self):
        text = read_text(os.path.join(ROOT_DIR, "stage-cdc-acm"))
        body = shell_function_body(text, "load_and_arm")
        rmmod = body.index("rmmod cdc_acm")
        disable = body.index('\n    disarm "')
        restart = body.index('systemctl restart "$UNIT"')
        active = body.index('systemctl is-active --quiet "$UNIT"')
        enable = body.rindex('systemctl enable --quiet "$UNIT"')
        # Disarmed only once cdc_acm is unloaded: a busy port leaves an armed unit armed.
        self.assertLess(rmmod, disable)
        self.assertLess(disable, restart)
        self.assertLess(restart, active)
        self.assertLess(active, enable)
        # "Loaded" is initstate live: a module whose init crashed keeps its /sys/module directory.
        self.assertIn('[ "$(module_state)" != live ]', body[restart:enable])
        # The only other enable restores the arming of the very build that was armed before.
        early = body.index('systemctl enable --quiet "$UNIT"')
        if early != enable:
            self.assertRegex(body[:early], r'if \[ "\$same" = 1 \]; then\s*$')
        # Every disarm is checked: a failed disable must not leave an untested build armed.
        code = "\n".join(code_lines(text))
        self.assertEqual(1, code.count('systemctl disable --quiet "$UNIT"'))
        disarm = shell_function_body(text, "disarm")
        self.assertIn('systemctl disable --quiet "$UNIT"', disarm)
        self.assertRegex(disarm, r'\[ "\$state" = disabled \] \|\|\s+die ')
        # The module is read from standard input into a private directory, never from a path.
        self.assertIn('head -c $((MAX_BYTES + 1)) > "$ko"', text)
        self.assertIn('mktemp -d "$STORE/by-kernel/.stage.XXXXXX"', text)
        self.assertNotRegex(text, r"(?m)^[^#]*\binsmod\b")

    def test_stage_decides_before_it_changes_anything(self):
        text = read_text(os.path.join(ROOT_DIR, "stage-cdc-acm"))
        main = text[text.index("\nrequire_unloadable\n"):]
        # in use: stop before the module is read or the unit touched; then the sha256, then the
        # question, and only then disarm and swap
        read = main.index('head -c $((MAX_BYTES + 1))')
        shown = main.index('echo "  sha256   $sum')
        asked = main.index("\nconfirm\n")
        disarm = main.index('\ndisarm "')
        swap = main.index('mv -T -- "$work" "$DIR"')
        self.assertLess(read, shown)
        self.assertLess(shown, asked)
        self.assertLess(asked, disarm)
        self.assertLess(disarm, swap)
        confirm = shell_function_body(text, "confirm")
        self.assertIn("</dev/tty", confirm)         # standard input holds the module
        self.assertIn('[ "$YES" = 0 ] || return 0', confirm)
        self.assertIn("refcnt", shell_function_body(text, "require_unloadable"))

    def test_loader_keeps_the_crash_marker_when_insmod_is_killed(self):
        text = read_text(os.path.join(ROOT_DIR, "load-cdc-acm"))
        after = text[text.index('insmod "$KO"'):]
        killed = after.index('if [ "$rc" -ge 128 ]; then')
        removed = after.index('rm -f -- "$MARK"')
        self.assertLess(killed, removed)
        self.assertIn("refuse", after[killed:removed])
        # success means initstate live, never just an existing /sys/module/cdc_acm
        self.assertIn('if [ "$state" = live ]; then', after[removed:])
        for name in ("load-cdc-acm", "stage-cdc-acm"):
            code = "\n".join(code_lines(read_text(os.path.join(ROOT_DIR, name))))
            self.assertIn("/sys/module/cdc_acm/initstate", code, name)
            self.assertNotRegex(code, r"(?:if|\|\||&&) \[ -d /sys/module/cdc_acm \]", name)

    def test_installer_disarms_a_changed_boot_path_before_it_replaces_it(self):
        text = read_text(os.path.join(ROOT_DIR, "install-cdc-acm-loader.sh"))
        main = text[text.index("\nsame_file() {"):]
        refused = main.index('if [ "$state" = live ] && [ "$refcnt" != 0 ]; then')
        disabled = main.index('systemctl disable --quiet "$UNIT"')
        unlinked = main.index('rm -f -- "$WANTS"')
        checked = main.index('[ "$disarm_ok" = 1 ] ||')
        copied = main.index('install -o root -g root -m 0755 "$here/load-cdc-acm"')
        staged = main.index('"$STORE/stage-cdc-acm" "${stage_args[@]}"')
        self.assertLess(refused, disabled)   # a busy port changes nothing
        self.assertLess(disabled, unlinked)
        self.assertLess(unlinked, checked)
        self.assertLess(checked, copied)
        self.assertLess(copied, staged)
        # confirmed disarmed: no link, and "disabled" ("not-found" only when there was no unit file)
        self.assertIn('if [ ! -e "$WANTS" ] && [ ! -L "$WANTS" ]; then', main[unlinked:checked])
        self.assertIn('disabled) disarm_ok=1 ;;', main[unlinked:checked])
        self.assertIn('not-found) [ "$unit_present" = 1 ] || disarm_ok=1 ;;', main[unlinked:checked])
        # armed unless known not to be: a wants link, or an installed unit that is not "disabled"
        decided = main[:main.index("boot_changed=0")]
        self.assertIn('if [ -e "$WANTS" ] || [ -L "$WANTS" ]; then\n    was_enabled=1', decided)
        self.assertIn('elif [ "$unit_present" = 1 ] && [ "$was_state" != disabled ]; then\n    was_enabled=1',
                      decided)
        self.assertNotIn('= enabled ]', decided)
        # --rearm only for a module staged on exactly the running kernel build
        self.assertIn('[ "$boot_changed" = 1 ] && staged_for_this_build; then\n    stage_args=(--rearm)', main)
        self.assertIn('= "$(uname -v)" ]', shell_function_body(text, "staged_for_this_build"))
        # stage-cdc-acm is the only thing that arms the unit again; the installer never enables it
        self.assertNotIn("systemctl enable", "\n".join(code_lines(text)))
        self.assertNotRegex(text, r'(?m)^\s*exec "\$STORE/stage-cdc-acm"')   # the installer reports afterwards

    def test_installer_ships_every_root_file(self):
        text = read_text(os.path.join(ROOT_DIR, "install-cdc-acm-loader.sh"))
        loop = re.search(r"(?m)^for f in (.*); do$", text)
        self.assertIsNotNone(loop)
        shipped = set(loop.group(1).split())
        present = set(os.listdir(ROOT_DIR)) - {"install-cdc-acm-loader.sh"}
        self.assertEqual(present, shipped)

    def test_keep_list_covers_every_etc_file(self):
        keep = code_lines(read_text(os.path.join(ROOT_DIR, "babble-tracker.atomic-update.conf")))
        self.assertEqual(sorted(keep), [
            "/etc/atomic-update.conf.d/babble-tracker.conf",
            "/etc/systemd/system/babble-cdc-acm.service",
            "/etc/systemd/system/multi-user.target.wants/babble-cdc-acm.service",
            "/etc/udev/rules.d/70-babble-tracker.rules",
        ])

    def test_sudo_scanner_finds_calls_in_any_command_position(self):
        script = "\n".join([
            'echo "run: sudo stage-cdc-acm --rearm; or sudo x"',       # text, not a call
            "cat <<MSG",
            "    sudo /usr/bin/bash $here/x.sh",                        # here-document: data
            "MSG",
            "sudo stage-cdc-acm --rearm",
            'if true; then sudo "$STORE/stage-cdc-acm" --rearm; fi',
            "x && /usr/local/bin/sudo -v",
            "exec /usr/bin/sudo /usr/bin/bash /x",
        ])
        self.assertEqual(["sudo stage-cdc-acm --rearm", 'sudo "$STORE/stage-cdc-acm" --rearm; fi',
                          "/usr/local/bin/sudo -v", "/usr/bin/sudo /usr/bin/bash /x"],
                         [call for _, call in sudo_calls(script)])

    def test_scripts_run_only_usr_bin_sudo_with_absolute_targets(self):
        # sudo keeps the user's PATH on SteamOS: a relative sudo or target could be anything.
        store = "/var/lib/babble-tracker"
        found = []
        for rel, data in repo_files():
            if not is_shell_script(rel, data):
                continue
            for line, call in sudo_calls(data.decode("utf-8")):
                found.append((rel, line))
                self.assertRegex(call, r'^/usr/bin/sudo (?:/|"\$STORE/)', "%s: %s" % (rel, line))
                if '"$STORE/' in call:
                    self.assertRegex(data.decode("utf-8"), r"(?m)^STORE=%s\s" % re.escape(store), rel)
        build = [line for rel, line in found if rel == "build-cdc-acm.sh"]
        self.assertEqual(2, len(build), found)
        for line in build:
            self.assertIn('< "$OUT/cdc-acm.ko"', line)

    def test_hints_do_not_assume_a_checkout_in_home(self):
        # A release tarball unpacks to ~/babble-bridge-X.Y.Z: printed hints name the copy they
        # come from, or no fixed directory at all.
        for rel in ("root/load-cdc-acm", "root/stage-cdc-acm", "root/install-cdc-acm-loader.sh",
                    "root/babble-cdc-acm.service", "build-cdc-acm.sh", "overlay/tools/gl_exit_test.sh",
                    "overlay/install-overlay.sh"):
            for line in code_lines(read_text(os.path.join(ROOT, rel))):
                self.assertNotIn("~/Babble-Bridge", line, rel)
        # The README itself installs into one place, ~/Babble-Bridge, and uses it throughout.
        readme = read_text(os.path.join(ROOT, "README.md"))
        self.assertIn("bash ~/Babble-Bridge/build-cdc-acm.sh --install'", readme)
        self.assertNotIn("~/babble-bridge-", readme)


class RepositoryHygieneTests(unittest.TestCase):
    def test_every_text_file_has_lf_line_endings(self):
        crlf = [rel for rel, data in repo_files() if b"\r" in data]
        self.assertEqual([], crlf)

    def test_gitattributes_forces_lf(self):
        text = read_text(os.path.join(ROOT, ".gitattributes"))
        self.assertRegex(text, r"(?m)^\* text=auto eol=lf$")
        self.assertRegex(text, r"(?m)^root/\* text eol=lf$")
        self.assertRegex(text, r"(?m)^\*\.png binary$")

    def ci_step(self, name):
        text = read_text(os.path.join(ROOT, ".forgejo", "workflows", "ci.yml"))
        match = re.search(r"(?ms)^      - name: %s\n(.*?)(?=^      - name: |\Z)" % re.escape(name), text)
        self.assertIsNotNone(match, name)
        return match.group(1)

    def test_ci_checks_the_syntax_of_every_shell_script(self):
        step = self.ci_step("Shell syntax")
        self.assertIn('bash -n "$f"', step)   # one file per bash -n: extra arguments are not checked
        listed = set(re.search(r"for f in (.*?); do", step, re.S).group(1).replace("\\", " ").split())
        scripts = {rel for rel, data in repo_files() if is_shell_script(rel, data)}
        self.assertEqual(scripts, listed)

    def test_release_tarball_ships_root(self):
        step = self.ci_step("Build tarball")
        copy = re.search(r"cp -r (.*?)\"dist/", step, re.S).group(1).replace("\\", " ").split()
        self.assertIn("root", copy)
        self.assertIn("build-cdc-acm.sh", copy)
        for rel in copy:
            self.assertTrue(os.path.exists(os.path.join(ROOT, rel)), rel)

    @unittest.skipUnless(BASH, "needs bash (Linux, or BABBLE_TEST_BASH)")
    def test_shell_scripts_parse(self):
        for rel, data in repo_files():
            if is_shell_script(rel, data):
                result = subprocess.run([BASH, "-n", os.path.join(ROOT, rel)],
                                        capture_output=True, text=True, check=False)
                self.assertEqual(0, result.returncode, "%s: %s" % (rel, result.stderr))


if __name__ == "__main__":
    unittest.main()
