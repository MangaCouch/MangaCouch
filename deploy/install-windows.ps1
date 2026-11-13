param(
  [Parameter(Mandatory=$true)][string]$Wheel,
  [Parameter(Mandatory=$true)][string]$InstallDirectory,
  [Parameter(Mandatory=$true)][string]$Constraints,
  [string]$PythonExecutable
)
$ErrorActionPreference = 'Stop'
$Wheel = (Resolve-Path -LiteralPath $Wheel).Path
$Constraints = (Resolve-Path -LiteralPath $Constraints).Path
if (Test-Path -LiteralPath $InstallDirectory) { throw 'Choose a new install directory.' }

function Invoke-BasePython {
  if ($PythonExecutable) { & $PythonExecutable @args } else { & py -3 @args }
}

# Validate the base interpreter before anything is written to disk.
Invoke-BasePython -c "import platform, struct, sys; assert (3, 12) <= sys.version_info < (3, 16), 'Python 3.12 through 3.15 is required'; assert struct.calcsize('P') == 8 and platform.machine().lower() in ('amd64', 'x86_64'), 'Use x64 Python 3.12 through 3.15; ARM64 and 32-bit Python are not supported by the locked dependencies'"
if ($LASTEXITCODE -ne 0) { throw 'Use x64 Python 3.12 through 3.15.' }

try {
  Invoke-BasePython -m venv $InstallDirectory
  if ($LASTEXITCODE -ne 0) { throw 'Python environment creation failed.' }
  $Python = Join-Path $InstallDirectory 'Scripts\python.exe'
  & $Python -m pip install --constraint $Constraints $Wheel
  if ($LASTEXITCODE -ne 0) { throw 'Wheel installation failed.' }
  & $Python -m pip check
  if ($LASTEXITCODE -ne 0) { throw 'Installed dependencies are inconsistent.' }
  & $Python -m mangacouch --help
  if ($LASTEXITCODE -ne 0) { throw 'CLI smoke check failed.' }
} catch {
  # Remove the half-built environment so the same directory can be retried.
  if (Test-Path -LiteralPath $InstallDirectory) {
    Remove-Item -LiteralPath $InstallDirectory -Recurse -Force -ErrorAction SilentlyContinue
  }
  throw
}
Write-Output "Installed Python: $Python"
Write-Output 'Next: from the source checkout or sdist, run scripts/check_installation.py with this Python before initializing a real library.'
