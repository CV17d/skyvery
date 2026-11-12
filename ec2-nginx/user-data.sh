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

dnf install -y nginx git
