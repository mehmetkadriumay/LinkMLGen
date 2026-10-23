import { defineConfig } from '@playwright/test';

export default defineConfig({
  testDir: '.\\tests',
  fullyParallel: false,
  timeout: 60_000,
  expect: { timeout: 30_000 },
  use: { baseURL: 'http://127.0.0.1:5173', headless: true },
  webServer: [
    {
      command: '.venv\\Scripts\\python.exe -m uvicorn app:app --app-dir backend --host 127.0.0.1 --port 8000',
      url: 'http://127.0.0.1:8000/api/health',
      timeout: 120_000,
    },
    { command: 'npm run dev -- --port 5173 --strictPort', url: 'http://127.0.0.1:5173', timeout: 60_000 },
  ],
});
