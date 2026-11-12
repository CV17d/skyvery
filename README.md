# Skyvery — Plataforma de Delivery con Seguimiento en Tiempo Real

Proyecto 10 · AWS: **API Gateway (WebSocket) + Lambda + DynamoDB + S3 + SNS + EC2 + ALB**

## 1. ¿Qué hay que construir?

Un cliente abre una página web y ve **en vivo** dónde va el repartidor de su pedido.

1. El **repartidor** (app simulada en Python) envía su GPS cada **5 segundos** por WebSocket (ruta `location-update`).
2. Una **Lambda** guarda la ubicación en DynamoDB **solo si es más nueva** que la última (conditional write) y la reenvía **solo a los clientes que siguen esa orden**.
3. Cuando el pedido se entrega, una Lambda hace una **transacción DynamoDB** (3 operaciones atómicas) y **SNS** envía la confirmación.
4. Un **panel de operaciones** corre en **EC2 con NGINX** detrás de un **ALB**, con rate limiting por IP (10 req/s, burst 20), demostrado con prueba de carga y logs con **429**.

## 2. Arquitectura

```
 Repartidor (simulator) ──WS location-update──┐
                                               ▼
 Cliente web (S3) ──WS $connect?orderId=X──► API Gateway WebSocket
                                               │
                    ┌──────────────┬───────────┼──────────────┐
                    ▼              ▼           ▼              ▼
              ws_connect     ws_disconnect  location_update  complete_delivery (REST/HTTP)
                    │              │           │   │              │
                    └──────► DynamoDB ◄────────┘   └─► postToConnection (broadcast filtrado)
                                               ▲              │
                                               └── TransactWriteItems ──► SNS (email/SMS)

 Operador ──► ALB ──► EC2 (NGINX, limit_req 10r/s burst 20) ──► ops-panel (consulta API REST)
```

## 3. Modelo de datos (DynamoDB)

| Tabla | PK / SK | GSI | Uso |
|---|---|---|---|
| `Subscriptions` | PK `connectionId` | **GSI `orderId-index`** (PK `orderId`) | Saber qué conexiones siguen cada orden |
| `Orders` | PK `orderId` | **GSI `zone-status-index`** (PK `zone`, SK `status`) | Estado de la orden; consultas por zona geográfica |
| `DriverLocations` | PK `driverId` | **GSI `geohash-index`** (PK `geohashPrefix`, SK `ts`) | Última ubicación (con `ts` para conditional write) |
| `DeliveryHistory` | PK `orderId`, SK `deliveredAt` | — | Registro histórico de entregas |
| `Drivers` | PK `driverId` | — | Datos del repartidor + `deliveriesCount` |

Reglas clave:
- **Conditional write**: `ConditionExpression: attribute_not_exists(ts) OR ts < :newTs`. Si falla (`ConditionalCheckFailedException`) → se descarta **en silencio** (return 200, sin broadcast).
- **Transacción** (`TransactWriteItems`): `Update Orders status=DELIVERED` + `Put DeliveryHistory` + `Update Drivers ADD deliveriesCount 1`. Todo o nada.
- **Broadcast filtrado**: `Query Subscriptions` por `orderId-index` → `postToConnection` a cada `connectionId`; si responde `410 Gone` se borra la suscripción.

## 4. Estructura del repositorio

```
infra/template.yaml          # AWS SAM: tablas, Lambdas, WebSocket API, HTTP API, SNS, S3
lambdas/ws_connect/          # $connect   → guarda {connectionId, orderId}
lambdas/ws_disconnect/       # $disconnect→ borra la suscripción
lambdas/location_update/     # location-update → conditional write + broadcast
lambdas/complete_delivery/   # transacción + publicación SNS
lambdas/orders_api/          # API REST: órdenes por zona, repartidores, repartidores por área
simulator/driver_sim.py      # repartidor simulado (GPS cada 5 s)
frontend/tracking-client/    # página del cliente (S3 static website + mapa Leaflet)
frontend/ops-panel/          # panel de operaciones (servido por NGINX en EC2)
ec2-nginx/                   # nginx.conf + user-data + guía EC2/ALB
loadtest/                    # prueba de carga (carga.py)
tests/                       # pruebas locales (sin AWS)
docs/                        # websocket.md, location.md, evidencias/
```

### API REST (`OrdersApiUrl`)

| Método y ruta | Qué hace | Índice |
|---|---|---|
| `POST /orders` | Crea una orden (`zone` obligatorio) | — |
| `GET /orders/{orderId}` | Consulta una orden | — |
| `GET /zones/{zone}/orders?status=` | Órdenes de una zona, opcionalmente por estado | `zone-status-index` |
| `POST /orders/{orderId}/complete` | Entrega: transacción de 3 operaciones + SNS | — |
| `POST /drivers` · `GET /drivers` · `GET /drivers/{driverId}` | Crear / listar / consultar repartidores | — |
| `GET /areas/{geohashPrefix}/drivers?since=` | Repartidores en una celda geohash de 5 caracteres (~5 km) | `geohash-index` |

### Pruebas locales

```bash
pip install pytest boto3 moto websockets
python -m pytest -q tests
```

## 5. Reparto de trabajo

### Etapa 1 — Código (terminada ✅)

- **Cristian**: plantilla SAM (tablas + GSI, SNS, S3, HTTP API), `complete_delivery` (transacción), `orders_api` (órdenes por zona, repartidores, repartidores por área), pruebas e integración.
- **Carlos**: WebSocket API (`$connect`, `$disconnect`, `location-update`), `ws_connect`, `ws_disconnect`, permisos, stage y pruebas.
- **Oskar**: `location_update` (conditional write, geohash, broadcast filtrado, limpieza `410 Gone`), simulador y pruebas.
- **Sebas**: NGINX con rate limiting, `user-data.sh`, guía EC2/ALB, página de seguimiento, panel de operaciones y `loadtest/carga.py`.

### Etapa 2 — Despliegue, pruebas reales y evidencias (en curso)

#### Stack desplegado (`skyvery-prod`, us-east-1)

| Recurso | URL / valor |
|---|---|
| WebSocket (`WebSocketUrl`) | `wss://3aeuxt4nid.execute-api.us-east-1.amazonaws.com/prod` |
| API REST (`OrdersApiUrl`) | `https://qhbyd6ixm7.execute-api.us-east-1.amazonaws.com/prod` |
| Página de seguimiento (`FrontendUrl`) | `http://skyvery-frontend-485530831247-prod.s3-website-us-east-1.amazonaws.com` (vacía hasta que Sebas suba el `index.html`) |
| Tema SNS | `skyvery-delivery-completed-prod` |

Datos de prueba ya creados: repartidor `DRV-1`; órdenes `ORD-A` (zona `centro`, ya `DELIVERED`) y `ORD-B` (zona `norte`, `PENDING`).
Para sus pruebas creen órdenes nuevas, por ejemplo:

```bash
curl -X POST https://qhbyd6ixm7.execute-api.us-east-1.amazonaws.com/prod/orders -H "Content-Type: application/json" -d '{"orderId":"ORD-10","zone":"centro","driverId":"DRV-1"}'
```

- **Solo Cristian opera AWS** (perfil `skyvery`). Carlos, Oskar y Sebas trabajan únicamente en GitHub (rama propia + PR a `main`); no necesitan cuenta de AWS.
- Al terminar la demo: `sam delete --stack-name skyvery-prod --profile skyvery` y terminar la EC2 y el ALB.

Cada evidencia (captura o log) se guarda en `docs/evidencias/` con el nombre indicado.

**Cristian — todo lo que se hace en AWS**
- [x] `sam deploy` del stack y compartir las URL con el grupo.
- [x] Confirmar la suscripción SNS en el correo.
- [x] Crear repartidor y órdenes de prueba en dos zonas; probar la transacción (éxito y fallo con 409).
- [ ] Subir la página de seguimiento al bucket S3 (cuando Sebas ponga las URL).
- [ ] Lanzar la EC2 con `ec2-nginx/user-data.sh`, crear target group + ALB y verificar `/health`.
- [ ] Prueba de carga con `loadtest/carga.py` contra el ALB y sacar los 429 del log de NGINX.
- [ ] Ejecutar el simulador y `wscat` contra el stack para las evidencias del WebSocket.
- [ ] Tomar las capturas de la lista de evidencias (sección 8).

**Carlos — GitHub**
- [ ] `docs/demo.md`: guion paso a paso de la demo (qué se muestra, en qué orden, qué comando o pantalla y qué requisito cumple), usando las URL de esta sección.

**Oskar — GitHub**
- [ ] Opción `--route pasto` en `simulator/driver_sim.py` con una ruta en Pasto, para que coincida con el centro del mapa de la página de seguimiento (mantener la ruta de Bogotá por defecto y actualizar sus pruebas).

**Sebas — GitHub**
- [ ] Poner `WS_URL`/`API_URL` en `frontend/tracking-client/index.html` y `API_URL`/`TRACKING_URL` en `frontend/ops-panel/index.html` con las URL de esta sección.
- [ ] Sección de repartidores en el panel (`POST /drivers`, `GET /drivers`) y lista desplegable al marcar una orden como entregada.

**Todos**
- [ ] Ensayar la demo completa (sección 8).

## 6. Plan por fases

| Fase | Qué | Responsable | Estado |
|---|---|---|---|
| 1 | Estructura del repo + README | Cristian | ✅ |
| 2 | Tablas + GSI + SNS + HTTP API en SAM | Cristian | ✅ |
| 3 | WebSocket API + connect/disconnect | Carlos | ✅ |
| 4 | location_update + simulador | Oskar | ✅ |
| 5 | complete_delivery (transacción) + orders_api | Cristian | ✅ |
| 6 | NGINX + EC2 + frontends + prueba de carga (código) | Sebas | ✅ |
| 7 | Despliegue en AWS, evidencias y demo final | Todos | ⏳ |

## 7. Flujo de trabajo Git
- Cada integrante trabaja en su rama (`feat/websocket`, `feat/location`, `feat/nginx`, ...) y abre PR hacia `main`.
- Nunca subir credenciales AWS, `.pem` ni `.env`.

## 8. Requisitos para la demo y evidencias

| # | Requisito | Captura (`docs/evidencias/`) |
|---|---|---|
| 1 | Stack desplegado | `01-stack-outputs.png`: CloudFormation → `skyvery-prod` → Salidas |
| 2 | WebSocket con 3 rutas | `02-websocket-rutas.png`: API Gateway → `skyvery-ws-prod` → Rutas (`$connect`, `$disconnect`, `location-update`) |
| 3 | Suscripciones con GSI | `03-subscriptions-gsi.png`: DynamoDB → `skyvery-subscriptions-prod` → Índices (`orderId-index`) y elementos con dos clientes conectados |
| 4 | Broadcast filtrado | `04-broadcast-filtrado.png`: dos navegadores/`wscat` siguiendo órdenes distintas; cada uno recibe solo la suya |
| 5 | Conditional write | `05-ts-viejo-descartado.png`: simulador con `--stale-after 3` + log de CloudWatch de `location_update` con `ignored/stale` y la tabla `DriverLocations` con el `ts` más nuevo |
| 6 | Transacción exitosa | `06-entrega-ok.png`: respuesta 200 + orden `DELIVERED` + registro en `DeliveryHistory` + `deliveriesCount` aumentado |
| 7 | Transacción fallida | `07-entrega-409.png`: respuesta 409 con repartidor inexistente + la orden sigue `PENDING` y no hay historial |
| 8 | Confirmación SNS | `08-correo-sns.png`: correo "pedido … entregado" |
| 9 | GSI por zona geográfica | `09-gsi-zona.png`: `GET /zones/{zone}/orders` y `GET /areas/{geohashPrefix}/drivers` |
| 10 | Panel en EC2 detrás del ALB | `10-panel-alb.png`: panel abierto con la URL del ALB + target group *healthy* |
| 11 | Rate limiting | `11-carga-resumen.txt` (salida de `carga.py`) y `12-nginx-429.txt` (`grep " 429 " /var/log/nginx/access.log`) |
