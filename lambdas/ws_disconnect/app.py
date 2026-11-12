"""Ruta $disconnect de la WebSocket API: elimina la suscripción de la conexión.

Es idempotente: si la conexión no tenía suscripción (p. ej. un repartidor),
delete_item no falla.
"""
import os

import boto3

table = boto3.resource("dynamodb").Table(os.environ["SUBSCRIPTIONS_TABLE"])


def handler(event, context):
    connection_id = event["requestContext"]["connectionId"]
    table.delete_item(Key={"connectionId": connection_id})
    return {"statusCode": 200, "body": "disconnected"}
