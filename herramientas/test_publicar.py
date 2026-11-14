#!/usr/bin/env python3
"""Pruebas de publicar.py (sin clave ni red): python3 -m unittest test_publicar (desde herramientas/comunidad)."""
import json
import os
import tempfile
import unittest
from datetime import date

import publicar as p

HOY = date(2026, 11, 13)


def fila(ident="a1", inicio="2026-11-20", fin="", url="https://ejemplo.org/c", estado="ok"):
    return {"estado": estado, "id": ident, "titulo": "Concierto", "inicio": inicio, "fin": fin, "municipio": "Burgos",
            "provincia": "Burgos", "organizador": "Asociación", "url": url}


def ids(filas, previos=frozenset(), dominios=None):
    eventos, _ = p.seleccionar(list(enumerate(filas, start=2)), HOY, set(previos), dominios)
    return [e["id"] for e in eventos]


class Publicar(unittest.TestCase):
    def test_solo_ok(self):
        self.assertEqual(["a1"], ids([fila(), fila("a2", estado="rechazado"), fila("a3", estado="")]))

    def test_nuevo_con_menos_de_dos_dias_se_descarta_con_motivo(self):
        _, desc = p.seleccionar([(2, fila(inicio="2026-11-14"))], HOY, set())
        self.assertEqual(1, len(desc))
        self.assertIn("llega tarde", desc[0][1])
        self.assertEqual(["a1"], ids([fila(inicio="2026-11-15")]))  # justo a 2 días: entra

    def test_ya_publicado_sigue_aunque_empiece_pronto(self):
        self.assertEqual(["a1"], ids([fila(inicio="2026-11-13")], previos={"a1"}))

    def test_exposicion_larga_ya_empezada_entra_si_no_acaba_pronto(self):
        self.assertEqual(["a1"], ids([fila(inicio="2026-11-01", fin="2027-01-31")]))
        self.assertEqual([], ids([fila(inicio="2026-11-01", fin="2026-11-14")]))

    def test_tope_de_nuevos_por_ejecucion(self):
        muchos = [fila(f"n{i}", url=f"https://ejemplo.org/{i}") for i in range(p.MAX_NUEVOS + 5)]
        self.assertEqual(p.MAX_NUEVOS, len(ids(muchos)))
        previos = {"n0", "n1", "n2"}  # los ya publicados no cuentan para el tope
        self.assertEqual(p.MAX_NUEVOS + 3, len(ids(muchos, previos=previos)))

    def test_tope_atiende_antes_a_los_mas_proximos_y_respeta_el_orden_del_csv(self):
        muchos = [fila(f"n{i}", inicio="2026-12-20", url=f"https://ejemplo.org/{i}") for i in range(p.MAX_NUEVOS)]
        urgente = fila("urgente", inicio="2026-11-16", url="https://ejemplo.org/u")
        elegidos = ids(muchos + [urgente])
        self.assertIn("urgente", elegidos)
        self.assertEqual(p.MAX_NUEVOS, len(elegidos))

    def test_dos_dias_seguidos_el_que_quedo_en_cola_entra_al_siguiente(self):
        cola = [fila(f"n{i}", inicio="2026-11-25", url=f"https://ejemplo.org/{i}") for i in range(p.MAX_NUEVOS + 1)]
        dia1, _ = p.seleccionar(list(enumerate(cola, start=2)), HOY, set())
        publicados = {e["id"] for e in dia1}
        dia2, desc = p.seleccionar(list(enumerate(cola, start=2)), date(2026, 11, 14), publicados)
        self.assertEqual(p.MAX_NUEVOS + 1, len(dia2))
        self.assertEqual([], desc)

    def test_duplicado_gana_el_ya_publicado_aunque_vaya_despues(self):
        self.assertEqual(["viejo"], ids([fila("nuevo"), fila("viejo")], previos={"viejo"}))

    def test_mismo_enlace_y_fecha_es_duplicado(self):
        self.assertEqual(["a1"], ids([fila("a1"), fila("a2")]))
        self.assertEqual(["a1", "a2"], ids([fila("a1"), fila("a2", inicio="2026-11-21")]))

    def test_lista_de_dominios(self):
        dom = {"ejemplo.org"}
        self.assertEqual(["a1", "a2"], ids([fila("a1"), fila("a2", url="https://www.ejemplo.org/x")], dominios=dom))
        self.assertEqual([], ids([fila(url="https://ejemplo.org.malo.com/x")], dominios=dom))
        self.assertEqual([], ids([fila(url="https://otroejemplo.org/x")], dominios=dom))
        self.assertEqual([], ids([fila(url="https://malo.com\\.ejemplo.org/x")], dominios=dom))
        self.assertEqual(["a1"], ids([fila(url="https://otro.es/x")]))  # sin lista: no se filtra por dominio

    def test_enlaces_peligrosos_siguen_fuera(self):
        for url in ("http://ejemplo.org", "https://bit.ly/x", "https://xn--exmplo-8wa.org", "javascript:alert(1)",
                    "https://usuario@ejemplo.org/", "https://malo.com\\.ejemplo.org/x", "https://mal_o.ejemplo.org/x"):
            self.assertEqual([], ids([fila(url=url)]), url)

    def test_leer_dominios_ignora_comentarios(self):
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as f:
            f.write("# lista\nEjemplo.org\n\nayto.es  # ayuntamiento\n")
        try:
            self.assertEqual({"ejemplo.org", "ayto.es"}, p.leer_dominios(f.name))
        finally:
            os.unlink(f.name)

    def test_anterior_lee_version_e_ids(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f:
            json.dump({"payload": json.dumps({"version": 7, "events": [{"id": "x1"}, {"id": "x2"}]})}, f)
        try:
            self.assertEqual((7, {"x1", "x2"}), p.anterior(f.name))
        finally:
            os.unlink(f.name)
        self.assertEqual((0, set()), p.anterior("/no/existe.json"))
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f:
            f.write("{roto")
        try:
            self.assertEqual((0, set()), p.anterior(f.name))  # corrupto = como si no hubiera (main avisa)
        finally:
            os.unlink(f.name)

    def test_correo_o_telefono_en_un_texto_publico_se_descarta(self):
        for campo, v in (("descripcion", "Escribe a ana@correo.es"), ("organizador", "Ana 612 345 678"),
                         ("titulo", "Llama al +34 912-34-56-78")):
            f = fila()
            f[campo] = v
            _, desc = p.seleccionar([(2, f)], HOY, set())
            self.assertIn("correo o un teléfono", desc[0][1], campo)
            self.assertTrue(desc[0][3], "es grave")
        f = fila()
        f["descripcion"] = "Del 20-11-2026 al 22-11-2026, 10 € la entrada. El 21-11-2026 18:00 y el 21.11.2026 20:00"
        self.assertEqual(["a1"], ids([f]))
        for tel in ("612345678", "91 234 56 78", "+34 612.345.678"):
            self.assertTrue(p.parece_telefono(f"Info {tel}."), tel)
        self.assertFalse(p.parece_telefono("612 34 56"))
        f = fila()
        f["municipio"] = "yo@correo.es"
        self.assertEqual([], ids([f]))

    def test_descartes_normales_y_graves(self):
        filas = [fila("fin", inicio="2026-11-01", fin="2026-11-10"), fila("tarde", inicio="2026-11-14", url="https://e.org/t"),
                 fila("dup"), fila("dup2"), fila("roto", url="http://e.org/r")]
        _, desc = p.seleccionar(list(enumerate(filas, start=2)), HOY, set())
        graves = {i: g for _, _, i, g in desc}
        self.assertEqual({"fin": False, "tarde": False, "dup2": False, "roto": True}, graves)

    def test_frenar(self):
        eventos, desc = p.seleccionar([(2, fila())], HOY, set())
        self.assertIsNone(p.frenar(set(), eventos, desc))
        _, desc = p.seleccionar([(2, fila(url="http://e.org"))], HOY, set())
        self.assertIn("filas 2", p.frenar(set(), [], desc))
        previos = {f"v{i}" for i in range(20)}
        self.assertIn("desaparecen 20", p.frenar(previos, [], []))  # hoja vacía: no se publica
        self.assertIsNone(p.frenar(previos, [], [], forzar=True))
        self.assertIsNone(p.frenar({f"v{i}" for i in range(5)}, [{"id": "v0"}], []))  # pocos: se admite
        self.assertIn("ningún plan", p.frenar({f"v{i}" for i in range(3)}, [], []))  # pero no quedarse sin nada
        self.assertIn("ningún plan", p.frenar(set(), [], []))  # ni la primera vez sin nada
        terminados = [(i + 2, "ya ha terminado", f"v{i}", False) for i in range(20)]
        self.assertIsNone(p.frenar(previos, [], terminados))  # los que terminan no cuentan
        mitad = [{"id": f"v{i}"} for i in range(10)]
        self.assertIsNone(p.frenar(previos, mitad, []))  # justo la mitad: vale
        self.assertIn("desaparecen 11", p.frenar(previos, mitad[:9], []))


if __name__ == "__main__":
    unittest.main()
