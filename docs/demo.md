# Guion de la demo — Skyvery

Duración sugerida: 12–15 minutos. Cada paso dice **qué se muestra**, **cómo** y **qué requisito cumple**.

## 0. Preparación (antes de presentar)

1. Verificar que los dos stacks existen (si se borraron, ver "Recrear todo" al final):
   ```bash
   aws cloudformation describe-stacks --profile skyvery --query "Stacks[?starts_with(StackName,'skyvery')].[StackName,StackStatus]" --output table
   ```
2. Abrir en pestañas:
   - Panel de operaciones (ALB → EC2): http://skyvery-ops-alb-996397180.us-east-1.elb.amazonaws.com/
   - Consola AWS: CloudFormation, API Gateway (`skyvery-ws-prod`), DynamoDB, CloudWatch Logs, EC2 → Target groups.
   - Gmail (cj29772@gmail.com).
3. Abrir **tres terminales** en la carpeta del repo y definir la URL del WebSocket (PowerShell):
   ```powershell
   $env:WS_URL = "wss://3aeuxt4nid.execute-api.us-east-1.amazonaws.com/prod"
   ```
4. Instalar dependencias si es otro computador: `pip install websockets`.

## 1. Arquitectura (1–2 min)

Mostrar el diagrama del README (sección 2) y explicar el flujo:
repartidor → WebSocket `location-update` → Lambda → DynamoDB (conditional write) → broadcast solo a quienes siguen la orden → entrega con transacción → SNS. Panel en EC2 + NGINX detrás de ALB.

## 2. Infraestructura desplegada (1 min)

- CloudFormation → `skyvery-prod` (serverless) y `skyvery-ops` (EC2 + ALB) → pestaña **Salidas**.
- API Gateway → `skyvery-ws-prod` → **Rutas**: `$connect`, `$disconnect`, `location-update`.
  *Requisito: WebSocket con tres rutas.*
- DynamoDB → `skyvery-subscriptions-prod` → **Índices**: `orderId-index`.
  *Requisito: tabla de suscripciones {orderId, connectionId} con GSI por orderId.*

## 3. Crear datos desde el panel (2 min)

En el panel (servido por NGINX en EC2 a través del ALB):
1. **Nuevo repartidor**: `DRV-DEMO`, nombre "Repartidor demo" → aparece con 0 entregas.
2. **Nueva orden**: zona `centro`, cliente "Profe", repartidor `DRV-DEMO`, ID `ORD-DEMO` → queda "En camino".
3. **Nueva orden**: zona `centro`, repartidor `DRV-1`, ID `ORD-OTRA`.
4. Consultar zona `centro` y filtrar por estado.
   *Requisito: GSI por zona geográfica (`zone-status-index`).*

## 4. Seguimiento en tiempo real + broadcast filtrado (3 min)

1. En la tabla del panel, clic en **Ver mapa** de `ORD-DEMO` (abre la página de S3).
2. Terminal 1 — cliente que sigue **otra** orden:
   ```powershell
   python simulator/cliente_ws.py --url $env:WS_URL --order-id ORD-OTRA
   ```
3. Terminal 2 — cliente que sigue `ORD-DEMO`:
   ```powershell
   python simulator/cliente_ws.py --url $env:WS_URL --order-id ORD-DEMO
   ```
4. Mostrar DynamoDB → `skyvery-subscriptions-prod` → Explorar elementos: hay una fila por conexión con su `orderId`.
5. Terminal 3 — repartidor (envía cada 5 s y mete un mensaje viejo después del 3.º punto):
   ```powershell
   python simulator/driver_sim.py --url $env:WS_URL --order-id ORD-DEMO --driver-id DRV-DEMO --route pasto --stale-after 3
   ```
6. Mostrar: el mapa se mueve cada 5 s; la terminal 2 imprime cada ubicación; **la terminal 1 no recibe nada**.
   *Requisito: broadcast solo a los clientes que siguen esa orden (Query al GSI `orderId-index`).*

## 5. Conditional write: ubicación vieja descartada (2 min)

1. En la salida del simulador, señalar la línea `VIEJO (debe descartarse)`.
2. Terminal 2 y mapa: ese punto **no** llegó (el repartidor no "salta hacia atrás").
3. CloudWatch → Log groups → `/aws/lambda/skyvery-prod-LocationUpdateFunction-...` → buscar `"ignored"`:
   `{"status": "ignored", ..., "reason": "stale"}`.
4. DynamoDB → `skyvery-driver-locations-prod` → item `DRV-DEMO`: `ts` es el más nuevo.
   *Requisito: conditional write `attribute_not_exists(ts) OR ts < :newTs`; si falla se descarta en silencio (200, sin error).*

## 6. Transacción atómica + SNS (3 min)

1. **Caso fallido**: en el panel, orden `ORD-OTRA` → cambiar repartidor no es posible desde la tabla, así que usar la API:
   ```powershell
   curl.exe -X POST https://qhbyd6ixm7.execute-api.us-east-1.amazonaws.com/prod/orders/ORD-OTRA/complete -H "Content-Type: application/json" -d '{\"driverId\":\"NO-EXISTE\"}'
   ```
   → **409** "Transacción cancelada, nada se guardó". La orden sigue "En camino" y no hay historial.
2. **Caso exitoso**: en el panel, **Marcar entregada** en `ORD-DEMO`.
   - La orden pasa a "Entregado".
   - Tabla de repartidores: `DRV-DEMO` pasa de 0 a **1** entrega.
   - DynamoDB → `skyvery-delivery-history-prod`: nuevo registro.
   - La página de seguimiento muestra **"Pedido entregado ✓"** (consulta la API cada 10 s).
   - Gmail: llega el correo **"Skyvery: pedido ORD-DEMO entregado"**.
3. Intentar entregar de nuevo `ORD-DEMO` (API) → 409; el contador sigue en 1.
   *Requisito: TransactWriteItems — orden DELIVERED + historial + contador; si una falla, ninguna persiste. SNS envía confirmación.*

## 7. Rate limiting en NGINX (2 min)

1. EC2 → Target groups → `skyvery-ops-tg` → target **healthy** (health check `/health`).
2. Prueba de carga (50 req/s durante 10 s):
   ```powershell
   python loadtest/carga.py http://skyvery-ops-alb-996397180.us-east-1.elb.amazonaws.com/ --rps 50 --segundos 10
   ```
   Resultado esperado ≈ 120 respuestas 200 (burst 20 + 10/s × 10 s) y el resto **429**.
3. Logs de NGINX (sin SSH, con SSM):
   ```powershell
   aws ssm start-session --profile skyvery --target i-07196cd4fb4c12158
   sudo grep " 429 " /var/log/nginx/access.log | tail
   sudo grep limiting /var/log/nginx/error.log | tail -3
   ```
   (Requiere el plugin de Session Manager; alternativa: EC2 → Conectar → Session Manager en la consola.)
   *Requisito: limit_req_zone 10 r/s por IP, burst 20, 429 visibles en logs.*

## 8. Cierre (1 min)

Resumen de los 4 requisitos cumplidos + pruebas automáticas (`python -m pytest -q tests`, 49 pruebas) + evidencias en `docs/evidencias/`.

---

## Recrear todo (si se borró)

```bash
sam build -t infra/template.yaml
sam deploy --profile skyvery --stack-name skyvery-prod --region us-east-1 --resolve-s3 --capabilities CAPABILITY_IAM --no-confirm-changeset --parameter-overrides Stage=prod NotificationEmail=cj29772@gmail.com
aws cloudformation deploy --profile skyvery --region us-east-1 --stack-name skyvery-ops --template-file infra/ec2-alb.yaml --capabilities CAPABILITY_IAM --parameter-overrides VpcId=vpc-0b9cbefe567b66acc SubnetIds=subnet-05069bac24b4d5cd8,subnet-085d79d7a661c96b8
```

Al recrear, las URL cambian: actualizar `WS_URL`/`API_URL` en `frontend/tracking-client/index.html`, `API_URL`/`TRACKING_URL` en `frontend/ops-panel/index.html`, subir la página a S3 (`aws s3 cp frontend/tracking-client/index.html s3://skyvery-frontend-485530831247-prod/index.html --profile skyvery`) y confirmar de nuevo el correo SNS.

## Borrar todo (después de la presentación)

```bash
aws cloudformation delete-stack --profile skyvery --stack-name skyvery-ops
aws s3 rm s3://skyvery-frontend-485530831247-prod --recursive --profile skyvery
sam delete --profile skyvery --stack-name skyvery-prod --no-prompts
```
