"""Integración local: ws_connect + location_update + ws_disconnect sobre DynamoDB
simulado (moto), con las tablas creadas a partir de infra/template.yaml.

Verifica que el código de location_update encaja con la infraestructura y con
las Lambdas de conexión:
  - nombres de variables de entorno, tablas, GSI y tipos de atributos del template
  - broadcast solo a los suscriptores de la orden (Subscriptions creadas por ws_connect)
  - descarte silencioso de timestamps viejos (conditional write real)
  - limpieza de conexiones 410 Gone

Requiere:  pip install boto3 "moto[dynamodb]" pyyaml
Si no están instaladas, estas pruebas se omiten (skip).

Ejecutar desde la raíz del repo:  python -m unittest discover -s tests -v
"""
import importlib.util
import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

try:
    import boto3
    import yaml
    from boto3.dynamodb.conditions import Key
    from botocore.exceptions import ClientError
    from moto import mock_aws
except ImportError as exc:  # pragma: no cover
    raise unittest.SkipTest(f"faltan dependencias para integración ({exc.name}): "
                            "pip install boto3 \"moto[dynamodb]\" pyyaml")

ROOT = Path(__file__).resolve().parent.parent


class _CfnLoader(yaml.SafeLoader):
    """Loader que acepta las etiquetas de CloudFormation (!Ref, !Sub, ...)."""


def _cfn_tag(loader, tag_suffix, node):
    if isinstance(node, yaml.ScalarNode):
        value = loader.construct_scalar(node)
    elif isinstance(node, yaml.SequenceNode):
        value = loader.construct_sequence(node, deep=True)
    else:
        value = loader.construct_mapping(node, deep=True)
    return {tag_suffix: value}


_CfnLoader.add_multi_constructor("!", _cfn_tag)


def load_template():
    with open(ROOT / "infra" / "template.yaml", encoding="utf-8") as f:
        return yaml.load(f, Loader=_CfnLoader)


def load_lambda(name):
    spec = importlib.util.spec_from_file_location(f"{name}_integ", ROOT / "lambdas" / name / "app.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeManagementApi:
    """Sustituye a apigatewaymanagementapi: registra envíos y simula 410 Gone."""

    def __init__(self, gone=()):
        self.gone = set(gone)
        self.sent = {}

    def post_to_connection(self, ConnectionId, Data):
        if ConnectionId in self.gone:
            raise ClientError({"Error": {"Code": "GoneException", "Message": "gone"},
                               "ResponseMetadata": {"HTTPStatusCode": 410}}, "PostToConnection")
        self.sent.setdefault(ConnectionId, []).append(json.loads(Data))


class TemplateContractTest(unittest.TestCase):
    """El template ya trae lo que location_update necesita."""

    @classmethod
    def setUpClass(cls):
        cls.tpl = load_template()
        cls.res = cls.tpl["Resources"]

    def test_variables_de_entorno(self):
        env = self.tpl["Globals"]["Function"]["Environment"]["Variables"]
        self.assertEqual(env["DRIVER_LOCATIONS_TABLE"], {"Ref": "DriverLocationsTable"})
        self.assertEqual(env["SUBSCRIPTIONS_TABLE"], {"Ref": "SubscriptionsTable"})
        fn_env = self.res["LocationUpdateFunction"]["Properties"]["Environment"]["Variables"]
        self.assertIn("WS_ENDPOINT", fn_env)

    def test_gsi_y_tipos(self):
        locs = self.res["DriverLocationsTable"]["Properties"]
        types = {a["AttributeName"]: a["AttributeType"] for a in locs["AttributeDefinitions"]}
        self.assertEqual(types, {"driverId": "S", "geohashPrefix": "S", "ts": "N"})
        gsi = {g["IndexName"]: g for g in locs["GlobalSecondaryIndexes"]}
        self.assertIn("geohash-index", gsi)

        subs = self.res["SubscriptionsTable"]["Properties"]
        names = [g["IndexName"] for g in subs["GlobalSecondaryIndexes"]]
        self.assertIn("orderId-index", names)

    def test_permisos_y_ruta(self):
        props = self.res["LocationUpdateFunction"]["Properties"]
        self.assertEqual(props["CodeUri"], "../lambdas/location_update/")
        self.assertEqual(props["Handler"], "app.handler")
        policies = json.dumps(props["Policies"])
        self.assertIn("DriverLocationsTable", policies)
        self.assertIn("SubscriptionsTable", policies)
        self.assertIn("execute-api:ManageConnections", policies)
        self.assertEqual(self.res["LocationUpdateRoute"]["Properties"]["RouteKey"], "location-update")


@mock_aws
class EndToEndTest(unittest.TestCase):
    def setUp(self):
        env = {"AWS_DEFAULT_REGION": "us-east-1", "AWS_ACCESS_KEY_ID": "test",
               "AWS_SECRET_ACCESS_KEY": "test", "AWS_SESSION_TOKEN": "test"}
        tpl = load_template()
        for var, ref in tpl["Globals"]["Function"]["Environment"]["Variables"].items():
            env[var] = ref["Ref"]  # tabla con el nombre lógico del template
        env["WS_ENDPOINT"] = "https://ws.example/prod"
        self._env = mock.patch.dict(os.environ, env)
        self._env.start()
        self.addCleanup(self._env.stop)

        ddb = boto3.client("dynamodb")
        for logical_id, res in tpl["Resources"].items():
            if res["Type"] != "AWS::DynamoDB::Table":
                continue
            p = res["Properties"]
            kwargs = {"TableName": logical_id, "BillingMode": p["BillingMode"],
                      "AttributeDefinitions": p["AttributeDefinitions"], "KeySchema": p["KeySchema"]}
            if "GlobalSecondaryIndexes" in p:
                kwargs["GlobalSecondaryIndexes"] = p["GlobalSecondaryIndexes"]
            ddb.create_table(**kwargs)

        self.connect = load_lambda("ws_connect")
        self.disconnect = load_lambda("ws_disconnect")
        self.location = load_lambda("location_update")
        self.api = FakeManagementApi(gone={"conn-gone"})
        patcher = mock.patch.object(self.location, "_management", return_value=self.api)
        patcher.start()
        self.addCleanup(patcher.stop)

        res = boto3.resource("dynamodb")
        self.subs = res.Table("SubscriptionsTable")
        self.locs = res.Table("DriverLocationsTable")

    # -- helpers
    def ws_connect(self, connection_id, query):
        return self.connect.handler({"requestContext": {"connectionId": connection_id, "routeKey": "$connect"},
                                     "queryStringParameters": query}, None)

    def send(self, order_id, driver_id, lat, lng, ts, connection_id="conn-driver"):
        body = {"action": "location-update", "orderId": order_id, "driverId": driver_id,
                "lat": lat, "lng": lng, "ts": ts}
        resp = self.location.handler({"requestContext": {"connectionId": connection_id,
                                                         "routeKey": "location-update"},
                                      "body": json.dumps(body)}, None)
        self.assertEqual(resp["statusCode"], 200)
        return json.loads(resp["body"])

    def subscribe_clients(self):
        self.assertEqual(self.ws_connect("conn-a", {"orderId": "ORD-1"})["statusCode"], 200)
        self.assertEqual(self.ws_connect("conn-b", {"orderId": "ORD-2"})["statusCode"], 200)
        self.assertEqual(self.ws_connect("conn-gone", {"orderId": "ORD-1"})["statusCode"], 200)
        self.assertEqual(self.ws_connect("conn-driver", {"role": "driver"})["statusCode"], 200)

    # -- escenarios de la demo
    def test_cada_cliente_solo_recibe_su_orden(self):
        self.subscribe_clients()
        self.assertEqual(self.send("ORD-1", "DRV-1", 4.65, -74.05, 100)["sent"], 1)
        self.assertEqual(self.send("ORD-2", "DRV-2", 4.70, -74.04, 100)["sent"], 1)

        self.assertEqual([m["orderId"] for m in self.api.sent["conn-a"]], ["ORD-1"])
        self.assertEqual([m["orderId"] for m in self.api.sent["conn-b"]], ["ORD-2"])
        self.assertNotIn("conn-driver", self.api.sent)
        self.assertEqual(self.api.sent["conn-a"][0]["driverId"], "DRV-1")

    def test_conexion_gone_se_borra_de_subscriptions(self):
        self.subscribe_clients()
        self.assertIn("Item", self.subs.get_item(Key={"connectionId": "conn-gone"}))
        body = self.send("ORD-1", "DRV-1", 4.65, -74.05, 100)
        self.assertEqual(body, {"status": "ok", "sent": 1, "removed": 1})
        self.assertNotIn("Item", self.subs.get_item(Key={"connectionId": "conn-gone"}))
        self.assertIn("Item", self.subs.get_item(Key={"connectionId": "conn-a"}))

    def test_timestamp_viejo_o_repetido_se_descarta(self):
        self.subscribe_clients()
        self.send("ORD-1", "DRV-1", 4.65, -74.05, 100)
        self.assertEqual(self.send("ORD-1", "DRV-1", 1.0, 1.0, 90)["status"], "ignored")
        self.assertEqual(self.send("ORD-1", "DRV-1", 1.0, 1.0, 100)["status"], "ignored")

        item = self.locs.get_item(Key={"driverId": "DRV-1"})["Item"]
        self.assertEqual(item["ts"], 100)
        self.assertEqual(str(item["lat"]), "4.65")
        self.assertEqual(len(self.api.sent["conn-a"]), 1)  # los descartados no se reenvían

        self.assertEqual(self.send("ORD-1", "DRV-1", 4.66, -74.05, 105)["status"], "ok")
        self.assertEqual(self.locs.get_item(Key={"driverId": "DRV-1"})["Item"]["ts"], 105)
        self.assertEqual([m["ts"] for m in self.api.sent["conn-a"]], [100, 105])

    def test_consulta_por_zona_con_geohash_index(self):
        self.send("ORD-1", "DRV-1", 4.65, -74.05, 100)
        self.send("ORD-2", "DRV-2", 4.6501, -74.0502, 101)
        self.send("ORD-3", "DRV-3", 10.39, -75.51, 102)  # Cartagena: otra zona
        prefix = self.location.geohash_encode(4.65, -74.05)[:self.location.GEOHASH_PREFIX_PRECISION]
        res = self.locs.query(IndexName="geohash-index",
                              KeyConditionExpression=Key("geohashPrefix").eq(prefix) & Key("ts").gte(100))
        self.assertEqual(sorted(i["driverId"] for i in res["Items"]), ["DRV-1", "DRV-2"])

    def test_despues_de_desconectar_no_recibe(self):
        self.subscribe_clients()
        self.disconnect.handler({"requestContext": {"connectionId": "conn-a", "routeKey": "$disconnect"}}, None)
        self.disconnect.handler({"requestContext": {"connectionId": "conn-gone", "routeKey": "$disconnect"}}, None)
        self.assertEqual(self.send("ORD-1", "DRV-1", 4.65, -74.05, 100)["sent"], 0)
        self.assertNotIn("conn-a", self.api.sent)


if __name__ == "__main__":
    unittest.main()
