#Requires -Version 7.2
[CmdletBinding()]
param(
    [string] $BaseUrl = 'http://localhost:8000',
    [string] $Message = 'I need a loan of 250,000 euros to buy a 160-square-metre house in Espoo, Finland. What is your offer?',
    [string] $ContextId,
    [string] $TaskId,
    [ValidateRange(1, 3600)]
    [int] $TimeoutSeconds = 300
)

$ErrorActionPreference = 'Stop'
$card = Invoke-RestMethod -Uri "$($BaseUrl.TrimEnd('/'))/.well-known/agent-card.json" `
    -TimeoutSec $TimeoutSeconds -MaximumRedirection 0
$interface = $card.supportedInterfaces | Where-Object {
    $_.protocolBinding -eq 'JSONRPC' -and $_.protocolVersion -eq '1.0'
} | Select-Object -First 1
if (-not $interface) { throw 'Agent card has no JSONRPC 1.0 interface.' }
$endpoint = [uri] $interface.url
$origin = [uri] $BaseUrl
if ($endpoint.Scheme -ne $origin.Scheme -or $endpoint.Authority -ne $origin.Authority) {
    throw 'Agent card points outside the anonymous proxy.'
}

function Invoke-A2A {
    param([string] $Method, [hashtable] $Parameters)
    $requestId = [guid]::NewGuid().ToString()
    if ($interface.tenant) { $Parameters.tenant = $interface.tenant }
    $payload = @{
        jsonrpc = '2.0'
        id = $requestId
        method = $Method
        params = $Parameters
    } | ConvertTo-Json -Depth 30 -Compress
    $response = Invoke-RestMethod -Uri $endpoint -Method Post `
        -Headers @{ 'A2A-Version' = '1.0' } -ContentType 'application/json; charset=utf-8' `
        -Body ([Text.Encoding]::UTF8.GetBytes($payload)) -TimeoutSec $TimeoutSeconds `
        -MaximumRedirection 0
    if ($response.jsonrpc -ne '2.0' -or $response.id -ne $requestId) {
        throw 'Invalid JSON-RPC response envelope.'
    }
    if ($response.error) { throw ($response.error | ConvertTo-Json -Depth 30) }
    if (-not $response.PSObject.Properties['result']) { throw 'Missing JSON-RPC result.' }
    return $response.result
}

$messageBody = @{
    messageId = [guid]::NewGuid().ToString()
    role = 'ROLE_USER'
    parts = @(@{ text = $Message })
}
if ($ContextId) { $messageBody.contextId = $ContextId }
if ($TaskId) { $messageBody.taskId = $TaskId }
$result = Invoke-A2A -Method SendMessage -Parameters @{
    message = $messageBody
    configuration = @{ returnImmediately = $false }
}
$result | ConvertTo-Json -Depth 100
if ($result.task.status.state -in @(
    'TASK_STATE_FAILED', 'TASK_STATE_REJECTED', 'TASK_STATE_CANCELED'
)) {
    throw "Agent task ended with $($result.task.status.state)."
}