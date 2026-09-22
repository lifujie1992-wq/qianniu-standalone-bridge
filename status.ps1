$config = Get-Content -Raw -Encoding UTF8 -LiteralPath (Join-Path $PSScriptRoot 'config.json') | ConvertFrom-Json
$uri = "http://127.0.0.1:$($config.api_port)/api/v1/status"
Invoke-RestMethod -Uri $uri -TimeoutSec 3 | ConvertTo-Json -Depth 8
