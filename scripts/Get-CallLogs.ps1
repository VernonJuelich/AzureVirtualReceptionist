<#
.SYNOPSIS
    Queries Azure Application Insights for Virtual Receptionist call logs.

.PARAMETER AppInsightsName
    Name of the App Insights resource.

.PARAMETER ResourceGroup
    Resource group name.

.PARAMETER Hours
    How many hours back to query. Default: 24

.PARAMETER Filter
    Optional: filter logs by keyword (e.g. "FAILED", "Hanson", "Transfer")

.EXAMPLE
    .\Get-CallLogs.ps1 -AppInsightsName "contoso-receptionist-ai" -ResourceGroup "rg-virtual-receptionist" -Hours 48
    .\Get-CallLogs.ps1 -AppInsightsName "contoso-receptionist-ai" -ResourceGroup "rg-virtual-receptionist" -Filter "FAILED"
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory)] [string] $AppInsightsName,
    [Parameter(Mandatory)] [string] $ResourceGroup,
    [int]    $Hours  = 24,
    [string] $Filter = ""
)

$AppId = az monitor app-insights component show `
    --app            $AppInsightsName `
    --resource-group $ResourceGroup `
    --query          "appId" `
    --output         tsv

$WhereClause = if ($Filter) { "| where message contains '$Filter'" } else { "" }

$Query = @"
traces
| where timestamp > ago(${Hours}h)
$WhereClause
| order by timestamp desc
| project timestamp, message, severityLevel
| take 200
"@

Write-Host "`n=== Call Logs — Last $Hours hours ===" -ForegroundColor Cyan
if ($Filter) { Write-Host "Filter: '$Filter'" -ForegroundColor DarkGray }
Write-Host ""

$Results = az monitor app-insights query `
    --apps  $AppId `
    --analytics-query $Query `
    --output json | ConvertFrom-Json

if ($Results.tables[0].rows.Count -eq 0) {
    Write-Host "No logs found." -ForegroundColor DarkGray
} else {
    foreach ($Row in $Results.tables[0].rows) {
        $Ts  = $Row[0]
        $Msg = $Row[1]
        $Sev = $Row[2]
        $Color = switch ($Sev) {
            1 { "Yellow" }   # Warning
            2 { "Red"    }   # Error
            3 { "Red"    }   # Critical
            default { "Gray" }
        }
        Write-Host "$Ts  " -NoNewline -ForegroundColor DarkGray
        Write-Host $Msg -ForegroundColor $Color
    }
    Write-Host "`n$($Results.tables[0].rows.Count) log entries shown." -ForegroundColor DarkGray
}
