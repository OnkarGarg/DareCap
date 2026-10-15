$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

Remove-Item -Recurse -Force build, loader.zip -ErrorAction SilentlyContinue
New-Item -ItemType Directory build | Out-Null

python -m pip install pymysql tzdata -t build
if ($LASTEXITCODE -ne 0) { throw "pip install failed" }

Copy-Item loader.py, load.py, global-bundle.pem build\
Compress-Archive -Path build\* -DestinationPath loader.zip