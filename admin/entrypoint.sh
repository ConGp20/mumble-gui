#!/usr/bin/env bash
# Einstiegspunkt des Admin-Containers.
#
# Hier wird bewusst NICHT auf murmur gewartet -- das macht die Compose ueber
# depends_on/healthcheck, und die Anwendung baut die Ice-Verbindung ohnehin
# selbst wieder auf, wenn der Server spaeter kommt.
set -euo pipefail

DATA_DIR="${DATA_DIR:-/data}"

if [ ! -d "${DATA_DIR}" ]; then
  mkdir -p "${DATA_DIR}" 2> /dev/null || true
fi

if [ ! -w "${DATA_DIR}" ]; then
  cat >&2 <<MELDUNG
FEHLER: ${DATA_DIR} ist nicht beschreibbar.

Der Container laeuft als UID $(id -u):$(id -g). Das gemountete Verzeichnis
gehoert jemand anderem. Auf dem NAS im Projektverzeichnis:

    sudo chown -R 10000:10000 ./admin-data

Ohne beschreibbares ${DATA_DIR} gibt es keinen Metrik-Verlauf und kein
Audit-Log. Das GUI startet trotzdem und meldet es als Banner.
MELDUNG
fi

exec "$@"
