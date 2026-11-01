param([string]$Destination)
$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
$metadata = Get-Content -LiteralPath (Join-Path $repo 'runtime\wechatagent.json') -Raw -Encoding UTF8 | ConvertFrom-Json
if (-not $Destination) { $Destination = Join-Path $repo 'runtime\installers' }
$directory = [IO.Path]::GetFullPath($Destination)
[IO.Directory]::CreateDirectory($directory) | Out-Null
$target = Join-Path $directory $metadata.installer
if (Test-Path -LiteralPath $target) {
    if ((Get-FileHash -LiteralPath $target -Algorithm SHA256).Hash.ToLowerInvariant() -ne $metadata.sha256) {
        throw 'Existing installer hash does not match. Move it aside before downloading again.'
    }
} else {
    $partial = $target + '.partial'
    if (Test-Path -LiteralPath $partial) { throw 'An incomplete download already exists. Move it aside before retrying.' }
    Invoke-WebRequest -Uri $metadata.url -OutFile $partial -UseBasicParsing
    if ((Get-Item -LiteralPath $partial).Length -ne $metadata.bytes -or
        (Get-FileHash -LiteralPath $partial -Algorithm SHA256).Hash.ToLowerInvariant() -ne $metadata.sha256) {
        throw 'Installer verification failed; the file was not launched.'
    }
    Move-Item -LiteralPath $partial -Destination $target
}
Write-Output ('Verified installer: ' + $target)
Write-Output 'Open the installer manually, finish local account setup, then follow runtime/README.md.'
