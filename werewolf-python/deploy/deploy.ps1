# Deploy werewolf-python + NapCat to 8.218.5.34 (boss-vlinks)
# Usage: .\deploy\deploy.ps1
$ErrorActionPreference = "Stop"
$HostName = if ($args[0]) { $args[0] } else { "boss-vlinks" }
$RemoteRoot = "/opt/werewolf-python"
$Root = Split-Path -Parent $PSScriptRoot
$Tar = Join-Path $env:TEMP "werewolf-python.tgz"

Write-Host "[1/6] pack"
if (Test-Path $Tar) { Remove-Item $Tar -Force }
Push-Location $Root
try {
  tar -czf $Tar --exclude=.git --exclude=.venv --exclude=.pytest_cache --exclude=__pycache__ --exclude=.env --exclude=deploy/napcat --exclude=docs --exclude=tests --exclude=openspec --exclude=.agents --exclude=.codegraph --exclude=_probe.sh --exclude=_probe2.sh .
} finally {
  Pop-Location
}

Write-Host "[2/6] sync → ${HostName}:${RemoteRoot}"
ssh -o BatchMode=yes $HostName "mkdir -p ${RemoteRoot}"
scp -o BatchMode=yes $Tar "${HostName}:/tmp/werewolf-python.tgz"
scp -o BatchMode=yes "$Root\deploy\napcat\config\onebot11.json" "${HostName}:/tmp/werewolf-onebot11.json"
scp -o BatchMode=yes "$Root\deploy\setup-caddy.sh" "$Root\deploy\remote-up.sh" "${HostName}:/tmp/"
ssh -o BatchMode=yes $HostName "bash -lc 'set -euo pipefail; mkdir -p ${RemoteRoot}; tar -xzf /tmp/werewolf-python.tgz -C ${RemoteRoot}; mkdir -p ${RemoteRoot}/deploy/napcat/config ${RemoteRoot}/deploy/napcat/ntqq; cp /tmp/werewolf-onebot11.json ${RemoteRoot}/deploy/napcat/config/onebot11.json; chmod +x /tmp/setup-caddy.sh /tmp/remote-up.sh'"

Write-Host "[3/6] docker builder prune (disk)"
ssh -o BatchMode=yes $HostName "docker builder prune -f >/tmp/werewolf-prune.log; tail -5 /tmp/werewolf-prune.log; df -h / | tail -1"

Write-Host "[4/6] compose up"
ssh -o BatchMode=yes $HostName "bash /tmp/remote-up.sh"

Write-Host "[5/6] Caddy lrs.vlinks.vip"
ssh -o BatchMode=yes $HostName "bash /tmp/setup-caddy.sh"

Write-Host "[6/6] smoke"
ssh -o BatchMode=yes $HostName "bash -lc 'sleep 2; echo === local ===; curl -fsS http://127.0.0.1:18100/healthz; echo; echo === https ===; curl -sI --max-time 20 https://lrs.vlinks.vip/healthz | head -20; echo === dashboard ===; curl -sI --max-time 20 https://lrs.vlinks.vip/dashboard | head -20'"
Write-Host "Done → https://lrs.vlinks.vip/dashboard"
