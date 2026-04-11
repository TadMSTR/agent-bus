const path = require('path');

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
