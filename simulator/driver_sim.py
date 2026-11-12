"""Repartidor simulado: recorre una ruta y envía su GPS cada 5 s por WebSocket.

Mensaje enviado (ruta location-update):
  {"action": "location-update", "orderId", "driverId", "lat", "lng", "ts"}

Uso:
  pip install websockets
  python simulator/driver_sim.py --url wss://<api>.execute-api.<region>.amazonaws.com/prod \
      --order-id ORD-1 --driver-id DRV-1

  # Demostrar el descarte: después del paso 3 envía un mensaje con ts viejo
  python simulator/driver_sim.py --url ... --order-id ORD-1 --driver-id DRV-1 --stale-after 3

  # Ver los mensajes sin conectarse a AWS
  python simulator/driver_sim.py --order-id ORD-1 --driver-id DRV-1 --dry-run --interval 0

La URL también se puede pasar con la variable de entorno WS_URL. El simulador
se conecta con ?role=driver (contrato de ws_connect: el repartidor no se
suscribe a ninguna orden).
"""
import argparse
import json
import os
import sys
import time
from urllib.parse import urlencode, urlparse, parse_qsl, urlunparse

# Ruta de ejemplo en Bogotá (Parque de la 93 -> Unicentro), puntos de paso.
DEFAULT_ROUTE = [
    (4.67680, -74.04830),
    (4.67950, -74.04700),
    (4.68320, -74.04560),
    (4.68730, -74.04480),
    (4.69150, -74.04390),
    (4.69580, -74.04300),
    (4.70020, -74.04230),
    (4.70210, -74.04160),
]


def interpolate(route, steps_per_segment):
    """Agrega puntos intermedios entre cada par de puntos de la ruta."""
    if steps_per_segment < 1:
        raise ValueError("steps_per_segment debe ser >= 1")
    points = []
    for (lat1, lng1), (lat2, lng2) in zip(route, route[1:]):
        for i in range(steps_per_segment):
            f = i / steps_per_segment
            points.append((round(lat1 + (lat2 - lat1) * f, 6), round(lng1 + (lng2 - lng1) * f, 6)))
    points.append(route[-1])
    return points


def location_message(order_id, driver_id, lat, lng, ts):
    return {"action": "location-update", "orderId": order_id, "driverId": driver_id,
            "lat": lat, "lng": lng, "ts": ts}


def build_plan(points, stale_after=None):
    """Lista de (lat, lng, es_viejo). Si stale_after=N, tras el N-ésimo punto se
    agrega un envío con timestamp viejo, que la Lambda debe descartar."""
    plan = []
    for i, (lat, lng) in enumerate(points, start=1):
        plan.append((lat, lng, False))
        if stale_after == i:
            plan.append((points[0][0], points[0][1], True))
    return plan


def driver_url(url):
    """Agrega role=driver al query string de la URL del WebSocket."""
    parts = urlparse(url)
    query = dict(parse_qsl(parts.query))
    query["role"] = "driver"
    return urlunparse(parts._replace(query=urlencode(query)))


def run(ws, plan, order_id, driver_id, interval, stale_age=60,
        clock=time.time, sleep=time.sleep, log=print):
    """Envía el plan por la conexión `ws` (cualquier objeto con .send).

    El ts (epoch en segundos) se toma al momento de enviar y es estrictamente
    creciente; el envío "viejo" usa (último ts) - stale_age."""
    last_ts = 0
    for n, (lat, lng, stale) in enumerate(plan, start=1):
        if n > 1 and not stale:
            sleep(interval)
        if stale:
            ts = last_ts - stale_age
        else:
            ts = max(int(clock()), last_ts + 1)
            last_ts = ts
        msg = location_message(order_id, driver_id, lat, lng, ts)
        ws.send(json.dumps(msg))
        tag = "VIEJO (debe descartarse)" if stale else "ok"
        log(f"[{n}/{len(plan)}] ts={ts} lat={lat} lng={lng} {tag}")


class _DryRun:
    def send(self, data):
        print(f"  -> {data}")


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="Simulador de repartidor Skyvery")
    p.add_argument("--url", default=os.environ.get("WS_URL"), help="wss://... (o env WS_URL)")
    p.add_argument("--order-id", required=True)
    p.add_argument("--driver-id", required=True)
    p.add_argument("--interval", type=float, default=5.0, help="segundos entre mensajes (default 5)")
    p.add_argument("--steps", type=int, default=3, help="puntos intermedios por tramo de la ruta")
    p.add_argument("--stale-after", type=int, default=None,
                   help="tras el punto N envía un mensaje con timestamp viejo")
    p.add_argument("--stale-age", type=int, default=60, help="segundos de antigüedad del mensaje viejo")
    p.add_argument("--dry-run", action="store_true", help="imprime los mensajes sin conectarse")
    args = p.parse_args(argv)
    if not args.dry_run and not args.url:
        p.error("falta --url (o la variable de entorno WS_URL)")
    return args


def main(argv=None):
    args = parse_args(argv)
    points = interpolate(DEFAULT_ROUTE, args.steps)
    plan = build_plan(points, args.stale_after)

    if args.dry_run:
        run(_DryRun(), plan, args.order_id, args.driver_id, args.interval, args.stale_age)
        return 0

    try:
        from websockets.sync.client import connect
    except ImportError:
        print("Falta la librería websockets: pip install websockets", file=sys.stderr)
        return 1

    url = driver_url(args.url)
    print(f"Conectando a {url} ...")
    with connect(url) as ws:
        run(ws, plan, args.order_id, args.driver_id, args.interval, args.stale_age)
    print("Ruta completada.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
