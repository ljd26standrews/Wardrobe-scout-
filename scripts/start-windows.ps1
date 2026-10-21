# Windows (PowerShell): run from the repo root after setup (see README → Windows).
$root = Split-Path -Parent $PSScriptRoot
Start-Process -NoNewWindow -FilePath "$root\backend\.venv\Scripts\uvicorn.exe" -ArgumentList "app.main:app --host 127.0.0.1 --port 8000" -WorkingDirectory "$root\backend"
Set-Location "$root\frontend"
npx next start -p 3000 -H 127.0.0.1
