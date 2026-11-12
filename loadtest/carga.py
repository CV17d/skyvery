#!/usr/bin/env python3
"""Prueba de carga contra el ALB de Skyvery (solo librería estándar de Python).

Envía peticiones a un ritmo constante y cuenta los códigos de respuesta.
Con el límite de NGINX (10 req/s, burst 20) todo lo que pase de ahí debe
volver como 429.

Uso:
    python loadtest/carga.py http://<DNS-del-ALB>/
    python loadtest/carga.py http://<DNS-del-ALB>/ --rps 50 --segundos 20

El resumen se imprime y se guarda en docs/evidencias/.
"""
import argparse
import threading
import time
import urllib.error
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

EVIDENCIAS = Path(__file__).resolve().parent.parent / "docs" / "evidencias"

conteo = Counter()
candado = threading.Lock()


def pedir(url, timeout):
    """Hace una petición GET y devuelve el código HTTP (o 'ERROR')."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            r.read()
            codigo = r.status
    except urllib.error.HTTPError as e:      # 4xx / 5xx (aquí llega el 429)
        codigo = e.code
    except Exception:                        # timeout, conexión rechazada...
        codigo = "ERROR"
    with candado:
        conteo[codigo] += 1


def main():
    ap = argparse.ArgumentParser(description="Prueba de carga a ritmo constante")
    ap.add_argument("url", help="URL del ALB, p. ej. http://skyvery-alb-123.us-east-1.elb.amazonaws.com/")
    ap.add_argument("--rps", type=int, default=50, help="peticiones por segundo (por defecto 50)")
    ap.add_argument("--segundos", type=int, default=10, help="duración de la prueba (por defecto 10)")
    ap.add_argument("--timeout", type=float, default=5, help="timeout por petición en segundos")
    args = ap.parse_args()

    total = args.rps * args.segundos
    intervalo = 1 / args.rps
    print(f"Enviando {total} peticiones a {args.url} ({args.rps} req/s durante {args.segundos} s)...")

    inicio = time.perf_counter()
    with ThreadPoolExecutor(max_workers=min(200, args.rps * 4)) as pool:
        for i in range(total):
            # Espera hasta el instante exacto que le toca a la petición i
            espera = inicio + i * intervalo - time.perf_counter()
            if espera > 0:
                time.sleep(espera)
            pool.submit(pedir, args.url, args.timeout)
    duracion = time.perf_counter() - inicio

    ok = conteo.get(200, 0)
    limitadas = conteo.get(429, 0)
    lineas = [
        "Skyvery - prueba de carga (rate limiting NGINX: 10 req/s, burst 20)",
        f"Fecha:        {datetime.now():%Y-%m-%d %H:%M:%S}",
        f"URL:          {args.url}",
        f"Ritmo:        {args.rps} req/s durante {args.segundos} s",
        f"Enviadas:     {total} en {duracion:.1f} s ({total / duracion:.1f} req/s reales)",
        "",
        "Respuestas por código:",
    ]
    for codigo, n in sorted(conteo.items(), key=lambda par: str(par[0])):
        lineas.append(f"  {codigo}: {n:5d}  ({100 * n / total:.1f} %)")
    lineas += [
        "",
        f"Aceptadas (200):  {ok}",
        f"Limitadas (429):  {limitadas}",
        f"Esperado aprox.:  {20 + 10 * args.segundos} aceptadas (burst 20 + 10 req/s x {args.segundos} s)",
    ]
    resumen = "\n".join(lineas)
    print("\n" + resumen)

    EVIDENCIAS.mkdir(parents=True, exist_ok=True)
    archivo = EVIDENCIAS / f"carga-{datetime.now():%Y%m%d-%H%M%S}.txt"
    archivo.write_text(resumen + "\n", encoding="utf-8")
    print(f"\nResumen guardado en {archivo}")

    if limitadas == 0:
        print("\nAVISO: no hubo ningún 429. Revisa que la prueba apunte al ALB y que "
              "nginx.conf esté cargado (sudo nginx -T | grep limit_req).")


if __name__ == "__main__":
    main()
