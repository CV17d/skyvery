# Guía de estudio — Skyvery (Proyecto 10)

> Plataforma de delivery con seguimiento en tiempo real sobre AWS.
> Esta guía explica **qué** se construyó, **cómo** funciona cada pieza, **por qué** se decidió así y **qué preguntas** pueden hacer en la sustentación (con sus respuestas).

---

## Índice

1. [El proyecto en 1 minuto](#1-el-proyecto-en-1-minuto)
2. [Los servicios de AWS que usamos (conceptos base)](#2-los-servicios-de-aws-que-usamos-conceptos-base)
3. [Arquitectura y recorrido completo de un pedido](#3-arquitectura-y-recorrido-completo-de-un-pedido)
4. [Modelo de datos en DynamoDB](#4-modelo-de-datos-en-dynamodb)
5. [Requisito 1 — WebSocket, suscripciones y broadcast filtrado](#5-requisito-1--websocket-suscripciones-y-broadcast-filtrado)
6. [Requisito 2 — Conditional writes (escrituras condicionales)](#6-requisito-2--conditional-writes-escrituras-condicionales)
7. [Requisito 3 — Transacción atómica de entrega + SNS](#7-requisito-3--transacción-atómica-de-entrega--sns)
8. [Requisito 4 — NGINX en EC2 con rate limiting detrás de un ALB](#8-requisito-4--nginx-en-ec2-con-rate-limiting-detrás-de-un-alb)
9. [Complejidad adicional — GSI por zona geográfica (geohash)](#9-complejidad-adicional--gsi-por-zona-geográfica-geohash)
10. [Infraestructura como código (SAM y CloudFormation)](#10-infraestructura-como-código-sam-y-cloudformation)
11. [Frontends: página de seguimiento y panel de operaciones](#11-frontends-página-de-seguimiento-y-panel-de-operaciones)
12. [Seguridad](#12-seguridad)
13. [Escalabilidad, rendimiento y costos](#13-escalabilidad-rendimiento-y-costos)
14. [Pruebas y evidencias](#14-pruebas-y-evidencias)
15. [Limitaciones conocidas y mejoras posibles](#15-limitaciones-conocidas-y-mejoras-posibles)
16. [Banco de preguntas de sustentación (con respuestas)](#16-banco-de-preguntas-de-sustentación-con-respuestas)
17. [Glosario](#17-glosario)
18. [Comandos y datos de referencia](#18-comandos-y-datos-de-referencia)
19. [Quién hizo qué](#19-quién-hizo-qué)

---

## 1. El proyecto en 1 minuto

**Problema:** un cliente que pidió un domicilio quiere ver en un mapa, en vivo, dónde va el repartidor.

**Solución:**
- El repartidor (simulado con un script de Python) envía su posición GPS **cada 5 segundos** por una conexión **WebSocket** a **API Gateway**.
- Una **Lambda** guarda la posición en **DynamoDB** solo si es **más nueva** que la última guardada (**conditional write**) y la reenvía **únicamente** a los clientes que siguen **ese pedido** (gracias a una tabla de suscripciones con un **GSI** por `orderId`).
- El cliente ve el mapa en una página web estática publicada en **S3**.
- Cuando el pedido se entrega, otra Lambda ejecuta una **transacción** de DynamoDB que, de forma atómica, marca la orden como `DELIVERED`, crea el registro de historial y suma 1 al contador de entregas del repartidor. Luego **SNS** envía un correo de confirmación.
- Los operadores usan un **panel web** servido por **NGINX en una EC2**, detrás de un **Application Load Balancer (ALB)**. NGINX limita a **10 peticiones por segundo por IP con ráfaga de 20**, y responde **429** al exceso; lo demostramos con una prueba de carga.
- Complejidad adicional: **GSI por zona geográfica** (zona del pedido y **geohash** de la posición del repartidor).

**Frase para abrir la presentación:**
> "Skyvery es una plataforma serverless de seguimiento de domicilios en tiempo real. Usa WebSockets de API Gateway para empujar la ubicación del repartidor solo a los clientes interesados, DynamoDB con escrituras condicionales y transacciones para mantener los datos consistentes, SNS para notificar la entrega, y un panel en EC2 con NGINX que protege el servicio con rate limiting por IP."

---

## 2. Los servicios de AWS que usamos (conceptos base)

| Servicio | Qué es | Para qué lo usamos |
|---|---|---|
| **API Gateway (WebSocket API)** | Servicio administrado que mantiene conexiones WebSocket con los clientes y enruta cada mensaje a un backend (Lambda) según una "ruta". | Canal bidireccional y persistente: el repartidor envía ubicaciones y los clientes las reciben sin estar preguntando (sin *polling*). |
| **API Gateway (HTTP API)** | API REST liviana y barata (versión "v2"). | API de órdenes y repartidores: crear, consultar por zona, completar entrega. |
| **Lambda** | Funciones que se ejecutan bajo demanda; no hay servidores que administrar; se cobra por invocación y por milisegundos. | 5 funciones: `ws_connect`, `ws_disconnect`, `location_update`, `complete_delivery`, `orders_api`. |
| **DynamoDB** | Base de datos NoSQL clave-valor, totalmente administrada, latencia de milisegundos, escala automática. | 5 tablas: suscripciones, órdenes, ubicaciones, historial y repartidores. |
| **S3** | Almacenamiento de objetos; puede servir un sitio web estático. | Página de seguimiento del cliente (HTML + JavaScript + mapa Leaflet). |
| **SNS** | Servicio de notificaciones *pub/sub*: se publica un mensaje en un "tema" y llega a todos los suscriptores (correo, SMS, Lambda, SQS…). | Correo "pedido entregado". |
| **EC2** | Máquina virtual. | Servidor Amazon Linux 2023 con NGINX que sirve el panel de operaciones. |
| **ALB (Application Load Balancer)** | Balanceador de capa 7 (HTTP). Distribuye peticiones entre "targets" y revisa su salud. | Punto de entrada público al panel; la EC2 queda privada detrás de él. |
| **IAM** | Usuarios, roles y permisos. | Cada Lambda tiene un rol con permisos mínimos; la EC2 tiene un rol para SSM; usamos el usuario `skyvery-deployer`. |
| **CloudFormation / SAM** | Infraestructura como código: un archivo YAML describe todos los recursos. SAM es una extensión que simplifica Lambdas y APIs. | `infra/template.yaml` (todo lo serverless) e `infra/ec2-alb.yaml` (EC2 + ALB). |
| **CloudWatch Logs** | Registros de las Lambdas y servicios. | Evidencia de mensajes aceptados (`ok`) y descartados (`ignored`). |
| **SSM (Systems Manager)** | Administración de instancias sin SSH. | Leer los logs de NGINX y actualizar el panel sin abrir el puerto 22. |

### 2.1 ¿Qué es un WebSocket y por qué no HTTP normal?
- HTTP clásico es **petición–respuesta**: el cliente pregunta y el servidor contesta. Para "tiempo real" habría que preguntar cada pocos segundos (*polling*), gastando peticiones aunque no haya cambios.
- WebSocket abre **una conexión persistente y bidireccional**: el servidor puede **empujar** datos al cliente cuando ocurren.
- En API Gateway, cada conexión recibe un **`connectionId`**. El backend le envía mensajes con la **API de administración de conexiones** (`ApiGatewayManagementApi.post_to_connection`).

### 2.2 ¿Qué es serverless?
No significa "sin servidores", sino que **no los administramos**: AWS aprovisiona, escala y parchea. Pagamos por uso. Ventajas: escala automática, costo ~0 cuando no hay tráfico. Desventajas: *cold starts* (primera invocación más lenta), límites de tiempo de ejecución (Lambda máx. 15 min), menos control.

---

## 3. Arquitectura y recorrido completo de un pedido

```
 Repartidor (driver_sim.py) ──WS location-update──┐
                                                   ▼
 Cliente web (S3) ──WS $connect?orderId=X──►  API Gateway WebSocket  ◄── postToConnection
                                                   │                          ▲
                 ┌─────────────────┬───────────────┼───────────────┐          │
                 ▼                 ▼               ▼               │          │
            ws_connect       ws_disconnect    location_update ─────┴──────────┘
                 │                 │            │        │
                 ▼                 ▼            ▼        ▼
           Subscriptions  ◄──────────     DriverLocations  (conditional write)
           (GSI orderId-index)  ◄── Query ──┘

 Operador ─► ALB ─► EC2 (NGINX: limit_req 10r/s burst 20) ─► panel (HTML)
                                                              │ fetch
                                                              ▼
                                         API Gateway HTTP API ─► orders_api ─► Orders (GSI zone-status-index)
                                                              │                Drivers, DriverLocations (GSI geohash-index)
                                                              └► complete_delivery ─► TransactWriteItems
                                                                                     (Orders + DeliveryHistory + Drivers)
                                                                                   └► SNS ─► correo
```

### Recorrido paso a paso

1. **Se crea el repartidor** (`POST /drivers`) → fila en `Drivers` con `deliveriesCount = 0`.
2. **Se crea la orden** (`POST /orders`, zona `centro`, repartidor `DRV-2`) → fila en `Orders` con `status = IN_TRANSIT` (o `PENDING` si no tiene repartidor).
3. **El cliente abre la página de seguimiento** (`index.html?orderId=ORD-1`). El JavaScript abre `wss://…/prod?orderId=ORD-1`.
4. **API Gateway invoca `ws_connect`** (ruta `$connect`) con el `connectionId`. La Lambda guarda `{connectionId, orderId, ttl}` en `Subscriptions`. Si no hay `orderId` (y no es repartidor) responde **400** y API Gateway **rechaza** la conexión.
5. **El repartidor se conecta** con `?role=driver` (se acepta sin suscribirlo a nada) y cada 5 s envía:
   ```json
   {"action": "location-update", "orderId": "ORD-1", "driverId": "DRV-2", "lat": 1.2142, "lng": -77.2784, "ts": 1794527862}
   ```
6. **API Gateway mira el campo `action`** (`routeSelectionExpression = $request.body.action`) y como vale `location-update` invoca la Lambda `location_update`.
7. **`location_update`**:
   1. Valida el mensaje (campos obligatorios, números, rangos de lat/lng, `ts` positivo).
   2. Calcula el **geohash** (9 caracteres) y su prefijo (5 caracteres).
   3. Hace `PutItem` en `DriverLocations` **con condición** `attribute_not_exists(ts) OR ts < :newTs`.
   4. Si la condición falla → la ubicación es vieja → responde `200 {"status":"ignored"}` y **no hace broadcast**.
   5. Si se guardó → hace `Query` al GSI `orderId-index` de `Subscriptions` para obtener los `connectionId` que siguen esa orden y llama `post_to_connection` a cada uno. Si alguno responde **410 Gone** (cliente ya desconectado) borra esa suscripción.
8. **El navegador recibe el mensaje** y mueve el marcador en el mapa.
9. **El operador marca la orden como entregada** en el panel → `POST /orders/ORD-1/complete {driverId}` → `complete_delivery` ejecuta la **transacción** y luego **publica en SNS** → llega el correo.
10. **La página de seguimiento** consulta `GET /orders/ORD-1` cada 10 s; al ver `DELIVERED` muestra "Pedido entregado ✓".
11. **El cliente cierra la página** → API Gateway invoca `ws_disconnect` (ruta `$disconnect`) → se borra la suscripción.

---

## 4. Modelo de datos en DynamoDB

### 4.1 Conceptos clave
- **Partition key (PK)**: atributo que decide en qué partición física vive el ítem. Las búsquedas eficientes (`GetItem`, `Query`) necesitan la PK.
- **Sort key (SK)**: opcional; ordena ítems con la misma PK y permite rangos (`>=`, `between`, `begins_with`).
- **GSI (Global Secondary Index)**: una "copia" de la tabla con **otra clave** (PK/SK distintas). Permite consultar eficientemente por atributos que no son la clave principal. Se actualiza **automáticamente** pero de forma **eventualmente consistente** (puede tardar fracciones de segundo).
- **`Scan`** lee toda la tabla (lento y caro). **`Query`** lee solo una partición (rápido y barato). **Diseñamos los GSI para nunca necesitar `Scan` en los caminos críticos.**
- **Proyección**: qué atributos se copian al GSI (`KEYS_ONLY`, `INCLUDE`, `ALL`).
- **Modo de capacidad**: usamos **on-demand (`PAY_PER_REQUEST`)**: sin aprovisionar; se paga por lectura/escritura.

### 4.2 Las cinco tablas

| Tabla | Clave principal | GSI | Patrón de acceso que resuelve |
|---|---|---|---|
| `skyvery-subscriptions-prod` | PK `connectionId` | `orderId-index` (PK `orderId`, proyección `KEYS_ONLY`) | "Borrar la suscripción de esta conexión" (por `connectionId`) y "¿qué conexiones siguen la orden X?" (por `orderId`) |
| `skyvery-orders-prod` | PK `orderId` | `zone-status-index` (PK `zone`, SK `status`, `ALL`) | "Dame la orden X" y "órdenes de la zona Z (opcionalmente en estado S)" |
| `skyvery-driver-locations-prod` | PK `driverId` | `geohash-index` (PK `geohashPrefix`, SK `ts`, `ALL`) | "Última ubicación del repartidor" (con condición por `ts`) y "repartidores en la celda geográfica G desde el tiempo T" |
| `skyvery-delivery-history-prod` | PK `orderId`, SK `deliveredAt` | — | Registro histórico de entregas |
| `skyvery-drivers-prod` | PK `driverId` | — | Datos del repartidor y `deliveriesCount` |

### 4.3 Decisiones importantes (y por qué)
- **`Subscriptions` usa `connectionId` como PK** porque en `$disconnect` solo conocemos el `connectionId`; así el borrado es un `DeleteItem` directo. Para el broadcast necesitamos lo contrario (por `orderId`), por eso el **GSI**. Proyección `KEYS_ONLY` porque solo necesitamos el `connectionId` (más barato).
- **TTL en `Subscriptions`** (atributo `ttl` = ahora + 2 h): API Gateway corta toda conexión WebSocket a las **2 horas**; si por algún motivo `$disconnect` no se ejecuta, DynamoDB borra la suscripción huérfana automáticamente.
- **`DriverLocations` guarda solo la última posición** por repartidor (PK `driverId`): es lo que necesita el seguimiento; la condición por `ts` evita retrocesos.
- **`geohashPrefix` + `ts` en el GSI**: consulta "repartidores en esta zona, actualizados recientemente" sin escanear.
- **`zone` + `status` en el GSI de órdenes**: consulta "pedidos en camino en el centro" con un solo `Query`.

---

## 5. Requisito 1 — WebSocket, suscripciones y broadcast filtrado

**Enunciado:** *El WebSocket debe manejar `$connect`, `$disconnect` y `location-update`. La Lambda de `location-update` debe hacer broadcast solo a los clientes que siguen esa orden. DynamoDB debe tener una tabla de suscripciones `{orderId, connectionId}` con GSI para consultar por `orderId`.*

### 5.1 Rutas
| Ruta | Cuándo se invoca | Lambda | Qué hace |
|---|---|---|---|
| `$connect` | Al abrir la conexión (handshake) | `ws_connect` | Lee `orderId` del query string y guarda la suscripción. Sin `orderId` → 400 (conexión rechazada). Con `role=driver` → acepta sin suscribir. |
| `$disconnect` | Al cerrar la conexión (cliente, timeout o error) | `ws_disconnect` | `DeleteItem` por `connectionId` (idempotente). |
| `location-update` | Mensaje con `"action": "location-update"` | `location_update` | Conditional write + broadcast filtrado. |

- **`routeSelectionExpression: $request.body.action`**: API Gateway lee el JSON del mensaje y usa el valor de `action` como nombre de ruta.
- `$connect` y `$disconnect` son rutas **predefinidas** de API Gateway; `location-update` es **custom**.
- Existe además `$default` (cuando ninguna ruta coincide); no la definimos, así que un `action` desconocido da error de ruta.

### 5.2 Broadcast filtrado (código clave de `location_update`)
```python
def subscribers(order_id):
    kwargs = {"IndexName": "orderId-index", "KeyConditionExpression": Key("orderId").eq(order_id)}
    while True:
        res = subscriptions.query(**kwargs)
        for it in res.get("Items", []):
            yield it["connectionId"]
        if "LastEvaluatedKey" not in res:
            return
        kwargs["ExclusiveStartKey"] = res["LastEvaluatedKey"]   # paginación
```
```python
for connection_id in subscribers(item["orderId"]):
    try:
        api.post_to_connection(ConnectionId=connection_id, Data=payload)
    except ClientError as e:
        if e.response["Error"]["Code"] == "GoneException":      # 410: ya no existe
            subscriptions.delete_item(Key={"connectionId": connection_id})
```
**Puntos críticos:**
- Se usa **`Query` al GSI**, no `Scan`: el costo depende de cuántos siguen **esa** orden, no del total de conexiones.
- **Paginación** (`LastEvaluatedKey`): un `Query` devuelve máx. 1 MB; se recorre por páginas.
- **410 Gone**: la conexión cerró sin que `$disconnect` alcanzara a limpiar (o se cayó la red). Se borra la suscripción ("auto-sanación").
- El endpoint para `post_to_connection` es `https://{apiId}.execute-api.{región}.amazonaws.com/{stage}` (variable `WS_ENDPOINT`). La Lambda necesita el permiso **`execute-api:ManageConnections`**.
- El repartidor **no** recibe sus propios mensajes (no está suscrito a ninguna orden).

### 5.3 Límites de API Gateway WebSocket que conviene saber
- Conexión máxima: **2 horas**. Tiempo de inactividad: **10 minutos** (por eso la página reconecta sola con espera exponencial 1 s, 2 s, 4 s… hasta 15 s).
- Tamaño máximo de mensaje: 128 KB (tramas de 32 KB). Nuestro mensaje pesa ~150 bytes.

### 5.4 Evidencia
`docs/evidencias/03-websocket-connect-disconnect.txt` (rechazo sin `orderId`, 2 conexiones en el GSI, borrado al desconectar) y `05-broadcast-y-ts-viejo.txt` (cliente de `ORD-PASTO-1` recibe, cliente de `ORD-PASTO-2` recibe 0).

---

## 6. Requisito 2 — Conditional writes (escrituras condicionales)

**Enunciado:** *Solo aceptar la actualización si el timestamp nuevo es mayor al último almacenado. Si la condición falla, descartar silenciosamente sin error.*

### 6.1 ¿Por qué pueden llegar desordenadas?
- Cada mensaje del WebSocket genera una **invocación independiente** de Lambda; varias pueden ejecutarse **en paralelo** y terminar en distinto orden.
- Reintentos de red, reconexiones o un celular que envía datos guardados offline pueden mandar posiciones viejas.
- Sin protección, una posición vieja sobrescribiría a una nueva y el repartidor "saltaría hacia atrás" en el mapa.

### 6.2 Cómo se implementó
```python
locations.put_item(
    Item=item,
    ConditionExpression="attribute_not_exists(ts) OR ts < :newTs",
    ExpressionAttributeValues={":newTs": msg["ts"]},
)
...
except ClientError as e:
    if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
        return None            # → handler responde 200 {"status":"ignored","reason":"stale"} sin broadcast
    raise
```
- **`attribute_not_exists(ts)`**: primera ubicación del repartidor (no hay `ts` guardado).
- **`ts < :newTs`**: el guardado es **estrictamente** menor que el nuevo → un `ts` **igual** también se descarta (evita duplicados).
- La condición la evalúa **DynamoDB del lado del servidor, de forma atómica**, en la misma operación de escritura. **No** hacemos "leer y luego escribir" (eso tendría una *race condition*: dos Lambdas leen el mismo valor viejo y ambas escriben).
- "Silenciosamente": responde **200** (no 4xx/5xx), no lanza excepción, no hace broadcast. Lo registramos en CloudWatch como `{"status": "ignored", "reason": "stale"}` para poder demostrarlo.

### 6.3 Evidencia
`05-broadcast-y-ts-viejo.txt`: el simulador (`--stale-after 3`) envía un `ts` 60 s más viejo; CloudWatch muestra `ignored/stale`; el cliente no lo recibe; el ítem de DynamoDB conserva el `ts` más nuevo.

---

## 7. Requisito 3 — Transacción atómica de entrega + SNS

**Enunciado:** *Al completarse una entrega, en una sola operación atómica: estado `DELIVERED`, registro en historial e incremento del contador del repartidor. Si una falla, ninguna persiste.*

### 7.1 Código clave (`complete_delivery`)
```python
ddb.transact_write_items(TransactItems=[
  {"Update": {  # 1) Orden → DELIVERED
     "TableName": ORDERS, "Key": {"orderId": {"S": order_id}},
     "UpdateExpression": "SET #s = :delivered, deliveredAt = :now, driverId = :d",
     "ConditionExpression": "attribute_exists(orderId) AND #s <> :delivered", ...}},
  {"Put": {     # 2) Historial
     "TableName": HISTORY, "Item": {...},
     "ConditionExpression": "attribute_not_exists(orderId)"}},
  {"Update": {  # 3) Contador del repartidor
     "TableName": DRIVERS, "Key": {"driverId": {"S": driver_id}},
     "UpdateExpression": "ADD deliveriesCount :one",
     "ConditionExpression": "attribute_exists(driverId)", ...}},
])
```

### 7.2 Por qué cada condición
| Condición | Evita |
|---|---|
| `attribute_exists(orderId)` | Crear una orden "fantasma" al entregar un ID que no existe (`Update` crea el ítem si no existe). |
| `#s <> :delivered` | Entregar dos veces la misma orden y sumar dos veces al contador. |
| `attribute_not_exists(orderId)` en historial | Duplicar el registro histórico. |
| `attribute_exists(driverId)` | Sumar entregas a un repartidor inexistente (y crearlo por accidente con `ADD`). |

- `#s` es un **alias** porque `status` es **palabra reservada** de DynamoDB.
- `ADD deliveriesCount :one` es un **incremento atómico** (no hay que leer el valor actual).

### 7.3 Qué pasa si algo falla
- DynamoDB lanza **`TransactionCanceledException`** con **`CancellationReasons`** (una por operación, en orden). Ejemplo real: `["None", "None", "ConditionalCheckFailed"]` = falló la 3.ª (repartidor inexistente).
- **Ninguna** de las tres escrituras se aplica (todo o nada). La API responde **409 Conflict** con los motivos.
- Evidencia `06-07-transaccion.txt`: caso fallido (409, orden sigue `IN_TRANSIT`, historial vacío), caso exitoso (las tres cosas cambiaron), y repetición (409, contador sigue en 1).

### 7.4 Propiedades de las transacciones de DynamoDB (para preguntas)
- **ACID** dentro de la transacción; aisladas de otras escrituras.
- Hasta **100 acciones** y 4 MB por transacción; pueden abarcar **varias tablas** de la misma cuenta y región.
- Consumen **el doble** de capacidad que una escritura normal (preparar + confirmar).
- Acciones posibles: `Put`, `Update`, `Delete`, `ConditionCheck`.
- Idempotencia opcional con `ClientRequestToken` (no lo usamos; las condiciones ya evitan la doble entrega).

### 7.5 SNS
- Después de la transacción exitosa: `sns.publish(TopicArn, Subject, Message)` al tema `skyvery-delivery-completed-prod`, suscrito por **correo** (`cj29772@gmail.com`).
- Una suscripción de correo **debe confirmarse** (clic en "Confirm subscription"); antes de eso queda `PendingConfirmation` y no recibe nada.
- **Punto crítico:** SNS está **fuera** de la transacción (DynamoDB no puede incluir SNS en una transacción). Si la transacción se confirma y luego SNS fallara, la entrega queda registrada pero sin correo. Mejora: **DynamoDB Streams** sobre `DeliveryHistory` → Lambda → SNS (patrón *outbox*), con reintentos automáticos.

---

## 8. Requisito 4 — NGINX en EC2 con rate limiting detrás de un ALB

**Enunciado:** *EC2 con NGINX sirve el panel con rate limiting por IP (`limit_req_zone`): máx. 10 req/s por IP con burst de 20. Demostrar con prueba de carga y mostrar los logs con 429.*

### 8.1 Configuración clave (`ec2-nginx/nginx.conf`)
```nginx
set_real_ip_from 10.0.0.0/8;  set_real_ip_from 172.16.0.0/12;  set_real_ip_from 192.168.0.0/16;
real_ip_header    X-Forwarded-For;
real_ip_recursive on;

limit_req_zone $binary_remote_addr zone=perip:10m rate=10r/s;
limit_req_status 429;

location = /health { access_log off; return 200 "ok\n"; }          # health check del ALB, sin límite
location / { limit_req zone=perip burst=20 nodelay; try_files $uri $uri/ /index.html; }
error_page 429 @limite;   # responde JSON {"error": "Demasiadas peticiones..."} + Retry-After: 1
```

### 8.2 Cómo funciona `limit_req` (algoritmo *leaky bucket*)
- **`rate=10r/s`** = se "drena" 1 petición cada **100 ms** por IP.
- **`burst=20`** = se toleran hasta **20 peticiones de exceso** acumuladas (el "balde").
- **`nodelay`** = las peticiones dentro del burst se atienden **de inmediato** (sin encolarlas ni retrasarlas); el espacio del balde se libera al ritmo de 10/s.
- Lo que excede el balde → **429 Too Many Requests** (por defecto NGINX usaría 503; `limit_req_status 429` lo cambia).
- **`$binary_remote_addr`**: la IP del cliente en binario (4 bytes en IPv4) → clave del contador. **`zone=perip:10m`**: 10 MB de memoria compartida ≈ **160 000 IPs**.

### 8.3 Por qué `real_ip` es crítico detrás del ALB
- La conexión TCP a NGINX viene **del ALB** (IP privada de la VPC, 172.31.x.x). Sin configuración, **todos los clientes compartirían la misma IP** → el límite sería global, no por IP.
- El ALB agrega el encabezado **`X-Forwarded-For`** con la IP real del cliente. `set_real_ip_from` dice "confía en ese encabezado solo si la petición viene de la red privada" (evita que alguien de internet falsifique el encabezado). `real_ip_recursive on` toma la última IP no confiable de la cadena.
- En el log se ve la IP real del cliente y `xff="…"`.

### 8.4 La prueba de carga y el resultado
- `loadtest/carga.py`: envía peticiones a **ritmo constante** (50 req/s durante 10 s = 500) con hilos, y cuenta los códigos.
- **Esperado:** aceptadas ≈ burst + rate × tiempo = 20 + 10 × 10 = **120**; el resto 429.
- **Obtenido (real, contra el ALB):** **119 × 200 y 381 × 429**. Logs: 381 líneas `429 … limit=REJECTED` en `access.log` y `limiting requests, excess: 20.x by zone "perip"` en `error.log` (evidencias `11-carga-resumen.txt` y `12-nginx-429.txt`).
- La diferencia de 1 se debe a la discretización del tiempo (el primer instante y el redondeo del "balde").

### 8.5 EC2, ALB y target group (`infra/ec2-alb.yaml`)
- **EC2** `t3.micro`, Amazon Linux 2023, **user-data** que instala NGINX y git, clona el repo, copia el panel y `nginx.conf`, valida con `nginx -t` y arranca NGINX.
- **Security groups:** el del ALB permite 80 desde internet; el de la EC2 permite 80 **solo desde el security group del ALB** → la EC2 no es accesible directamente.
- **Sin SSH (sin puerto 22):** rol IAM con `AmazonSSMManagedInstanceCore` para administrarla con **SSM**.
- **Target group** con health check `GET /health` cada 15 s (2 éxitos = *healthy*). `/health` no tiene límite ni se registra, para no contaminar logs ni ser bloqueado.
- **ALB** `internet-facing` en 2 zonas de disponibilidad (requisito del ALB), listener HTTP:80 → target group.

---

## 9. Complejidad adicional — GSI por zona geográfica (geohash)

### 9.1 Dos niveles de "zona"
1. **Zona lógica del pedido** (`zone`: "centro", "norte"): GSI `zone-status-index` (PK `zone`, SK `status`). Consulta: `GET /zones/{zone}/orders?status=IN_TRANSIT`.
2. **Zona física del repartidor** (**geohash**): GSI `geohash-index` (PK `geohashPrefix`, SK `ts`). Consulta: `GET /areas/{geohashPrefix}/drivers?since=<epoch>`.

### 9.2 ¿Qué es un geohash?
- Codifica (lat, lng) en una cadena base-32 dividiendo el mundo recursivamente en rectángulos: cada bit alterna longitud/latitud ("¿estoy en la mitad izquierda o derecha?").
- **Propiedad clave:** puntos cercanos comparten **prefijo**. Un prefijo más largo = celda más pequeña.

| Caracteres | Tamaño aprox. de la celda |
|---|---|
| 5 | ≈ 4,9 km × 4,9 km |
| 6 | ≈ 1,2 km × 0,6 km |
| 7 | ≈ 153 m × 153 m |
| 9 | ≈ 4,8 m × 4,8 m |

- Guardamos el geohash de **9** caracteres (posición precisa) y el **prefijo de 5** como PK del GSI → "todos los repartidores en esta celda de ~5 km". Ej.: Pasto centro = `d21nc`.
- Con `ts` como SK se filtra "actualizados después de T" (repartidores activos) dentro del mismo `Query`.
- **Limitación (pregunta frecuente):** un punto cerca del **borde** de la celda puede tener vecinos en la celda contigua. Solución estándar: consultar la celda y sus **8 vecinas**. Para la demo, una celda basta.
- Implementado **sin librerías externas** (`geohash_encode` en `location_update`).

Evidencia: `09-gsi-zona.txt`.

---

## 10. Infraestructura como código (SAM y CloudFormation)

- **`infra/template.yaml` (SAM)** → stack `skyvery-prod`: 5 tablas, tema SNS, bucket S3 con política de lectura pública, HTTP API + 2 Lambdas, WebSocket API (`AWS::ApiGatewayV2::Api` con 3 rutas, integraciones, permisos, *deployment* y *stage* `prod`) + 3 Lambdas.
- **`infra/ec2-alb.yaml` (CloudFormation)** → stack `skyvery-ops`: security groups, rol + instance profile, EC2, target group, ALB y listener.
- **Ventajas:** reproducible (se borra y recrea con un comando), versionado en git, revisable, sin "clics olvidados".
- **Políticas SAM** (`DynamoDBCrudPolicy`, `DynamoDBReadPolicy`, `SNSPublishMessagePolicy`) generan IAM con **mínimo privilegio** por tabla/tema.
- **Detalle técnico:** en WebSocket, CloudFormation no crea un *deployment* nuevo si cambian las rutas; hay que renombrar el recurso `WebSocketDeployment` para forzarlo (nota en la plantilla).
- **Error real que tuvimos:** rutas como `Path: /orders/{orderId}` dentro de un mapa YAML en línea `{ ... }` rompían el YAML (las llaves se confunden). Se arregló con comillas: `Path: "/orders/{orderId}"`. Validado con `cfn-lint`.
- Despliegue: `sam build` + `sam deploy` (sube el código a un bucket de SAM y crea/actualiza el stack con un *change set*).

---

## 11. Frontends: página de seguimiento y panel de operaciones

### 11.1 Página de seguimiento (`frontend/tracking-client/index.html`, publicada en S3)
- Mapa **Leaflet** con mosaicos de OpenStreetMap; centro inicial en Pasto.
- Lee `orderId` de la URL; abre `wss://…?orderId=…`; en cada mensaje mueve el marcador 🛵 y dibuja el rastro.
- **Defensa extra en el cliente:** ignora mensajes de otra orden y mensajes con `ts` menor al último (aunque el backend ya filtra).
- **Reconexión automática** con espera exponencial (1, 2, 4… hasta 15 s).
- Cada 10 s consulta `GET /orders/{id}`; si está `DELIVERED` muestra "Pedido entregado ✓".
- **Modo demo sin backend:** `?demo=1`.
- S3 sitio web estático solo sirve **HTTP**; para HTTPS se usaría **CloudFront**.

### 11.2 Panel de operaciones (`frontend/ops-panel/index.html`, servido por NGINX en EC2)
- Crear órdenes, listar por zona y estado (GSI), **marcar entregada** (transacción), enlace "Ver mapa".
- Crear y listar **repartidores** con su contador de entregas; autocompletado de `driverId`.
- Llama a la HTTP API desde el navegador → requiere **CORS** (configurado en la HTTP API: orígenes `*`, métodos GET/POST/OPTIONS).
- Las URL de la API vienen por defecto en el código y se pueden cambiar en "Configuración" (guardado en `localStorage`).
- Construye el HTML con `textContent` (no `innerHTML`) → evita inyección de HTML/XSS con datos de la API.

---

## 12. Seguridad

| Aspecto | Qué hicimos | Qué faltaría en producción |
|---|---|---|
| Permisos de Lambdas | Rol por función con acceso solo a sus tablas/tema (mínimo privilegio) | — |
| EC2 | Sin SSH; SG solo acepta tráfico del ALB; administración por SSM | Parches automáticos (SSM Patch Manager) |
| Rate limiting | NGINX 10 r/s por IP real | AWS WAF en el ALB (límite global y reglas administradas) |
| Autenticación | **Ninguna** (`AuthorizationType: NONE`) — cualquiera con la URL puede conectarse o crear órdenes | Cognito / JWT authorizer en HTTP API; Lambda authorizer en `$connect` (validar que el usuario es dueño de la orden); el repartidor debería autenticarse |
| Transporte | API Gateway: HTTPS/WSS. ALB y S3: **HTTP** | Certificado ACM en el ALB (HTTPS 443) y CloudFront delante de S3 |
| CORS | `*` | Solo el dominio del panel |
| Bucket S3 | Lectura pública (necesario para sitio estático) | CloudFront + OAC con bucket privado |
| Validación de entrada | `location_update` valida tipos y rangos; APIs validan campos obligatorios | Esquemas de validación en API Gateway |
| Credenciales | Nunca en el repo (`.gitignore` incluye `.env`, `*.pem`); perfil local `skyvery` | Rotación de llaves, MFA |
| Privacidad | IP pública enmascarada en evidencias del repo | — |

---

## 13. Escalabilidad, rendimiento y costos

- **Lambda** escala sola (cuota de concurrencia típica de 1 000 por región; las cuentas nuevas pueden tener menos). **DynamoDB on-demand** escala sin configurar. **API Gateway** soporta muchas conexiones concurrentes.
- **Fan-out:** el costo del broadcast es proporcional a los suscriptores **de esa orden** (normalmente 1–3), gracias al GSI. Si una orden tuviera miles de seguidores, convendría enviar en paralelo o por lotes.
- **Frecuencia:** 1 mensaje cada 5 s por repartidor = 12/min. Con 1 000 repartidores ≈ 200 invocaciones/s: perfectamente manejable.
- **Cuello de botella del diseño:** el panel en **una sola EC2**. Mejora: **Auto Scaling Group** detrás del ALB. Ojo: con varias instancias, el `limit_req` es **por instancia** (cada NGINX cuenta por su lado); para un límite global usar **AWS WAF rate-based rules** o un almacén compartido.
- **GSI eventualmente consistente:** un cliente que acaba de conectarse podría perder la primera ubicación (milisegundos de retraso del índice); recibe la siguiente 5 s después. Aceptable para seguimiento.
- **Cold starts:** la primera invocación de una Lambda tarda más (~300–800 ms); luego se reutiliza el entorno (por eso el cliente de DynamoDB/SNS se crea fuera del handler).
- **Costos aproximados** (demo): Lambda, API Gateway, DynamoDB y SNS ≈ centavos (o capa gratuita). **EC2 t3.micro ≈ 0,0104 USD/h** y **ALB ≈ 0,0225 USD/h + LCU** → ≈ 0,8 USD/día entre ambos. **Por eso hay que borrar los stacks después de la presentación.**

---

## 14. Pruebas y evidencias

### 14.1 Pruebas automáticas (`python -m pytest -q tests` → **49 pruebas**)
| Archivo | Qué prueba | Técnica |
|---|---|---|
| `test_ws_handlers.py` | `ws_connect` / `ws_disconnect` (guardar, rechazar sin `orderId`, repartidor, TTL, borrado idempotente) | `unittest.mock` de boto3 |
| `test_location_update.py` | Validación, conditional write, descarte silencioso, geohash, broadcast filtrado, limpieza de 410 | mocks |
| `test_integration_location.py` | Integración con el template y con `ws_connect`/`ws_disconnect` | moto (DynamoDB simulado) |
| `test_driver_sim.py` | Ruta, interpolación, mensaje viejo, `ts` creciente, CLI, ruta Pasto | unitarias |
| `test_entregas_y_ordenes.py` | Transacción (éxito, repartidor inexistente sin persistir nada, doble entrega), consultas por zona y por área, repartidores | **moto** (DynamoDB + SNS en memoria) |

Las pruebas encontraron un error real: la API devolvía números de DynamoDB (`Decimal`) como texto (`"0"`); se corrigió con un serializador JSON.

### 14.2 Evidencias contra AWS real (`docs/evidencias/`)
`03` connect/disconnect · `05` broadcast filtrado + `ts` viejo (CloudWatch + DynamoDB) · `06-07` transacción (3 casos) · `09` GSI por zona y por geohash · `11` resumen de carga · `12` logs NGINX con 429.

---

## 15. Limitaciones conocidas y mejoras posibles

1. **Sin autenticación** (ver seguridad) — la mejora más importante.
2. **SNS fuera de la transacción** → usar DynamoDB Streams (*outbox*).
3. **Una sola EC2** → Auto Scaling Group; WAF para límite global.
4. **HTTP en ALB y S3** → ACM + HTTPS, CloudFront.
5. **El fin de entrega no se empuja por WebSocket**: la página consulta la API cada 10 s. Mejora: que `complete_delivery` haga broadcast de `{"status":"DELIVERED"}` a los suscriptores (la página ya sabe procesarlo).
6. **Geohash de una sola celda** → consultar celda + 8 vecinas.
7. **`GET /drivers` usa `Scan`** (aceptable con pocos repartidores; con muchos, paginar o un GSI).
8. **Historial solo de entregas**; no guardamos el recorrido completo (podría ir a una tabla con TTL o a S3/Kinesis para analítica).
9. **El repartidor podría enviar ubicaciones de órdenes ajenas** (no se valida que `driverId` sea el asignado a la orden).
10. **Reconexión del cliente** no recupera la última posición al instante (espera al siguiente mensaje); mejora: endpoint `GET /orders/{id}/location`.

---

## 16. Banco de preguntas de sustentación (con respuestas)

### Arquitectura y decisiones
1. **¿Por qué WebSocket y no que el cliente consulte la API cada 5 s?**
   Con *polling* cada cliente hace peticiones aunque no haya cambios (costo y latencia). Con WebSocket el servidor empuja la ubicación apenas llega; una sola conexión persistente, menos tráfico y actualización inmediata.
2. **¿Por qué Lambda y no un servidor para el WebSocket?**
   API Gateway mantiene las conexiones; nosotros solo procesamos eventos. Lambda escala automáticamente, no hay que administrar servidores y no cuesta nada sin tráfico.
3. **¿Por qué DynamoDB y no una base relacional (RDS)?**
   Patrones de acceso simples por clave, latencia de milisegundos, escala sin administración, modo on-demand, soporta escrituras condicionales, transacciones, TTL y GSI. Una relacional necesitaría servidor, conexiones (problemáticas con muchas Lambdas) y escalado manual.
4. **¿Por qué HTTP API y no REST API de API Gateway?**
   HTTP API es más simple, más rápida y ~70 % más barata; no necesitábamos funciones exclusivas de REST API (planes de uso, validación de modelos, caché).
5. **¿Por qué el panel en EC2 si todo lo demás es serverless?**
   Lo exige el enunciado (NGINX con `limit_req_zone`), y permite demostrar rate limiting a nivel de servidor web, balanceador, health checks y security groups.
6. **¿Para qué sirve el ALB si hay una sola EC2?**
   Punto de entrada estable (DNS), health checks, la EC2 queda privada (solo acepta tráfico del ALB) y permite escalar a varias instancias sin cambiar la URL. Además inyecta `X-Forwarded-For`.

### WebSocket y suscripciones
7. **¿Qué es el `connectionId` y quién lo genera?** Un identificador único que API Gateway asigna a cada conexión; lo recibimos en `requestContext.connectionId` y lo usamos para enviar mensajes con `post_to_connection`.
8. **¿Cómo sabe API Gateway qué Lambda invocar?** Por la `routeSelectionExpression` `$request.body.action`: lee el campo `action` del JSON. `$connect`/`$disconnect` se invocan automáticamente al abrir/cerrar.
9. **¿Cómo se suscribe un cliente a una orden?** En el *handshake*: `wss://…/prod?orderId=ORD-1`. `ws_connect` guarda `{connectionId, orderId}`.
10. **¿Qué pasa si el cliente se conecta sin `orderId`?** `ws_connect` responde 400 y API Gateway rechaza la conexión (evidencia 03).
11. **¿Por qué la PK de `Subscriptions` es `connectionId` y no `orderId`?** Porque al desconectar solo sabemos el `connectionId` (borrado directo). Para consultar por orden usamos el GSI. Así ambos accesos son eficientes.
12. **¿Por qué un GSI y no un `Scan` con filtro?** `Scan` lee **toda** la tabla en cada mensaje (costo y latencia crecen con el total de conexiones). `Query` al GSI lee solo las conexiones de esa orden.
13. **¿Qué es `410 Gone` y qué hacen con él?** API Gateway responde así cuando la conexión ya no existe. Borramos la suscripción para no volver a intentarlo.
14. **¿Y si `$disconnect` nunca se ejecuta?** Limpieza doble: el 410 en el siguiente broadcast y el **TTL** de 2 h en DynamoDB.
15. **¿El repartidor recibe mensajes?** No; se conecta con `role=driver` y no queda suscrito a ninguna orden.
16. **¿Puede un cliente seguir varias órdenes?** Con este diseño, una conexión = una orden (PK `connectionId`). Para varias, abriría varias conexiones o cambiaríamos la clave a `connectionId + orderId`.
17. **¿Qué límites tiene la conexión WebSocket?** 10 min de inactividad y 2 h máximo; por eso el cliente reconecta automáticamente.

### Conditional writes
18. **¿Qué problema resuelve el conditional write?** Que una ubicación vieja (llegada tarde o procesada en otro orden) sobrescriba una más nueva.
19. **¿Por qué no leer el `ts` y comparar en la Lambda?** *Race condition*: dos Lambdas concurrentes leen el mismo valor y ambas escriben. La condición en DynamoDB es atómica.
20. **¿Qué significa "descartar silenciosamente"?** Capturar `ConditionalCheckFailedException`, no propagar error, responder 200 `ignored` y no hacer broadcast.
21. **¿Qué pasa si llega el mismo `ts` dos veces?** Se descarta (`ts < :newTs` es estricto). Evita duplicados.
22. **¿Dónde se ve en la demo?** Simulador con `--stale-after 3` → línea "VIEJO", CloudWatch `{"status":"ignored","reason":"stale"}`, el cliente no lo recibe, DynamoDB conserva el `ts` mayor.
23. **¿Por qué el `ts` lo pone el repartidor y no el servidor?** Representa **cuándo se tomó** la posición GPS; el orden de llegada al servidor no es confiable.

### Transacción
24. **¿Qué es atomicidad?** Todas las operaciones se aplican o ninguna; no existen estados intermedios visibles.
25. **¿Qué API usaron?** `TransactWriteItems` con `Update` (orden), `Put` (historial) y `Update` con `ADD` (contador).
26. **Demuestre que si una falla, ninguna persiste.** Repartidor inexistente → `CancellationReasons = [None, None, ConditionalCheckFailed]` → 409; la orden sigue `IN_TRANSIT`, historial vacío (evidencia 06-07 y prueba automática).
27. **¿Qué evita entregar dos veces?** La condición `status <> DELIVERED`; la segunda vez da 409 y el contador no cambia.
28. **¿Por qué `#s` en vez de `status`?** `status` es palabra reservada en DynamoDB; se usa un alias en `ExpressionAttributeNames`.
29. **¿Cuántas operaciones admite una transacción?** Hasta 100 acciones (4 MB), en varias tablas de la misma región; cuesta el doble de capacidad.
30. **¿SNS es parte de la transacción?** No. Se publica después de confirmar. Si SNS fallara, la entrega queda guardada sin correo; la mejora es DynamoDB Streams + Lambda (patrón outbox).
31. **¿Por qué se debe confirmar el correo de SNS?** Para evitar spam: AWS exige confirmación del dueño del correo antes de enviarle notificaciones.

### NGINX y rate limiting
32. **Explique `rate=10r/s burst=20 nodelay`.** 10 por segundo sostenido (1 cada 100 ms), hasta 20 de exceso tolerado atendidas de inmediato; lo demás recibe 429.
33. **¿Qué algoritmo usa NGINX?** *Leaky bucket* (balde con fuga).
34. **¿Por qué 119 aceptadas en la prueba?** Esperado 20 (burst) + 10 × 10 s = 120; la diferencia de 1 es por discretización del tiempo.
35. **¿Por qué 429 y no 503?** 429 *Too Many Requests* es el código semántico correcto; NGINX por defecto usa 503, lo cambiamos con `limit_req_status 429`.
36. **¿Cómo limitan por IP si las peticiones vienen del ALB?** Con el módulo `real_ip`: confiamos en `X-Forwarded-For` solo si viene de la red privada (el ALB). En los logs se ve la IP real del cliente.
37. **¿Qué pasa con el health check?** `/health` no tiene límite ni log, para que el ALB nunca marque la instancia como no saludable por el rate limiting.
38. **¿Qué significa `zone=perip:10m`?** Zona de memoria compartida de 10 MB para los contadores (~160 000 IPs).
39. **Si hubiera 3 instancias, ¿el límite sigue siendo 10 r/s?** No: sería por instancia (hasta ~30 r/s si el ALB reparte). Para límite global: AWS WAF con reglas *rate-based*.
40. **¿Cómo leyeron los logs sin SSH?** Con AWS Systems Manager (`send-command` / Session Manager); la EC2 tiene un rol con `AmazonSSMManagedInstanceCore`.

### GSI y geografía
41. **¿Qué es un GSI?** Índice con otra clave de partición/orden, mantenido automáticamente por DynamoDB, que permite `Query` eficientes por otros atributos. Es eventualmente consistente.
42. **¿Diferencia entre GSI y LSI?** LSI comparte la PK de la tabla y cambia solo la SK; se define al crear la tabla y permite lectura fuertemente consistente. GSI puede tener otra PK, se puede crear después y es eventualmente consistente.
43. **¿Qué es geohash y por qué sirve?** Codificación de coordenadas en texto donde lugares cercanos comparten prefijo; el prefijo es una "zona" ideal como PK del GSI.
44. **¿Por qué prefijo de 5?** Celda de ~5 km: tamaño razonable de "zona de reparto" en una ciudad.
45. **¿Qué problema tiene el geohash?** Bordes: dos puntos cercanos pueden caer en celdas distintas; se soluciona consultando las 8 celdas vecinas.
46. **¿Para qué sirve el `ts` como SK del GSI geográfico?** Para filtrar repartidores con ubicación reciente (`since`) en la misma consulta.

### Infraestructura, seguridad y operación
47. **¿Cómo se despliega todo?** `sam build` + `sam deploy` (stack serverless) y `aws cloudformation deploy` (EC2 + ALB). Todo está en YAML en el repo.
48. **¿Qué permisos tiene cada Lambda?** Solo los necesarios: p. ej. `location_update` puede leer/escribir `DriverLocations` y `Subscriptions` y `execute-api:ManageConnections` sobre su API.
49. **¿Cuál es la principal debilidad de seguridad?** No hay autenticación; cualquiera con la URL puede suscribirse a una orden o enviar ubicaciones. Mejora: Cognito/JWT y Lambda authorizer.
50. **¿Cuánto cuesta?** Serverless ≈ centavos con poco tráfico; EC2 + ALB ≈ 0,8 USD/día. Se borra todo al terminar.
51. **¿Qué pasa si se cae la EC2?** El health check la marca *unhealthy* y el ALB deja de enviarle tráfico (responde 503). Con un Auto Scaling Group se reemplazaría sola. El seguimiento en tiempo real **no se afecta** (es serverless e independiente).
52. **¿Cómo probaron sin gastar en AWS?** Pruebas locales con mocks y **moto** (simula DynamoDB/SNS), 49 en total; luego pruebas reales en AWS con evidencias.

---

## 17. Glosario

| Término | Significado |
|---|---|
| **ACID** | Atomicidad, Consistencia, Aislamiento, Durabilidad (propiedades de una transacción). |
| **ALB** | Application Load Balancer: balanceador HTTP (capa 7). |
| **Broadcast / fan-out** | Enviar un mismo mensaje a varios destinatarios. |
| **Burst** | Exceso de peticiones tolerado por encima del ritmo sostenido. |
| **Cold start** | Primera ejecución de una Lambda, más lenta por inicializar el entorno. |
| **Conditional write** | Escritura que DynamoDB solo aplica si se cumple una condición sobre el ítem actual. |
| **CORS** | Mecanismo del navegador que exige permiso del servidor para llamadas entre dominios. |
| **Eventual consistency** | Las lecturas pueden no reflejar la última escritura por un breve tiempo. |
| **Geohash** | Codificación de lat/lng en texto; prefijo común = cercanía. |
| **GSI** | Global Secondary Index de DynamoDB. |
| **Health check** | Prueba periódica del balanceador para saber si un servidor está sano. |
| **IaC** | Infraestructura como código (SAM/CloudFormation). |
| **Idempotente** | Ejecutarlo varias veces produce el mismo resultado que una vez. |
| **Leaky bucket** | Algoritmo de rate limiting: un balde que se vacía a ritmo constante. |
| **Polling** | Preguntar periódicamente al servidor si hay cambios. |
| **Race condition** | Error por el orden imprevisible de operaciones concurrentes. |
| **Security group** | Firewall virtual de AWS a nivel de instancia/recurso. |
| **Stack** | Conjunto de recursos creado por una plantilla de CloudFormation. |
| **Stage** | Versión publicada de una API (aquí `prod`). |
| **Target group** | Grupo de destinos (instancias) al que el ALB envía tráfico. |
| **TTL** | Time To Live: DynamoDB borra el ítem cuando pasa la fecha del atributo. |
| **User data** | Script que una EC2 ejecuta al arrancar por primera vez. |
| **X-Forwarded-For** | Encabezado HTTP con la IP original del cliente cuando hay un proxy/balanceador. |

---

## 18. Comandos y datos de referencia

| Dato | Valor |
|---|---|
| Región | `us-east-1` |
| Perfil AWS CLI | `skyvery` (usuario IAM `skyvery-deployer`) |
| Stacks | `skyvery-prod` (SAM), `skyvery-ops` (EC2 + ALB) |
| WebSocket | `wss://3aeuxt4nid.execute-api.us-east-1.amazonaws.com/prod` |
| API REST | `https://qhbyd6ixm7.execute-api.us-east-1.amazonaws.com/prod` |
| Seguimiento (S3) | `http://skyvery-frontend-485530831247-prod.s3-website-us-east-1.amazonaws.com/index.html?orderId=…` |
| Panel (ALB) | `http://skyvery-ops-alb-996397180.us-east-1.elb.amazonaws.com/` |
| EC2 | `i-07196cd4fb4c12158` |

```bash
# Pruebas locales
python -m pytest -q tests

# Cliente que sigue una orden / repartidor simulado
python simulator/cliente_ws.py --url wss://3aeuxt4nid.execute-api.us-east-1.amazonaws.com/prod --order-id ORD-1
python simulator/driver_sim.py --url wss://3aeuxt4nid.execute-api.us-east-1.amazonaws.com/prod --order-id ORD-1 --driver-id DRV-1 --route pasto --stale-after 3

# API
curl -X POST https://qhbyd6ixm7.execute-api.us-east-1.amazonaws.com/prod/drivers -H "Content-Type: application/json" -d '{"driverId":"DRV-9"}'
curl https://qhbyd6ixm7.execute-api.us-east-1.amazonaws.com/prod/zones/centro/orders?status=IN_TRANSIT
curl https://qhbyd6ixm7.execute-api.us-east-1.amazonaws.com/prod/areas/d21nc/drivers

# Prueba de carga
python loadtest/carga.py http://skyvery-ops-alb-996397180.us-east-1.elb.amazonaws.com/ --rps 50 --segundos 10

# Borrar todo
aws cloudformation delete-stack --profile skyvery --stack-name skyvery-ops
aws s3 rm s3://skyvery-frontend-485530831247-prod --recursive --profile skyvery
sam delete --profile skyvery --stack-name skyvery-prod --no-prompts
```

Guion paso a paso de la presentación: [`docs/demo.md`](demo.md).

---

## 19. Quién hizo qué

| Integrante | Aporte |
|---|---|
| **Cristian** | Diseño del modelo de datos y plantilla SAM (tablas + GSI, SNS, S3, HTTP API); `complete_delivery` (transacción); `orders_api` (órdenes por zona, repartidores, repartidores por geohash); plantilla EC2 + ALB; despliegue en AWS; demo en vivo y evidencias; integración, revisión y coordinación. |
| **Carlos** | WebSocket API (3 rutas, integraciones, permisos, stage); `ws_connect` y `ws_disconnect`; pruebas; documentación del WebSocket. |
| **Oskar** | `location_update` (conditional write, geohash, broadcast filtrado, limpieza 410); simulador del repartidor; pruebas unitarias y de integración. |
| **Sebas** | Configuración NGINX (rate limiting, IP real, health check, 429 en JSON); user-data; página de seguimiento con Leaflet; panel de operaciones; script de prueba de carga. |

**Consejo para la sustentación:** cada integrante debe poder explicar **todo** el flujo (sección 3), no solo su parte. Repasen especialmente las preguntas 12, 19, 26, 30, 36 y 49: son las que más suelen preguntar.
