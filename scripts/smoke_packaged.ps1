param(
  [string]$Url = "https://www.youtube.com/watch?v=jNQXAC9IVRw",
  [string]$AccountName = "Packaged Smoke",
  [int]$TimeoutSeconds = 180,
  [switch]$KeepRuntimeData
)

$ErrorActionPreference = "Stop"

$exePath = Resolve-Path "dist\NicheFlowStudio\NicheFlowStudio.exe"
$runtimeBase = Join-Path $env:LOCALAPPDATA "NicheFlow Studio\data"
$dbPath = Join-Path $runtimeBase "nicheflow.db"

function Get-MainWindow {
  param([int]$TimeoutSeconds = 20)

  Add-Type -AssemblyName UIAutomationClient
  Add-Type -AssemblyName UIAutomationTypes

  $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
  $condition = New-Object System.Windows.Automation.PropertyCondition(
    [System.Windows.Automation.AutomationElement]::NameProperty,
    "NicheFlow Studio"
  )

  do {
    $window = [System.Windows.Automation.AutomationElement]::RootElement.FindFirst(
      [System.Windows.Automation.TreeScope]::Children,
      $condition
    )
    if ($window -ne $null) {
      return $window
    }
    Start-Sleep -Milliseconds 500
  } while ((Get-Date) -lt $deadline)

  throw "Could not find the packaged app window."
}

function Get-ControlCollection {
  param(
    [System.Windows.Automation.AutomationElement]$Window,
    [System.Windows.Automation.ControlType]$ControlType
  )

  $condition = New-Object System.Windows.Automation.PropertyCondition(
    [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
    $ControlType
  )

  return $Window.FindAll([System.Windows.Automation.TreeScope]::Descendants, $condition)
}

function Set-EditValue {
  param(
    [System.Windows.Automation.AutomationElement]$Edit,
    [string]$Value
  )

  $pattern = $Edit.GetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern)
  $pattern.SetValue($Value)
}

function Invoke-ButtonByName {
  param(
    [System.Windows.Automation.AutomationElement]$Window,
    [string]$Name
  )

  $buttons = Get-ControlCollection -Window $Window -ControlType ([System.Windows.Automation.ControlType]::Button)
  foreach ($button in $buttons) {
    if ($button.Current.Name -eq $Name) {
      $button.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern).Invoke()
      return
    }
  }

  throw "Could not find button '$Name'."
}

function Select-ComboItem {
  param(
    [System.Windows.Automation.AutomationElement]$Combo,
    [string]$ItemName
  )

  $expand = $Combo.GetCurrentPattern([System.Windows.Automation.ExpandCollapsePattern]::Pattern)
  $expand.Expand()
  Start-Sleep -Milliseconds 400

  $items = $Combo.FindAll(
    [System.Windows.Automation.TreeScope]::Descendants,
    (New-Object System.Windows.Automation.PropertyCondition(
      [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
      [System.Windows.Automation.ControlType]::ListItem
    ))
  )

  foreach ($item in $items) {
    if ($item.Current.Name -eq $ItemName) {
      $item.GetCurrentPattern([System.Windows.Automation.SelectionItemPattern]::Pattern).Select()
      Start-Sleep -Milliseconds 400
      return
    }
  }

  throw "Could not find combo item '$ItemName'."
}

function Read-LatestItem {
  param([string]$DbPath)

  if (!(Test-Path $DbPath)) {
    return $null
  }

  $script = @"
import sqlite3
import sys

conn = sqlite3.connect(sys.argv[1])
row = conn.execute(
    "select id, source_url, status, error_message, file_path from download_items order by id desc limit 1"
).fetchone()
print(row if row else None)
"@

  return ($script | .\.venv\Scripts\python.exe - $DbPath)
}

function Read-LatestItemJson {
  param([string]$DbPath)

  if (!(Test-Path $DbPath)) {
    return $null
  }

  $script = @"
import json
import sqlite3
import sys

conn = sqlite3.connect(sys.argv[1])
row = conn.execute(
    """
    select d.id, d.source_url, d.status, d.error_message, d.file_path, d.extractor, d.video_id, d.title,
           a.name
    from download_items d
    left join accounts a on a.id = d.account_id
    order by d.id desc
    limit 1
    """
).fetchone()
if row is None:
    print("null")
else:
    print(json.dumps({
        "id": row[0],
        "source_url": row[1],
        "status": row[2],
        "error_message": row[3],
        "file_path": row[4],
        "extractor": row[5],
        "video_id": row[6],
        "title": row[7],
        "account_name": row[8],
    }))
"@

  return ($script | .\.venv\Scripts\python.exe - $DbPath)
}

function Get-RowCount {
  param([string]$DbPath)

  if (!(Test-Path $DbPath)) {
    return 0
  }

  $script = @"
import sqlite3
import sys

conn = sqlite3.connect(sys.argv[1])
print(conn.execute("select count(*) from download_items").fetchone()[0])
"@

  return [int](($script | .\.venv\Scripts\python.exe - $DbPath))
}

if (!$KeepRuntimeData -and (Test-Path $runtimeBase)) {
  Remove-Item -Recurse -Force $runtimeBase
}

$proc = Start-Process -FilePath $exePath -PassThru
try {
  $window = Get-MainWindow
  $edits = Get-ControlCollection -Window $window -ControlType ([System.Windows.Automation.ControlType]::Edit)
  if ($edits.Count -lt 2) {
    throw "Expected at least two edit fields in the main window."
  }

  Set-EditValue -Edit $edits.Item(1) -Value $AccountName
  Invoke-ButtonByName -Window $window -Name "Save Account"
  Start-Sleep -Seconds 1

  Set-EditValue -Edit $edits.Item(0) -Value $Url
  Invoke-ButtonByName -Window $window -Name "Download"

  $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
  do {
    Start-Sleep -Seconds 2
    $latest = Read-LatestItem -DbPath $dbPath
    if ($latest) {
      Write-Output "LATEST_ITEM=$latest"
    }
    if ($latest -match "downloaded" -or $latest -match "failed") {
      break
    }
  } while ((Get-Date) -lt $deadline)

  if (!($latest -match "downloaded")) {
    throw "Packaged download did not reach 'downloaded'. Latest row: $latest"
  }
}
finally {
  if ($proc -and -not $proc.HasExited) {
    Stop-Process -Id $proc.Id -Force
  }
}

if (!(Test-Path $dbPath)) {
  throw "Packaged app did not create the runtime database at $dbPath"
}

$downloadsDir = Join-Path $runtimeBase "downloads"
if (!(Test-Path $downloadsDir)) {
  throw "Packaged app did not create the downloads directory at $downloadsDir"
}

$rowCountBeforeRestart = Get-RowCount -DbPath $dbPath
$proc = Start-Process -FilePath $exePath -PassThru
try {
  $window = Get-MainWindow
  $combos = Get-ControlCollection -Window $window -ControlType ([System.Windows.Automation.ControlType]::ComboBox)
  if ($combos.Count -lt 1) {
    throw "Expected the current-workspace combo box in the packaged app."
  }

  Select-ComboItem -Combo $combos.Item(0) -ItemName "$AccountName (youtube)"
  Start-Sleep -Seconds 2
}
finally {
  if ($proc -and -not $proc.HasExited) {
    Stop-Process -Id $proc.Id -Force
  }
}

$rowCountAfterRestart = Get-RowCount -DbPath $dbPath
if ($rowCountAfterRestart -lt 1 -or $rowCountAfterRestart -ne $rowCountBeforeRestart) {
  throw "History did not persist across restart. Before=$rowCountBeforeRestart After=$rowCountAfterRestart"
}

$latestJson = Read-LatestItemJson -DbPath $dbPath

Write-Output "PACKAGED_SMOKE_OK"
Write-Output "RUNTIME_BASE=$runtimeBase"
Write-Output "DB_PATH=$dbPath"
Write-Output "DOWNLOADS_DIR=$downloadsDir"
Write-Output "ROW_COUNT=$rowCountAfterRestart"
Write-Output "ACCOUNT_NAME=$AccountName"
Write-Output "KEEP_RUNTIME_DATA=$KeepRuntimeData"
if ($latestJson) {
  Write-Output "LATEST_ITEM_JSON=$latestJson"
}
