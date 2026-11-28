import { defineConfig } from "@playwright/test";
import { fileURLToPath } from "node:url";

const root = fileURLToPath(new URL("../", import.meta.url));
const python = fileURLToPath(new URL(process.platform === "win32" ? "../.venv/Scripts/python.exe" : "../.venv/bin/python", import.meta.url));
const backendPort = Number(process.env.DASHBOARD_TEST_BACKEND_PORT || 8002);
const backend = `http://127.0.0.1:${backendPort}`;

export default defineConfig({
  testDir: "./tests",
  timeout: 30000,
  use: {
    baseURL: "http://127.0.0.1:5174",
    headless: true,
    channel: process.env.PLAYWRIGHT_CHANNEL,
  },
  webServer: [
    {
      command: `"${python}" -m uvicorn self_improving_ai.dashboard.app:app --host 127.0.0.1 --port ${backendPort}`,
      env: {PYTHONPATH: `${root}/src`, PYTHONUTF8: "1"},
      url: `${backend}/api/health`,
      reuseExistingServer: false,
    },
    {
      command: `"${process.execPath}" node_modules/vite/bin/vite.js --host 127.0.0.1 --port 5174 --strictPort`,
      env: {COMPONENT_A_API_TARGET:backend},
      url: "http://127.0.0.1:5174",
      reuseExistingServer: false,
    },
  ],
});
