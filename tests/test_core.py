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
        "fix-remmina-socket", "journal-limit", "remove-snap-revisions", "apt-autoremove", "clean-crash"}


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
