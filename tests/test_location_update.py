"""Pruebas locales de location_update (sin AWS ni dependencias).

Ejecutar desde la raíz del repo:  python -m unittest discover -s tests -v
"""
import importlib.util
import json
import os
import sys
import unittest
from decimal import Decimal
from pathlib import Path
from types import ModuleType
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent


class FakeClientError(Exception):
    def __init__(self, code, status=400):
        super().__init__(code)
        self.response = {"Error": {"Code": code}, "ResponseMetadata": {"HTTPStatusCode": status}}


def load_location_update():
    """Carga lambdas/location_update/app.py con boto3/botocore simulados.

    Devuelve (módulo, tabla DriverLocations, tabla Subscriptions, cliente management)."""
    tables = {"locs-test": mock.MagicMock(name="locations"), "subs-test": mock.MagicMock(name="subscriptions")}
    api = mock.MagicMock(name="apigatewaymanagementapi")

    fake_boto3 = mock.MagicMock()
    fake_boto3.resource.return_value.Table.side_effect = lambda name: tables[name]
    fake_boto3.client.return_value = api

    conditions = ModuleType("boto3.dynamodb.conditions")
    conditions.Key = mock.MagicMock(name="Key")
    exceptions = ModuleType("botocore.exceptions")
    exceptions.ClientError = FakeClientError

    modules = {
        "boto3": fake_boto3,
        "boto3.dynamodb": ModuleType("boto3.dynamodb"),
        "boto3.dynamodb.conditions": conditions,
        "botocore": ModuleType("botocore"),
        "botocore.exceptions": exceptions,
    }
    env = {"DRIVER_LOCATIONS_TABLE": "locs-test", "SUBSCRIPTIONS_TABLE": "subs-test",
           "WS_ENDPOINT": "https://ws.example/prod"}
    spec = importlib.util.spec_from_file_location("location_update_app", ROOT / "lambdas" / "location_update" / "app.py")
    module = importlib.util.module_from_spec(spec)
    with mock.patch.dict(sys.modules, modules), mock.patch.dict(os.environ, env):
        spec.loader.exec_module(module)
    module._fake_boto3 = fake_boto3
    return module, tables["locs-test"], tables["subs-test"], api


def location_event(body, connection_id="driver-conn"):
    return {"requestContext": {"connectionId": connection_id, "routeKey": "location-update",
                               "domainName": "abc.execute-api.us-east-1.amazonaws.com", "stage": "prod"},
            "body": body if isinstance(body, str) else json.dumps(body)}


MSG = {"action": "location-update", "orderId": "ORD-1", "driverId": "DRV-1",
       "lat": 4.65, "lng": -74.05, "ts": 1700000000}


class GeohashTest(unittest.TestCase):
    def setUp(self):
        self.app, *_ = load_location_update()

    def test_vectores_conocidos(self):
        # Valores de referencia del algoritmo geohash estándar.
        self.assertEqual(self.app.geohash_encode(57.64911, 10.40744, 11), "u4pruydqqvj")
        self.assertEqual(self.app.geohash_encode(42.6, -5.6, 5), "ezs42")
        self.assertEqual(self.app.geohash_encode(0, 0, 4), "s000")

    def test_puntos_cercanos_comparten_prefijo(self):
        a = self.app.geohash_encode(4.6500, -74.0500)
        b = self.app.geohash_encode(4.6505, -74.0503)
        self.assertEqual(a[:self.app.GEOHASH_PREFIX_PRECISION], b[:self.app.GEOHASH_PREFIX_PRECISION])

    def test_acepta_decimal(self):
        self.assertEqual(self.app.geohash_encode(Decimal("42.6"), Decimal("-5.6"), 5), "ezs42")


class ParseMessageTest(unittest.TestCase):
    def setUp(self):
        self.app, *_ = load_location_update()

    def test_mensaje_valido_usa_decimal(self):
        msg, err = self.app.parse_message(json.dumps(MSG))
        self.assertIsNone(err)
        self.assertEqual(msg["lat"], Decimal("4.65"))
        self.assertEqual(msg["ts"], Decimal(1700000000))
        self.assertIsInstance(msg["lng"], Decimal)

    def test_errores(self):
        casos = [
            None, "", "no-json", "[1,2]",
            json.dumps({**MSG, "orderId": ""}),
            json.dumps({k: v for k, v in MSG.items() if k != "driverId"}),
            json.dumps({k: v for k, v in MSG.items() if k != "ts"}),
            json.dumps({**MSG, "lat": "4.65"}),
            json.dumps({**MSG, "lat": True}),
            json.dumps({**MSG, "lat": 91}),
            json.dumps({**MSG, "lng": -181}),
            json.dumps({**MSG, "ts": 0}),
            '{"orderId":"O","driverId":"D","lat":NaN,"lng":1,"ts":1}',
        ]
        for raw in casos:
            with self.subTest(raw=raw):
                msg, err = self.app.parse_message(raw)
                self.assertIsNone(msg)
                self.assertTrue(err)


class HandlerTest(unittest.TestCase):
    def setUp(self):
        self.app, self.locations, self.subs, self.api = load_location_update()
        self.subs.query.return_value = {"Items": [{"connectionId": "c1", "orderId": "ORD-1"},
                                                  {"connectionId": "c2", "orderId": "ORD-1"}]}

    def test_guarda_con_conditional_write(self):
        resp = self.app.handler(location_event(MSG), None)
        self.assertEqual(resp["statusCode"], 200)
        kwargs = self.locations.put_item.call_args.kwargs
        self.assertEqual(kwargs["ConditionExpression"], "attribute_not_exists(ts) OR ts < :newTs")
        self.assertEqual(kwargs["ExpressionAttributeValues"], {":newTs": Decimal(1700000000)})
        item = kwargs["Item"]
        self.assertEqual(item["driverId"], "DRV-1")
        self.assertEqual(item["orderId"], "ORD-1")
        self.assertEqual(item["ts"], Decimal(1700000000))
        self.assertEqual(len(item["geohash"]), self.app.GEOHASH_PRECISION)
        self.assertEqual(item["geohashPrefix"], item["geohash"][:self.app.GEOHASH_PREFIX_PRECISION])
        # Sin floats: boto3 resource rechaza float en DynamoDB.
        self.assertFalse(any(isinstance(v, float) for v in item.values()))

    def test_broadcast_solo_a_suscriptores_de_la_orden(self):
        resp = self.app.handler(location_event(MSG), None)
        self.assertEqual(json.loads(resp["body"]), {"status": "ok", "sent": 2, "removed": 0})
        self.assertEqual(self.subs.query.call_args.kwargs["IndexName"], "orderId-index")
        self.app.Key.assert_called_with("orderId")
        self.app.Key.return_value.eq.assert_called_with("ORD-1")
        self.app._fake_boto3.client.assert_called_with("apigatewaymanagementapi",
                                                       endpoint_url="https://ws.example/prod")
        enviados = [c.kwargs["ConnectionId"] for c in self.api.post_to_connection.call_args_list]
        self.assertEqual(enviados, ["c1", "c2"])
        data = json.loads(self.api.post_to_connection.call_args.kwargs["Data"])
        self.assertEqual(data, {"type": "location", "orderId": "ORD-1", "driverId": "DRV-1",
                                "lat": 4.65, "lng": -74.05, "ts": 1700000000,
                                "geohash": data["geohash"]})

    def test_timestamp_viejo_se_descarta_en_silencio(self):
        self.locations.put_item.side_effect = FakeClientError("ConditionalCheckFailedException")
        resp = self.app.handler(location_event(MSG), None)
        self.assertEqual(resp["statusCode"], 200)
        self.assertEqual(json.loads(resp["body"])["status"], "ignored")
        self.subs.query.assert_not_called()
        self.api.post_to_connection.assert_not_called()

    def test_otro_error_de_dynamodb_se_propaga(self):
        self.locations.put_item.side_effect = FakeClientError("ProvisionedThroughputExceededException")
        with self.assertRaises(FakeClientError):
            self.app.handler(location_event(MSG), None)

    def test_conexion_410_gone_se_elimina(self):
        self.api.post_to_connection.side_effect = [FakeClientError("GoneException", 410), None]
        resp = self.app.handler(location_event(MSG), None)
        self.assertEqual(json.loads(resp["body"]), {"status": "ok", "sent": 1, "removed": 1})
        self.subs.delete_item.assert_called_once_with(Key={"connectionId": "c1"})

    def test_otro_error_de_post_no_borra_ni_corta_el_broadcast(self):
        self.api.post_to_connection.side_effect = [FakeClientError("LimitExceededException", 429), None]
        with mock.patch("builtins.print"):
            resp = self.app.handler(location_event(MSG), None)
        self.assertEqual(json.loads(resp["body"]), {"status": "ok", "sent": 1, "removed": 0})
        self.subs.delete_item.assert_not_called()

    def test_paginacion_de_suscriptores(self):
        self.subs.query.side_effect = [
            {"Items": [{"connectionId": "c1"}], "LastEvaluatedKey": {"connectionId": "c1"}},
            {"Items": [{"connectionId": "c2"}]},
        ]
        resp = self.app.handler(location_event(MSG), None)
        self.assertEqual(json.loads(resp["body"])["sent"], 2)
        self.assertEqual(self.subs.query.call_args_list[1].kwargs["ExclusiveStartKey"], {"connectionId": "c1"})

    def test_sin_suscriptores(self):
        self.subs.query.return_value = {"Items": []}
        resp = self.app.handler(location_event(MSG), None)
        self.assertEqual(json.loads(resp["body"]), {"status": "ok", "sent": 0, "removed": 0})
        self.locations.put_item.assert_called_once()

    def test_mensaje_invalido_400_sin_escribir(self):
        resp = self.app.handler(location_event({"action": "location-update"}), None)
        self.assertEqual(resp["statusCode"], 400)
        self.locations.put_item.assert_not_called()
        self.api.post_to_connection.assert_not_called()

    def test_endpoint_desde_request_context_si_no_hay_env(self):
        with mock.patch.object(self.app, "WS_ENDPOINT", None):
            self.assertEqual(self.app._endpoint(location_event(MSG)),
                             "https://abc.execute-api.us-east-1.amazonaws.com/prod")


if __name__ == "__main__":
    unittest.main()
