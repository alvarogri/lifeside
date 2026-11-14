#!/usr/bin/env python3
"""Pruebas de punta a punta de publicar.py --estricto + verificar.py (necesitan `cryptography`; sin red).

python3 -m unittest test_verificar (desde herramientas/comunidad)
"""
import base64
import csv
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import date, timedelta

import publicar as p

try:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    import verificar as v
except ImportError:  # pragma: no cover
    Ed25519PrivateKey = None

AQUI = os.path.dirname(os.path.abspath(__file__))
PRONTO = (date.today() + timedelta(days=7)).isoformat()


def claves():
    k = Ed25519PrivateKey.generate()
    priv = k.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())
    pub = k.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return base64.b64encode(priv).decode(), base64.b64encode(pub).decode()


def plan(ident, **extra):
    f = {c: "" for c in p.COLUMNAS_PUBLICAS}
    f.update(estado="ok", id=ident, titulo="Concierto", inicio=PRONTO, municipio="Burgos", provincia="Burgos",
             organizador="Asociación", url=f"https://ejemplo.org/{ident}")
    f.update(extra)
    return f


@unittest.skipIf(Ed25519PrivateKey is None, "falta cryptography (pip install cryptography)")
class PuntaAPunta(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.priv, self.pub = claves()
        self.salida = os.path.join(self.dir.name, "datos", "index.json")

    def tearDown(self):
        self.dir.cleanup()

    def csv(self, filas, columnas=p.COLUMNAS_PUBLICAS):
        ruta = os.path.join(self.dir.name, "entrada.csv")
        with open(ruta, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=columnas, extrasaction="ignore")
            w.writeheader()
            w.writerows(filas)
        return ruta

    def publicar(self, filas, *extra, columnas=p.COLUMNAS_PUBLICAS):
        entorno = dict(os.environ, LIFESIDE_COMUNIDAD_CLAVE=self.priv)
        return subprocess.run([sys.executable, "-I", os.path.join(AQUI, "publicar.py"), self.csv(filas, columnas),
                               self.salida, "--kid", "ci1", "--estricto", *extra],
                              env=entorno, capture_output=True, text=True)

    def leer(self):
        with open(self.salida, "rb") as f:
            return f.read()

    def test_publica_y_verifica(self):
        r = self.publicar([plan("a1"), plan("a2", estado="rechazado")])
        self.assertEqual(0, r.returncode, r.stderr)
        self.assertIsNone(v.problema(self.leer(), "ci1", self.pub))
        eventos = json.loads(json.loads(self.leer())["payload"])["events"]
        self.assertEqual(["a1"], [e["id"] for e in eventos])

    def test_otra_clave_o_kid_o_firma_tocada(self):
        self.assertEqual(0, self.publicar([plan("a1")]).returncode)
        self.assertIn("firma", v.problema(self.leer(), "ci1", claves()[1]))
        self.assertIn("kid", v.problema(self.leer(), "k1", self.pub))
        sobre = json.loads(self.leer())
        sobre["payload"] = sobre["payload"].replace("Concierto", "Conciertx")
        self.assertIn("firma", v.problema(json.dumps(sobre).encode(), "ci1", self.pub))

    def test_columnas_distintas_no_publica(self):
        r = self.publicar([plan("a1")], columnas=p.COLUMNAS_PUBLICAS + ["contacto"])
        self.assertNotEqual(0, r.returncode)
        self.assertIn("columnas", r.stderr)
        self.assertFalse(os.path.exists(self.salida))

    def test_una_fila_mal_no_publica_nada(self):
        r = self.publicar([plan("a1"), plan("a2", url="http://ejemplo.org/x")])
        self.assertNotEqual(0, r.returncode)
        self.assertFalse(os.path.exists(self.salida))

    def test_correo_no_publica_nada(self):
        r = self.publicar([plan("a1", descripcion="Info en yo@correo.es")])
        self.assertNotEqual(0, r.returncode)
        self.assertNotIn("yo@correo.es", r.stderr + r.stdout)  # el log de GitHub es público: no se copia el dato
        self.assertFalse(os.path.exists(self.salida))

    def test_hoja_vacia_frena_y_forzar_publica(self):
        self.assertEqual(0, self.publicar([plan(f"a{i}") for i in range(12)]).returncode)
        r = self.publicar([])
        self.assertNotEqual(0, r.returncode)
        self.assertIn("desaparecen 12", r.stderr)
        self.assertEqual(12, len(json.loads(json.loads(self.leer())["payload"])["events"]))  # sigue el anterior
        self.assertEqual(0, self.publicar([], "--forzar").returncode)
        self.assertEqual([], json.loads(json.loads(self.leer())["payload"])["events"])

    def test_verificar_detecta_correo_o_telefono_aunque_venga_firmado(self):
        k = Ed25519PrivateKey.from_private_bytes(base64.b64decode(self.priv))
        for texto in ("yo@correo.es", "612 345 678"):
            payload = json.dumps({"version": 1, "generatedAt": self.ahora(), "expiresAt": self.ahora(30),
                                  "events": [dict(self.evento(), desc=texto, lat=40.4167754, lon=-3.7)]})
            sobre = json.dumps({"kid": "ci1", "sig": base64.b64encode(k.sign(payload.encode())).decode(), "payload": payload})
            self.assertIn("correo o un teléfono", v.problema(sobre.encode(), "ci1", self.pub))

    @staticmethod
    def ahora(dias=0):
        from datetime import datetime, timezone
        t = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(days=dias)
        return t.isoformat().replace("+00:00", "Z")

    @staticmethod
    def evento():
        return {"id": "a1", "title": "Concierto", "town": "Burgos", "province": "Burgos", "organizer": "Asoc.",
                "start": PRONTO, "end": PRONTO, "url": "https://ejemplo.org/a"}

    def firmado(self, payload):
        k = Ed25519PrivateKey.from_private_bytes(base64.b64decode(self.priv))
        texto = payload if isinstance(payload, str) else json.dumps(payload)
        return json.dumps({"kid": "ci1", "sig": base64.b64encode(k.sign(texto.encode())).decode(),
                           "payload": texto}).encode()

    def test_verificar_comprueba_todo_aunque_venga_firmado(self):
        bueno = {"version": 2, "generatedAt": self.ahora(), "expiresAt": self.ahora(45), "events": [self.evento()]}
        self.assertIsNone(v.problema(self.firmado(bueno), "ci1", self.pub))
        self.assertIn("no sube", v.problema(self.firmado(bueno), "ci1", self.pub, version_previa=2))
        casos = {
            "enlace raro": dict(self.evento(), url="http://ejemplo.org/a"),
            "en town": dict(self.evento(), town="a@b.es"),
            "antes de empezar": dict(self.evento(), end="2020-01-01"),
            "no se entiende": dict(self.evento(), time="25:99"),
            "fuera de rango": dict(self.evento(), lat=95, lon=0),
            "lat sin lon": dict(self.evento(), lat=40),
            "free": dict(self.evento(), free="sí"),
            "claves": dict(self.evento(), contacto="x"),
        }
        for esperado, e in casos.items():
            self.assertIn(esperado, v.problema(self.firmado(dict(bueno, events=[e])), "ci1", self.pub), esperado)
        self.assertIn("repite id", v.problema(self.firmado(dict(bueno, events=[self.evento(), self.evento()])), "ci1", self.pub))
        self.assertIn("fuera de lo normal", v.problema(self.firmado(dict(bueno, expiresAt=self.ahora(90))), "ci1", self.pub))
        self.assertIn("fuera de lo normal", v.problema(self.firmado(dict(bueno, generatedAt=self.ahora(-3))), "ci1", self.pub))
        repetida = json.dumps(bueno)[:-1] + ', "version": 9}'
        self.assertIn("no es JSON", v.problema(self.firmado(repetida), "ci1", self.pub))
        nan = json.dumps(dict(bueno, events=[dict(self.evento(), lat=float("nan"), lon=0)]))
        self.assertIn("no es JSON", v.problema(self.firmado(nan), "ci1", self.pub))

    def test_primera_vez_sin_planes_frena(self):
        r = self.publicar([plan("a1", estado="rechazado")])
        self.assertNotEqual(0, r.returncode)
        self.assertIn("ningún plan", r.stderr)
        self.assertEqual(0, self.publicar([], "--forzar").returncode)

    def test_anterior_ilegible_no_publica(self):
        os.makedirs(os.path.dirname(self.salida))
        with open(self.salida, "w") as f:
            f.write("{roto")
        r = self.publicar([plan("a1")], "--forzar")
        self.assertNotEqual(0, r.returncode)
        self.assertIn("no se puede leer", r.stderr)

    def test_fecha_mal_no_sale_en_el_registro(self):
        r = self.publicar([plan("a1", inicio="20/11/2026-secreto")])
        self.assertNotEqual(0, r.returncode)
        self.assertNotIn("secreto", r.stderr + r.stdout)

    def test_verificar_forma(self):
        self.assertEqual("no es JSON", v.problema(b"{roto", "ci1", self.pub))
        self.assertIn("sobre", v.problema(b'{"kid":"ci1"}', "ci1", self.pub))
        self.assertEqual("supera 1 MB", v.problema(b" " * (p.MAX_BYTES + 1), "ci1", self.pub))


if __name__ == "__main__":
    unittest.main()
