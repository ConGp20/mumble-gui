#!/usr/bin/env bash
# Laedt die Frontend-Bibliotheken beim IMAGE-BAU herunter.
#
# Warum beim Bau und nicht zur Laufzeit: das Stadion hat nicht immer Internet.
# Alles, was der Browser braucht, liegt im Image. Kein CDN, kein Nachladen.
#
# Warum ohne npm: node gehoert nicht ins Laufzeit-Image, und fuer drei Dateien
# lohnt keine zweite Werkzeugkette. Die Registry liefert die Tarballs auch so.
#
# Aufruf:  fetch_vendor.sh <zielverzeichnis>
set -euo pipefail

DEST="${1:-/opt/intercom/static/vendor}"

# Feste Versionen. Keine "latest"-Ueberraschung beim naechsten Bau, und die
# Pruefsummen fallen sofort auf, wenn sich unter der Hand etwas aendert.
#
# Die SSE-Erweiterung von htmx wird bewusst NICHT mitgeliefert: die
# Live-Aktualisierung laeuft ueber ein EventSource in der Alpine-Komponente,
# weil der Server JSON schickt und nicht fertige HTML-Schnipsel. Eine
# ungenutzte Bibliothek im Image waere nur Ballast.
#
#   Paket|Version|Pfad im Tarball|Zieldatei|sha256
PACKAGES=(
  "htmx.org|2.0.10|package/dist/htmx.min.js|htmx.min.js|71ea67185bfa8c98c39d31717c6fce5d852370fcdfd129db4543774d3145c0de"
  "alpinejs|3.16.3|package/dist/cdn.min.js|alpine.min.js|e31d6d92aefd41979d3c66f994d3a6b77fafa5062aec67d13f3ec5099d70d5d6"
)

mkdir -p "${DEST}"

sha256_of() {
  if command -v sha256sum > /dev/null 2>&1; then
    sha256sum "$1" | cut -d' ' -f1
  else
    shasum -a 256 "$1" | cut -d' ' -f1
  fi
}

tmp="$(mktemp -d)"
trap 'rm -rf "${tmp}"' EXIT

for entry in "${PACKAGES[@]}"; do
  IFS='|' read -r name version member target want <<< "${entry}"
  out="${DEST}/${target}"

  if [ -f "${out}" ] && [ "$(sha256_of "${out}")" = "${want}" ]; then
    echo "  ${target} ist bereits aktuell."
    continue
  fi

  # Die Registry legt Tarballs unter einem festen Muster ab; der Basisname ist
  # bei Scoped-Paketen der Teil hinter dem Schraegstrich.
  base="${name##*/}"
  url="https://registry.npmjs.org/${name}/-/${base}-${version}.tgz"

  echo "  ${target} <- ${name}@${version}"
  curl -fsSL --retry 3 --retry-delay 2 -o "${tmp}/pkg.tgz" "${url}"
  tar -xzf "${tmp}/pkg.tgz" -C "${tmp}" "${member}"
  got="$(sha256_of "${tmp}/${member}")"

  if [ "${got}" != "${want}" ]; then
    echo "FEHLER: ${name}@${version} hat die falsche Pruefsumme." >&2
    echo "        erwartet ${want}" >&2
    echo "        bekommen ${got}" >&2
    echo "        Entweder wurde das Paket neu veroeffentlicht oder die" >&2
    echo "        Verbindung ist nicht vertrauenswuerdig. Nicht ignorieren." >&2
    exit 1
  fi

  mv "${tmp}/${member}" "${out}"
  rm -rf "${tmp:?}/package"
done

echo "Frontend-Bibliotheken liegen in ${DEST}:"
ls -l "${DEST}"
