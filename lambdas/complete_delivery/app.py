"""POST /orders/{orderId}/complete  body: {"driverId": "..."}

Transacción atómica (TransactWriteItems):
  1. Orders: status -> DELIVERED (solo si existe y aún no está DELIVERED)
  2. DeliveryHistory: crea registro de la entrega
  3. Drivers: deliveriesCount += 1
Si cualquiera falla, ninguna persiste. Luego publica confirmación en SNS.
"""
import json
import os
from datetime import datetime, timezone

import boto3
from botocore.exceptions import ClientError

ddb = boto3.client("dynamodb")
sns = boto3.client("sns")

ORDERS = os.environ["ORDERS_TABLE"]
HISTORY = os.environ["HISTORY_TABLE"]
DRIVERS = os.environ["DRIVERS_TABLE"]
TOPIC = os.environ["DELIVERY_TOPIC_ARN"]


def _resp(code, body):
    return {"statusCode": code, "headers": {"Content-Type": "application/json"},
            "body": json.dumps(body)}


def handler(event, context):
    order_id = (event.get("pathParameters") or {}).get("orderId")
    body = json.loads(event.get("body") or "{}")
    driver_id = body.get("driverId")
    if not order_id or not driver_id:
        return _resp(400, {"error": "orderId y driverId son obligatorios"})

    now = datetime.now(timezone.utc).isoformat()

    try:
        ddb.transact_write_items(TransactItems=[
            {"Update": {
                "TableName": ORDERS,
                "Key": {"orderId": {"S": order_id}},
                "UpdateExpression": "SET #s = :delivered, deliveredAt = :now, driverId = :d",
                "ConditionExpression": "attribute_exists(orderId) AND #s <> :delivered",
                "ExpressionAttributeNames": {"#s": "status"},
                "ExpressionAttributeValues": {
                    ":delivered": {"S": "DELIVERED"},
                    ":now": {"S": now},
                    ":d": {"S": driver_id},
                },
            }},
            {"Put": {
                "TableName": HISTORY,
                "Item": {
                    "orderId": {"S": order_id},
                    "deliveredAt": {"S": now},
                    "driverId": {"S": driver_id},
                },
                "ConditionExpression": "attribute_not_exists(orderId)",
            }},
            {"Update": {
                "TableName": DRIVERS,
                "Key": {"driverId": {"S": driver_id}},
                "UpdateExpression": "ADD deliveriesCount :one",
                "ConditionExpression": "attribute_exists(driverId)",
                "ExpressionAttributeValues": {":one": {"N": "1"}},
            }},
        ])
    except ClientError as e:
        if e.response["Error"]["Code"] == "TransactionCanceledException":
            reasons = [r.get("Code") for r in
                       e.response.get("CancellationReasons", [])]
            return _resp(409, {"error": "Transacción cancelada, nada se guardó",
                               "reasons": reasons})
        raise

    sns.publish(
        TopicArn=TOPIC,
        Subject=f"Skyvery: pedido {order_id} entregado",
        Message=f"El pedido {order_id} fue entregado por {driver_id} el {now}.",
    )
    return _resp(200, {"orderId": order_id, "status": "DELIVERED", "deliveredAt": now})
