# Cómo encontrar Skyvery en la consola de AWS

**Antes de empezar:** inicia sesión con el usuario `skyvery-deployer` en
https://485530831247.signin.aws.amazon.com/console y, arriba a la derecha, elige la región
**EE. UU. Este (Norte de Virginia) · us-east-1**. Si la región es otra, no vas a ver nada.

Todos los recursos llevan el prefijo `skyvery`. Escribe `skyvery` en el buscador de cada servicio.

---

## Enlaces para probar el programa

| Qué | URL |
|---|---|
| Panel de operaciones (ALB → EC2 con NGINX) | http://skyvery-ops-alb-1544769596.us-east-1.elb.amazonaws.com/ |
| Health check del panel | http://skyvery-ops-alb-1544769596.us-east-1.elb.amazonaws.com/health |
| Página de seguimiento del cliente (S3) | http://skyvery-frontend-485530831247-prod.s3-website-us-east-1.amazonaws.com/index.html?orderId=ORD-VIVO |
| API REST (ejemplo: lista de repartidores) | https://qhbyd6ixm7.execute-api.us-east-1.amazonaws.com/prod/drivers |
| API REST (órdenes de la zona centro) | https://qhbyd6ixm7.execute-api.us-east-1.amazonaws.com/prod/zones/centro/orders |
| WebSocket (para el simulador y el cliente de consola) | `wss://3aeuxt4nid.execute-api.us-east-1.amazonaws.com/prod` |

Para ver al repartidor moverse, abre la página de seguimiento y en una terminal, en la carpeta del repo:

```bash
python simulator/driver_sim.py --url wss://3aeuxt4nid.execute-api.us-east-1.amazonaws.com/prod --order-id ORD-VIVO --driver-id DRV-1 --route pasto
```

---

## 1. CloudFormation: todo el proyecto de un vistazo

Consola: https://us-east-1.console.aws.amazon.com/cloudformation/home?region=us-east-1#/stacks

| Stack | Qué contiene |
|---|---|
| `skyvery-prod` | Todo lo serverless: 2 APIs, 5 Lambdas, 5 tablas, SNS, S3 y roles IAM |
| `skyvery-ops` | EC2, balanceador (ALB), target group, security groups y rol de la EC2 |

Dentro de cada stack:
- Pestaña **Recursos**: la lista completa con enlaces directos a cada recurso.
- Pestaña **Salidas**: las URL del WebSocket, la API, S3 y el panel.
- Pestaña **Plantilla**: el YAML con el que se creó.

---

## 2. API Gateway: WebSocket y API REST

Consola: https://us-east-1.console.aws.amazon.com/apigateway/main/apis?region=us-east-1

| API | ID | Tipo |
|---|---|---|
| `skyvery-ws-prod` | `3aeuxt4nid` | WebSocket |
| `skyvery-prod` | `qhbyd6ixm7` | HTTP |

**WebSocket (`skyvery-ws-prod`):**
- **Rutas** (menú izquierdo): `$connect`, `$disconnect` y `location-update`. Clic en cada una muestra la Lambda que invoca.
- **Configuración de la API**: la *expresión de selección de rutas* `$request.body.action`.
- **Etapas → prod**: la URL `wss://…/prod`.

Enlace directo a las rutas: https://us-east-1.console.aws.amazon.com/apigateway/main/develop/routes?api=3aeuxt4nid&region=us-east-1

**HTTP API (`qhbyd6ixm7`):**
- **Rutas**: `/orders`, `/orders/{orderId}`, `/orders/{orderId}/complete`, `/zones/{zone}/orders`, `/drivers`, `/drivers/{driverId}`, `/areas/{geohashPrefix}/drivers`.
- **CORS**: la configuración que permite al panel llamar la API.

Enlace directo: https://us-east-1.console.aws.amazon.com/apigateway/main/develop/routes?api=qhbyd6ixm7&region=us-east-1

---

## 3. Lambda: las cinco funciones

Consola: https://us-east-1.console.aws.amazon.com/lambda/home?region=us-east-1#/functions?fo=and&o0=%3A&v0=skyvery

| Función (empieza por `skyvery-prod-`) | Qué hace |
|---|---|
| `WsConnectFunction-…` | Ruta `$connect`: guarda la suscripción |
| `WsDisconnectFunction-…` | Ruta `$disconnect`: borra la suscripción |
| `LocationUpdateFunction-…` | Ruta `location-update`: escritura condicional y broadcast filtrado |
| `CompleteDeliveryFunction-…` | Transacción de entrega y publicación en SNS |
| `OrdersApiFunction-…` | API REST de órdenes, repartidores y consultas por zona |

En cada función:
- Pestaña **Código**: el código Python.
- Pestaña **Configuración → Variables de entorno**: los nombres de tablas y el tema SNS.
- Pestaña **Configuración → Permisos**: el rol IAM con acceso solo a sus tablas.
- Pestaña **Monitorear → Ver registros de CloudWatch**: los logs.

---

## 4. DynamoDB: tablas e índices

Consola: https://us-east-1.console.aws.amazon.com/dynamodbv2/home?region=us-east-1#tables

| Tabla | Índice (pestaña **Índices**) |
|---|---|
| `skyvery-subscriptions-prod` | `orderId-index` |
| `skyvery-orders-prod` | `zone-status-index` |
| `skyvery-driver-locations-prod` | `geohash-index` |
| `skyvery-delivery-history-prod` | — |
| `skyvery-drivers-prod` | — |

- Para ver los datos: clic en la tabla → **Explorar elementos de la tabla**.
- Para ver el GSI: clic en la tabla → pestaña **Índices**.
- Para consultar por el GSI: **Explorar elementos** → **Consultar** → elige el índice en el desplegable.
- `skyvery-subscriptions-prod` estará vacía salvo cuando haya clientes conectados (abre la página de seguimiento y actualiza la tabla para verla aparecer).

---

## 5. CloudWatch: logs de las Lambdas

Consola: https://us-east-1.console.aws.amazon.com/cloudwatch/home?region=us-east-1#logsV2:log-groups$3FlogGroupNameFilter$3Dskyvery

- Grupo `/aws/lambda/skyvery-prod-LocationUpdateFunction-…`: cada ubicación registra `{"status": "ok", …}` o `{"status": "ignored", "reason": "stale"}`.
- Para ver el descarte: abre el grupo → **Buscar en todo** → escribe `ignored`.

---

## 6. S3: página de seguimiento

Consola: https://s3.console.aws.amazon.com/s3/buckets/skyvery-frontend-485530831247-prod?region=us-east-1

- Pestaña **Objetos**: `index.html`.
- Pestaña **Propiedades** → al final, **Alojamiento de sitios web estáticos**: la URL pública.
- Pestaña **Permisos**: la política que permite leer la página.

---

## 7. SNS: notificación de entrega

Consola: https://us-east-1.console.aws.amazon.com/sns/v3/home?region=us-east-1#/topics

- Tema `skyvery-delivery-completed-prod`.
- Pestaña **Suscripciones**: el correo `cj29772@gmail.com` en estado **Confirmada**.
- Botón **Publicar mensaje** para enviar una prueba manual.

---

## 8. EC2: servidor del panel

Instancias: https://us-east-1.console.aws.amazon.com/ec2/home?region=us-east-1#Instances:search=skyvery

- Instancia `skyvery-ops-panel` (`i-0aac583a57e796cea`), estado **En ejecución**.
- Pestaña **Seguridad**: el security group que solo acepta tráfico del balanceador; sin puerto 22.
- **Conectar → Session Manager → Conectar**: abre una terminal en la instancia sin SSH. Comandos útiles:
  ```bash
  sudo grep -n limit_req /etc/nginx/nginx.conf
  sudo tail -n 20 /var/log/nginx/access.log
  sudo grep " 429 " /var/log/nginx/access.log | tail
  sudo grep limiting /var/log/nginx/error.log | tail -n 5
  ```

---

## 9. Balanceador (ALB) y target group

Balanceadores: https://us-east-1.console.aws.amazon.com/ec2/home?region=us-east-1#LoadBalancers:search=skyvery

- `skyvery-ops-alb`: su **Nombre de DNS** es la URL del panel.
- Pestaña **Agentes de escucha**: HTTP:80 → `skyvery-ops-tg`.

Target groups: https://us-east-1.console.aws.amazon.com/ec2/home?region=us-east-1#TargetGroups:search=skyvery

- `skyvery-ops-tg` → pestaña **Destinos**: la instancia en estado **healthy** (en buen estado).
- Pestaña **Comprobaciones de estado**: ruta `/health`.

---

## 10. IAM: roles y usuario

Roles: https://us-east-1.console.aws.amazon.com/iam/home#/roles?search=skyvery

- Un rol por Lambda (`skyvery-prod-…FunctionRole-…`) con permisos solo sobre sus tablas.
- `skyvery-ops-InstanceRole-…`: el rol de la EC2 para Systems Manager.

Usuarios: https://us-east-1.console.aws.amazon.com/iam/home#/users → `skyvery-deployer`.

---

## Apagar después de revisar

La EC2 y el ALB cobran por hora. Para borrarlos (lo serverless se queda):

```bash
aws cloudformation delete-stack --profile skyvery --stack-name skyvery-ops
```

Al recrearlos, la URL del panel cambia.
