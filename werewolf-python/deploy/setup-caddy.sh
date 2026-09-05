#!/usr/bin/env bash
set -euo pipefail
CF=/etc/caddy/Caddyfile
BLOCK='
# werewolf observe dashboard + NapCat WebUI
lrs.vlinks.vip {
	encode zstd gzip

	handle /webui* {
		reverse_proxy 127.0.0.1:18099
	}
	handle /api/observe* {
		reverse_proxy 127.0.0.1:18100
	}
	handle /ws/observe {
		reverse_proxy 127.0.0.1:18100
	}
	handle /api* {
		reverse_proxy 127.0.0.1:18099
	}

	redir / /dashboard 302
	reverse_proxy 127.0.0.1:18100
}
'
if grep -q 'lrs.vlinks.vip' "$CF"; then
  echo "Caddy already has lrs.vlinks.vip"
else
  cp "$CF" "${CF}.bak.$(date +%Y%m%d%H%M%S)"
  printf '%s\n' "$BLOCK" >> "$CF"
fi
caddy validate --config "$CF"
systemctl reload caddy
grep -n lrs.vlinks.vip "$CF"
