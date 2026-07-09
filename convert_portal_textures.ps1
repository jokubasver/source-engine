param(
    [string]$PortalDir = "C:\Program Files (x86)\Steam\steamapps\common\Portal",
    [string]$OutputDir = "./portal_astc",
    [switch]$DownloadAstcenc,
    [int]$Threads = 8
)

$ErrorActionPreference = "Stop"

$AstcencUrl = "https://github.com/ARM-software/astc-encoder/releases/download/5.0.0/astcenc-5.0.0-windows-x64.exe"
$AstcencLocal = Join-Path $PSScriptRoot "astcenc.exe"
$Vtf2Tga = Join-Path $PortalDir "bin\vtf2tga.exe"
$Python = "python"
$PackVtf = Join-Path $PSScriptRoot "pack_vtf.py"

$IMAGE_FORMAT_ASTC4x4 = 41

function Write-Step  { param([string]$Msg); Write-Host "`n=== $Msg ===" -ForegroundColor Cyan }
function Write-OK    { param([string]$Msg); Write-Host "  $Msg" -ForegroundColor Green }
function Write-Warn  { param([string]$Msg); Write-Host "  WARN: $Msg" -ForegroundColor Yellow }
function Write-Fail  { param([string]$Msg); Write-Host "  FAIL: $Msg" -ForegroundColor Red }

function Check-Requirement {
    param([string]$Path, [string]$Name)
    if (-not (Test-Path -LiteralPath $Path)) {
        Write-Error "Required tool not found: $Name at $Path"
        exit 1
    }
    Write-OK $Name
}

function Get-Astcenc {
    param([string]$Dest)
    if (Test-Path -LiteralPath $Dest) { Write-OK "astcenc already present"; return }
    Write-Host "  Downloading astcenc from $AstcencUrl ..."
    Invoke-WebRequest -Uri $AstcencUrl -OutFile $Dest -UseBasicParsing
    Write-OK "Downloaded"
}

function Extract-VtfViaPython {
    param([string]$VpkDirPath, [string]$OutDir, [string]$VpkName = "portal_pak_dir.vpk")
    $vpkPath = Join-Path $VpkDirPath $VpkName
    if (-not (Test-Path -LiteralPath $vpkPath)) { return 0 }
    $extractScript = @"
import vpk, os, sys
vpk_path = r'$vpkPath'
out_dir = r'$OutDir'
pak = vpk.open(vpk_path)
vtf_files = [f for f in pak if f.endswith('.vtf')]
extracted = 0
for f in vtf_files:
    data = pak[f].read()
    full = os.path.join(out_dir, f)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, 'wb') as out:
        out.write(data)
    extracted += 1
    if extracted % 500 == 0:
        print(f'{extracted}/{len(vtf_files)}')
print(f'DONE:{extracted}')
"@
    $result = & $Python -c $extractScript 2>&1 | Out-String
    if ($result -match 'DONE:(\d+)') {
        return [int]$Matches[1]
    }
    return -1
}

function Get-TextureFormat {
    param([string]$VtfPath)
    $bytes = [System.IO.File]::ReadAllBytes($VtfPath)
    if ($bytes.Length -lt 56) { return -1 }
    $fmtOff = 52
    return [System.BitConverter]::ToUInt32($bytes, $fmtOff)
}

# ============================================================
# MAIN
# ============================================================

Write-Step "Checking prerequisites"
Check-Requirement $Vtf2Tga "vtf2tga.exe"
if ($DownloadAstcenc) { Get-Astcenc $AstcencLocal }
Check-Requirement $AstcencLocal "astcenc.exe"

try {
    & $Python -c "import vpk" 2>&1 | Out-Null
    Write-OK "Python + vpk package"
}
catch {
    Write-Host "  Installing vpk package via pip..." -ForegroundColor Yellow
    & $Python -m pip install vpk 2>&1 | Out-Null
    try {
        & $Python -c "import vpk" 2>&1 | Out-Null
        Write-OK "Python + vpk package installed"
    }
    catch {
        Write-Error "Failed to install Python 'vpk' package. Run: pip install vpk"
        exit 1
    }
}

try {
    & $Python -c "from PIL import Image" 2>&1 | Out-Null
    Write-OK "Python + Pillow package"
}
catch {
    Write-Host "  Installing Pillow package via pip..." -ForegroundColor Yellow
    & $Python -m pip install Pillow 2>&1 | Out-Null
    try {
        & $Python -c "from PIL import Image" 2>&1 | Out-Null
        Write-OK "Python + Pillow package installed"
    }
    catch {
        Write-Error "Failed to install Python 'Pillow' package. Run: pip install Pillow"
        exit 1
    }
}

Write-Step "Extracting VTF files from VPKs via Python"
$tmpDir = Join-Path $env:TEMP "portal_vtf_$(Get-Random)"
$null = New-Item -ItemType Directory -Path $tmpDir -Force

$totalExtracted = 0
$vpkSearchDirs = @(
    @{ Dir = (Join-Path $PortalDir "portal"); Label = "portal" }
)
foreach ($v in $vpkSearchDirs) {
    if (-not (Test-Path -LiteralPath $v.Dir)) { continue }
    $vpkFiles = Get-ChildItem -Path $v.Dir -Filter "*_dir.vpk" -File -ErrorAction SilentlyContinue
    foreach ($vpk in $vpkFiles) {
        Write-Host "  Extracting from $($v.Label)/$($vpk.Name) ..." -ForegroundColor Gray
        $count = Extract-VtfViaPython -VpkDirPath $v.Dir -OutDir $tmpDir -VpkName $vpk.Name
        if ($count -gt 0) {
            Write-OK "  $($v.Label)/$($vpk.Name): $count VTF files"
            $totalExtracted += $count
        } elseif ($count -eq -1) {
            Write-Warn "  $($v.Label)/$($vpk.Name): VPK extraction failed"
        }
    }
}
if ($totalExtracted -le 0) { Write-Fail "No VTF files extracted from any VPK"; exit 1 }
Write-OK "Total extracted: $totalExtracted VTF files"

Write-Step "Converting textures DXT -> ASTC 4x4"
$vtfFiles = Get-ChildItem -Path $tmpDir -Filter "*.vtf" -Recurse
$total = $vtfFiles.Count

$alreadyDone = 0
$todo = foreach ($vtf in $vtfFiles) {
    $rel = $vtf.FullName.Substring($tmpDir.Length + 1)
    $relDir = Split-Path $rel -Parent
    $nameNoExt = [System.IO.Path]::GetFileNameWithoutExtension($rel)
    $outSubdir = Join-Path $OutputDir $relDir
    $outVtf = Join-Path $outSubdir "$nameNoExt.vtf"
    if (Test-Path -LiteralPath $outVtf) {
        $alreadyDone++
    } else {
        $vtf
    }
}
Write-OK "Already done: $alreadyDone | Remaining: $($todo.Count) / $total"

$converted = 0; $skipped = 0; $tiny = 0; $failed = 0
$failedFiles = [System.Collections.Generic.List[string]]::new()

$isPwsh7 = $PSVersionTable.PSVersion.Major -ge 7

if ($isPwsh7 -and $todo.Count -gt 1) {
    Write-Host "  Using $Threads parallel threads (PowerShell $($PSVersionTable.PSVersion))" -ForegroundColor Cyan

    $results = $todo | ForEach-Object -ThrottleLimit $Threads -Parallel {
        $vtfPath = $_.FullName
        $rel = $vtfPath.Substring($using:tmpDir.Length + 1)
        $relDir = Split-Path $rel -Parent
        $nameNoExt = [System.IO.Path]::GetFileNameWithoutExtension($rel)
        $outSubdir = Join-Path $using:OutputDir $relDir
        $outVtf = Join-Path $outSubdir "$nameNoExt.vtf"

        if (Test-Path -LiteralPath $outVtf) { "skip"; return }

        New-Item -ItemType Directory -Path $outSubdir -Force | Out-Null

        $bytes = [System.IO.File]::ReadAllBytes($vtfPath)
        if ($bytes.Length -lt 20) { "tiny"; return }
        $w = [System.BitConverter]::ToUInt16($bytes, 16)
        $h = [System.BitConverter]::ToUInt16($bytes, 18)
        if ($w -lt 2 -or $h -lt 2) {
            Copy-Item -LiteralPath $vtfPath -Destination $outVtf
            "tiny"; return
        }

        $tmpId = [guid]::NewGuid().ToString("N")
        $tgaFile = Join-Path $env:TEMP "$tmpId.tga"

        try {
            $proc = Start-Process -FilePath $using:Vtf2Tga -ArgumentList "-i `"$vtfPath`"" -Wait -NoNewWindow -PassThru
            $tgaCreated = [System.IO.Path]::ChangeExtension($vtfPath, ".tga")
            if (-not (Test-Path -LiteralPath $tgaCreated)) {
                Copy-Item -LiteralPath $vtfPath -Destination $outVtf
                "fail_tga:$rel" ; return
            }
            Move-Item -LiteralPath $tgaCreated -Destination $tgaFile -Force

            $proc = Start-Process -FilePath $using:Python -ArgumentList "`"$using:PackVtf`" --vtf `"$vtfPath`" --tga `"$tgaFile`" --astcenc `"$using:AstcencLocal`" --output `"$outVtf`" --rgba-fallback" -Wait -NoNewWindow -PassThru
            if ($proc.ExitCode -ne 0 -or -not (Test-Path -LiteralPath $outVtf)) {
                Copy-Item -LiteralPath $vtfPath -Destination $outVtf
                "fail_pack:$rel" ; return
            }
            "ok"
        }
        finally {
            Remove-Item -LiteralPath $tgaFile -Force -ErrorAction SilentlyContinue
        }
    }

    foreach ($r in $results) {
        if ($r -eq "ok") { $converted++ }
        elseif ($r -eq "skip") { $skipped++ }
        elseif ($r -eq "tiny") { $tiny++ }
        # Fixed PowerShell syntax here
        elseif ($r -like "fail_*") {
            $failed++
            $null = $failedFiles.Add($r.Substring($r.IndexOf(':') + 1))
        }
    }
} else {
    $done = 0
    foreach ($vtf in $todo) {
        $done++
        $rel = $vtf.FullName.Substring($tmpDir.Length + 1)
        Write-Progress -Activity "Converting textures" -Status "$done/$($todo.Count) : $rel" -PercentComplete (($done / $todo.Count) * 100)

        $formatId = Get-TextureFormat $vtf.FullName
        if ($formatId -eq $IMAGE_FORMAT_ASTC4x4) {
            $skipped++
            continue
        }

        $relDir = Split-Path $rel -Parent
        $nameNoExt = [System.IO.Path]::GetFileNameWithoutExtension($rel)
        $outSubdir = Join-Path $OutputDir $relDir
        $outVtf = Join-Path $outSubdir "$nameNoExt.vtf"

        if (Test-Path -LiteralPath $outVtf) { $skipped++; continue }

        New-Item -ItemType Directory -Path $outSubdir -Force | Out-Null

        $bytes = [System.IO.File]::ReadAllBytes($vtf.FullName)
        if ($bytes.Length -ge 20) {
            $w = [System.BitConverter]::ToUInt16($bytes, 16)
            $h = [System.BitConverter]::ToUInt16($bytes, 18)
            if ($w -lt 2 -or $h -lt 2) {
                Copy-Item -LiteralPath $vtf.FullName -Destination $outVtf
                $tiny++
                continue
            }
        }

        $tmpId = [guid]::NewGuid().ToString("N")
        $tgaFile = Join-Path $env:TEMP "$tmpId.tga"

        try {
            $proc = Start-Process -FilePath $Vtf2Tga -ArgumentList "-i `"$($vtf.FullName)`"" -Wait -NoNewWindow -PassThru
            $tgaCreated = [System.IO.Path]::ChangeExtension($vtf.FullName, ".tga")
            if (-not (Test-Path -LiteralPath $tgaCreated)) {
                Copy-Item -LiteralPath $vtf.FullName -Destination $outVtf
                $failed++
                $null = $failedFiles.Add($rel)
                continue
            }
            Move-Item -LiteralPath $tgaCreated -Destination $tgaFile -Force

            $proc = Start-Process -FilePath $Python -ArgumentList "`"$PackVtf`" --vtf `"$($vtf.FullName)`" --tga `"$tgaFile`" --astcenc `"$AstcencLocal`" --output `"$outVtf`" --rgba-fallback" -Wait -NoNewWindow -PassThru
            if ($proc.ExitCode -ne 0 -or -not (Test-Path -LiteralPath $outVtf)) {
                Copy-Item -LiteralPath $vtf.FullName -Destination $outVtf
                $failed++
                $null = $failedFiles.Add($rel)
                continue
            }
            $converted++
        }
        finally {
            Remove-Item -LiteralPath $tgaFile -Force -ErrorAction SilentlyContinue
        }
    }
}

Write-Progress -Activity "Converting textures" -Completed
Write-Host ""
Write-OK "Converted: $converted | Skipped: $skipped | Tiny: $tiny | Failed: $failed | Total: $total"

if ($failed -gt 0) {
    Write-Host ""
    Write-Host "  WARNING: $failed textures failed conversion and were copied as original DXT." -ForegroundColor Yellow
    Write-Host "  These will appear purple/black on Mali. Failed items listed below:" -ForegroundColor Yellow
    foreach ($file in $failedFiles) {
        Write-Host "    -> $file" -ForegroundColor Red
    }
    Write-Host ""
}

Write-Step "Copying non-texture game files"
$dirsToCopy = @("portal", "hl2", "platform", "steam_input")
foreach ($d in $dirsToCopy) {
    $src = Join-Path $PortalDir $d
    if (Test-Path -LiteralPath $src) {
        $dst = Join-Path $OutputDir $d
        Write-Host "  Copying $d/ ..." -ForegroundColor Gray
        & robocopy $src $dst /E /XF "*.vtf" /NFL /NDL /NJH /NJS /NC /NS /NP
    } else {
        Write-Warn "Source directory not found: $src"
    }
}

Write-Step "Cleanup temp"
Remove-Item -LiteralPath $tmpDir -Recurse -Force

Write-Step "Done!"
$totalSize = (Get-ChildItem -Path $OutputDir -Recurse -File | Measure-Object -Property Length -Sum).Sum
Write-Host "  Output: $OutputDir ($([math]::Round($totalSize / 1MB, 1)) MB)" -ForegroundColor Green