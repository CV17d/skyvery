"""Ruta location-update de la WebSocket API.

Mensaje del repartidor:
  {"action": "location-update", "orderId": "ORD-1", "driverId": "DRV-1",
   "lat": 4.65, "lng": -74.05, "ts": 1700000000}

1. Conditional write en DriverLocations: solo se guarda si el ts es más nuevo
   que el almacenado (attribute_not_exists(ts) OR ts < :newTs). Si es viejo se
   descarta en silencio: 200 y sin broadcast.
2. Calcula geohash / geohashPrefix (para el GSI geohash-index).
3. Broadcast solo a las conexiones suscritas a esa orden (Query al GSI
   orderId-index de Subscriptions). Las conexiones que responden 410 Gone se
   borran de Subscriptions.
"""
import json
import os
from datetime import datetime, timezone
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

_ddb = boto3.resource("dynamodb")
locations = _ddb.Table(os.environ["DRIVER_LOCATIONS_TABLE"])
subscriptions = _ddb.Table(os.environ["SUBSCRIPTIONS_TABLE"])
# Endpoint de ApiGatewayManagementApi (lo define el template). Si no está, se
# arma con el domainName/stage del evento.
WS_ENDPOINT = os.environ.get("WS_ENDPOINT")

ORDER_INDEX = "orderId-index"
GEOHASH_PRECISION = 9         # ~5 m: posición exacta
GEOHASH_PREFIX_PRECISION = 5  # ~5 km: zona usada como PK del GSI geohash-index

_BASE32 = "0123456789bcdefghjkmnpqrstuvwxyz"
_management_clients = {}


def geohash_encode(lat, lng, precision=GEOHASH_PRECISION):
    """Geohash estándar (base32) sin dependencias externas."""
    lat_rng, lng_rng = [-90.0, 90.0], [-180.0, 180.0]
    lat, lng = float(lat), float(lng)
    chars, bits, ch, even = [], 0, 0, True
    while len(chars) < precision:
        rng, val = (lng_rng, lng) if even else (lat_rng, lat)
        mid = (rng[0] + rng[1]) / 2
        if val >= mid:
            ch = (ch << 1) | 1
            rng[0] = mid
        else:
            ch <<= 1
            rng[1] = mid
        even = not even
        bits += 1
        if bits == 5:
            chars.append(_BASE32[ch])
            bits, ch = 0, 0
    return "".join(chars)


def _resp(code, body):
    return {"statusCode": code, "body": json.dumps(body)}


def _json_default(value):
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    raise TypeError(f"no serializable: {type(value)}")


def parse_message(raw):
    """Valida el body y devuelve (msg, None) o (None, error)."""
    try:
        body = json.loads(raw or "{}", parse_float=Decimal)
    except (TypeError, ValueError):
        return None, "body no es JSON válido"
    if not isinstance(body, dict):
        return None, "body debe ser un objeto JSON"

    order_id = str(body.get("orderId") or "").strip()
    driver_id = str(body.get("driverId") or "").strip()
    if not order_id or not driver_id:
        return None, "orderId y driverId son obligatorios"

    values = [body.get(k) for k in ("lat", "lng", "ts")]
    if not all(isinstance(v, (int, Decimal)) and not isinstance(v, bool) for v in values):
        return None, "lat, lng y ts deben ser numéricos"
    lat, lng, ts = (Decimal(v) for v in values)
    if not (lat.is_finite() and lng.is_finite() and ts.is_finite()):
        return None, "lat, lng y ts deben ser numéricos"
    if not (-90 <= lat <= 90 and -180 <= lng <= 180):
        return None, "lat/lng fuera de rango"
    if ts <= 0:
        return None, "ts debe ser positivo"

    return {"orderId": order_id, "driverId": driver_id, "lat": lat, "lng": lng, "ts": ts}, None


def save_if_newer(msg):
    """Conditional write. Devuelve el item guardado, o None si el ts no es más nuevo."""
    geohash = geohash_encode(msg["lat"], msg["lng"])
    item = {
        "driverId": msg["driverId"],
        "orderId": msg["orderId"],
        "lat": msg["lat"],
        "lng": msg["lng"],
        "ts": msg["ts"],
        "geohash": geohash,
        "geohashPrefix": geohash[:GEOHASH_PREFIX_PRECISION],
        "updatedAt": datetime.now(timezone.utc).isoformat(),
    }
    try:
        locations.put_item(
            Item=item,
            ConditionExpression="attribute_not_exists(ts) OR ts < :newTs",
            ExpressionAttributeValues={":newTs": msg["ts"]},
        )
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return None
        raise
    return item


def subscribers(order_id):
    """connectionIds suscritos a la orden (GSI orderId-index, con paginación)."""
    kwargs = {"IndexName": ORDER_INDEX, "KeyConditionExpression": Key("orderId").eq(order_id)}
    while True:
        res = subscriptions.query(**kwargs)
        for it in res.get("Items", []):
            yield it["connectionId"]
        if "LastEvaluatedKey" not in res:
            return
        kwargs["ExclusiveStartKey"] = res["LastEvaluatedKey"]


def _endpoint(event):
    if WS_ENDPOINT:
        return WS_ENDPOINT
    ctx = event["requestContext"]
    return f"https://{ctx['domainName']}/{ctx['stage']}"


def _management(endpoint):
    if endpoint not in _management_clients:
        _management_clients[endpoint] = boto3.client("apigatewaymanagementapi", endpoint_url=endpoint)
    return _management_clients[endpoint]


def broadcast(event, item):
    """Envía la ubicación a los suscriptores de la orden. Devuelve (enviados, eliminados)."""
    payload = json.dumps({
        "type": "location",
        "orderId": item["orderId"],
        "driverId": item["driverId"],
        "lat": item["lat"],
        "lng": item["lng"],
        "ts": item["ts"],
        "geohash": item["geohash"],
    }, default=_json_default).encode("utf-8")

    api = _management(_endpoint(event))
    sent, gone = 0, 0
    for connection_id in subscribers(item["orderId"]):
        try:
            api.post_to_connection(ConnectionId=connection_id, Data=payload)
            sent += 1
        except ClientError as e:
            code = e.response["Error"]["Code"]
            status = e.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            if code == "GoneException" or status == 410:
                subscriptions.delete_item(Key={"connectionId": connection_id})
                gone += 1
            else:
                print(f"postToConnection falló para {connection_id}: {code}")
    return sent, gone


def handler(event, context):
    msg, error = parse_message(event.get("body"))
    if error:
        return _resp(400, {"error": error})

    item = save_if_newer(msg)
    if item is None:
        # Timestamp viejo o repetido: se descarta en silencio, sin broadcast.
        return _resp(200, {"status": "ignored", "reason": "stale"})

    sent, gone = broadcast(event, item)
    return _resp(200, {"status": "ok", "sent": sent, "removed": gone})
