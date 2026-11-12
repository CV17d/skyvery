# EC2 + NGINX + ALB (panel de operaciones)

Responsable: Sebas. Aquí está el paso a paso para montar la fase 6.

```
Operador ──► ALB (puerto 80) ──► EC2 (NGINX, 10 req/s por IP, burst 20) ──► ops-panel
```

| Archivo | Para qué |
|---|---|
| `nginx.conf` | Configuración completa de NGINX: rate limiting, IP real del cliente, `/health`, logs |
| `user-data.sh` | Script de arranque de la EC2: instala NGINX y publica el panel |

## 0. Antes de empezar

`user-data.sh` descarga los archivos desde GitHub, así que la rama indicada en
`BRANCH` (dentro del script) debe tener ya el `nginx.conf` y el `ops-panel`
terminados. Mientras el PR no esté en `main`, cambiar `BRANCH="main"` por
`BRANCH="sebastian"` al pegar el script.

Todo se crea en la misma región y VPC (sirve la VPC por defecto).

## 1. Security groups

| Nombre | Regla de entrada | Origen |
|---|---|---|
| `skyvery-alb-sg` | HTTP 80 | `0.0.0.0/0` |
| `skyvery-ec2-sg` | HTTP 80 | el grupo `skyvery-alb-sg` |
| `skyvery-ec2-sg` | SSH 22 | solo mi IP (para revisar logs) |

La EC2 solo acepta tráfico web que venga del ALB; nadie puede saltarse el balanceador.

## 2. Lanzar la EC2

EC2 > Launch instance:

- Nombre: `skyvery-ops-panel`
- AMI: **Amazon Linux 2023**
- Tipo: `t2.micro` o `t3.micro`
- Key pair: una propia (el `.pem` **no** se sube al repo)
- Network: security group `skyvery-ec2-sg`, IP pública habilitada
- Advanced details > **User data**: pegar el contenido de `user-data.sh`

Comprobar después de 1–2 minutos, entrando por SSH:

```bash
cat /var/log/skyvery-user-data.log     # debe terminar con "health: 200"
sudo nginx -T | grep limit_req         # debe mostrar la zona perip y el burst
```

## 3. Target group

EC2 > Target groups > Create:

- Tipo: **Instances**, protocolo HTTP, puerto 80, misma VPC
- Health check: HTTP, ruta **`/health`**
- Registrar la instancia `skyvery-ops-panel`

## 4. ALB

EC2 > Load balancers > Create > **Application Load Balancer**:

- Nombre: `skyvery-alb`, esquema **Internet-facing**, IPv4
- Subredes: mínimo **2 zonas de disponibilidad**
- Security group: `skyvery-alb-sg`
- Listener HTTP 80 → reenviar al target group

Cuando el target aparezca **healthy**, abrir `http://<DNS-del-ALB>/` y debe cargar el panel.

## 5. Configurar el panel

En el panel, botón **Configuración**: pegar `OrdersApiUrl` y `FrontendUrl`
(salidas de `sam deploy`). También se pueden dejar fijas en las constantes
`API_URL` y `TRACKING_URL` al inicio de `frontend/ops-panel/index.html`.

## 6. Repartidores para la demo (requisito de "Marcar entregada")

El botón **Marcar entregada** llama a `POST /orders/{orderId}/complete`. Esa
transacción exige que el repartidor **ya exista** en la tabla `Drivers`; si no
existe, responde **409** y no se guarda nada.

Los repartidores se crean con la API de órdenes (`OrdersApiUrl` del stack):

```bash
curl -X POST "$API_URL/drivers" -H "Content-Type: application/json" -d '{"driverId":"driver-1","name":"Repartidor 1"}'
curl "$API_URL/drivers"            # listar
curl "$API_URL/drivers/driver-1"   # ver deliveriesCount (sube tras cada entrega)
```

`POST /drivers` crea el repartidor con `deliveriesCount = 0` y responde 409 si
ya existe. Mejora opcional del panel: una sección para crear/listar
repartidores y una lista desplegable en lugar del campo de texto.

## 7. Publicar la página de seguimiento en S3

El bucket lo crea la plantilla SAM (`skyvery-frontend-<cuenta>-prod`).
Antes de subir, poner la URL del WebSocket en la constante `WS_URL` de
`frontend/tracking-client/index.html`.

```bash
aws s3 cp frontend/tracking-client/index.html s3://skyvery-frontend-<cuenta>-prod/index.html
```

Abrir `<FrontendUrl>/index.html?orderId=<id>`.
Para probar el mapa sin backend: `index.html?orderId=prueba&demo=1`.

## 8. Cómo funciona el rate limiting (para la sustentación)

- `limit_req_zone $binary_remote_addr zone=perip:10m rate=10r/s;`
  lleva la cuenta por IP: 10 peticiones por segundo.
- `limit_req zone=perip burst=20 nodelay;`
  admite una ráfaga de 20 peticiones extra y las atiende de inmediato.
- `limit_req_status 429;` lo que pase de ahí recibe **429 Too Many Requests**.
- `real_ip_header X-Forwarded-For` + `set_real_ip_from <red privada>`:
  detrás del ALB todas las peticiones llegan con la IP del balanceador. Con
  esto NGINX toma la IP real del cliente que el ALB escribe en la cabecera
  `X-Forwarded-For`; sin esto, todos los usuarios compartirían un solo límite.
- `/health` no tiene límite, para que el ALB nunca marque la instancia como caída
  durante la prueba de carga.

## 9. Actualizar después de un cambio

```bash
cd /opt/skyvery && sudo git pull
sudo cp ec2-nginx/nginx.conf /etc/nginx/nginx.conf
sudo cp -r frontend/ops-panel/. /usr/share/nginx/html/ops-panel/
sudo nginx -t && sudo systemctl reload nginx
```
