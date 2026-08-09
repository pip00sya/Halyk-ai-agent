param(
    [int]$Port = 8765,
    [switch]$NoBrowser
)

$ErrorActionPreference = "Stop"
$pythonCommand = Get-Command python -ErrorAction SilentlyContinue
if (-not $pythonCommand) {
    $pythonCommand = Get-Command py -ErrorAction SilentlyContinue
}
if (-not $pythonCommand) {
    throw "Python 3.11+ is required. Install Python and retry."
}

$arguments = @("dashboard_server.py", "--port", $Port)
if ($NoBrowser) {
    $arguments += "--no-browser"
}
& $pythonCommand.Source @arguments
