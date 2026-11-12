# Ubicación en tiempo real y simulador

Archivos: `lambdas/location_update/app.py`, `simulator/driver_sim.py`.
No requiere cambios en `infra/template.yaml`: la ruta `location-update`, la integración,
`WS_ENDPOINT` y los permisos ya están definidos (ver `docs/websocket.md`).

## `location_update`

Mensaje que recibe (ruta `location-update`, lo envía el repartidor conectado con `?role=driver`):

```json
{"action": "location-update", "orderId": "ORD-1", "driverId": "DRV-1", "lat": 4.65, "lng": -74.05, "ts": 1700000000}
```

`ts` es epoch en **segundos**. `lat`, `lng` y `ts` deben ser números.

Flujo:

1. **Validación**: si falta un campo o los valores no son válidos → `400`, no se escribe nada.
2. **Conditional write** en `DriverLocations`:
   `ConditionExpression: attribute_not_exists(ts) OR ts < :newTs`.
   Si falla con `ConditionalCheckFailedException` (ts viejo o repetido) se descarta **en silencio**:
   `200 {"status": "ignored", "reason": "stale"}` y **no** hay broadcast.
3. Item guardado: `driverId, orderId, lat, lng, ts, geohash (9 caracteres, ~5 m), geohashPrefix (5 caracteres, ~5 km), updatedAt`.
   `geohashPrefix` + `ts` alimentan el GSI `geohash-index` (repartidores por zona).
4. **Broadcast filtrado**: `Query` a `Subscriptions` por el GSI `orderId-index` (con paginación) y
   `postToConnection` solo a esas conexiones. Si una responde `410 Gone` se borra su suscripción.
   Respuesta: `200 {"status": "ok", "sent": N, "removed": M}`.

### Mensaje que reciben los clientes web (para `frontend/tracking-client`)

```json
{"type": "location", "orderId": "ORD-1", "driverId": "DRV-1", "lat": 4.65, "lng": -74.05, "ts": 1700000000, "geohash": "d2g66yw2j"}
```

Ejemplo en el navegador:

```js
const ws = new WebSocket(`${WS_URL}?orderId=${orderId}`);
ws.onmessage = (e) => {
  const msg = JSON.parse(e.data);
  if (msg.type === "location") marker.setLatLng([msg.lat, msg.lng]);
};
```

### Consultar repartidores por zona (GSI `geohash-index`)

```bash
aws dynamodb query --table-name skyvery-driver-locations-prod --index-name geohash-index \
  --key-condition-expression "geohashPrefix = :g" --expression-attribute-values '{":g":{"S":"d2g66"}}'
```

## Simulador `simulator/driver_sim.py`

Recorre una ruta en Bogotá (Parque de la 93 → Unicentro) y envía la ubicación cada 5 s.
Se conecta con `?role=driver`. El `ts` se toma al momento de enviar y siempre es creciente.

```bash
pip install websockets
WS_URL=$(aws cloudformation describe-stacks --stack-name <stack> \
  --query "Stacks[0].Outputs[?OutputKey=='WebSocketUrl'].OutputValue" --output text)

# Recorrido normal (GPS cada 5 s)
python simulator/driver_sim.py --url "$WS_URL" --order-id ORD-1 --driver-id DRV-1

# Demo del descarte: después del punto 3 envía un mensaje con ts 60 s más viejo
python simulator/driver_sim.py --url "$WS_URL" --order-id ORD-1 --driver-id DRV-1 --stale-after 3

# Ver los mensajes sin conectarse
python simulator/driver_sim.py --order-id ORD-1 --driver-id DRV-1 --dry-run --interval 0
```

Opciones: `--interval` (segundos, default 5), `--steps` (puntos intermedios por tramo, default 3),
`--stale-after N`, `--stale-age` (segundos, default 60). La URL también se toma de la variable `WS_URL`.

## Demo

1. Navegador A: `wscat -c "$WS_URL?orderId=ORD-1"`. Navegador B: `wscat -c "$WS_URL?orderId=ORD-2"`.
2. `python simulator/driver_sim.py --url "$WS_URL" --order-id ORD-1 --driver-id DRV-1 --stale-after 3`
   → solo A recibe las ubicaciones; B no recibe nada.
3. El mensaje marcado `VIEJO` no llega a A y en CloudWatch Logs la Lambda responde `ignored`.
   `aws dynamodb get-item --table-name skyvery-driver-locations-prod --key '{"driverId":{"S":"DRV-1"}}'`
   muestra el último `ts`, no el viejo.

## Pruebas locales

```bash
# Unitarias (sin dependencias): location_update, simulador y las de ws_connect/ws_disconnect
python -m unittest discover -s tests -v

# Además, integración con DynamoDB simulado (tablas creadas desde infra/template.yaml,
# junto con ws_connect y ws_disconnect). Sin estas librerías la prueba se omite.
pip install boto3 "moto[dynamodb]" pyyaml
python -m unittest discover -s tests -v
```
