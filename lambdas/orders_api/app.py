"""API REST de órdenes.

POST /orders                 body: {"orderId"?, "zone", "customer", "driverId"?}
GET  /orders/{orderId}
GET  /zones/{zone}/orders    ?status=PENDING|IN_TRANSIT|DELIVERED  (usa GSI zone-status-index)
"""
import json
import os
import uuid
from datetime import datetime, timezone

import boto3
from boto3.dynamodb.conditions import Key

table = boto3.resource("dynamodb").Table(os.environ["ORDERS_TABLE"])
VALID_STATUS = {"PENDING", "IN_TRANSIT", "DELIVERED"}


def _resp(code, body):
    return {"statusCode": code, "headers": {"Content-Type": "application/json"},
            "body": json.dumps(body, default=str)}


def create_order(event):
    body = json.loads(event.get("body") or "{}")
    if not body.get("zone"):
        return _resp(400, {"error": "zone es obligatorio"})
    item = {
        "orderId": body.get("orderId") or str(uuid.uuid4())[:8],
        "zone": body["zone"],
        "status": "PENDING",
        "customer": body.get("customer", "anonimo"),
        "createdAt": datetime.now(timezone.utc).isoformat(),
    }
    if body.get("driverId"):
        item["driverId"] = body["driverId"]
        item["status"] = "IN_TRANSIT"
    table.put_item(Item=item, ConditionExpression="attribute_not_exists(orderId)")
    return _resp(201, item)


def get_order(order_id):
    item = table.get_item(Key={"orderId": order_id}).get("Item")
    return _resp(200, item) if item else _resp(404, {"error": "no existe"})


def list_by_zone(zone, status):
    cond = Key("zone").eq(zone)
    if status:
        if status not in VALID_STATUS:
            return _resp(400, {"error": f"status debe ser uno de {sorted(VALID_STATUS)}"})
        cond = cond & Key("status").eq(status)
    res = table.query(IndexName="zone-status-index", KeyConditionExpression=cond)
    return _resp(200, {"zone": zone, "count": res["Count"], "items": res["Items"]})


def handler(event, context):
    route = event.get("routeKey", "")
    params = event.get("pathParameters") or {}
    qs = event.get("queryStringParameters") or {}

    if route == "POST /orders":
        return create_order(event)
    if route == "GET /orders/{orderId}":
        return get_order(params["orderId"])
    if route == "GET /zones/{zone}/orders":
        return list_by_zone(params["zone"], qs.get("status"))
    return _resp(404, {"error": f"ruta no soportada: {route}"})
