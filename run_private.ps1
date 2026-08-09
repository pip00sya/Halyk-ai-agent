param(
    [Parameter(Mandatory = $true)]
    [string]$DataDirectory,
    [Parameter(Mandatory = $true)]
    [string]$Team,
    [Parameter(Mandatory = $true)]
    [string]$ContactEmail,
    [string]$Model = "gpt-5.6",
    [ValidateSet("medium", "high", "xhigh", "max")]
    [string]$ReasoningEffort = "high",
    [ValidateRange(1, 6)]
    [int]$Workers = 3
)

$ErrorActionPreference = "Stop"
if (-not $env:OPENAI_API_KEY) {
    throw "Set OPENAI_API_KEY in the current terminal before the private run."
}

function Assert-AgentStep {
    param([string]$Name)
    if ($LASTEXITCODE -ne 0) {
        throw "$Name failed with exit code $LASTEXITCODE. Fix the reported blocker and rerun the script."
    }
}

python run_agent.py preflight `
    --data $DataDirectory `
    --team $Team `
    --contact-email $ContactEmail `
    --mode llm
Assert-AgentStep "Preflight"
python run_agent.py run `
    --data $DataDirectory `
    --output submission.json `
    --team $Team `
    --contact-email $ContactEmail `
    --mode llm `
    --model $Model `
    --reasoning-effort $ReasoningEffort `
    --workers $Workers
Assert-AgentStep "Private agent run"
python run_agent.py validate --data $DataDirectory --submission submission.json
Assert-AgentStep "Submission validation"
