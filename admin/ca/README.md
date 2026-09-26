# Eigene Zertifizierungsstellen fuer den Bau

Hier abgelegte `*.crt`-Dateien (PEM) werden **in der Builder-Stufe** in den
Zertifikatsspeicher aufgenommen. Gedacht fuer Netze, in denen ein Proxy TLS
aufbricht: ohne die CA scheitern `pip`, `git clone` und `curl` beim Bau mit
`CERTIFICATE_VERIFY_FAILED` bzw. `self-signed certificate in chain`.

Im Regelfall bleibt das Verzeichnis leer und der Bau verhaelt sich unveraendert.

**Die Zertifikate landen nicht im Laufzeit-Image.** Die Builder-Stufe wird
verworfen; die Laufzeitstufe laedt nichts mehr aus dem Netz (`pip install
--no-index` aus den fertigen Wheels). Ein Bau hinter einem Firmenproxy erzeugt
damit dasselbe Image wie ein Bau ohne.

Dateiendung muss `.crt` sein -- `update-ca-certificates` ignoriert alles andere.
Proxy-Adressen kommen getrennt als Build-Argumente:

```bash
docker build \
  --build-arg HTTPS_PROXY="http://proxy.intern:3128" \
  --build-arg NO_PROXY="localhost,127.0.0.1" \
  -t stadion-intercom/mumble-admin:v1.5.735 admin/
```
