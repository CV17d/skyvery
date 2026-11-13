"""Cliente WebSocket de consola: sigue una orden e imprime cada ubicación que llega.

Sirve para demostrar el broadcast filtrado: abre dos terminales siguiendo órdenes
distintas y ejecuta el simulador para una sola de ellas.

Uso:
  pip install websockets
  python simulator/cliente_ws.py --url wss://<api>.execute-api.us-east-1.amazonaws.com/prod --order-id ORD-1
"""
import argparse
import json
import os
import sys
from datetime import datetime


def main(argv=None):
    p = argparse.ArgumentParser(description="Cliente WebSocket de Skyvery")
    p.add_argument("--url", default=os.environ.get("WS_URL"), help="wss://... (o env WS_URL)")
    p.add_argument("--order-id", required=True)
    args = p.parse_args(argv)
    if not args.url:
        p.error("falta --url (o la variable de entorno WS_URL)")

    from websockets.sync.client import connect

    url = f"{args.url}{'&' if '?' in args.url else '?'}orderId={args.order_id}"
    print(f"Siguiendo la orden {args.order_id} en {args.url}  (Ctrl+C para salir)")
    try:
        with connect(url) as ws:
            for raw in ws:
                m = json.loads(raw)
                hora = datetime.fromtimestamp(float(m.get("ts", 0))).strftime("%H:%M:%S")
                print(f"[{hora}] {m.get('driverId')} -> lat={m.get('lat')} lng={m.get('lng')} "
                      f"geohash={m.get('geohash')}")
    except KeyboardInterrupt:
        print("\nDesconectado.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
