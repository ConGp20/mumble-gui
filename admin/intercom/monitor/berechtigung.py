"""Der Monitor-Bot braucht ``Register`` am obersten Platz -- und bekommt es selbst.

murmur 1.5.735, ``Server::msgUserStats`` (``src/murmur/Messages.cpp``)::

    bool extend = (uSource == pDstServerUser)
                  || hasPermission(uSource, qhChannels.value(0), ChanACL::Register);
    ...
    bool local  = extend || (pDstServerUser->cChannel == uSource->cChannel);

Die Paketzaehler (``from_client``/``from_server``) fuellt murmur nur bei
``local``. Ohne ``Register`` am obersten Platz saehe der Bot sie also nur fuer
Clients auf seinem eigenen Platz, und die Verlustspalte bliebe fuer alle
anderen leer -- ohne Fehlermeldung. Gemessen in
``test_monitor_braucht_register_am_obersten_platz_nicht_ban``; dort steht auch,
dass ``Ban`` (worauf sich D-015 berief) in dieser Version **nicht** reicht.

Wie: eine Regel fuer die Gruppe ``$<Zertifikats-Hash>``. murmur vergleicht sie
mit ``user.qsHash`` (``Group::appliesToUser``) -- es braucht also weder eine
Registrierung noch eine Rolle, und das Recht haengt genau an diesem einen
Zertifikat. Gilt nur am obersten Platz selbst, nicht darunter: dort wird
``Register`` ohnehin nicht ausgewertet.

Die Regel gehoert der Anwendung, nicht dem Aufbau. Planer und Export lassen sie
deshalb in Ruhe (``geschuetzte_gruppen``): eine Show, die auf einer anderen
Installation gespeichert wurde, kennt diesen Hash nicht, und "Aufraeumen" darf
sie nicht wegraeumen. DECISIONS D-035.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..ice.types import ACLEntry, ChannelACL
from ..ice.wirkung import REGISTER

if TYPE_CHECKING:  # pragma: no cover
    from ..ice.client import IceClient

__all__ = ["gruppe", "ist_regel", "sicherstellen"]


def gruppe(fingerabdruck: str) -> str:
    """Die Gruppenangabe, unter der murmur genau dieses Zertifikat erkennt."""
    return "$" + fingerabdruck.strip().lower()


def ist_regel(eintrag: ACLEntry, fingerabdruck: str) -> bool:
    """Ist das die vollstaendige Regel fuer den Bot?"""
    return (
        eintrag.is_group
        and eintrag.group == gruppe(fingerabdruck)
        and eintrag.apply_here
        and bool(eintrag.allow & REGISTER)
        and not eintrag.deny & REGISTER
    )


def sicherstellen(client: IceClient, fingerabdruck: str) -> bool:
    """Setzt die Regel, falls sie fehlt. ``True``, wenn geschrieben wurde.

    Liest erst und schreibt nur bei Bedarf -- ``setACL`` ersetzt alle Regeln
    und Rollen des obersten Platzes auf einmal, und ein unnoetiger Umlauf ist
    ein unnoetiges Risiko. Rollen, Vererbung und alle anderen Regeln bleiben
    unveraendert; die neue steht am Ende, damit kein spaeterer Eintrag sie
    wieder wegnimmt.
    """
    acl = client.get_acl(0)
    eigene = acl.own_acls()
    if any(ist_regel(e, fingerabdruck) for e in eigene):
        return False
    # Eine unvollstaendige Fassung derselben Gruppe (etwa von Hand verbogen)
    # wird ersetzt statt verdoppelt.
    behalten = [e for e in eigene if not (e.is_group and e.group == gruppe(fingerabdruck))]
    behalten.append(
        ACLEntry(
            apply_here=True,
            apply_subs=False,
            group=gruppe(fingerabdruck),
            allow=REGISTER,
            deny=0,
        )
    )
    client.set_channel_acl(
        ChannelACL(channel_id=0, acls=behalten, groups=acl.own_groups(), inherit=acl.inherit)
    )
    return True
