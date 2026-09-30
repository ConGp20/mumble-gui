"""Pruefungen an der Compose-Datei selbst.

Sie ist der Vertrag zwischen dem Rechner und der Anwendung. Fehler darin
faellt niemandem beim Programmieren auf -- sondern jemandem im Stadion, der
etwas anhalten will und es nicht kann.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

WURZEL = Path(__file__).resolve().parent.parent.parent
COMPOSE = WURZEL / "docker-compose.yml"


@pytest.fixture(scope="module")
def compose() -> dict:
    if not COMPOSE.exists():
        pytest.skip(f"{COMPOSE} nicht gefunden")
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))


def test_kein_dienst_haengt_an_einem_profil(compose):
    """Ein Profil macht ``docker compose down`` unvollstaendig.

    Genau das ist passiert: mumble-admin lag hinter ``--profile gui``, und ein
    schlichtes ``docker compose down`` liess es laufen -- man kam per SSH nicht
    dazu, alles zu beenden. Das Profil war nur dafuer da, dass setup.sh zuerst
    den Server hochfahren kann; das geht mit ``up -d mumble-server`` genauso.
    """
    mit_profil = {
        name: dienst.get("profiles")
        for name, dienst in compose["services"].items()
        if dienst.get("profiles")
    }
    assert not mit_profil, (
        "Diese Dienste haengen an einem Profil und werden von 'docker compose "
        f"down' nicht beendet: {mit_profil}"
    )


def test_beide_dienste_starten_von_allein_wieder(compose):
    """Nach einem Stromausfall im Stadion soll niemand hinfahren muessen."""
    for name, dienst in compose["services"].items():
        assert dienst.get("restart") == "unless-stopped", (
            f"{name} hat restart={dienst.get('restart')!r} -- nach einem "
            "Neustart des Rechners bliebe der Dienst aus."
        )


def test_setup_startet_ohne_profilschalter():
    """Sonst entstuende dasselbe Problem ueber die Hintertuer."""
    skript = (WURZEL / "setup.sh").read_text(encoding="utf-8")
    assert "--profile" not in skript
