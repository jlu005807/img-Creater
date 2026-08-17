# Gallery E2E

The browser test is skipped during the normal Python discovery run. To run it,
install the existing environment's Chromium browser once, start the Vite
frontend, and opt in explicitly:

```powershell
.\.venv\Scripts\python.exe -m playwright install chromium
$env:RUN_FRONTEND_E2E = "1"
.\.venv\Scripts\python.exe -m unittest tests.e2e.test_frontend_gallery_batch_download
```

The test expects the frontend at `http://127.0.0.1:5173`. A convenient
server-managed invocation is:

```powershell
$env:RUN_FRONTEND_E2E = "1"
.\.venv\Scripts\python.exe C:\Users\12894\.agents\skills\webapp-testing\scripts\with_server.py `
  --server "npm.cmd run --prefix frontend dev -- --host 127.0.0.1" `
  --port 5173 `
  -- .\.venv\Scripts\python.exe -m unittest tests.e2e.test_frontend_gallery_batch_download
```

The route mock supplies two cursor pages and tiny PNG responses, so the test
does not depend on a running backend or external image hosts.
