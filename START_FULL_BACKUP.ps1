$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$logDir = Join-Path $PSScriptRoot 'Custom_GPT_backup\run_logs'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$stamp = Get-Date -Format 'yyyyMMdd_HHmmss'
$logFile = Join-Path $logDir "direct_gpt_v11_$stamp.log"
Write-Host 'Requires already-authorized Chrome on a LOCAL CDP port. Stop on 401/403/429.'
& python -u .\scripts\download_direct_gpt_knowledge_ui_v11.py --unattended --attach --confirm-access-restored --pause-min 15 --pause-max 30 2>&1 | Tee-Object -FilePath $logFile
$code = $LASTEXITCODE
Write-Host "Exit code: $code. Private log: $logFile"
exit $code
