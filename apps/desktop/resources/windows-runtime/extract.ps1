param([Parameter(Mandatory=$true)][string]$Archive, [Parameter(Mandatory=$true)][string]$Destination, [string]$Prefix = '', [ValidateSet('zip','tar')][string]$Format = 'zip')
$ErrorActionPreference='Stop'
Add-Type -AssemblyName System.IO.Compression.FileSystem
if (Test-Path -LiteralPath $Destination) { throw 'Runtime destination already exists' }
$root=[IO.Path]::GetFullPath($Destination).TrimEnd('\')+'\'
[void][IO.Directory]::CreateDirectory($root)
if ($Format -eq 'tar') {
    # Keep native tar argv ASCII; .NET handles the original Unicode paths.
    $workspace=Join-Path ([IO.Path]::GetDirectoryName($root.TrimEnd('\'))) ('extract-'+[Guid]::NewGuid().ToString('N'))
    [void][IO.Directory]::CreateDirectory($workspace)
    $tar=Join-Path $env:SystemRoot 'System32\tar.exe'
    $prefixPath=$Prefix.Replace('\','/').TrimEnd('/')
    if ($prefixPath.StartsWith('/') -or $prefixPath.Contains(':') -or ($prefixPath.Split('/') -contains '..')) { throw 'Unsafe runtime archive prefix' }
    try {
        $archivePath=if ([IO.Path]::IsPathRooted($Archive)) { $Archive } else { Join-Path (Get-Location).Path $Archive }
        [IO.File]::Copy([IO.Path]::GetFullPath($archivePath),(Join-Path $workspace 'payload.tar'),$false)
        Push-Location -LiteralPath $workspace
        try {
            $entries=& $tar -tf 'payload.tar'
            if ($LASTEXITCODE -ne 0 -or !$entries) { throw 'Cannot inspect runtime archive' }
            foreach ($name in $entries) {
                if ($name.StartsWith('/') -or $name.Contains(':') -or ($name.Replace('\','/').Split('/') -contains '..')) { throw 'Unsafe runtime archive entry' }
            }
            [void][IO.Directory]::CreateDirectory((Join-Path $workspace 'content'))
            & $tar -xf 'payload.tar' -C 'content'
            if ($LASTEXITCODE -ne 0) { throw 'Runtime archive extraction failed' }
            $source=Join-Path $workspace 'content'
            if ($prefixPath) { $source=Join-Path $source $prefixPath }
            if (!(Test-Path -LiteralPath $source -PathType Container)) { throw 'Runtime archive is missing its expected directory' }
            foreach ($entry in [IO.Directory]::GetFileSystemEntries($source)) {
                Copy-Item -LiteralPath $entry -Destination $root -Recurse -Force
            }
        } finally { Pop-Location }
    } finally {
        # Cleanup never replaces the extraction failure or invalidates success.
        try { Remove-Item -LiteralPath $workspace -Recurse -Force -ErrorAction Stop } catch { Write-Warning ('Runtime extraction cleanup failed: '+$_.Exception.Message) }
    }
    exit 0
}
$zip=[IO.Compression.ZipFile]::OpenRead($Archive)
try {
    foreach ($entry in $zip.Entries) {
        $name=$entry.FullName.Replace('\','/')
        if ($Prefix) {
            if (!$name.StartsWith($Prefix,[StringComparison]::Ordinal)) { continue }
            $name=$name.Substring($Prefix.Length)
        }
        if (!$name) { continue }
        $target=[IO.Path]::GetFullPath((Join-Path $root $name))
        if ($name.Contains(':') -or !$target.StartsWith($root,[StringComparison]::OrdinalIgnoreCase) -or (($entry.ExternalAttributes -shr 16) -band 0xF000) -eq 0xA000) { throw 'Unsafe runtime archive entry' }
        if ($name.EndsWith('/')) { [void][IO.Directory]::CreateDirectory($target); continue }
        [void][IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($target))
        [IO.Compression.ZipFileExtensions]::ExtractToFile($entry,$target,$false)
    }
} finally { $zip.Dispose() }
