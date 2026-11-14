#!/usr/bin/env python3
"""Convierte el CSV exportado de la hoja en el index.json firmado.

Uso: python3 publicar.py entrada.csv salida/index.json --kid k1 [--clave RUTA | variable LIFESIDE_COMUNIDAD_CLAVE]
                         [--version-previa RUTA/index.json] [--dominios dominios.txt] [--estricto [--forzar]]
Solo filas con estado=ok. Las columnas de contacto se ignoran (no se leen). Sube `version` respecto al index.json
anterior (el de --version-previa o, si no, el de salida), pone expiresAt a +45 días y firma el payload.

Un plan es «nuevo» si su id no estaba en el index.json anterior. A los nuevos se les exige además (13-11-2026):
- que no empiecen en menos de 2 días (o, si duran 2 días o más, que no acaben antes): no llegarían a tiempo a la app;
- como mucho MAX_NUEVOS por ejecución (el resto, a la siguiente).
Con --dominios, solo se admiten enlaces de esos dominios (o subdominios suyos); uno por línea, # para comentarios.
Dos planes con el mismo enlace y la misma fecha de inicio se tratan como la misma propuesta: queda el ya publicado o, si
ninguno lo estaba, el primero. Los nuevos se atienden por fecha de inicio (los más próximos antes del tope).
Un correo o un teléfono en un texto público descarta el plan (el contacto es privado y va aparte).

--estricto (la publicación diaria de GitHub, 14-11-2026): no firma nada si
- el CSV no trae exactamente las columnas de la pestaña «publicar»;
- alguna fila `ok` está mal (no por haber pasado, llegar tarde, repetirse o esperar al tope: eso es lo normal);
- desaparecen de golpe más de la mitad de los planes publicados (y más de 5) sin haber terminado, o no queda ninguno
  (salvo que todos hayan terminado);
- hay un index.json anterior que no se puede leer.
Con --forzar, sí (lo primero no: las columnas y las filas mal nunca).
"""
import argparse
import base64
import csv
import json
import os
import re
import sys
import unicodedata
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlparse

MAX_EVENTOS = 500
MAX_NUEVOS = 30
DIAS_CADUCIDAD = 45  # publicación diaria; la app admite hasta 60, así un fallo de varias semanas no la deja sin planes
DIAS_ANTELACION = 2
MAX_BYTES = 1024 * 1024
MAX_DESAPARECIDOS = 5
# Las columnas de la pestaña «publicar» del Apps Script, en su orden
COLUMNAS_PUBLICAS = ["estado", "id", "titulo", "inicio", "fin", "hora", "lugar", "municipio", "provincia", "categoria",
                     "gratis", "precio", "url", "organizador", "descripcion"]
# Las mismas que Codigo.gs (CORREO, TELEFONO, CON_TEXTO_LIBRE)
CORREO = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", re.ASCII)
TELEFONO = re.compile(r"(?<!\d)(?:\+\d{2,3}[ .-]?)?([6789]\d{1,8}(?:[ .-]\d{2,3}){0,4})(?!\d)", re.ASCII)
CON_TEXTO_LIBRE = ("titulo", "lugar", "municipio", "provincia", "categoria", "precio", "organizador", "descripcion")


def parece_telefono(texto: str) -> bool:
    """9 cifras seguidas o en grupos de 2-3 (6, 7, 8 o 9 delante): una fecha con el año (grupo de 4) no lo es."""
    i = 0
    while (m := TELEFONO.search(texto, i)):
        if sum(c.isdigit() for c in m.group(1)) >= 9:
            return True
        i = m.start() + 1
    return False
ACORTADORES = {"bit.ly", "tinyurl.com", "t.co", "goo.gl", "ow.ly", "is.gd", "cutt.ly"}
# columna del CSV -> (clave del JSON, longitud máxima)
CAMPOS = {
    "id": ("id", 64), "titulo": ("title", 120), "lugar": ("place", 80), "municipio": ("town", 80),
    "provincia": ("province", 80), "categoria": ("category", 80), "precio": ("price", 80),
    "organizador": ("organizer", 80), "descripcion": ("desc", 600),
}


class Normal(ValueError):
    """Descarte esperado (plan terminado, que llega tarde, repetido o que espera al tope): no frena --estricto."""


def fecha(texto: str) -> date:
    # Motivos fijos, sin el valor: el registro de GitHub es público
    try:
        return date.fromisoformat(texto.strip())
    except ValueError:
        raise ValueError("fecha que no se entiende") from None


def https_valida(url: str) -> str:
    u = urlparse(url.strip())
    if u.scheme != "https" or not u.hostname or u.username or len(url) > 500 or any(c.isspace() for c in url.strip()):
        raise ValueError("url debe ser https sin espacios ni credenciales")
    if any(p.startswith("xn--") for p in u.hostname.lower().split(".")):
        raise ValueError("host xn-- no admitido")
    if any(c not in "abcdefghijklmnopqrstuvwxyz0123456789.-" for c in u.hostname.lower()):
        raise ValueError("host con caracteres no admitidos")
    if u.hostname.lower() in ACORTADORES:
        raise ValueError("acortador de enlaces no admitido")
    return url.strip()


def numero(texto: str, minimo: float, maximo: float):
    if not texto.strip():
        return None
    try:
        v = float(texto.strip().replace(",", "."))
    except ValueError:
        raise ValueError("coordenada que no se entiende") from None
    if v != v or not minimo <= v <= maximo:
        raise ValueError("coordenada fuera de rango")
    return v


def evento(fila: dict, hoy: date) -> dict:
    e = {}
    for col, (clave, largo) in CAMPOS.items():
        v = (fila.get(col) or "").strip()
        if any(unicodedata.category(c) in ("Cc", "Cf", "Cs", "Co", "Zl", "Zp") for c in v):
            raise ValueError(f"{col} lleva caracteres de control, formato o bidi")
        if v:
            if len(v) > largo:
                raise ValueError(f"{col} supera {largo} caracteres")
            if col in CON_TEXTO_LIBRE and (CORREO.search(v) or parece_telefono(v)):
                raise ValueError(f"{col} lleva un correo o un teléfono (no se publican)")
            e[clave] = v
    for obligatorio in ("id", "title", "town", "province", "organizer"):
        if obligatorio not in e:
            raise ValueError(f"falta {obligatorio}")
    ini = fecha(fila["inicio"])
    fin = fecha(fila["fin"]) if (fila.get("fin") or "").strip() else ini
    if fin < ini or ini > hoy + timedelta(days=365):
        raise ValueError("fechas incoherentes o a más de un año")
    if fin < hoy:
        raise Normal("ya ha terminado")
    e["start"], e["end"] = ini.isoformat(), fin.isoformat()
    hora = (fila.get("hora") or "").strip()
    if hora:
        try:
            datetime.strptime(hora, "%H:%M")
        except ValueError:
            raise ValueError("hora que no se entiende") from None
        e["time"] = hora
    e["url"] = https_valida(fila["url"])
    lat, lon = numero(fila.get("lat") or "", -90, 90), numero(fila.get("lon") or "", -180, 180)
    if (lat is None) != (lon is None):
        raise ValueError("lat y lon van juntas")
    if lat is not None:
        e["lat"], e["lon"] = lat, lon
    gratis = (fila.get("gratis") or "").strip().lower()
    if gratis in ("si", "sí", "true", "1"):
        e["free"] = True
    elif gratis in ("no", "false", "0"):
        e["free"] = False
    return e


def dominio_admitido(url: str, dominios) -> bool:
    if dominios is None:
        return True
    host = (urlparse(url).hostname or "").lower()
    return any(host == d or host.endswith("." + d) for d in dominios)


def leer_dominios(ruta: str) -> set:
    with open(ruta, encoding="utf-8") as f:
        return {l.split("#")[0].strip().lower() for l in f if l.split("#")[0].strip()}


def seleccionar(filas, hoy: date, previos: set, dominios=None):
    """Filas del CSV (dicts, con su número de fila) -> (eventos, descartes). Sin red ni clave.

    Cada descarte es (fila, motivo, id, grave): grave = la fila está mal (no es un descarte Normal)."""
    eventos, descartes, ids, enlaces, nuevos = [], [], set(), set(), 0
    # Primero lo ya publicado (gana en duplicados y no gasta tope); los nuevos, por fecha de inicio (los que urgen antes)
    orden = sorted(filas, key=lambda nf: ((nf[1].get("id") or "").strip() not in previos, (nf[1].get("inicio") or "").strip()))
    for n, fila in orden:
        if (fila.get("estado") or "").strip().lower() != "ok":
            continue
        try:
            e = evento(fila, hoy)
            if e["id"] in ids:
                raise ValueError("id repetido")
            if (e["url"], e["start"]) in enlaces:
                raise Normal("mismo enlace y fecha que otra propuesta")
            if not dominio_admitido(e["url"], dominios):
                raise Normal(f"dominio no admitido: {urlparse(e['url']).hostname} (añádelo a la lista si es de fiar)")
            if e["id"] not in previos:
                ini, fin = date.fromisoformat(e["start"]), date.fromisoformat(e["end"])
                limite = hoy + timedelta(days=DIAS_ANTELACION)
                if (ini if (fin - ini).days < 2 else fin) < limite:
                    raise Normal(f"llega tarde: empieza en menos de {DIAS_ANTELACION} días")
                if nuevos >= MAX_NUEVOS:
                    raise Normal(f"más de {MAX_NUEVOS} nuevos en esta ejecución: irá en la siguiente")
                nuevos += 1
        except (ValueError, KeyError) as err:
            motivo = str(err) if not isinstance(err, KeyError) else f"falta la columna {err}"
            descartes.append((n, motivo, (fila.get("id") or "").strip(), not isinstance(err, Normal)))
            continue
        ids.add(e["id"])
        enlaces.add((e["url"], e["start"]))
        eventos.append(e)
    return eventos, descartes


def frenar(previos: set, eventos, descartes, forzar: bool = False):
    """Motivo para no publicar en modo estricto, o None."""
    graves = [n for n, _, _, grave in descartes if grave]
    if graves:
        return f"{len(graves)} filas aprobadas están mal (filas {', '.join(map(str, graves[:10]))})"
    normales = {i for _, _, i, grave in descartes if not grave}
    desaparecidos = previos - {e["id"] for e in eventos} - normales
    if not forzar and len(desaparecidos) > max(MAX_DESAPARECIDOS, len(previos) // 2):
        return (f"desaparecen {len(desaparecidos)} de los {len(previos)} planes publicados sin haber terminado "
                "(¿hoja vacía o cambiada?); si es a propósito, lánzalo a mano con «forzar»")
    if not forzar and not eventos and (desaparecidos or not previos):
        return "no queda ningún plan (¿hoja vacía?); si es a propósito, lánzalo a mano con «forzar»"
    return None


def leer_clave(ruta):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    if ruta:
        with open(os.path.expanduser(ruta)) as f:
            b64 = f.read().strip()
    else:
        b64 = os.environ.get("LIFESIDE_COMUNIDAD_CLAVE", "").strip()
    if not b64:
        sys.exit("Falta la clave privada: --clave RUTA o la variable LIFESIDE_COMUNIDAD_CLAVE")
    return Ed25519PrivateKey.from_private_bytes(base64.b64decode(b64))


def anterior(ruta: str):
    """(versión, ids publicados) del index.json anterior; (0, vacío) si no hay."""
    try:
        with open(ruta, encoding="utf-8") as f:
            p = json.loads(json.load(f)["payload"])
        return int(p["version"]), {e.get("id") for e in p.get("events", []) if isinstance(e, dict)}
    except (OSError, ValueError, KeyError, TypeError):
        return 0, set()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("csv")
    ap.add_argument("salida")
    ap.add_argument("--kid", required=True)
    ap.add_argument("--clave")
    ap.add_argument("--version-previa")
    ap.add_argument("--dominios")
    ap.add_argument("--estricto", action="store_true", help="no firmar si algo no cuadra (publicación automática)")
    ap.add_argument("--forzar", action="store_true", help="con --estricto, publicar aunque desaparezcan muchos planes")
    a = ap.parse_args()
    hoy = date.today()
    ruta_previa = a.version_previa or a.salida
    version, previos = anterior(ruta_previa)
    if a.estricto and version == 0 and os.path.exists(ruta_previa):
        sys.exit("El index.json anterior no se puede leer: no se publica nada")
    if version == 0:
        print("Aviso: no hay index.json anterior; todo cuenta como nuevo (antelación y tope de 30 se aplican a todo).",
              file=sys.stderr)
    with open(a.csv, newline="", encoding="utf-8-sig") as f:
        lector = csv.DictReader(f)
        filas = list(enumerate(lector, start=2))
        if a.estricto and lector.fieldnames != COLUMNAS_PUBLICAS:
            sys.exit("Las columnas del CSV no son las de la pestaña «publicar»: no se publica nada")
    eventos, descartes = seleccionar(filas, hoy, previos, leer_dominios(a.dominios) if a.dominios else None)
    for n, motivo, _, _ in descartes:
        print(f"Fila {n} descartada: {motivo}", file=sys.stderr)
    if a.estricto:
        problema = frenar(previos, eventos, descartes, a.forzar)
        if problema:
            sys.exit(problema + ": no se publica nada")
    if len(eventos) > MAX_EVENTOS:
        sys.exit(f"Más de {MAX_EVENTOS} eventos: la app rechazaría el fichero")
    ahora = datetime.now(timezone.utc).replace(microsecond=0)
    payload = json.dumps(
        {
            "version": version + 1,
            "generatedAt": ahora.isoformat().replace("+00:00", "Z"),
            "expiresAt": (ahora + timedelta(days=DIAS_CADUCIDAD)).isoformat().replace("+00:00", "Z"),
            "events": eventos,
        },
        ensure_ascii=False, separators=(",", ":"),
    )
    firma = leer_clave(a.clave).sign(payload.encode("utf-8"))
    sobre = json.dumps({"kid": a.kid, "sig": base64.b64encode(firma).decode(), "payload": payload}, ensure_ascii=False)
    if len(sobre.encode("utf-8")) > MAX_BYTES:
        sys.exit("El fichero supera 1 MB: la app lo rechazaría")
    os.makedirs(os.path.dirname(os.path.abspath(a.salida)), exist_ok=True)
    with open(a.salida, "w", encoding="utf-8") as f:
        f.write(sobre)
    print(f"{len(eventos)} eventos publicados en {a.salida}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
