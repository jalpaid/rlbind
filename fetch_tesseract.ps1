# Downloads the official UB-Mannheim Tesseract-OCR Windows build and unpacks a
# minimal portable copy into .\tesseract\ (tesseract.exe + DLLs + eng.traineddata).
# The folder is gitignored; run this once after cloning, before building the
# installer or portable zip.
#Requires -Version 5.1
$ErrorActionPreference = 'Stop'

$TessVersion = '5.4.0.20240606'
$Url = "https://github.com/UB-Mannheim/tesseract/releases/download/v$TessVersion/tesseract-ocr-w64-setup-$TessVersion.exe"
$SevenZip = 'C:\Program Files\7-Zip\7z.exe'
$Work = Join-Path $env:TEMP 'rlbind_tesseract_fetch'
$Dest = Join-Path $PSScriptRoot 'tesseract'

if (Test-Path (Join-Path $Dest 'tesseract.exe')) {
    Write-Host 'tesseract\ already present; delete it first to re-fetch.'
    exit 0
}
if (-not (Test-Path $SevenZip)) { throw "7-Zip not found at $SevenZip" }

New-Item -ItemType Directory -Force $Work | Out-Null
$Setup = Join-Path $Work 'tesseract-setup.exe'
Write-Host "Downloading $Url"
curl.exe -L -o $Setup $Url
if ($LASTEXITCODE -ne 0) { throw 'Download failed' }

Write-Host 'Extracting...'
& $SevenZip x $Setup "-o$Work\extracted" -y | Out-Null
$Src = Join-Path $Work 'extracted'

if (Test-Path $Dest) { Remove-Item $Dest -Recurse -Force }
New-Item -ItemType Directory "$Dest\tessdata" | Out-Null
Copy-Item "$Src\tesseract.exe" $Dest
Copy-Item "$Src\*.dll" $Dest
Copy-Item "$Src\tessdata\eng.traineddata" "$Dest\tessdata\"
Copy-Item "$Src\doc\LICENSE" $Dest

Remove-Item $Work -Recurse -Force
& (Join-Path $Dest 'tesseract.exe') --version | Select-Object -First 1
Write-Host "Done. Tesseract $TessVersion unpacked to $Dest"
