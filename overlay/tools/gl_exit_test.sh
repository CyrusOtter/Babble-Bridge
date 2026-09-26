#!/usr/bin/env bash
# gl_exit_test.sh - on-headset exit test for the FCAM overlay's GL texture path (blocks A-D and F).
#
# Run ON the Steam Frame as steamos, with SteamVR running and the headset awake, from the copy to be
# tested (a checkout such as ~/Babble-Bridge, or ~/babble-bridge-X.Y.Z from the release tarball), in
# a terminal: over ssh use ssh -t (blocks B2 and F ask you to open the tab; without a terminal the
# script refuses to start unless --no-prompt skips them, and then the result cannot be PASS).
#
# Keep the headset awake for the whole run (about 25 minutes, mostly with the dashboard closed):
# wear it, or cover the proximity sensor, or turn auto-sleep off. A compositor in standby answers
# uploads with RequestFailed: such a run is not counted and is repeated, and after 3 of them in a
# row the test stops unfinished. SteamVR may also shut down in standby: new compositor pids with no
# crash in the journal while SteamVR stopped, restarted or asked the overlay to quit (during a run,
# or between runs, e.g. while B2 or F waits for Enter) stop the test unfinished too. Neither is
# counted as a GL failure.
# The overlay is copied to ~/fcam-gltest and run from there with its own lock, log directory and an
# ephemeral UDP port on 127.0.0.1; it is not registered with SteamVR and does not open the tracker.
# The installed overlay (~/fcam) is stopped first and started again at the end; if its texture mode
# could be raw (stopping it would crash the compositor), the script refuses to start. Raw mode
# (SetOverlayRaw) is never used.
#
#   bash overlay/tools/gl_exit_test.sh              # blocks A-D, about 25 minutes
#   bash overlay/tools/gl_exit_test.sh --blocks A   # only the harness check
#   bash overlay/tools/gl_exit_test.sh --blocks F   # smoothness and resources, with the tab open
#
# Blocks (every run uploads at 25 Hz, FCAM_DEBUG_FORCE_UPLOAD_HZ=25, also with the tab hidden;
# file mode still waits 2 s between uploads):
#   A   file mode, SIGTERM after 10 s                          3 runs   checks the harness itself
#   B1  gl, SIGTERM after 20 s, dashboard closed              20 runs
#   B2  gl, SIGTERM 10 s after the FCAM Bridge tab opened      5 runs   asks you to open the tab; a run
#                                                                       where the tab was not open when
#                                                                       the signal went out is repeated
#   C   gl, SIGKILL after 20 s (no teardown at all)           10 runs
#   D   gl, reconnect every 10 s for 120 s, then SIGTERM       1 run    GL context kept across sessions
#   F   tab open: gl for 120 s, then file mode for 60 s        2 runs   only with --blocks F; samples fds,
#       (FCAM_DEBUG_FILE_INTERVAL=0.5: it blinks)                       dmabuf fds and RSS every 10 s
# B2 and F ask for the open tab: they need a terminal and are skipped with --no-prompt.
#
# A run fails when vrcompositor or gamescope has a different pid afterwards, the journal reports one
# of them terminating abnormally, the overlay never confirms its texture, or it does not exit
# cleanly. The script stops at the first failure. Pass criterion (auto, the default mode, uses GL):
# every run of blocks A-D passes, which is 36 GL exits (B1 20 + B2 5 + C 10 + D 1), 10 of them
# SIGKILL; no crash in 36 exits puts the crash rate below about 8 % (95 % confidence, rule of three).
# Block E (SteamVR restart, standby) is manual, and in F you judge the smoothness; see README.md.
# Exit status: 0 no failure, 1 FAIL (or interrupted), 3 not finished (standby, SteamVR restart).
set -uo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
src="$(cd "$here/.." && pwd)"
bridge="$(cd "$src/.." && pwd)/fcam_bridge.py"
work="${FCAM_GLTEST_DIR:-$HOME/fcam-gltest}"
installed="$HOME/fcam"
uid="$(id -u)"
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$uid}"

MATRIX="A B1 B2 C D"   # the blocks the pass criterion needs, all of them complete
blocks="A,B1,B2,C,D"
prompt=1
while [ $# -gt 0 ]; do
    case "$1" in
        --blocks) blocks="${2:?--blocks needs a list such as A,B1,C}"; shift 2 ;;
        --no-prompt) prompt=0; shift ;;
        -h|--help) sed -n '2,/^[^#]/{/^#/p}' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "unknown option $1 (see --help)" >&2; exit 2 ;;
    esac
done

# -- state -----------------------------------------------------------------------------------------

T0="$(date '+%F %T')"
VRC=""
GS=""
INV0=""               # steamvr.service InvocationID at the start ("" when it cannot be told)
SVC0=""               # steamvr.service state at the start (only "active" is used as evidence)
checked_until=""      # the journal was checked for crashes up to here
journal_ok=1          # 0: the system journal is not readable (weaker crash evidence)
P=""                  # pid of the running test instance
RC=""                 # exit status of the last test instance
failure=""
unfinished=""         # why the test stopped without a result (standby, SteamVR restart)
standby_runs=0        # runs in a row that were not counted because of standby
started=0
gl_exits=0            # passed gl runs of blocks A-D
gl_kills=0
installed_was_running=0
declare -A passed=() planned=()

# -- helpers ---------------------------------------------------------------------------------------

say() { printf '%s\n' "$*"; }
die() { failure="${failure:-$*}"; printf 'gl_exit_test: %s\n' "$*" >&2; exit 1; }
wants() { case ",$blocks," in *",$1,"*) return 0 ;; *) return 1 ;; esac; }
pids() { pgrep -x "$1" | sort -n | tr '\n' ' ' | sed 's/ $//'; }
needs_tab() { [ "$1" = B2 ] || [ "$1" = F ]; }

exited() {   # pid -> true once it has exited (a zombie counts as exited)
    [ -d "/proc/$1" ] || return 0
    [ "$(sed 's/.*) //' "/proc/$1/stat" 2>/dev/null | cut -d' ' -f1)" = Z ]
}

steamvr_invocation() {   # InvocationID of the steamvr.service user unit ("" when it cannot be told)
    systemctl --user show -p InvocationID --value steamvr.service 2>/dev/null || true
}

steamvr_state() {   # ActiveState of the steamvr.service user unit
    systemctl --user is-active steamvr.service 2>/dev/null || true
}

# [logfile] -> why SteamVR is no longer the session the test started with ("" when nothing says so):
# steamvr.service restarted (a new invocation; systemd keeps the old one while the unit is only
# inactive) or not active any more, no vrcompositor at all, or SteamVR asked the overlay to quit.
steamvr_gone() {
    local now
    if [ -n "$INV0" ] && [ "$(steamvr_invocation)" != "$INV0" ]; then
        echo "steamvr.service was stopped or restarted"
        return
    fi
    now="$(steamvr_state)"
    if [ "$SVC0" = active ] && [ "$now" != active ]; then
        echo "steamvr.service is ${now:-not active}"
    elif [ -z "$(pids vrcompositor)" ]; then
        echo "vrcompositor is not running"
    elif [ -n "${1:-}" ] && grep -q "SteamVR requested quit" "$1" 2>/dev/null; then
        echo "SteamVR asked the overlay to quit"
    fi
}

compositor_changes() {   # -> "vrcompositor pid A -> B, ..." against the start ("" when unchanged)
    local now out=""
    now="$(pids vrcompositor)"
    [ "$now" = "$VRC" ] || out="vrcompositor pid $VRC -> ${now:-none}"
    now="$(pids gamescope)"
    [ "$now" = "$GS" ] || out="${out:+$out, }gamescope pid $GS -> ${now:-none}"
    printf '%s' "$out"
}

crash_lines() {   # since -> journal lines about vrcompositor or gamescope dying
    {
        journalctl --system --since "$1" --no-pager -q 2>/dev/null \
            | grep -E "\((vrcompositor|gamescope)[^)]*\) of user $uid terminated abnormally"
        journalctl --user --since "$1" --no-pager -q 2>/dev/null \
            | grep -E "\((vrcompositor|gamescope)[^)]*\).*dumped core"
    } | cut -c1-220
}

# "pid mode restartable" of the running installed overlay ("0 - no" when none runs), judged by the
# build under test: a 0.1.x instance counts as restartable only with positive evidence of file mode.
installed_instance() {
    [ -f "$installed/fcam_overlay.py" ] || { echo "0 - no"; return; }
    FCAM_GLTEST_INSTALLED="$installed" python3 -B - "$src" <<'PY' 2>/dev/null || echo "? unknown no"
import os, sys
sys.path.insert(0, sys.argv[1])
import fcam_overlay as o
o.LEGACY_LOCK = os.path.join(os.environ["FCAM_GLTEST_INSTALLED"], "fcam_overlay.lock")
info = o.running_instance()
if not info:
    print("0 - no")
else:
    mode = o.instance_texture_mode(info)
    print(int(info.get("pid") or 0), mode or "unknown", "yes" if mode in o.RESTARTABLE_MODES else "no")
PY
}

restart_installed() {
    [ "$installed_was_running" = 1 ] || return 0
    local pid rest
    read -r pid rest <<<"$(installed_instance)"
    if [ "${pid:-0}" != 0 ] && [ "$pid" != "?" ]; then
        say "   installed overlay is running again (pid $pid)"
        return 0
    fi
    say "== starting the installed overlay again"
    (cd "$installed" && python3 - <<'PY'
import json, os, shlex, subprocess, sys
import openvr_min as ovr
try:
    session = ovr.Session().init(ovr.VRApplication_Background)
except ovr.OpenVRError as e:
    session = None
    print("   SteamVR not reachable (%s)" % e)
if session is not None:
    try:
        session.applications.launch("ottlabs.fcam")
        print("   relaunched through SteamVR (with the manifest arguments)")
        sys.exit(0)
    except ovr.OpenVRError as e:
        print("   SteamVR did not launch it (%s)" % e)
    finally:
        session.shutdown()
with open("fcam.vrmanifest", encoding="utf-8") as fh:
    args = shlex.split(json.load(fh)["applications"][0].get("arguments", ""))
with open(os.devnull, "rb") as stdin, open(os.devnull, "ab") as out:
    process = subprocess.Popen([sys.executable, "fcam_overlay.py"] + args, stdin=stdin, stdout=out, stderr=out,
                               start_new_session=True)
print("   started headless (pid %d)" % process.pid)
PY
    ) || say "   could not start it: run  bash $src/install-overlay.sh"
}

start_overlay() {   # mode logfile [VAR=value ...]
    local mode="$1" logf="$2"
    shift 2
    env "$@" FCAM_DEBUG_FORCE_UPLOAD_HZ=25 python3 "$work/fcam_overlay.py" --texture-mode "$mode" \
        --listen 127.0.0.1:0 --usb-id 0000:0000 --lock-file "$work/test.lock" --log-file "$logf" -v \
        >/dev/null 2>"$logf.stderr" &
    P=$!
}

wait_texture() {   # mode logfile -> true once the overlay confirmed its texture
    local i
    for i in $(seq 1 30); do
        grep -q "panel texture .* set ($1 mode)" "$2" 2>/dev/null && return 0
        exited "$P" && return 1
        sleep 1
    done
    return 1
}

tab_state() {   # logfile -> visible, hidden or unknown: the last tab change the overlay logged (-v)
    case "$(grep -E 'fcam\.overlay tab (visible|hidden)$' "$1" 2>/dev/null | tail -1)" in
        *visible) echo visible ;;
        *hidden) echo hidden ;;
        *) echo unknown ;;
    esac
}

wait_tab_visible() {   # logfile seconds -> true once the overlay logged the tab as visible
    local i
    for i in $(seq 1 "$2"); do
        [ "$(tab_state "$1")" = visible ] && return 0
        exited "$P" && return 1
        sleep 1
    done
    return 1
}

proc_sample() {   # pid -> "fds=N dmabuf=N rss=NkB" (n/a where /proc/<pid> is not readable)
    local listing fds=n/a dmabuf=n/a rss
    if listing="$(ls -l "/proc/$1/fd" 2>/dev/null)"; then
        fds="$(printf '%s\n' "$listing" | grep -c -- ' -> ')"
        dmabuf="$(printf '%s\n' "$listing" | grep -c dmabuf)"
    fi
    rss="$(awk '/^VmRSS/ {print $2 "kB"}' "/proc/$1/status" 2>/dev/null)"
    printf 'fds=%s dmabuf=%s rss=%s' "$fds" "$dmabuf" "${rss:-n/a}"
}

observe() {   # block seconds: waits; block F samples the overlay and vrcompositor every 10 s meanwhile
    local block="$1" secs="$2" start=$SECONDS p
    if [ "$block" != F ]; then
        sleep "$secs"
        return
    fi
    while :; do
        say "   t+$((SECONDS - start))s overlay $(proc_sample "$P")"
        for p in $(pids vrcompositor); do
            say "          vrcompositor $p $(proc_sample "$p")"
        done
        [ $((SECONDS - start + 10)) -le "$secs" ] || break
        sleep 10
    done
    sleep $((secs - (SECONDS - start) > 0 ? secs - (SECONDS - start) : 0))
}

run_one() {   # block mode signal seconds n [VAR=value ...] -> 0 passed, 1 failed, 2 not counted, 3 stop unfinished
    local block="$1" mode="$2" sig="$3" secs="$4" n="$5"
    shift 5
    local logf="$work/logs/$block-$mode-$sig-$n.log"
    local t lines count gone crashed="" uncounted="" also=""
    local -a problems=()
    # Still the SteamVR session the test started with? After a wait (the B2/F prompt has no time
    # limit, and the headset may sleep meanwhile) a run in another session could only fail.
    crashed="$(compositor_changes)"
    gone="$(steamvr_gone)"
    if [ -n "$crashed" ] || [ -n "$gone" ]; then
        lines="$(crash_lines "$checked_until")"
        if [ -n "$lines" ]; then
            failure="$block $mode SIG$sig #$n (before it started): CRASH: ${crashed:-$gone}, journal: $(printf '%s' "$lines" | head -3 | tr '\n' '|')"
            say "   FAIL $failure"
            return 1
        fi
        unfinished="before $block $mode SIG$sig #$n, SteamVR was no longer the session the test started with (${gone:-same steamvr.service}${crashed:+; $crashed}) and the journal has no crash; the headset probably went to standby"
        say "   NOT STARTED $block $mode SIG$sig #$n: $unfinished"
        return 3
    fi
    t="$(date '+%F %T')"
    rm -f "$logf" "$logf.stderr"
    start_overlay "$mode" "$logf" "$@"
    if ! wait_texture "$mode" "$logf"; then
        problems+=("texture never confirmed within 30 s")
    elif [ "$mode" = gl ] && ! grep -q "GL texture path: " "$logf"; then
        problems+=("GL texture path not in use")
    fi
    if [ ${#problems[@]} -eq 0 ]; then
        if ! needs_tab "$block"; then
            observe "$block" "$secs"
        elif ! wait_tab_visible "$logf" 60; then
            uncounted="the FCAM Bridge tab was not open within 60 s"
        else
            say "   tab open; SIG$sig in ${secs} s, keep it open"
            observe "$block" "$secs"
            [ "$(tab_state "$logf")" = visible ] || uncounted="the tab was closed before SIG$sig"
        fi
    fi
    if ! stop_overlay "$sig"; then
        problems+=("still running 20 s after SIG$sig")
        kill -KILL "$P" 2>/dev/null
        wait "$P" 2>/dev/null
        P=""
    fi
    sleep 5
    if [ "$sig" = TERM ] && [ ${#problems[@]} -eq 0 ]; then
        [ "$RC" = 0 ] || problems+=("exit status $RC after SIGTERM")
        grep -q "SteamVR session closed" "$logf" || problems+=("no 'SteamVR session closed' in the log")
        grep -q "FCAM overlay stopped" "$logf" || problems+=("no 'FCAM overlay stopped' in the log")
    fi
    if [ "$block" = D ] && [ ${#problems[@]} -eq 0 ]; then
        count="$(grep -c "SteamVR connected" "$logf")"
        [ "$count" -ge 10 ] || problems+=("only $count SteamVR sessions (expected about 12)")
        count="$(grep -c "GL texture path: " "$logf")"
        [ "$count" -eq 1 ] || problems+=("GL context created $count times (expected once)")
    fi
    if [ "$block" = F ]; then
        count="$(grep -c "left by SetOverlayTexture" "$logf")"
        say "   GL errors left by SetOverlayTexture: $count line(s) in $logf"
    fi
    crashed="$(compositor_changes)"
    checked_until="$(date '+%F %T')"
    lines="$(crash_lines "$t")"
    [ ${#problems[@]} -eq 0 ] || also=" (also: $(printf '%s; ' "${problems[@]}" | sed 's/; $//'))"
    if [ -n "$lines" ]; then
        problems+=("CRASH: ${crashed:+$crashed, }journal: $(printf '%s' "$lines" | head -3 | tr '\n' '|')")
    elif [ -n "$crashed" ]; then
        # New pids but no crash in the journal: SteamVR may have shut down in standby (and may be
        # back already). Only pids that changed while SteamVR stayed up count as a crash.
        gone="$(steamvr_gone "$logf")"
        if [ -n "$gone" ]; then
            unfinished="SteamVR shut down or restarted during $block $mode SIG$sig #$n ($gone; $crashed) with no crash in the journal; the headset probably went to standby"
            say "   NOT COUNTED $block $mode SIG$sig #$n: $unfinished$also"
            return 3
        fi
        problems+=("CRASH: $crashed, with no crash in the journal, while SteamVR showed no sign of a shutdown (same steamvr.service invocation, still active, no quit request)")
    elif grep -q "compositor in standby" "$logf" 2>/dev/null; then
        # Uploads failed with RequestFailed/TimedOut: the run says nothing about the GL exit path.
        standby_runs=$((standby_runs + 1))
        say "   NOT COUNTED $block $mode SIG$sig #$n: the compositor was in standby (no crash); wake the headset and keep it awake$also"
        if [ "$standby_runs" -ge 3 ]; then
            unfinished="the headset stayed in standby for $standby_runs runs in a row"
            return 3
        fi
        return 2
    fi
    if [ ${#problems[@]} -eq 0 ] && [ -n "$uncounted" ]; then
        say "   NOT COUNTED $block $mode SIG$sig #$n: $uncounted (no crash); this run is repeated"
        return 2
    fi
    if [ ${#problems[@]} -eq 0 ]; then
        say "   PASS $block $mode SIG$sig #$n"
        standby_runs=0
        passed[$block]=$(( ${passed[$block]:-0} + 1 ))
        if [ "$mode" = gl ] && [ "$block" != F ]; then
            gl_exits=$((gl_exits + 1))
            [ "$sig" = KILL ] && gl_kills=$((gl_kills + 1))
        fi
        return 0
    fi
    failure="$block $mode SIG$sig #$n: $(printf '%s; ' "${problems[@]}")log $logf"
    say "   FAIL $failure"
    return 1
}

stop_overlay() {   # signal -> true once the process exited (RC = its exit status)
    local i
    kill -"$1" "$P" 2>/dev/null
    for i in $(seq 1 40); do
        if exited "$P"; then
            wait "$P" 2>/dev/null
            RC=$?
            P=""
            return 0
        fi
        sleep 0.5
    done
    return 1
}

ask_for_tab() {   # block n -> false when the operator skips the block
    local answer
    if [ "$1" = F ]; then
        printf '   F #%s: open the dashboard, select the FCAM Bridge tab and watch the marker at the top right.\n' "$2"
    else
        printf '   %s #%s: open the dashboard, select the FCAM Bridge tab as soon as it appears and keep it open.\n' "$1" "$2"
    fi
    printf '   Press Enter to start (s = skip %s): ' "$1"
    read -r answer || answer=s
    [ "$answer" != s ]
}

block() {   # name mode signal seconds runs [VAR=value ...]
    local name="$1" mode="$2" sig="$3" secs="$4" runs="$5" n=1 rc
    shift 5
    wants "$name" || return 0
    if needs_tab "$name" && [ "$prompt" = 0 ]; then
        say "== block $name skipped (--no-prompt: it asks for the open tab)"
        return 0
    fi
    planned[$name]=$(( ${planned[$name]:-0} + runs ))
    say "== block $name: $mode, SIG$sig after ${secs}s, $runs run(s)"
    while [ "$n" -le "$runs" ]; do
        if needs_tab "$name" && ! ask_for_tab "$name" "$n"; then
            say "   $name skipped"
            unset "planned[$name]"
            return 0
        fi
        run_one "$name" "$mode" "$sig" "$secs" "$n" "$@"
        rc=$?
        case "$rc" in
            0) n=$((n + 1)) ;;
            2) ;;
            3) exit 3 ;;
            *) exit 1 ;;
        esac
    done
}

summary() {
    local b complete=1
    say ""
    say "== summary (since $T0)"
    for b in $MATRIX F; do
        if [ -n "${planned[$b]:-}" ]; then say "   $b: ${passed[$b]:-0}/${planned[$b]} passed"; fi
    done
    for b in $MATRIX; do
        if [ -z "${planned[$b]:-}" ] || [ "${passed[$b]:-0}" -lt "${planned[$b]}" ]; then complete=0; fi
    done
    say "   GL exits without a compositor crash (A-D): $gl_exits ($gl_kills of them SIGKILL)"
    if [ -n "$failure" ]; then
        say "   RESULT: FAIL at $failure"
        case "$failure" in
            *" gl SIG"*CRASH*) say "   A GL run crashed the compositor: switch to file mode: bash $src/install-overlay.sh --texture-mode file" ;;
        esac
        say "   If the headset UI loops, see README.md 'Known issues'."
    elif [ -n "$unfinished" ]; then
        say "   RESULT: NOT FINISHED: $unfinished."
        say "   No crash was found, so this is no GL failure. Keep the headset awake (worn, or the proximity"
        say "   sensor covered) and run the test again."
        if [ "$journal_ok" = 0 ]; then
            say "   The system journal was not readable, so a compositor crash may have gone unseen: check"
            say "   coredumpctl list --since '$T0' --no-pager before you rely on this."
        fi
    elif [ "$complete" = 1 ]; then
        say "   RESULT: PASS (every run of blocks A-D: $gl_exits GL exits, $gl_kills of them SIGKILL, no crash)."
        say "   Next: block E by hand (README.md) and --blocks F."
    else
        say "   RESULT: no failure in the blocks that ran ($blocks), but not the complete matrix A-D"
    fi
    if [ -n "${planned[F]:-}" ]; then
        say "   F: judge the smoothness you saw; fds, dmabuf and RSS above should stay flat in gl mode"
    fi
    say "   core dumps since the start: coredumpctl list --since '$T0' --no-pager"
    say "   logs: $work/logs"
}

cleanup() {
    local status=$?
    trap - EXIT INT TERM
    if [ -n "$P" ] && ! exited "$P"; then
        say "== stopping the test instance (pid $P)"
        stop_overlay TERM || { kill -KILL "$P" 2>/dev/null; wait "$P" 2>/dev/null; }
    fi
    restart_installed
    [ "$started" = 1 ] && summary
    [ -z "$failure" ] || exit 1
    [ -z "$unfinished" ] || exit 3
    exit "$status"
}

# -- preconditions ---------------------------------------------------------------------------------

# Before anything is stopped: B2 and F wait for you to open the tab, which needs a terminal.
if [ ! -t 0 ] && [ "$prompt" = 1 ] && { wants B2 || wants F; }; then
    die "blocks B2 and F ask you to open the FCAM Bridge tab and need a terminal: run this with ssh -t (or in a shell on the headset), or pass --no-prompt to skip them (the result then cannot be PASS)"
fi
[ -t 0 ] || prompt=0
[ "$uid" -ne 0 ] || die "run as steamos, not as root"
command -v python3 >/dev/null || die "python3 not found"
for f in fcam_overlay.py openvr_min.py gl_texture.py fcam_font.py icon.png fcam.vrmanifest; do
    [ -f "$src/$f" ] || die "$src/$f missing: run this from a checkout or release tarball"
done
[ -f "$bridge" ] || die "$bridge missing"

VRC="$(pids vrcompositor)"
GS="$(pids gamescope)"
INV0="$(steamvr_invocation)"
SVC0="$(steamvr_state)"
checked_until="$T0"
[ -n "$VRC" ] || die "vrcompositor is not running: start SteamVR and wake the headset first"
[ -n "$GS" ] || die "gamescope is not running"

raw="$(pgrep -af -- '--texture-mode[= ]raw|--unsafe-raw|--no-gl' || true)"
[ -z "$raw" ] || die "an overlay in raw mode is running; stopping it would crash the compositor. Reboot the headset first:
$raw"

read -r ipid imode irestartable <<<"$(installed_instance)"
[ "$ipid" != "?" ] || die "cannot tell whether the installed overlay in $installed is running (python3 failed)"
if [ "$ipid" != 0 ] && [ "$irestartable" != yes ]; then
    die "the installed overlay (pid $ipid) may use SetOverlayRaw (texture mode $imode), and stopping it would crash the compositor. Install this build first (bash $src/install-overlay.sh), reboot the headset, then run the test again."
fi

journal_note="system journal readable"
if [ -z "$(journalctl --system -n 1 --no-pager -q 2>/dev/null)" ]; then
    journal_note="system journal NOT readable (not in wheel/adm/systemd-journal?): only pids and the user journal are checked"
    journal_ok=0
fi

say "== gl_exit_test $T0 on $(hostname)"
say "   test build:   $src"
say "   vrcompositor: $VRC   gamescope: $GS   ($journal_note)"
say "   steamvr.service: ${SVC0:-unknown}, invocation ${INV0:-unknown}"
eye="$(pgrep -af frameeye_overlay | cut -c1-120 || true)"
say "   eye overlay:  ${eye:-not running}"
say "   blocks:       $blocks"
say "   Keep the headset awake (worn, or the proximity sensor covered) for the whole run, and the"
say "   dashboard closed except when block B2 or F asks for the FCAM Bridge tab."

trap cleanup EXIT
trap 'say "interrupted"; failure="${failure:-interrupted}"; exit 130' INT TERM

# -- stop the installed overlay (only in a mode that is known to exit safely) -----------------------

if [ "$ipid" != 0 ]; then
    installed_was_running=1
    say "== stopping the installed overlay (pid $ipid, texture mode $imode)"
    kill -TERM "$ipid" 2>/dev/null
    for _ in $(seq 1 20); do exited "$ipid" && break; sleep 0.5; done
    exited "$ipid" || die "installed overlay pid $ipid did not stop within 10 s"
    sleep 5
    [ "$(pids vrcompositor)" = "$VRC" ] && [ "$(pids gamescope)" = "$GS" ] \
        || die "the compositor changed while the installed overlay stopped (vrcompositor $(pids vrcompositor), gamescope $(pids gamescope))"
fi

# -- the test copy ---------------------------------------------------------------------------------

for old in $(pgrep -f "$work/fcam_overlay.py" || true); do
    say "   stopping a leftover test instance (pid $old)"
    kill -TERM "$old" 2>/dev/null
done
mkdir -p "$work/logs" || die "cannot create $work"
install -m 0644 "$bridge" "$src/fcam_overlay.py" "$src/openvr_min.py" "$src/gl_texture.py" "$src/fcam_font.py" \
    "$src/icon.png" "$src/fcam.vrmanifest" "$work/" || die "cannot copy the test build to $work"
say "   test copy:    $work (logs in $work/logs)"
sleep 1

# -- the blocks ------------------------------------------------------------------------------------

started=1
block A file TERM 10 3
block B1 gl TERM 20 20
block B2 gl TERM 10 5
block C gl KILL 20 10
block D gl TERM 120 1 FCAM_DEBUG_RECONNECT_EVERY=10
block F gl TERM 120 1
block F file TERM 60 1 FCAM_DEBUG_FILE_INTERVAL=0.5
