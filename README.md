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
infra/ec2-alb.yaml           # CloudFormation: EC2 con NGINX + target group + ALB
lambdas/ws_connect/          # $connect   → guarda {connectionId, orderId}
lambdas/ws_disconnect/       # $disconnect→ borra la suscripción
lambdas/location_update/     # location-update → conditional write + broadcast
lambdas/complete_delivery/   # transacción + publicación SNS
lambdas/orders_api/          # API REST: órdenes por zona, repartidores, repartidores por área
simulator/driver_sim.py      # repartidor simulado (GPS cada 5 s, rutas bogota/pasto)
simulator/cliente_ws.py      # cliente WebSocket de consola (sigue una orden)
frontend/tracking-client/    # página del cliente (S3 static website + mapa Leaflet)
frontend/ops-panel/          # panel de operaciones (servido por NGINX en EC2)
ec2-nginx/                   # nginx.conf + user-data + guía EC2/ALB
loadtest/                    # prueba de carga (carga.py)
tests/                       # pruebas locales (sin AWS)
docs/                        # demo.md (guion), guia-estudio.md, websocket.md, location.md, evidencias/
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

## 5. Reparto de trabajo y estado

**Proyecto terminado ✅** — código, despliegue en AWS, pruebas reales y evidencias. No quedan tareas asignadas.

| Integrante | Aporte |
|---|---|
| **Cristian** | Plantilla SAM (5 tablas + GSI, SNS, S3, HTTP API), `complete_delivery` (transacción), `orders_api` (órdenes por zona, repartidores, repartidores por área geohash), plantilla EC2 + ALB, despliegue en AWS, demo en vivo, evidencias, integración y coordinación |
| **Carlos** | WebSocket API (`$connect`, `$disconnect`, `location-update`), `ws_connect`, `ws_disconnect`, permisos IAM, stage, pruebas y `docs/websocket.md` |
| **Oskar** | `location_update` (conditional write, geohash, broadcast filtrado, limpieza `410 Gone`), simulador del repartidor, pruebas de integración y `docs/location.md` |
| **Sebas** | `nginx.conf` con rate limiting e IP real tras el ALB, `user-data.sh`, guía EC2/ALB, página de seguimiento (Leaflet), panel de operaciones y `loadtest/carga.py` |

### Stack desplegado (us-east-1, perfil `skyvery`)

| Recurso | URL / valor |
|---|---|
| WebSocket (`WebSocketUrl`) | `wss://3aeuxt4nid.execute-api.us-east-1.amazonaws.com/prod` |
| API REST (`OrdersApiUrl`) | `https://qhbyd6ixm7.execute-api.us-east-1.amazonaws.com/prod` |
| Página de seguimiento (S3) | `http://skyvery-frontend-485530831247-prod.s3-website-us-east-1.amazonaws.com/index.html?orderId=<orden>` |
| Panel de operaciones (ALB → EC2 NGINX) | `http://skyvery-ops-alb-996397180.us-east-1.elb.amazonaws.com/` |
| Tema SNS | `skyvery-delivery-completed-prod` (correo confirmado) |
| Stacks | `skyvery-prod` (SAM) y `skyvery-ops` (EC2 + ALB) |

Solo Cristian opera AWS (perfil `skyvery`). La EC2 no tiene SSH: se administra con SSM (Session Manager).

**Guion de la demo:** [`docs/demo.md`](docs/demo.md) · **Guía de estudio:** [`docs/guia-estudio.md`](docs/guia-estudio.md)

**Al terminar la presentación, borrar todo** (la EC2 y el ALB cobran por hora; ver `docs/demo.md`):
```bash
aws cloudformation delete-stack --profile skyvery --stack-name skyvery-ops
aws s3 rm s3://skyvery-frontend-485530831247-prod --recursive --profile skyvery
sam delete --profile skyvery --stack-name skyvery-prod --no-prompts
```

## 6. Plan por fases

| Fase | Qué | Responsable | Estado |
|---|---|---|---|
| 1 | Estructura del repo + README | Cristian | ✅ |
| 2 | Tablas + GSI + SNS + HTTP API en SAM | Cristian | ✅ |
| 3 | WebSocket API + connect/disconnect | Carlos | ✅ |
| 4 | location_update + simulador | Oskar | ✅ |
| 5 | complete_delivery (transacción) + orders_api | Cristian | ✅ |
| 6 | NGINX + EC2 + frontends + prueba de carga (código) | Sebas | ✅ |
| 7 | Despliegue en AWS, EC2 + ALB, evidencias, guion y guía | Cristian | ✅ |

## 7. Flujo de trabajo Git
- Cada integrante trabaja en su rama (`feat/websocket`, `feat/location`, `feat/nginx`, ...) y abre PR hacia `main`.
- Nunca subir credenciales AWS, `.pem` ni `.env`.

## 8. Requisitos y evidencias

Evidencias generadas contra el stack real (carpeta `docs/evidencias/`):

| Requisito del enunciado | Cómo se cumple | Evidencia |
|---|---|---|
| WebSocket con `$connect`, `$disconnect` y `location-update` | API Gateway WebSocket, `routeSelectionExpression = $request.body.action` | `03-websocket-connect-disconnect.txt` |
| Tabla de suscripciones `{orderId, connectionId}` con GSI por `orderId` | `Subscriptions` + `orderId-index`; `$connect` inserta, `$disconnect` borra | `03-websocket-connect-disconnect.txt` |
| Broadcast solo a quienes siguen la orden | `Query` al GSI + `postToConnection` por conexión | `05-broadcast-y-ts-viejo.txt` |
| Conditional write por timestamp, descarte silencioso | `attribute_not_exists(ts) OR ts < :newTs`; `ConditionalCheckFailed` → 200 `ignored` | `05-broadcast-y-ts-viejo.txt` (CloudWatch + DynamoDB) |
| Transacción atómica de 3 operaciones | `TransactWriteItems` (orden, historial, contador) | `06-07-transaccion.txt` (éxito, fallo 409, repetición 409) |
| SNS al completar la entrega | `sns.publish` tras la transacción | Correo en cj29772@gmail.com |
| GSI para consultas por zona geográfica | `zone-status-index` (Orders) y `geohash-index` (DriverLocations) | `09-gsi-zona.txt` |
| NGINX en EC2 con rate limiting 10 r/s, burst 20 | `limit_req_zone … rate=10r/s`, `burst=20 nodelay`, `limit_req_status 429`, IP real vía `X-Forwarded-For` | `11-carga-resumen.txt` (119×200, 381×429), `12-nginx-429.txt` |

Capturas de pantalla opcionales para las diapositivas: salidas de CloudFormation, rutas del WebSocket, índices de DynamoDB, el mapa en vivo, el correo SNS, el target group *healthy* y el panel.
