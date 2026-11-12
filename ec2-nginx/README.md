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
