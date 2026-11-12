"""Pruebas locales del simulador del repartidor (sin red ni dependencias).

Ejecutar desde la raíz del repo:  python -m unittest discover -s tests -v
"""
import importlib.util
import io
import json
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("driver_sim", ROOT / "simulator" / "driver_sim.py")
sim = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sim)


class FakeWs:
    def __init__(self):
        self.sent = []

    def send(self, data):
        self.sent.append(json.loads(data))


class InterpolateTest(unittest.TestCase):
    def test_incluye_extremos_y_puntos_intermedios(self):
        pts = sim.interpolate([(0.0, 0.0), (1.0, 2.0)], 4)
        self.assertEqual(pts, [(0.0, 0.0), (0.25, 0.5), (0.5, 1.0), (0.75, 1.5), (1.0, 2.0)])

    def test_steps_invalido(self):
        with self.assertRaises(ValueError):
            sim.interpolate(sim.DEFAULT_ROUTE, 0)


class PlanTest(unittest.TestCase):
    def test_sin_mensaje_viejo(self):
        plan = sim.build_plan([(1, 1), (2, 2)])
        self.assertEqual(plan, [(1, 1, False), (2, 2, False)])

    def test_inserta_mensaje_viejo_tras_el_punto_n(self):
        plan = sim.build_plan([(1, 1), (2, 2), (3, 3)], stale_after=2)
        self.assertEqual([p[2] for p in plan], [False, False, True, False])


class RunTest(unittest.TestCase):
    def run_plan(self, plan, clock_values, interval=5):
        ws, sleep = FakeWs(), mock.Mock()
        clock = mock.Mock(side_effect=clock_values)
        sim.run(ws, plan, "ORD-1", "DRV-1", interval, stale_age=60, clock=clock, sleep=sleep, log=lambda *_: None)
        return ws.sent, sleep

    def test_formato_del_mensaje(self):
        sent, _ = self.run_plan([(4.65, -74.05, False)], [1700000000.7])
        self.assertEqual(sent, [{"action": "location-update", "orderId": "ORD-1", "driverId": "DRV-1",
                                 "lat": 4.65, "lng": -74.05, "ts": 1700000000}])

    def test_espera_el_intervalo_entre_mensajes(self):
        _, sleep = self.run_plan([(1, 1, False), (2, 2, False), (3, 3, False)], [100, 105, 110])
        self.assertEqual(sleep.call_args_list, [mock.call(5), mock.call(5)])

    def test_ts_estrictamente_creciente_aunque_el_reloj_no_avance(self):
        sent, _ = self.run_plan([(1, 1, False)] * 3, [100, 100, 100], interval=0)
        self.assertEqual([m["ts"] for m in sent], [100, 101, 102])

    def test_mensaje_viejo_tiene_ts_menor_al_ultimo(self):
        plan = sim.build_plan([(1, 1), (2, 2), (3, 3)], stale_after=2)
        sent, sleep = self.run_plan(plan, [100, 105, 110])
        self.assertEqual([m["ts"] for m in sent], [100, 105, 45, 110])
        self.assertLess(sent[2]["ts"], sent[1]["ts"])
        self.assertEqual(sleep.call_count, 2)  # el viejo se envía de inmediato


class CliTest(unittest.TestCase):
    def test_driver_url_agrega_role(self):
        self.assertEqual(sim.driver_url("wss://x.example/prod"), "wss://x.example/prod?role=driver")
        self.assertEqual(sim.driver_url("wss://x.example/prod?a=1"), "wss://x.example/prod?a=1&role=driver")

    def test_url_obligatoria_si_no_es_dry_run(self):
        with mock.patch.dict("os.environ", {}, clear=True), redirect_stdout(io.StringIO()), \
                mock.patch("sys.stderr", io.StringIO()), self.assertRaises(SystemExit):
            sim.parse_args(["--order-id", "O", "--driver-id", "D"])

    def test_dry_run(self):
        out = io.StringIO()
        with redirect_stdout(out):
            code = sim.main(["--order-id", "ORD-1", "--driver-id", "DRV-1", "--dry-run",
                             "--interval", "0", "--steps", "1", "--stale-after", "2"])
        self.assertEqual(code, 0)
        self.assertEqual(out.getvalue().count('"action": "location-update"'), len(sim.DEFAULT_ROUTE) + 1)
        self.assertIn("VIEJO", out.getvalue())

    def test_ruta_pasto(self):
        out = io.StringIO()
        with redirect_stdout(out):
            code = sim.main(["--order-id", "ORD-1", "--driver-id", "DRV-1", "--dry-run",
                             "--interval", "0", "--steps", "1", "--route", "pasto"])
        self.assertEqual(code, 0)
        self.assertEqual(out.getvalue().count('"action": "location-update"'), len(sim.PASTO_ROUTE))
        first = sim.PASTO_ROUTE[0]
        self.assertIn(f'"lat": {first[0]}', out.getvalue())

    def test_ruta_por_defecto_es_bogota(self):
        args = sim.parse_args(["--order-id", "O", "--driver-id", "D", "--dry-run"])
        self.assertEqual(args.route, "bogota")

    def test_ruta_invalida(self):
        with redirect_stdout(io.StringIO()), mock.patch("sys.stderr", io.StringIO()), \
                self.assertRaises(SystemExit):
            sim.parse_args(["--order-id", "O", "--driver-id", "D", "--dry-run", "--route", "cali"])


if __name__ == "__main__":
    unittest.main()
