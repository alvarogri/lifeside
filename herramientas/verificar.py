#!/usr/bin/env python3
"""Comprueba un index.json firmado ANTES de subirlo (último paso de la publicación diaria, 14-11-2026).

Uso: python3 verificar.py index.json --kid ci1 --publica BASE64 [--previo index-anterior.json]
     (la clave pública también por la variable COMUNIDAD_CLAVE_PUBLICA)
Falla (código 1, sin mostrar datos) si el sobre no es el de publicar.py, la firma no cuadra con esa clave pública,
el payload no tiene la forma que espera la app (claves, tipos, fechas, enlaces https, caducidad), `version` no sube
respecto al anterior o algún texto lleva algo que parece un correo o un teléfono.
"""
import argparse
import base64
import json
import os
import sys
from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # con `python3 -I` la carpeta del script no va sola
from publicar import CORREO, MAX_BYTES, MAX_EVENTOS, https_valida, parece_telefono  # noqa: E402

CLAVES_EVENTO = {"id", "title", "place", "town", "province", "category", "price", "organizer", "desc", "start", "end",
                 "time", "url", "lat", "lon", "free"}
TEXTOS = ("id", "title", "place", "town", "province", "category", "price", "organizer", "desc")
MAX_DIAS_CADUCIDAD = 60  # lo que admite la app


def estricto(texto: str):
    """JSON sin claves repetidas ni NaN/Infinity."""
    def sin_repetidas(pares):
        d = dict(pares)
        if len(d) != len(pares):
            raise ValueError("clave repetida")
        return d

    def sin_constantes(nombre):
        raise ValueError(nombre)
    return json.loads(texto, object_pairs_hook=sin_repetidas, parse_constant=sin_constantes)


def instante(texto) -> datetime:
    if not isinstance(texto, str) or not texto.endswith("Z"):
        raise ValueError("instante")
    return datetime.fromisoformat(texto[:-1] + "+00:00")


def problema_evento(e) -> str:
    if not isinstance(e, dict) or not set(e) <= CLAVES_EVENTO or not {"id", "title", "start", "end", "url"} <= set(e):
        return "tiene claves desconocidas o le faltan"
    for k in TEXTOS:
        v = e.get(k, "")
        if not isinstance(v, str):
            return f"tiene un {k} que no es texto"
        if CORREO.search(v) or parece_telefono(v):
            return f"lleva un correo o un teléfono en {k}"
    try:
        if not isinstance(e["url"], str) or https_valida(e["url"]) != e["url"]:
            raise ValueError
    except ValueError:
        return "tiene un enlace raro"
    if CORREO.search(e["url"]):
        return "lleva un correo en el enlace"
    try:
        if date.fromisoformat(e["end"]) < date.fromisoformat(e["start"]):
            return "acaba antes de empezar"
        if "time" in e:
            datetime.strptime(e["time"], "%H:%M")
    except (ValueError, TypeError):
        return "tiene una fecha u hora que no se entiende"
    if ("lat" in e) != ("lon" in e):
        return "tiene lat sin lon o al revés"
    for k, limite in (("lat", 90), ("lon", 180)):
        if k in e and (isinstance(e[k], bool) or not isinstance(e[k], (int, float)) or not -limite <= e[k] <= limite):
            return f"tiene un {k} fuera de rango"
    if "free" in e and not isinstance(e["free"], bool):
        return "tiene un free que no es sí/no"
    return None


def problema(contenido: bytes, kid: str, publica_b64: str, version_previa: int = 0):
    """None si el fichero está bien; si no, el motivo (sin copiar nada del contenido)."""
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    if len(contenido) > MAX_BYTES:
        return "supera 1 MB"
    try:
        sobre = estricto(contenido.decode("utf-8"))
    except ValueError:
        return "no es JSON"
    if not isinstance(sobre, dict) or set(sobre) != {"kid", "sig", "payload"}:
        return "el sobre no es {kid, sig, payload}"
    if sobre["kid"] != kid:
        return "kid distinto del esperado"
    try:
        publica = Ed25519PublicKey.from_public_bytes(base64.b64decode(publica_b64, validate=True))
        publica.verify(base64.b64decode(sobre["sig"], validate=True), sobre["payload"].encode("utf-8"))
    except (ValueError, TypeError, AttributeError, InvalidSignature):
        return "la firma no cuadra con la clave pública"
    try:
        p = estricto(sobre["payload"])
    except (ValueError, AttributeError):
        return "el payload no es JSON"
    if not isinstance(p, dict) or set(p) != {"version", "generatedAt", "expiresAt", "events"}:
        return "el payload no tiene las claves esperadas"
    if isinstance(p["version"], bool) or not isinstance(p["version"], int) or not isinstance(p["events"], list) \
            or len(p["events"]) > MAX_EVENTOS:
        return "version o events con forma rara"
    if p["version"] <= version_previa:
        return "version no sube respecto al index.json anterior"
    try:
        hecho, caduca = instante(p["generatedAt"]), instante(p["expiresAt"])
    except ValueError:
        return "generatedAt o expiresAt no se entienden"
    ahora = datetime.now(hecho.tzinfo)
    if abs(ahora - hecho) > timedelta(hours=1) or not hecho < caduca <= hecho + timedelta(days=MAX_DIAS_CADUCIDAD):
        return "generatedAt o expiresAt fuera de lo normal"
    ids = set()
    for i, e in enumerate(p["events"]):
        motivo = problema_evento(e)
        if motivo:
            return f"el evento {i + 1} {motivo}"
        if e["id"] in ids:
            return f"el evento {i + 1} repite id"
        ids.add(e["id"])
    return None


def version_de(ruta: str) -> int:
    """version del index.json anterior (0 si no hay)."""
    try:
        with open(ruta, encoding="utf-8") as f:
            return int(json.loads(json.load(f)["payload"])["version"])
    except FileNotFoundError:
        return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("fichero")
    ap.add_argument("--kid", required=True)
    ap.add_argument("--publica", default=os.environ.get("COMUNIDAD_CLAVE_PUBLICA", ""))
    ap.add_argument("--previo", help="index.json anterior: version tiene que subir")
    a = ap.parse_args()
    if not a.publica.strip():
        sys.exit("Falta la clave pública: --publica o la variable COMUNIDAD_CLAVE_PUBLICA")
    with open(a.fichero, "rb") as f:
        motivo = problema(f.read(), a.kid, a.publica.strip(), version_de(a.previo) if a.previo else 0)
    if motivo:
        sys.exit(f"index.json no válido: {motivo}")
    print("index.json comprobado: firma y formato bien")
    return 0


if __name__ == "__main__":
    sys.exit(main())
