$ErrorActionPreference = 'Stop'

$Source = (Resolve-Path 'dist\NovelTranslator').Path
$Root = Join-Path $env:TEMP 'Novel Translator 한글 경로 QA'
$Target = Join-Path $Root 'Portable Copy'

if (Test-Path $Root) { Remove-Item $Root -Recurse -Force }
New-Item -ItemType Directory -Path $Root | Out-Null
Copy-Item -Path $Source -Destination $Target -Recurse -Force

$Exe = Join-Path $Target 'NovelTranslator.exe'
if (-not (Test-Path $Exe)) { throw "Relocated EXE missing: $Exe" }
if (-not (Test-Path (Join-Path $Target 'config'))) { throw 'Relocated config folder missing' }
if (-not (Test-Path (Join-Path $Target 'profiles'))) { throw 'Relocated profiles folder missing' }
if (-not (Get-ChildItem -Path $Target -Filter '*.html' -File)) { throw 'User manual (html) missing next to the EXE' }
if (-not (Test-Path (Join-Path $Target 'README.txt'))) { throw 'README.txt missing next to the EXE' }
foreach ($n in 'LICENSE.txt','THIRD_PARTY_NOTICES.txt') { if (-not (Test-Path (Join-Path $Target $n))) { throw "$n missing next to the EXE" } }

function Invoke-CheckedExe([string]$Label, [string]$Argument) {
    Write-Host $Label
    $Process = Start-Process -FilePath $Exe -ArgumentList $Argument -Wait -PassThru -NoNewWindow
    if ($Process.ExitCode -ne 0) { throw "Relocated $Argument failed: $($Process.ExitCode)" }
}

Invoke-CheckedExe '[relocated 1/4] runtime self-test' '--self-test'
Invoke-CheckedExe '[relocated 2/4] engine self-test' '--engine-self-test'
Invoke-CheckedExe '[relocated 3/4] secret-store/DPAPI self-test' '--secret-self-test'
Invoke-CheckedExe '[relocated 4/4] Qt/MainWindow self-test' '--gui-self-test'

Remove-Item $Root -Recurse -Force
Write-Host 'RELOCATED_PORTABLE_GATE=PASS'
