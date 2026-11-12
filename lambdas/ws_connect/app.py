"""Ruta $connect de la WebSocket API.

Cliente web:  wss://.../prod?orderId=ORD-1   -> guarda {connectionId, orderId} en Subscriptions
Repartidor:   wss://.../prod?role=driver     -> se acepta la conexión sin suscribirla a ninguna orden

Si no llega orderId (y no es un repartidor) se rechaza la conexión con 400.
"""
import os
import time
from datetime import datetime, timezone

import boto3

table = boto3.resource("dynamodb").Table(os.environ["SUBSCRIPTIONS_TABLE"])

# API Gateway corta las conexiones WebSocket a las 2 horas; el TTL limpia
# las suscripciones que queden huérfanas si $disconnect no llega a ejecutarse.
TTL_SECONDS = 2 * 60 * 60


def handler(event, context):
    connection_id = event["requestContext"]["connectionId"]
    params = event.get("queryStringParameters") or {}
    order_id = (params.get("orderId") or "").strip()

    if not order_id:
        if params.get("role") == "driver":
            return {"statusCode": 200, "body": "connected (driver)"}
        return {"statusCode": 400, "body": "orderId es obligatorio (?orderId=...)"}

    table.put_item(Item={
        "connectionId": connection_id,
        "orderId": order_id,
        "connectedAt": datetime.now(timezone.utc).isoformat(),
        "ttl": int(time.time()) + TTL_SECONDS,
    })
    return {"statusCode": 200, "body": "connected"}
