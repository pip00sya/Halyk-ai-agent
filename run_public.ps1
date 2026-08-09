param(
    [string]$Team = "CHANGE_ME_TEAM",
    [string]$ContactEmail = "CHANGE_ME_EMAIL"
)

$ErrorActionPreference = "Stop"
python run_agent.py preflight `
    --data agentic-bank-public `
    --team $Team `
    --contact-email $ContactEmail `
    --mode public
python run_agent.py run `
    --data agentic-bank-public `
    --output submission.json `
    --team $Team `
    --contact-email $ContactEmail
python run_agent.py validate --data agentic-bank-public --submission submission.json
python run_agent.py score `
    --submission submission.json `
    --ground-truth agentic-bank-public/ground_truth.json
