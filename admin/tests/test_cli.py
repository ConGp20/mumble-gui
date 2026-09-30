"""CLI gegen das murmur-Doppel."""

from __future__ import annotations

import os
from unittest import mock

from tests.conftest import needs_ice

pytestmark = needs_ice


def _env(fake, tmp_path, config_text: str) -> dict[str, str]:
    config_file = tmp_path / "intercom.yaml"
    config_file.write_text(config_text, encoding="utf-8")
    return {
        "ICE_HOST": "127.0.0.1",
        "ICE_PORT": str(fake.port),
        "ICE_SECRET": fake.secret,
        "ICE_SERVER_ID": "1",
        "ADMIN_PASSWORD": "pw",
        "SESSION_SECRET": "s" * 32,
        "INTERCOM_CONFIG": str(config_file),
        "MUMBLE_VERSION": "v1.5.735",
        "DATA_DIR": str(tmp_path),
    }


MINIMAL = """
version: 1
groups: [regie]
channels:
  - name: Intercom
    children:
      - name: Regie
        speak: [regie]
"""

KAPUTT = """
version: 1
groups: [regie]
channels:
  - name: Intercom
    children:
      - name: Regie
        speak: [regei]
"""


def test_validate_meldet_tippfehler(fake_murmur, tmp_path, capsys):
    from intercom.cli import main

    with mock.patch.dict(os.environ, _env(fake_murmur, tmp_path, KAPUTT), clear=False):
        assert main(["validate"]) == 2
    output = capsys.readouterr().err
    assert "regei" in output
    assert 'meintest du "regie"' in output


def test_validate_ist_zufrieden(fake_murmur, tmp_path, capsys):
    from intercom.cli import main

    with mock.patch.dict(os.environ, _env(fake_murmur, tmp_path, MINIMAL), clear=False):
        assert main(["validate"]) == 0
    assert "In Ordnung" in capsys.readouterr().out


def test_plan_detailed_exitcode(fake_murmur, tmp_path, capsys):
    """Rueckgabewert 3 heisst 'es gaebe etwas zu tun' -- fuer setup.sh."""
    from intercom.cli import main

    env = _env(fake_murmur, tmp_path, MINIMAL)
    with mock.patch.dict(os.environ, env, clear=False):
        assert main(["plan", "--detailed-exitcode"]) == 3
        assert "Platz anlegen" in capsys.readouterr().out

        assert main(["apply", "--yes"]) == 0
        capsys.readouterr()
        # Nach dem Anwenden gibt es nichts mehr zu tun.
        assert main(["plan", "--detailed-exitcode"]) == 0
        assert "Keine Aenderungen" in capsys.readouterr().out


def test_apply_ohne_tty_bricht_ohne_yes_ab(fake_murmur, tmp_path):
    from intercom.cli import main

    env = _env(fake_murmur, tmp_path, MINIMAL)
    with mock.patch.dict(os.environ, env, clear=False):
        assert main(["apply"]) == 1  # pytest gibt kein TTY her
    assert fake_murmur.server.channels.keys() == {0}, "es wurde trotzdem geschrieben"


def test_export_geht_durch(fake_murmur, tmp_path, capsys):
    from intercom.cli import main

    env = _env(fake_murmur, tmp_path, MINIMAL)
    with mock.patch.dict(os.environ, env, clear=False):
        main(["apply", "--yes"])
        capsys.readouterr()
        assert main(["export"]) == 0
    text = capsys.readouterr().out
    assert "Intercom" in text and "Regie" in text


def test_status(fake_murmur, tmp_path, capsys):
    from intercom.cli import main

    fake_murmur.server.connect_user("regie-1", userid=1)
    env = _env(fake_murmur, tmp_path, MINIMAL)
    with mock.patch.dict(os.environ, env, clear=False):
        assert main(["status"]) == 0
    output = capsys.readouterr().out
    assert "1.5.735" in output
    assert "regie-1" in output


def test_fehlendes_ice_secret_wird_gemeldet(tmp_path, capsys):
    from intercom.cli import main

    with mock.patch.dict(os.environ, {"ICE_SECRET": ""}, clear=True):
        assert main(["validate"]) == 2
    assert "ICE_SECRET" in capsys.readouterr().err
