#!/bin/bash
# deploy/certbot-renew-hook.sh
# Install to: /etc/letsencrypt/renewal-hooks/post/restart-calculator.sh
# This script is called by certbot after successful certificate renewal.
docker restart magma-calculator
