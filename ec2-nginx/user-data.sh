#!/bin/bash
# =============================================================================
# Skyvery - user-data de la EC2 del panel de operaciones (Amazon Linux 2023)
#
# Se pega en "Advanced details > User data" al lanzar la instancia.
# Instala NGINX, descarga el repositorio y publica:
#   ec2-nginx/nginx.conf        -> /etc/nginx/nginx.conf
#   frontend/ops-panel/         -> /usr/share/nginx/html/ops-panel/
#
# El log de este script queda en /var/log/skyvery-user-data.log
# =============================================================================
set -euxo pipefail
exec > >(tee /var/log/skyvery-user-data.log) 2>&1

# Rama desde la que se descargan los archivos. Mientras el PR no esté
# integrado en main, usar la rama de trabajo (sebastian).
REPO_URL="https://github.com/CV17d/skyvery.git"
BRANCH="main"

APP_DIR="/opt/skyvery"
WEB_DIR="/usr/share/nginx/html/ops-panel"

dnf install -y nginx git

rm -rf "$APP_DIR"
git clone --depth 1 --branch "$BRANCH" "$REPO_URL" "$APP_DIR"

# Panel de operaciones
mkdir -p "$WEB_DIR"
cp -r "$APP_DIR/frontend/ops-panel/." "$WEB_DIR/"
chown -R nginx:nginx "$WEB_DIR"

# Configuración de NGINX (se guarda copia de la original)
cp /etc/nginx/nginx.conf /etc/nginx/nginx.conf.original
cp "$APP_DIR/ec2-nginx/nginx.conf" /etc/nginx/nginx.conf

# Si la configuración es inválida el script se detiene aquí (set -e)
nginx -t

systemctl enable nginx
systemctl restart nginx

# Comprobación local
curl -s -o /dev/null -w "health: %{http_code}\n" http://localhost/health
