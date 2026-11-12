# WebSocket API — conexiones

## Recursos (en `infra/template.yaml`)

| Recurso | Qué hace |
|---|---|
| `WebSocketApi` | API Gateway WebSocket, `RouteSelectionExpression: $request.body.action` |
| `$connect` → `ws_connect` | Guarda `{connectionId, orderId, connectedAt, ttl}` en `Subscriptions` |
| `$disconnect` → `ws_disconnect` | Borra la suscripción de esa conexión |
| `location-update` → `location_update` | Ruta, integración y permisos listos; el handler lo implementa Oskar |
| `WebSocketStage` | Stage `prod` (parámetro `Stage`) |

Outputs del stack: `WebSocketUrl` (`wss://...`) y `WebSocketManagementEndpoint` (`https://...`).

## Contrato de conexión

| Quién | URL | Resultado |
|---|---|---|
| Cliente web | `wss://<api>.execute-api.<region>.amazonaws.com/prod?orderId=ORD-1` | Conecta y queda suscrito a `ORD-1` |
| Repartidor (simulador) | `wss://.../prod?role=driver` | Conecta sin suscripción |
| Sin `orderId` ni `role=driver` | `wss://.../prod` | Conexión rechazada (400) |

Los mensajes del repartidor se enrutan por el campo `action`:

```json
{"action": "location-update", "orderId": "ORD-1", "driverId": "DRV-1", "lat": 4.65, "lng": -74.05, "ts": 1700000000}
```

## Para `location_update` (Oskar)

La función ya recibe:

- `WS_ENDPOINT`: endpoint para `boto3.client("apigatewaymanagementapi", endpoint_url=os.environ["WS_ENDPOINT"])`.
- Permiso `execute-api:ManageConnections` sobre `POST /@connections/*` del stage.
- CRUD sobre `DriverLocations` y `Subscriptions` (incluye `Query` al GSI `orderId-index` y borrar conexiones `410 Gone`).

El GSI `orderId-index` proyecta `KEYS_ONLY`: cada resultado trae `connectionId` y `orderId`.

## Probar con wscat

```bash
npm install -g wscat
sam build -t infra/template.yaml && sam deploy --guided   # una vez
WS_URL=$(aws cloudformation describe-stacks --stack-name <stack> \
  --query "Stacks[0].Outputs[?OutputKey=='WebSocketUrl'].OutputValue" --output text)

# 1. Cliente suscrito a una orden
wscat -c "$WS_URL?orderId=ORD-1"

# 2. Verificar que la suscripción existe (en otra terminal)
aws dynamodb scan --table-name skyvery-subscriptions-prod

# 3. Consultar por orden usando el GSI
aws dynamodb query --table-name skyvery-subscriptions-prod --index-name orderId-index \
  --key-condition-expression "orderId = :o" --expression-attribute-values '{":o":{"S":"ORD-1"}}'

# 4. Cerrar wscat (Ctrl+C) y repetir el scan: el item ya no debe estar

# 5. Sin orderId la conexión se rechaza
wscat -c "$WS_URL"            # error: Unexpected server response: 400

# 6. Repartidor: conecta y envía una ubicación
wscat -c "$WS_URL?role=driver"
> {"action":"location-update","orderId":"ORD-1","driverId":"DRV-1","lat":4.65,"lng":-74.05,"ts":1700000000}
```

## Pruebas locales (sin AWS)

```bash
python -m unittest discover -s tests -v
```

## Nota sobre redeploy

CloudFormation no crea un `Deployment` nuevo cuando cambian las rutas. Si se agrega o modifica una ruta de la WebSocket API, hay que renombrar el recurso `WebSocketDeployment` (por ejemplo `WebSocketDeploymentV2`) para que el cambio llegue al stage.
