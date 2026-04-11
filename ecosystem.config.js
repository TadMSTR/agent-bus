const path = require('path');
const fs = require('fs');

// Read .env so PM2 can inject values into the process env block.
// This avoids hardcoding secrets while still making them available
// on a fresh `pm2 start ecosystem.config.js` (not just cached reloads).
const envVars = {};
const envFile = path.join(__dirname, '.env');
if (fs.existsSync(envFile)) {
  fs.readFileSync(envFile, 'utf-8').split('\n').forEach(line => {
    const m = line.match(/^([^#\s][^=]*)=(.*)/);
    if (m) envVars[m[1].trim()] = m[2].trim();
  });
}

module.exports = {
  apps: [
    {
      name: 'agent-bus',
      script: 'server.py',
      interpreter: path.join(__dirname, 'venv/bin/python3'),
      cwd: __dirname,
      autorestart: true,
      watch: false,
      max_restarts: 10,
      exp_backoff_restart_delay_ms: 5000,
      env: {
        PYTHONUNBUFFERED: '1',
        NTFY_URL: envVars.NTFY_URL || '',
        NATS_URL: envVars.NATS_URL || 'nats://localhost:4222',
      },
    },
    {
      name: 'agent-bus-reconcile',
      script: 'reconcile.py',
      interpreter: path.join(__dirname, 'venv/bin/python3'),
      cwd: __dirname,
      cron_restart: '*/5 * * * *',
      autorestart: false,
      watch: false,
    },
    {
      name: 'agent-bus-cleanup',
      script: 'cleanup.sh',
      interpreter: 'bash',
      cwd: __dirname,
      cron_restart: '50 3 * * *',
      autorestart: false,
      watch: false,
    },
  ],
};
