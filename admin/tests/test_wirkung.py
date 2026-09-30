"""Die Rechte-Auswertung -- nachgebaut aus ACL.cpp und Group.cpp.

Jeder Test hier haelt ein Detail fest, das beim Nachbauen aus dem Gedaechtnis
falsch herauskommt. Alle wurden zusaetzlich gegen einen echten murmur v1.5.735
geprueft (siehe ``test_integration_real_server.py``); diese Tests laufen ohne
Server und sind die schnelle Absicherung.
"""

from __future__ import annotations

from intercom.ice import wirkung as W
from intercom.ice.types import ACLEntry, ChannelACL, ChannelGroup, MumbleChannel


def baum(*paare: tuple[int, int]) -> dict[int, MumbleChannel]:
    """``(id, parent)`` -> Kanalbaum. Die Wurzel kommt automatisch dazu."""
    kanaele = {0: MumbleChannel(id=0, name="Root", parent=-1)}
    for kid, parent in paare:
        kanaele[kid] = MumbleChannel(id=kid, name=f"K{kid}", parent=parent)
    return kanaele


def acl(
    kanal: int,
    *eintraege: ACLEntry,
    inherit: bool = True,
    gruppen: tuple[ChannelGroup, ...] = (),
) -> ChannelACL:
    return ChannelACL(
        channel_id=kanal, acls=list(eintraege), groups=list(gruppen), inherit=inherit
    )


def eintrag(
    gruppe: str,
    allow: int = 0,
    deny: int = 0,
    *,
    here: bool = True,
    subs: bool = True,
    userid: int = -1,
) -> ACLEntry:
    return ACLEntry(
        apply_here=here,
        apply_subs=subs,
        allow=allow,
        deny=deny,
        group=gruppe,
        userid=userid,
    )


# --------------------------------------------------------------------------- #
#  Die Grundausstattung
# --------------------------------------------------------------------------- #


def test_ohne_jeden_eintrag_gelten_die_grundrechte():
    """Ein frischer Server erlaubt jedem das Noetigste -- nicht nichts."""
    kanaele = baum((1, 0))
    acls = {0: acl(0), 1: acl(1)}
    w = W.rechte_einer_rolle(rolle="egal", ziel=1, kanaele=kanaele, acls=acls)
    assert w.darf(W.SPEAK) is True
    assert w.darf(W.ENTER) is True
    assert w.darf(W.LISTEN) is True
    assert w.darf(W.WRITE) is False
    assert w.unbestimmt == 0


# --------------------------------------------------------------------------- #
#  Stolperstein 1: deny gewinnt *innerhalb* eines Eintrags
# --------------------------------------------------------------------------- #


def test_im_selben_eintrag_schlaegt_deny_das_allow():
    """``granted |= allow`` steht vor ``granted &= ~deny`` -- deny gewinnt.

    Wer es andersherum annimmt, zeigt ein Recht als erteilt an, das der Server
    verweigert. Genau die Sorte Anzeige, die im Betrieb Zeit kostet.
    """
    kanaele = baum((1, 0))
    acls = {
        0: acl(0),
        1: acl(1, eintrag("all", allow=W.SPEAK, deny=W.SPEAK)),
    }
    w = W.rechte_einer_rolle(rolle="egal", ziel=1, kanaele=kanaele, acls=acls)
    assert w.darf(W.SPEAK) is False


def test_zwischen_eintraegen_gewinnt_der_letzte():
    """Ueber mehrere Eintraege hinweg zaehlt die Reihenfolge."""
    kanaele = baum((1, 0))
    acls = {
        0: acl(0),
        1: acl(
            1,
            eintrag("all", deny=W.SPEAK),
            eintrag("kampfrichter", allow=W.SPEAK),
        ),
    }
    darf = W.rechte_einer_rolle(
        rolle="kampfrichter", ziel=1, kanaele=kanaele, acls=acls
    )
    nicht = W.rechte_einer_rolle(rolle="zuschauer", ziel=1, kanaele=kanaele, acls=acls)
    assert darf.darf(W.SPEAK) is True
    assert nicht.darf(W.SPEAK) is False


# --------------------------------------------------------------------------- #
#  Stolperstein 2: Vererbung aus setzt zurueck, bricht die Kette aber nicht ab
# --------------------------------------------------------------------------- #


def test_vererbung_aus_setzt_auf_die_grundrechte_zurueck():
    kanaele = baum((1, 0), (2, 1))
    acls = {
        0: acl(0),
        1: acl(1, eintrag("all", deny=W.SPEAK)),
        2: acl(2, inherit=False),
    }
    oben = W.rechte_einer_rolle(rolle="x", ziel=1, kanaele=kanaele, acls=acls)
    unten = W.rechte_einer_rolle(rolle="x", ziel=2, kanaele=kanaele, acls=acls)
    assert oben.darf(W.SPEAK) is False
    # Der Kanal erbt nicht -- das Verbot von oben gilt hier nicht mehr.
    assert unten.darf(W.SPEAK) is True


def test_traverse_verlust_oben_wirkt_trotz_abgeschalteter_vererbung():
    """``bInheritACL`` stoppt die Kette **nicht**.

    ``effectivePermissions`` laeuft immer bis zur Wurzel; ein fehlendes
    Traverse weiter oben nimmt auch einem nicht erbenden Unterkanal alles.
    """
    kanaele = baum((1, 0), (2, 1))
    acls = {
        0: acl(0, eintrag("all", deny=W.TRAVERSE)),
        1: acl(1),
        2: acl(2, inherit=False),
    }
    w = W.rechte_einer_rolle(rolle="x", ziel=2, kanaele=kanaele, acls=acls)
    assert w.maske == 0


def test_write_haelt_die_kette_trotz_fehlendem_traverse():
    kanaele = baum((1, 0))
    acls = {
        0: acl(0, eintrag("all", allow=W.WRITE, deny=W.TRAVERSE)),
        1: acl(1),
    }
    w = W.rechte_einer_rolle(rolle="x", ziel=1, kanaele=kanaele, acls=acls)
    assert w.maske != 0
    assert w.darf(W.WRITE) is True


# --------------------------------------------------------------------------- #
#  Stolperstein 3: was Write mitbringt -- und was nicht
# --------------------------------------------------------------------------- #


def test_write_impliziert_vieles_aber_nicht_sprechen_und_nicht_fluestern():
    """Ein Admin darf alles verwalten, aber nicht ueberall reinreden."""
    kanaele = baum((1, 0))
    acls = {
        0: acl(0),
        1: acl(
            1,
            eintrag("all", deny=W.SPEAK | W.WHISPER | W.ENTER | W.LISTEN),
            eintrag("leitung", allow=W.WRITE),
        ),
    }
    w = W.rechte_einer_rolle(rolle="leitung", ziel=1, kanaele=kanaele, acls=acls)
    assert w.darf(W.ENTER) is True
    assert w.darf(W.LISTEN) is True
    assert w.darf(W.SPEAK) is False
    assert w.darf(W.WHISPER) is False


# --------------------------------------------------------------------------- #
#  Gruppen
# --------------------------------------------------------------------------- #


def test_geerbte_gruppe_wird_ueber_den_eigenen_eintrag_aufgeloest():
    """Geerbte Gruppen aus ``getACL`` haben eine leere ``add``-Liste.

    ``impl_Server_getACL`` fuellt bei geerbten Gruppen nur ``members``; ``add``
    bleibt leer. Wer die geerbten Eintraege zur Aufloesung heranzieht, haelt
    jede vererbte Rolle faelschlich fuer unbesetzt -- und zeigt dann zu wenig
    Rechte an.
    """
    kanaele = baum((1, 0))
    wurzel = acl(
        0,
        gruppen=(ChannelGroup(name="technik", add=[7]),),
    )
    # So wie getACL(1) es liefert: die Gruppe erscheint erneut, aber geerbt und
    # mit leerem add.
    unten = acl(
        1,
        eintrag("all", deny=W.SPEAK),
        eintrag("technik", allow=W.SPEAK),
        gruppen=(
            ChannelGroup(name="technik", add=[], members=[7], inherited=True),
        ),
    )
    acls = {0: wurzel, 1: unten}
    w = W.rechte_einer_person(userid=7, ziel=1, kanaele=kanaele, acls=acls)
    assert w.darf(W.SPEAK) is True


def test_gruppe_ohne_inherit_endet_beim_eigenen_kanal():
    kanaele = baum((1, 0))
    acls = {
        0: acl(0, gruppen=(ChannelGroup(name="technik", add=[7]),)),
        1: acl(
            1,
            eintrag("all", deny=W.SPEAK),
            eintrag("technik", allow=W.SPEAK),
            gruppen=(ChannelGroup(name="technik", inherit=False, add=[]),),
        ),
    }
    w = W.rechte_einer_person(userid=7, ziel=1, kanaele=kanaele, acls=acls)
    assert w.darf(W.SPEAK) is False


def test_remove_schlaegt_add_von_weiter_oben():
    kanaele = baum((1, 0))
    acls = {
        0: acl(0, gruppen=(ChannelGroup(name="technik", add=[7]),)),
        1: acl(
            1,
            eintrag("all", deny=W.SPEAK),
            eintrag("technik", allow=W.SPEAK),
            gruppen=(ChannelGroup(name="technik", remove=[7]),),
        ),
    }
    w = W.rechte_einer_person(userid=7, ziel=1, kanaele=kanaele, acls=acls)
    assert w.darf(W.SPEAK) is False


def test_ausrufezeichen_kehrt_um():
    kanaele = baum((1, 0))
    acls = {
        0: acl(0),
        1: acl(1, eintrag("!technik", deny=W.SPEAK)),
    }
    drin = W.rechte_einer_rolle(rolle="technik", ziel=1, kanaele=kanaele, acls=acls)
    draussen = W.rechte_einer_rolle(rolle="andere", ziel=1, kanaele=kanaele, acls=acls)
    assert drin.darf(W.SPEAK) is True
    assert draussen.darf(W.SPEAK) is False


# --------------------------------------------------------------------------- #
#  Unbestimmtheit
# --------------------------------------------------------------------------- #


def test_strong_macht_das_ergebnis_unbestimmt_statt_geraten():
    kanaele = baum((1, 0))
    acls = {
        0: acl(0),
        1: acl(1, eintrag("all", deny=W.SPEAK), eintrag("strong", allow=W.SPEAK)),
    }
    w = W.rechte_einer_rolle(rolle="x", ziel=1, kanaele=kanaele, acls=acls)
    assert w.darf(W.SPEAK) is None
    assert "strong" in w.gruende


def test_zugangswort_macht_das_ergebnis_unbestimmt():
    kanaele = baum((1, 0))
    acls = {
        0: acl(0),
        1: acl(1, eintrag("all", deny=W.ENTER), eintrag("#losung", allow=W.ENTER)),
    }
    w = W.rechte_einer_rolle(rolle="x", ziel=1, kanaele=kanaele, acls=acls)
    assert w.darf(W.ENTER) is None


def test_unbestimmt_bleibt_auf_die_betroffenen_bits_beschraenkt():
    kanaele = baum((1, 0))
    acls = {
        0: acl(0),
        1: acl(1, eintrag("all", deny=W.SPEAK), eintrag("strong", allow=W.SPEAK)),
    }
    w = W.rechte_einer_rolle(rolle="x", ziel=1, kanaele=kanaele, acls=acls)
    # Betreten haengt nicht am Zertifikat und bleibt eine klare Aussage.
    assert w.darf(W.ENTER) is True


def test_gegenlaeufige_angaben_werden_nicht_gegeneinander_aufgehoben():
    """``strong`` und ``!strong`` sind dieselbe Frage, nicht zwei.

    Wuerde man beide unabhaengig annehmen, entstuende der unmoegliche Fall
    "beide treffen zu" -- und das Ergebnis waere unnoetig unbestimmt.
    """
    kanaele = baum((1, 0))
    acls = {
        0: acl(0),
        1: acl(
            1,
            eintrag("strong", allow=W.MOVE),
            eintrag("!strong", allow=W.MOVE),
        ),
    }
    w = W.rechte_einer_rolle(rolle="x", ziel=1, kanaele=kanaele, acls=acls)
    # Einer von beiden trifft immer zu, also steht Move fest.
    assert w.darf(W.MOVE) is True


# --------------------------------------------------------------------------- #
#  Metagruppen
# --------------------------------------------------------------------------- #


def test_in_und_out_haengen_am_aufenthaltsort():
    kanaele = baum((1, 0), (2, 0))
    acls = {
        0: acl(0),
        1: acl(1, eintrag("out", deny=W.LISTEN)),
        2: acl(2),
    }
    drin = W.rechte_einer_person(
        userid=7, ziel=1, kanaele=kanaele, acls=acls, sitzt_in=1
    )
    woanders = W.rechte_einer_person(
        userid=7, ziel=1, kanaele=kanaele, acls=acls, sitzt_in=2
    )
    assert drin.darf(W.LISTEN) is True
    assert woanders.darf(W.LISTEN) is False


def test_sub_trifft_nur_unterhalb():
    """``sub`` prueft, ob der Nutzer tief genug unter dem Kanal steht."""
    kanaele = baum((1, 0), (2, 1))
    acls = {0: acl(0), 1: acl(1, eintrag("sub", allow=W.MOVE)), 2: acl(2)}
    drunter = W.rechte_einer_person(
        userid=7, ziel=1, kanaele=kanaele, acls=acls, sitzt_in=2
    )
    daneben = W.rechte_einer_person(
        userid=7, ziel=1, kanaele=kanaele, acls=acls, sitzt_in=1
    )
    assert drunter.darf(W.MOVE) is True
    assert daneben.darf(W.MOVE) is False
    assert drunter.unbestimmt == 0


# --------------------------------------------------------------------------- #
#  Wurzelrechte
# --------------------------------------------------------------------------- #


def test_wurzelrechte_gelten_nur_an_der_wurzel():
    kanaele = baum((1, 0))
    acls = {
        0: acl(0, eintrag("leitung", allow=W.KICK | W.BAN)),
        1: acl(1),
    }
    oben = W.rechte_einer_rolle(rolle="leitung", ziel=0, kanaele=kanaele, acls=acls)
    unten = W.rechte_einer_rolle(rolle="leitung", ziel=1, kanaele=kanaele, acls=acls)
    assert oben.darf(W.KICK) is True
    assert unten.darf(W.KICK) is False


def test_eintrag_fuer_eine_person_greift_ohne_gruppe():
    kanaele = baum((1, 0))
    acls = {
        0: acl(0),
        1: acl(1, eintrag("", deny=W.SPEAK, userid=7)),
    }
    betroffen = W.rechte_einer_person(userid=7, ziel=1, kanaele=kanaele, acls=acls)
    andere = W.rechte_einer_person(userid=8, ziel=1, kanaele=kanaele, acls=acls)
    assert betroffen.darf(W.SPEAK) is False
    assert andere.darf(W.SPEAK) is True
