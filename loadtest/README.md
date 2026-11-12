# Prueba de carga (rate limiting de NGINX)

Objetivo: demostrar que NGINX limita a **10 req/s por IP con burst de 20** y
responde **429** al resto. La prueba va **contra el ALB**, no contra la EC2.

## Opción A — script incluido (no requiere instalar nada, solo Python 3)

```bash
python loadtest/carga.py http://<DNS-del-ALB>/                      # 50 req/s durante 10 s
python loadtest/carga.py http://<DNS-del-ALB>/ --rps 50 --segundos 20
```

Imprime el resumen y lo guarda en `docs/evidencias/carga-<fecha>.txt`.

Resultado esperado con 50 req/s durante 10 s (500 peticiones):

| Código | Cantidad aprox. | Por qué |
|---|---|---|
| 200 | ~120 | 20 del burst + 10 por segundo × 10 s |
| 429 | ~380 | todo lo que supera el límite |

## Opción B — herramientas conocidas

```bash
hey -z 10s -q 50 -c 1 http://<DNS-del-ALB>/        # hey: 50 req/s durante 10 s
ab -n 500 -c 20 http://<DNS-del-ALB>/              # ApacheBench: 500 peticiones
```

## Evidencia de los 429 en el log de NGINX

En la EC2, por SSH, justo después de la prueba:

```bash
grep " 429 " /var/log/nginx/access.log | tail -n 20
grep -c " 429 " /var/log/nginx/access.log          # total de peticiones limitadas
```

Cada línea muestra la IP real del cliente (no la del ALB), el código 429 y
`limit=REJECTED`. Guardar la salida en `docs/evidencias/nginx-429.txt`:

```bash
grep " 429 " /var/log/nginx/access.log > nginx-429.txt
```

## Qué guardar en `docs/evidencias/`

- [ ] `carga-<fecha>.txt` (resumen de la prueba)
- [ ] `nginx-429.txt` (líneas del log con 429)
- [ ] Captura del target group con la instancia **healthy**
- [ ] Captura del panel cargando desde la URL del ALB
