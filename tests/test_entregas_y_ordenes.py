"""Pruebas de complete_delivery (transacción atómica) y orders_api (GSI por zona).

Usan moto para simular DynamoDB y SNS en memoria (sin AWS):
    pip install pytest boto3 moto
    python -m pytest -q tests
"""
import importlib.util
import json
import os
import unittest
from pathlib import Path
from unittest import mock

import boto3
from moto import mock_aws

ROOT = Path(__file__).resolve().parent.parent
REGION = "us-east-1"

ENV = {
    "AWS_DEFAULT_REGION": REGION,
    "AWS_ACCESS_KEY_ID": "test",
    "AWS_SECRET_ACCESS_KEY": "test",
    "ORDERS_TABLE": "orders",
    "HISTORY_TABLE": "history",
    "DRIVERS_TABLE": "drivers",
    "DRIVER_LOCATIONS_TABLE": "locations",
}


def _crear_tablas():
    ddb = boto3.client("dynamodb", region_name=REGION)
    ddb.create_table(
        TableName="orders", BillingMode="PAY_PER_REQUEST",
        AttributeDefinitions=[{"AttributeName": n, "AttributeType": "S"}
                              for n in ("orderId", "zone", "status")],
        KeySchema=[{"AttributeName": "orderId", "KeyType": "HASH"}],
        GlobalSecondaryIndexes=[{
            "IndexName": "zone-status-index",
            "KeySchema": [{"AttributeName": "zone", "KeyType": "HASH"},
                          {"AttributeName": "status", "KeyType": "RANGE"}],
            "Projection": {"ProjectionType": "ALL"}}])
    ddb.create_table(
        TableName="history", BillingMode="PAY_PER_REQUEST",
        AttributeDefinitions=[{"AttributeName": "orderId", "AttributeType": "S"},
                              {"AttributeName": "deliveredAt", "AttributeType": "S"}],
        KeySchema=[{"AttributeName": "orderId", "KeyType": "HASH"},
                   {"AttributeName": "deliveredAt", "KeyType": "RANGE"}])
    ddb.create_table(
        TableName="drivers", BillingMode="PAY_PER_REQUEST",
        AttributeDefinitions=[{"AttributeName": "driverId", "AttributeType": "S"}],
        KeySchema=[{"AttributeName": "driverId", "KeyType": "HASH"}])
    ddb.create_table(
        TableName="locations", BillingMode="PAY_PER_REQUEST",
        AttributeDefinitions=[{"AttributeName": "driverId", "AttributeType": "S"},
                              {"AttributeName": "geohashPrefix", "AttributeType": "S"},
                              {"AttributeName": "ts", "AttributeType": "N"}],
        KeySchema=[{"AttributeName": "driverId", "KeyType": "HASH"}],
        GlobalSecondaryIndexes=[{
            "IndexName": "geohash-index",
            "KeySchema": [{"AttributeName": "geohashPrefix", "KeyType": "HASH"},
                          {"AttributeName": "ts", "KeyType": "RANGE"}],
            "Projection": {"ProjectionType": "ALL"}}])


def _cargar(nombre):
    spec = importlib.util.spec_from_file_location(
        f"{nombre}_app_test", ROOT / "lambdas" / nombre / "app.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class BaseAws(unittest.TestCase):
    def setUp(self):
        self.env = mock.patch.dict(os.environ, ENV)
        self.env.start()
        self.aws = mock_aws()
        self.aws.start()
        _crear_tablas()
        sns = boto3.client("sns", region_name=REGION)
        os.environ["DELIVERY_TOPIC_ARN"] = sns.create_topic(Name="entregas")["TopicArn"]
        self.ddb = boto3.resource("dynamodb", region_name=REGION)
        self.orders = _cargar("orders_api")
        self.delivery = _cargar("complete_delivery")

    def tearDown(self):
        self.aws.stop()
        self.env.stop()

    def api(self, route, body=None, path=None, qs=None):
        resp = self.orders.handler({"routeKey": route, "body": json.dumps(body or {}),
                                    "pathParameters": path, "queryStringParameters": qs}, None)
        return resp["statusCode"], json.loads(resp["body"])

    def completar(self, order_id, driver_id):
        resp = self.delivery.handler({"pathParameters": {"orderId": order_id},
                                      "body": json.dumps({"driverId": driver_id})}, None)
        return resp["statusCode"], json.loads(resp["body"])

    def estado(self):
        orden = self.ddb.Table("orders").get_item(Key={"orderId": "ORD-1"}).get("Item")
        historial = self.ddb.Table("history").scan()["Items"]
        repartidor = self.ddb.Table("drivers").get_item(Key={"driverId": "DRV-1"}).get("Item")
        return orden, historial, repartidor


class CompleteDeliveryTest(BaseAws):
    def test_entrega_exitosa_hace_las_tres_operaciones(self):
        self.api("POST /drivers", {"driverId": "DRV-1"})
        self.api("POST /orders", {"orderId": "ORD-1", "zone": "norte", "driverId": "DRV-1"})
        code, body = self.completar("ORD-1", "DRV-1")
        self.assertEqual(code, 200)
        orden, historial, repartidor = self.estado()
        self.assertEqual(orden["status"], "DELIVERED")
        self.assertEqual(len(historial), 1)
        self.assertEqual(repartidor["deliveriesCount"], 1)

    def test_repartidor_inexistente_no_persiste_nada(self):
        self.api("POST /orders", {"orderId": "ORD-1", "zone": "norte"})
        code, body = self.completar("ORD-1", "DRV-1")
        self.assertEqual(code, 409)
        orden, historial, repartidor = self.estado()
        self.assertEqual(orden["status"], "PENDING")   # no cambió
        self.assertEqual(historial, [])                 # no se creó
        self.assertIsNone(repartidor)

    def test_orden_ya_entregada_no_suma_dos_veces(self):
        self.api("POST /drivers", {"driverId": "DRV-1"})
        self.api("POST /orders", {"orderId": "ORD-1", "zone": "norte"})
        self.assertEqual(self.completar("ORD-1", "DRV-1")[0], 200)
        self.assertEqual(self.completar("ORD-1", "DRV-1")[0], 409)
        _, historial, repartidor = self.estado()
        self.assertEqual(len(historial), 1)
        self.assertEqual(repartidor["deliveriesCount"], 1)

    def test_faltan_datos(self):
        self.assertEqual(self.completar("ORD-1", "")[0], 400)


class OrdersApiTest(BaseAws):
    def test_consulta_por_zona_y_estado(self):
        self.api("POST /orders", {"orderId": "A", "zone": "norte"})
        self.api("POST /orders", {"orderId": "B", "zone": "norte", "driverId": "DRV-1"})
        self.api("POST /orders", {"orderId": "C", "zone": "sur"})
        code, body = self.api("GET /zones/{zone}/orders", path={"zone": "norte"})
        self.assertEqual((code, body["count"]), (200, 2))
        code, body = self.api("GET /zones/{zone}/orders", path={"zone": "norte"},
                              qs={"status": "IN_TRANSIT"})
        self.assertEqual([o["orderId"] for o in body["items"]], ["B"])
        self.assertEqual(self.api("GET /zones/{zone}/orders", path={"zone": "norte"},
                                  qs={"status": "X"})[0], 400)

    def test_repartidores_crear_listar_y_duplicado(self):
        self.assertEqual(self.api("POST /drivers", {"driverId": "DRV-1"})[0], 201)
        self.assertEqual(self.api("POST /drivers", {"driverId": "DRV-1"})[0], 409)
        code, body = self.api("GET /drivers")
        self.assertEqual(body["count"], 1)
        code, body = self.api("GET /drivers/{driverId}", path={"driverId": "DRV-1"})
        self.assertEqual(body["deliveriesCount"], 0)

    def test_repartidores_por_area_geohash(self):
        tabla = self.ddb.Table("locations")
        tabla.put_item(Item={"driverId": "DRV-1", "geohashPrefix": "d2g6f", "ts": 100})
        tabla.put_item(Item={"driverId": "DRV-2", "geohashPrefix": "d2g6f", "ts": 200})
        tabla.put_item(Item={"driverId": "DRV-3", "geohashPrefix": "d2g6g", "ts": 300})
        ruta = "GET /areas/{geohashPrefix}/drivers"
        code, body = self.api(ruta, path={"geohashPrefix": "d2g6f"})
        self.assertEqual((code, body["count"]), (200, 2))
        code, body = self.api(ruta, path={"geohashPrefix": "d2g6f"}, qs={"since": "150"})
        self.assertEqual([d["driverId"] for d in body["items"]], ["DRV-2"])
        self.assertEqual(self.api(ruta, path={"geohashPrefix": "d2"})[0], 400)


if __name__ == "__main__":
    unittest.main()
