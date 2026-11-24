import {defineConfig} from '@playwright/test';
import {fileURLToPath} from 'node:url';
const root=fileURLToPath(new URL('../',import.meta.url));
export default defineConfig({
  testDir:'./tests',testMatch:['learning.spec.js','portfolio.spec.js'],workers:1,
  use:{baseURL:'http://127.0.0.1:8003',headless:true,channel:'chrome'},
  webServer:{command:`"${process.env.PORTFOLIO_TEST_PYTHON||'python'}" -m uvicorn self_improving_ai.dashboard.app:app --host 127.0.0.1 --port 8003`,
    cwd:root,url:'http://127.0.0.1:8003/api/health',reuseExistingServer:false,
    env:{PYTHONPATH:`${root}/src`,SIA_PORTFOLIO_LEARNING_DIR:`${root}/artifacts/browser-learning`,SIA_PORTFOLIO_LEARNER_FACTORY:'',PYTHONUTF8:'1'}}
});
