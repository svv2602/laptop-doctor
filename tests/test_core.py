import importlib.machinery
import importlib.util
from pathlib import Path

import pytest

from laptop_doctor import checks, cleanup
from laptop_doctor.util import human, parse_docker_size

ROOT = Path(__file__).resolve().parent.parent


def load_helper():
    path = ROOT / "helper" / "laptop-doctor-helper"
    loader = importlib.machinery.SourceFileLoader("ld_helper", str(path))
    spec = importlib.util.spec_from_loader("ld_helper", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------- parsers

def test_clean_ending_detects_shutdown():
    assert checks.is_clean_ending("x\nsystemd-journald[403]: Received SIGTERM from PID 1 (systemd-shutdow).\nJournal stopped")
    assert not checks.is_clean_ending("hw-watch[1]: acpitz=77 TCPU=70\nkdeconnectd: No uuids found")


def test_boot_time_parse():
    out = ("Startup finished in 7.413s (firmware) + 4.019s (loader) + 3.673s (kernel) + 6.238s (userspace) = 21.344s \n"
           "graphical.target reached after 6.235s in userspace.\n")
    assert checks.parse_boot_time(out) == pytest.approx((21.344, 6.238))
    out2 = "Startup finished in 3.1s (kernel) + 1min 2.500s (userspace) = 1min 5.600s\n"
    assert checks.parse_boot_time(out2) == pytest.approx((65.6, 62.5))


def test_journal_mb():
    assert checks.parse_journal_mb("Archived and active journals take up 438.6M in the file system.") == pytest.approx(438.6)
    assert checks.parse_journal_mb("take up 3.5G in the file system") == pytest.approx(3584)
    assert checks.parse_journal_mb("garbage") is None


def test_docker_size():
    assert parse_docker_size("1.207GB") == 1_207_000_000
    assert parse_docker_size("584MB") == 584_000_000
    assert parse_docker_size("0B") == 0
    assert parse_docker_size("4.68MiB") == int(4.68 * 1024 ** 2)


def test_short_sample_and_p95():
    s = "acpitz=77 TCPU=70 x86_pkg_temp=73 | ADP1=online:1 BAT1=Charging:96% | load=2.52 4.10"
    assert checks.short_sample(s) == "TCPU=70 x86_pkg_temp=73 BAT1=Charging:96% load=2.52"
    assert checks.p95(list(range(1, 101))) == 95
    assert checks.p95([42]) == 42


def test_human():
    assert human(512) == "512 Б"
    assert human(1536 * 1024 * 1024) == "1.5 ГБ"


# ---------------------------------------------------------------- cleanup path safety

def test_apply_user_paths_refuses_outside_home(tmp_path):
    with pytest.raises(ValueError):
        cleanup.apply_user_paths(cleanup.Plan(empty_paths=[Path("/etc")]))
    with pytest.raises(ValueError):
        cleanup.apply_user_paths(cleanup.Plan(remove_paths=[Path.home()]))


def test_apply_user_paths_empties_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(cleanup, "HOME", tmp_path)
    d = tmp_path / "cache"
    (d / "sub").mkdir(parents=True)
    (d / "sub" / "f").write_text("x")
    (d / "g").write_text("y")
    cleanup.apply_user_paths(cleanup.Plan(empty_paths=[d]))
    assert d.is_dir() and not any(d.iterdir())


# ---------------------------------------------------------------- helper validation

@pytest.fixture(scope="module")
def helper():
    return load_helper()


@pytest.mark.parametrize("args", [
    {"battery": "BAT1; rm -rf /", "value": 80},
    {"battery": "BAT1", "value": 20},
    {"battery": "BAT1", "value": "80"},
])
def test_helper_battery_validation(helper, args):
    with pytest.raises(helper.Bad):
        helper.set_battery_threshold(args)


@pytest.mark.parametrize("units", [["../../etc/passwd"], ["foo.service; reboot"], ["x.timer"], []])
def test_helper_unit_validation(helper, units):
    with pytest.raises(helper.Bad):
        helper.disable_units({"units": units})


def test_helper_pci_validation(helper):
    with pytest.raises(helper.Bad):
        helper.arc_autosuspend({"pci": "../../../etc"})


def test_helper_snap_validation(helper, monkeypatch):
    monkeypatch.setattr(helper, "sh", lambda cmd, **kw: "Name Version Rev Tracking Publisher Notes\n")
    with pytest.raises(helper.Bad):
        helper.remove_snap_revisions({"revisions": [["core; reboot", "1"]]})
    with pytest.raises(helper.Bad):
        helper.remove_snap_revisions({"revisions": [["core", "1 --purge"]]})


def test_helper_snap_only_disabled(helper, monkeypatch):
    calls = []
    listing = ("Name Version Rev Tracking Publisher Notes\n"
               "core 16 17284 latest/stable canonical core\n"
               "core 16 17200 latest/stable canonical core,disabled\n")
    monkeypatch.setattr(helper, "sh", lambda cmd, **kw: calls.append(cmd) or listing)
    helper.remove_snap_revisions({"revisions": [["core", "17284"], ["core", "17200"]]})
    removes = [c for c in calls if c[:2] == ["snap", "remove"]]
    assert removes == [["snap", "remove", "core", "--revision=17200"]]  # текущая версия не тронута


def test_helper_rejects_unknown_action(helper):
    assert "clean-docker-volumes" not in helper.ACTIONS
    assert set(helper.ACTIONS) == {
        "set-battery-threshold", "arc-autosuspend", "disable-units", "restart-units",
        "fix-remmina-socket", "journal-limit", "remove-snap-revisions", "apt-autoremove", "clean-crash",
        "swap-refresh"}


def test_unit_label():
    assert checks.unit_label(r"app-snap\x2duserd\x2dautostart@autostart.service") == "app-snap-userd-autostart@autostart"
    assert checks.unit_label("speech-dispatcher.socket") == "speech-dispatcher.socket"


def test_app_launch_units_ignored():
    assert checks.APP_LAUNCH_UNIT.match("app-remmina_remmina@b3931129d6bc4921826068b7cbd6cd53.service")
    assert not checks.APP_LAUNCH_UNIT.match(r"app-snap\x2duserd\x2dautostart@autostart.service")
    assert not checks.APP_LAUNCH_UNIT.match("speech-dispatcher.service")


def test_personal_checks_skip_when_unconfigured():
    from laptop_doctor.config import Config
    cfg = Config()
    assert cfg.dgpu_pci == "" and cfg.disabled_units == [] and cfg.abandoned_compose_projects == []
    assert checks.check_dgpu(cfg).status == checks.Status.INFO
    assert checks.check_reenabled(cfg).status == checks.Status.INFO
    assert checks.check_abandoned(cfg).status == checks.Status.INFO


def test_meminfo_and_psi():
    mi = checks.parse_meminfo("MemTotal:       31951972 kB\nMemAvailable:   22245952 kB\nHugePages_Total:       0\n")
    assert mi["MemTotal"] == 31951972 * 1024
    assert mi["HugePages_Total"] == 0
    psi = "some avg10=1.20 avg60=7.50 avg300=0.10 total=2174738\nfull avg10=0.00 avg60=0.00 avg300=0.00 total=1\n"
    assert checks.parse_psi_avg60(psi) == 7.5
    assert checks.parse_psi_avg60("") == 0.0


def test_memory_status(monkeypatch):
    from laptop_doctor.config import Config
    gib = 1024 * 1024  # в kB
    meminfo = (f"MemTotal: {32 * gib} kB\nMemAvailable: {2 * gib} kB\n"
               f"SwapTotal: {32 * gib} kB\nSwapFree: {10 * gib} kB\nCached: 0 kB\nShmem: 0 kB\n")
    files = {"/proc/meminfo": meminfo, "/proc/pressure/memory": "some avg10=0 avg60=1.00 avg300=0 total=1"}
    monkeypatch.setattr(checks, "read", lambda p, d=None: files.get(str(p), d))
    monkeypatch.setattr(checks, "top_memory", lambda n=10: [(5 * 1024 ** 3, "chrome", 30)])
    monkeypatch.setattr(checks, "oom_kills", lambda days=7: [])
    from laptop_doctor import testguard
    monkeypatch.setattr(testguard, "find_test_processes", lambda cfg: [])
    monkeypatch.setattr(testguard, "find_test_containers", lambda cfg: [])
    monkeypatch.setattr(testguard, "mem_snapshot", lambda: testguard.MemSnapshot(1, 1, 0, 0, 0, 0))
    r = checks.check_memory(Config())
    assert r.status == checks.Status.WARN  # доступно 6% и swap 69%
    assert "swap занят на 69%" in r.summary and "chrome (30 проц.)" in r.details
    monkeypatch.setattr(checks, "oom_kills", lambda days=7: ["kernel: Out of memory: Killed process 1 (x)"])
    assert checks.check_memory(Config()).status == checks.Status.ERROR


# ---------------------------------------------------------------- testguard

import os as _os
import subprocess as _sp
import sys as _sys

from laptop_doctor import testguard


def test_find_test_processes_filters(monkeypatch):
    from laptop_doctor.config import Config
    uid = _os.getuid()
    ps = "\n".join([
        f"  101 {uid}  4000  204800 php vendor/bin/phpunit --testsuite unit",
        f"  102 {uid}    10   1024 /bin/bash -c grep phpunit foo",           # шелл, упоминающий шаблон
        f"  103 0      4000  204800 php vendor/bin/phpunit",                  # чужой (root)
        f"  104 {uid}    20   2048 python3 -m pytest -x tests",
        f"  105 {uid}    20   2048 /usr/bin/python3 -m laptop_doctor",
        f"  106 {uid}    20   2048 code --type=renderer",
    ])
    monkeypatch.setattr(testguard, "run", lambda cmd, **kw: ps)
    got = testguard.find_test_processes(Config())
    assert [p.pid for p in got] == [101, 104]
    assert got[0].rss == 204800 * 1024 and got[0].age_s == 4000


def test_age_from_iso():
    assert testguard._age_from_iso("2000-01-01T00:00:00.123456789Z") > 10 ** 8
    assert testguard._age_from_iso("garbage") == 0


def test_swap_refresh_wanted():
    g = 1024 ** 3
    assert not testguard.swap_refresh_wanted(testguard.MemSnapshot(32 * g, 20 * g, 32 * g, g // 2, 0, 0))
    assert testguard.swap_refresh_wanted(testguard.MemSnapshot(32 * g, 20 * g, 32 * g, 5 * g, 0, 0))
    assert not testguard.swap_refresh_wanted(testguard.MemSnapshot(32 * g, 6 * g, 32 * g, 5 * g, 0, 0))
    # после остановки тестов освободится 10 ГБ — тогда хватит
    snap = testguard.MemSnapshot(32 * g, 6 * g, 32 * g, 5 * g, 0, 0)
    procs = [testguard.TestProc(1, 0, 10 * g, "phpunit")]
    assert testguard.swap_refresh_wanted_after(snap, procs, [])


def test_stop_tests_kills_stubborn_process():
    code = "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"
    obedient = _sp.Popen([_sys.executable, "-c", "import time; time.sleep(60)"])
    stubborn = _sp.Popen([_sys.executable, "-c", code])
    import time
    time.sleep(0.3)  # успеть поставить обработчик сигнала
    log = testguard.stop_tests([testguard.TestProc(obedient.pid, 0, 0, "x"),
                                testguard.TestProc(stubborn.pid, 0, 0, "y")], [])
    assert obedient.wait(timeout=5) == -15  # SIGTERM
    assert stubborn.wait(timeout=5) == -9   # не послушался — SIGKILL
    assert "принудительно: 1" in log[0]


def test_check_tests_statuses(monkeypatch):
    from laptop_doctor.config import Config
    g = 1024 ** 3
    cfg = Config()
    monkeypatch.setattr(testguard, "find_test_containers", lambda c: [])
    monkeypatch.setattr(testguard, "mem_snapshot", lambda: testguard.MemSnapshot(32 * g, 20 * g, 0, 0, 0, 0))
    monkeypatch.setattr(testguard, "find_test_processes", lambda c: [])
    assert checks.check_tests(cfg).status == checks.Status.OK
    monkeypatch.setattr(testguard, "find_test_processes",
                        lambda c: [testguard.TestProc(1, 60, g, "phpunit")])
    r = checks.check_tests(cfg)
    assert r.status == checks.Status.INFO and r.fix and r.fix.py_action
    monkeypatch.setattr(testguard, "find_test_processes",
                        lambda c: [testguard.TestProc(1, 3 * 3600, g, "phpunit")])
    assert checks.check_tests(cfg).status == checks.Status.WARN
    monkeypatch.setattr(testguard, "mem_snapshot", lambda: testguard.MemSnapshot(32 * g, g, 0, 0, 30, 30))
    assert checks.check_tests(cfg).status == checks.Status.ERROR


def test_helper_swap_refresh_refuses_without_room(helper, monkeypatch):
    g = 1024 ** 3
    calls = []
    monkeypatch.setattr(helper, "sh", lambda cmd, **kw: calls.append(cmd) or "")
    monkeypatch.setattr(helper, "meminfo_bytes",
                        lambda: {"SwapTotal": 32 * g, "SwapFree": 22 * g, "MemAvailable": 11 * g})
    with pytest.raises(RuntimeError, match="не хватит памяти"):
        helper.swap_refresh({})
    assert calls == []  # swapoff не вызывался
    monkeypatch.setattr(helper, "meminfo_bytes",
                        lambda: {"SwapTotal": 32 * g, "SwapFree": 22 * g, "MemAvailable": 20 * g})
    helper.swap_refresh({})
    assert calls == [["swapoff", "-a"], ["swapon", "-a"]]
