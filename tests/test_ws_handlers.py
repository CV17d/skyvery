"""Pruebas locales de ws_connect y ws_disconnect (sin AWS).

Ejecutar desde la raíz del repo:  python -m unittest discover -s tests -v
"""
import importlib.util
import os
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent


def load_handler(name):
    """Carga lambdas/<name>/app.py con boto3 simulado; devuelve (módulo, tabla mock)."""
    table = mock.MagicMock()
    fake_boto3 = mock.MagicMock()
    fake_boto3.resource.return_value.Table.return_value = table
    spec = importlib.util.spec_from_file_location(f"{name}_app", ROOT / "lambdas" / name / "app.py")
    module = importlib.util.module_from_spec(spec)
    with mock.patch.dict(sys.modules, {"boto3": fake_boto3}), \
            mock.patch.dict(os.environ, {"SUBSCRIPTIONS_TABLE": "subs-test"}):
        spec.loader.exec_module(module)
    fake_boto3.resource.return_value.Table.assert_called_once_with("subs-test")
    return module, table


def ws_event(connection_id="conn-1", query=None):
    return {"requestContext": {"connectionId": connection_id, "routeKey": "$connect"},
            "queryStringParameters": query}


class WsConnectTest(unittest.TestCase):
    def setUp(self):
        self.app, self.table = load_handler("ws_connect")

    def test_guarda_la_suscripcion(self):
        before = int(time.time())
        resp = self.app.handler(ws_event("abc=", {"orderId": "ORD-1"}), None)
        self.assertEqual(resp["statusCode"], 200)
        item = self.table.put_item.call_args.kwargs["Item"]
        self.assertEqual(item["connectionId"], "abc=")
        self.assertEqual(item["orderId"], "ORD-1")
        self.assertGreaterEqual(item["ttl"], before + self.app.TTL_SECONDS)
        self.assertIn("connectedAt", item)

    def test_sin_query_string_rechaza(self):
        resp = self.app.handler(ws_event(query=None), None)
        self.assertEqual(resp["statusCode"], 400)
        self.table.put_item.assert_not_called()

    def test_order_id_vacio_rechaza(self):
        resp = self.app.handler(ws_event(query={"orderId": "  "}), None)
        self.assertEqual(resp["statusCode"], 400)
        self.table.put_item.assert_not_called()

    def test_repartidor_conecta_sin_suscripcion(self):
        resp = self.app.handler(ws_event(query={"role": "driver"}), None)
        self.assertEqual(resp["statusCode"], 200)
        self.table.put_item.assert_not_called()


class WsDisconnectTest(unittest.TestCase):
    def setUp(self):
        self.app, self.table = load_handler("ws_disconnect")

    def test_borra_la_suscripcion(self):
        resp = self.app.handler(ws_event("abc="), None)
        self.assertEqual(resp["statusCode"], 200)
        self.table.delete_item.assert_called_once_with(Key={"connectionId": "abc="})


if __name__ == "__main__":
    unittest.main()
