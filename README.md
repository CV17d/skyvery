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
infra/template.yaml          # AWS SAM: tablas, Lambdas, WebSocket API, SNS, S3
lambdas/ws_connect/          # $connect   → guarda {connectionId, orderId}
lambdas/ws_disconnect/       # $disconnect→ borra la suscripción
lambdas/location_update/     # location-update → conditional write + broadcast
lambdas/complete_delivery/   # transacción + publicación SNS
lambdas/orders_api/          # API REST para crear/listar órdenes por zona (GSI)
simulator/driver_sim.py      # repartidor simulado (GPS cada 5 s)
frontend/tracking-client/    # página del cliente (S3 static website + mapa Leaflet)
frontend/ops-panel/          # panel de operaciones (servido por NGINX en EC2)
ec2-nginx/                   # nginx.conf + user-data
loadtest/                    # scripts de prueba de carga
docs/evidencias/             # capturas, logs 429, resultados de pruebas
```

## 5. Reparto de trabajo

### Cristian — Infraestructura base, datos y entrega (coordinador)
- [ ] `infra/template.yaml` (SAM): las 5 tablas DynamoDB con sus **GSI**, tema **SNS** con suscripción por email, bucket **S3**.
- [ ] `lambdas/complete_delivery`: **TransactWriteItems** (3 operaciones atómicas) + publicar en SNS. Probar que si una falla, ninguna persiste.
- [ ] `lambdas/orders_api`: crear orden, consultar órdenes por zona usando `zone-status-index`.
- [ ] Integrar el trabajo de todos, revisar PRs y mantener este README.

### Carlos — API Gateway WebSocket y conexiones
- [ ] Definir la **WebSocket API** con rutas `$connect`, `$disconnect` y `location-update` (`routeSelectionExpression: $request.body.action`).
- [ ] `lambdas/ws_connect`: leer `orderId` del query string y guardar `{connectionId, orderId}` en `Subscriptions`.
- [ ] `lambdas/ws_disconnect`: eliminar la suscripción.
- [ ] Permisos IAM (`execute-api:ManageConnections`) y stage `prod`. Probar con `wscat`.

### Oskar — Ubicación en tiempo real y simulador
- [ ] `lambdas/location_update`: **conditional write** por timestamp (descartar silenciosamente si es viejo), calcular `geohashPrefix`.
- [ ] Broadcast **solo a los suscriptores de la orden** (Query al GSI `orderId-index`), limpiar conexiones `410 Gone`.
- [ ] `simulator/driver_sim.py`: recorrer una ruta y enviar `{action:"location-update", orderId, driverId, lat, lng, ts}` cada 5 s; incluir un caso con timestamp viejo para demostrar el descarte.

### Sebas — EC2, NGINX, ALB, frontends y prueba de carga
- [ ] Lanzar **EC2** (Amazon Linux) con `ec2-nginx/user-data.sh` y `nginx.conf` con:
  `limit_req_zone $binary_remote_addr zone=perip:10m rate=10r/s;` y `limit_req zone=perip burst=20 nodelay; limit_req_status 429;`
  (detrás del ALB usar `real_ip_header X-Forwarded-For` para limitar por IP real del cliente).
- [ ] **ALB** + target group + health check hacia la EC2.
- [ ] `frontend/tracking-client` (S3, mapa Leaflet conectado al WebSocket) y `frontend/ops-panel`.
- [ ] `loadtest/`: prueba con `hey`/`ab`/`k6` (p. ej. 50 req/s), guardar en `docs/evidencias/` el resumen y `grep " 429 " /var/log/nginx/access.log`.

## 6. Plan por fases

| Fase | Qué | Responsable |
|---|---|---|
| 1 | Estructura del repo + README (este commit) | Cristian |
| 2 | Tablas + GSI + SNS en SAM; `sam deploy` inicial | Cristian |
| 3 | WebSocket API + connect/disconnect | Carlos |
| 4 | location_update + simulador | Oskar |
| 5 | complete_delivery (transacción) + orders_api | Cristian |
| 6 | EC2 + NGINX + ALB + frontends | Sebas |
| 7 | Prueba de carga, evidencias y demo final | Todos |

## 7. Flujo de trabajo Git
- Cada integrante trabaja en su rama (`feat/websocket`, `feat/location`, `feat/nginx`, ...) y abre PR hacia `main`.
- Nunca subir credenciales AWS, `.pem` ni `.env`.

## 8. Requisitos para la demo
1. Dos navegadores siguiendo órdenes distintas: cada uno solo recibe su repartidor.
2. Mensaje con timestamp viejo → descartado sin error.
3. Completar entrega → orden `DELIVERED`, historial creado, contador +1, email SNS recibido.
4. Prueba de carga → logs NGINX con respuestas **429**.
