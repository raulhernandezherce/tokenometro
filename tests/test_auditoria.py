import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path

from tokenometro.auditoria import (
    analizar, cargar_precios, coste, informe_json, informe_texto, leer_llamadas, main, precio_de,
)

DATOS = Path(__file__).parent / "datos"
AHORA = datetime(2026, 11, 22, tzinfo=timezone.utc)


class TestLectura(unittest.TestCase):
    def setUp(self):
        self.llamadas = leer_llamadas(DATOS)

    def test_deduplica_por_id_y_se_queda_con_el_ultimo_bloque(self):
        ids = [ll.id for ll in self.llamadas]
        self.assertEqual(sorted(ids), ["m1", "m2", "m3"])
        m1 = next(ll for ll in self.llamadas if ll.id == "m1")
        self.assertEqual(m1.output, 500)

    def test_ignora_sinteticos_y_lineas_rotas(self):
        self.assertNotIn("<synthetic>", {ll.modelo for ll in self.llamadas})

    def test_proyecto_y_subagente(self):
        self.assertEqual({ll.proyecto for ll in self.llamadas}, {"web"})
        self.assertTrue(next(ll for ll in self.llamadas if ll.id == "m3").subagente)


class TestPrecios(unittest.TestCase):
    def setUp(self):
        self.precios = cargar_precios()

    def test_prefijo_mas_largo(self):
        self.assertEqual(precio_de("claude-opus-5-5", self.precios)["input"], 4)
        self.assertEqual(precio_de("claude-opus-4-8", self.precios)["input"], 5)
        self.assertEqual(precio_de("claude-sonnet-4-5-20250929", self.precios)["input"], 3)
        self.assertIsNone(precio_de("gpt-5", self.precios))

    def test_coste_de_una_llamada(self):
        m1 = next(ll for ll in leer_llamadas(DATOS) if ll.id == "m1")
        # 10 input * 4 + 500 output * 20 + 100000 cache 1h * 8  (USD por millón)
        self.assertAlmostEqual(coste(m1, self.precios), (40 + 10_000 + 800_000) / 1e6)


class TestAnalisis(unittest.TestCase):
    def setUp(self):
        self.inf = analizar(leer_llamadas(DATOS), cargar_precios(), ahora=AHORA)

    def test_totales(self):
        t = self.inf.total
        self.assertEqual(t.llamadas, 3)
        self.assertEqual(t.output, 1700)
        self.assertEqual(t.cache_read, 150_000)
        self.assertEqual(self.inf.subagentes.llamadas, 1)
        self.assertEqual(set(self.inf.por_modelo), {"claude-opus-5-5", "claude-haiku-5-5"})

    def test_filtro_por_dias(self):
        inf = analizar(leer_llamadas(DATOS), cargar_precios(), dias=1, ahora=AHORA)
        self.assertEqual(inf.total.llamadas, 1)

    def test_filtro_por_proyecto(self):
        inf = analizar(leer_llamadas(DATOS), cargar_precios(), proyecto="otro", ahora=AHORA)
        self.assertEqual(inf.total.llamadas, 0)
        self.assertIn("No hay llamadas", inf.consejos[0])

    def test_detecta_mucha_escritura_en_cache(self):
        self.assertTrue(any("Escribes en caché" in c for c in self.inf.consejos))

    def test_salidas(self):
        texto = informe_texto(self.inf)
        self.assertIn("Por modelo", texto)
        self.assertIn("Consejos", texto)
        json.dumps(informe_json(self.inf))

    def test_cli(self):
        salida = io.StringIO()
        with redirect_stdout(salida), redirect_stderr(io.StringIO()):
            self.assertEqual(main(["--dir", str(DATOS), "--dias", "0", "--json"]), 0)
            self.assertEqual(main(["--dir", str(DATOS / "no-existe")]), 2)
        self.assertEqual(json.loads(salida.getvalue())["total"]["llamadas"], 3)


if __name__ == "__main__":
    unittest.main()
