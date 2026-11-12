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

### Etapa 2 — Despliegue, pruebas reales y evidencias (pendiente)

Cada evidencia (captura o log) se guarda en `docs/evidencias/` con un nombre claro.

**Cristian**
- [ ] `sam build` + `sam deploy --guided -t infra/template.yaml` (stage `prod`) y compartir con el grupo las salidas: `WebSocketUrl`, `OrdersApiUrl`, `FrontendUrl`.
- [ ] Confirmar la suscripción SNS en el correo.
- [ ] Crear repartidores (`POST /drivers`) y órdenes de prueba en dos zonas.
- [ ] Evidencia de la transacción: entrega exitosa (orden `DELIVERED`, historial, contador +1, correo SNS) y entrega fallida con repartidor inexistente (409 y nada guardado).
- [ ] Evidencia de consultas por GSI: `GET /zones/{zone}/orders` y `GET /areas/{geohashPrefix}/drivers`.

**Carlos**
- [ ] Probar con `wscat` contra el stack desplegado: conexión con `?orderId=`, rechazo sin `orderId`, desconexión borra la suscripción (captura de la tabla `Subscriptions`).

**Oskar**
- [ ] Ejecutar el simulador contra el stack desplegado con `--stale-after 3`.
- [ ] Evidencia del descarte: respuesta `ignored/stale` en los logs de CloudWatch de `location_update` y la tabla `DriverLocations` sin cambios con el `ts` viejo.
- [ ] Evidencia del broadcast filtrado: dos clientes siguiendo órdenes distintas, cada uno recibe solo su repartidor.

**Sebas**
- [ ] Poner `WS_URL`/`API_URL` en `frontend/tracking-client/index.html` y `API_URL`/`TRACKING_URL` en `frontend/ops-panel/index.html`; subir la página de seguimiento al bucket S3.
- [ ] Lanzar la EC2 con `user-data.sh`, crear target group + ALB y verificar `/health`.
- [ ] Prueba de carga con `loadtest/carga.py` contra el ALB y `grep " 429 " /var/log/nginx/access.log` → `docs/evidencias/`.
- [ ] (Opcional) Sección de repartidores en el panel usando `POST /drivers` y `GET /drivers`, con lista desplegable al marcar entregada.

**Todos**
- [ ] Ensayar la demo completa (sección 8) y apagar/eliminar los recursos al terminar (`sam delete`, terminar EC2 y ALB) para no generar costos.

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

## 8. Requisitos para la demo
1. Dos navegadores siguiendo órdenes distintas: cada uno solo recibe su repartidor.
2. Mensaje con timestamp viejo → descartado sin error.
3. Completar entrega → orden `DELIVERED`, historial creado, contador +1, email SNS recibido.
4. Prueba de carga → logs NGINX con respuestas **429**.
