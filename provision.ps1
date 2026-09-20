<#
.SYNOPSIS
  Provisions a Flex Consumption Azure Function App (Python) for the Trading 212
  overnight bot, sets all configuration as app settings, and deploys the code.

.EXAMPLE
  ./provision.ps1 -AppName riz-t212-bot -Ticker AAPL_US_EQ -Quantity ALL
  # -Quantity ALL buys with all available cash and sells the whole position
  # (compounding). Use a number instead (e.g. -Quantity 1) for a fixed size.

  # Later, go live (use LIVE keys - demo and live keys differ):
  ./provision.ps1 -AppName riz-t212-bot -Ticker AAPL_US_EQ -Quantity ALL `
                  -T212Env live -DryRun $false

Run from the folder containing function_app.py, host.json, requirements.txt.
Requires: Azure CLI, `az login` done. Safe to re-run to change settings/redeploy.
#>
param(
    [Parameter(Mandatory)] [string] $AppName,
    [string] $ResourceGroup  = "rg-t212-bot",
    [string] $Location       = "uksouth",
    [string] $StorageAccount = "",

    # --- trading config (become app settings / environment variables) ---
    [Parameter(Mandatory)] [string] $Ticker,             # e.g. AAPL_US_EQ
    [Parameter(Mandatory)]
    [ValidateScript({ $_ -eq "ALL" -or (($_ -as [double]) -gt 0) })]
    [string] $Quantity,               # shares per trade, or ALL = invest all available cash
    [ValidateSet("demo", "live")] [string] $T212Env = "demo",
    [string] $BuyTimeUk  = "20:59",   # 1 min before US close (normal 5h UK/US gap)
    [string] $SellTimeUk = "14:31",   # 1 min after US open   (normal 5h UK/US gap)
    [bool]   $AdjustForDstGap = $true,
    [bool]   $DryRun = $true,
    [string] $NoBuyDates = "",        # early-close days, e.g. "2026-11-27,2026-12-24"
    [string] $Schedule = "0 * 13-20 * * 1-5",   # NCRONTAB, evaluated in UTC

    # --- only used when -Quantity ALL (share count is worked out from a price) ---
    [string] $PriceSymbol = "",         # Yahoo symbol; blank = ticker before first "_"
    [string] $InstrumentCurrency = "USD",
    [double] $CashBufferPct = 8.0,       # % of cash left uninvested (price move, FX fee, spread)
    [string] $MaxTradeValue = "",       # optional cap per buy, in account currency

    # --- secrets: from env vars on THIS machine, else prompted ---
    [string] $ApiKey    = $env:T212_API_KEY,
    [string] $ApiSecret = $env:T212_API_SECRET
)

$ErrorActionPreference = "Stop"

function Invoke-Az {
    $out = & az @args
    if ($LASTEXITCODE -ne 0) { throw "az $($args -join ' ') failed (exit $LASTEXITCODE)" }
    $out
}

function Read-Secret([string]$prompt) {
    $ss = Read-Host $prompt -AsSecureString
    [System.Net.NetworkCredential]::new("", $ss).Password
}

if (-not $ApiKey)    { $ApiKey    = Read-Secret "Trading 212 API key" }
if (-not $ApiSecret) { $ApiSecret = Read-Secret "Trading 212 API secret" }

foreach ($f in "function_app.py", "host.json", "requirements.txt") {
    if (-not (Test-Path (Join-Path $PSScriptRoot $f))) { throw "Missing $f next to this script" }
}
Invoke-Az account show --query id -o tsv | Out-Null   # fails fast if not logged in

if (-not $StorageAccount) {
    $clean = ($AppName.ToLower() -replace "[^a-z0-9]", "")
    $StorageAccount = ("st" + $clean)
    if ($StorageAccount.Length -gt 24) { $StorageAccount = $StorageAccount.Substring(0, 24) }
}

Write-Host "Resource group: $ResourceGroup ($Location)"
Invoke-Az group create --name $ResourceGroup --location $Location -o none

Write-Host "Storage account: $StorageAccount"
Invoke-Az storage account create --name $StorageAccount --resource-group $ResourceGroup `
    --location $Location --sku Standard_LRS --kind StorageV2 `
    --allow-blob-public-access false --min-tls-version TLS1_2 -o none

$existing = Invoke-Az functionapp list --resource-group $ResourceGroup `
    --query "[?name=='$AppName'].name" -o tsv
if (-not $existing) {
    Write-Host "Creating Flex Consumption function app: $AppName"
    Invoke-Az functionapp create --name $AppName --resource-group $ResourceGroup `
        --storage-account $StorageAccount --flexconsumption-location $Location `
        --runtime python --runtime-version 3.12 --instance-memory 512 -o none
} else {
    Write-Host "Function app $AppName already exists, updating settings + code"
}

# Settings go via a temp JSON file so secrets never appear in process arguments.
$settings = @(
    @{ name = "T212_ENV";                   value = $T212Env }
    @{ name = "T212_API_KEY";               value = $ApiKey }
    @{ name = "T212_API_SECRET";            value = $ApiSecret }
    @{ name = "T212_TICKER";                value = $Ticker }
    @{ name = "TRADE_QUANTITY";             value = $Quantity.Trim().ToUpper() }
    @{ name = "PRICE_SYMBOL";               value = $PriceSymbol }
    @{ name = "INSTRUMENT_CURRENCY";        value = $InstrumentCurrency }
    @{ name = "CASH_BUFFER_PCT";            value = "$CashBufferPct" }
    @{ name = "MAX_TRADE_VALUE";            value = $MaxTradeValue }
    @{ name = "BUY_TIME_UK";                value = $BuyTimeUk }
    @{ name = "SELL_TIME_UK";               value = $SellTimeUk }
    @{ name = "ADJUST_FOR_US_UK_DST_GAP";   value = "$AdjustForDstGap".ToLower() }
    @{ name = "DRY_RUN";                    value = "$DryRun".ToLower() }
    @{ name = "NO_BUY_DATES";               value = $NoBuyDates }
    @{ name = "TRADE_SCHEDULE";             value = $Schedule }
) | ForEach-Object { $_ + @{ slotSetting = $false } }

$settingsFile = Join-Path ([System.IO.Path]::GetTempPath()) "t212-settings-$([guid]::NewGuid()).json"
$zip = Join-Path ([System.IO.Path]::GetTempPath()) "t212-func-$([guid]::NewGuid()).zip"
try {
    ConvertTo-Json -InputObject @($settings) | Set-Content -Path $settingsFile -Encoding utf8
    Write-Host "Applying app settings"
    Invoke-Az functionapp config appsettings set --name $AppName --resource-group $ResourceGroup `
        --settings "@$settingsFile" -o none

    Write-Host "Packaging and deploying code"
    Compress-Archive -Force -DestinationPath $zip -Path @(
        (Join-Path $PSScriptRoot "function_app.py"),
        (Join-Path $PSScriptRoot "host.json"),
        (Join-Path $PSScriptRoot "requirements.txt"))
    Invoke-Az functionapp deployment source config-zip --name $AppName `
        --resource-group $ResourceGroup --src $zip --build-remote true -o none
}
finally {
    Remove-Item $settingsFile, $zip -Force -ErrorAction SilentlyContinue
}

Write-Host ""
Write-Host "Done. env=$T212Env dryRun=$DryRun ticker=$Ticker qty=$Quantity"
Write-Host "Buy $BuyTimeUk / Sell $SellTimeUk (UK, DST-gap adjust: $AdjustForDstGap)"
if ($DryRun) { Write-Host "DRY RUN is on: check the logs in Application Insights, then re-run with -DryRun `$false" }
