#!/usr/bin/env bash
# Laedt MumbleServer.ice zum passenden Tag und uebersetzt sie mit slice2py.
#
# Wird im Dockerfile benutzt und laesst sich fuer die lokale Entwicklung
# genauso aufrufen:
#     admin/scripts/build_slice.sh v1.5.735 admin/slice
#
# Seit Mumble 1.5 heisst das Slice-Modul MumbleServer (vorher Murmur) und die
# Datei liegt weiterhin unter src/murmur/.
set -euo pipefail

MUMBLE_VERSION="${1:-${MUMBLE_VERSION:-v1.5.735}}"
OUT_DIR="${2:-${SLICE_DIR:-/opt/intercom/slice}}"
URL="https://raw.githubusercontent.com/mumble-voip/mumble/${MUMBLE_VERSION}/src/murmur/MumbleServer.ice"

mkdir -p "${OUT_DIR}"
echo "Slice ${MUMBLE_VERSION} von ${URL}"
curl -fsSL --retry 3 --retry-delay 2 -o "${OUT_DIR}/MumbleServer.ice" "${URL}"

# Ohne 'module MumbleServer' ist es die falsche Datei (z. B. eine 404-Seite).
grep -q '^module MumbleServer' "${OUT_DIR}/MumbleServer.ice" || {
  echo "FEHLER: ${OUT_DIR}/MumbleServer.ice enthaelt kein 'module MumbleServer'." >&2
  echo "        Existiert der Tag ${MUMBLE_VERSION}? Vor 1.5 hiess das Modul Murmur." >&2
  exit 1
}

# MumbleServer.ice bindet Ice-eigene Slices ein (Ice/SliceChecksumDict.ice).
# Wo slice2py sie findet, haengt an der Herkunft:
#
#   * aus dem PyPI-Paket: sie liegen im Paket selbst, slice2py findet sie allein.
#   * aus dem Debian-Paket (zeroc-ice-compilers): sie liegen unter
#     /usr/share/ice/slice und muessen ausdruecklich angegeben werden, sonst
#     bricht der Uebersetzer mit "Can't open include file" ab.
#
# Deshalb wird der Pfad nur gesetzt, wenn es ihn gibt -- damit laeuft dasselbe
# Skript in beiden Faellen.
SLICE_INCLUDE=""
for kandidat in /usr/share/ice/slice /usr/share/Ice/slice; do
  if [ -d "${kandidat}" ]; then
    SLICE_INCLUDE="-I${kandidat}"
    break
  fi
done

# --checksum erzeugt Pruefsummen, die wir zur Laufzeit gegen
# Meta.getSliceChecksums() halten. Ohne den Schalter bleibt Ice.sliceChecksums leer.
( cd "${OUT_DIR}" && slice2py ${SLICE_INCLUDE} --checksum MumbleServer.ice )

test -f "${OUT_DIR}/MumbleServer_ice.py" || { echo "slice2py hat nichts erzeugt" >&2; exit 1; }
echo "Slice uebersetzt nach ${OUT_DIR}"
