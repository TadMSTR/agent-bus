module.exports = {
  apps: [
    {
      name: 'agent-bus',
      script: 'server.py',
      interpreter: '/home/ted/repos/personal/agent-bus/venv/bin/python3',
      cwd: '/home/ted/repos/personal/agent-bus',
      autorestart: true,
      watch: false,
      max_restarts: 10,
      exp_backoff_restart_delay_ms: 5000,
      env: {
        PYTHONUNBUFFERED: '1',
        NTFY_URL: 'https://ntfy.your-domain.com/claudebox',
        NATS_URL: 'nats://localhost:4222',
      },
    },
    {
      name: 'agent-bus-reconcile',
      script: 'reconcile.py',
      interpreter: '/home/ted/repos/personal/agent-bus/venv/bin/python3',
      cwd: '/home/ted/repos/personal/agent-bus',
      cron_restart: '*/5 * * * *',
      autorestart: false,
      watch: false,
    },
    {
      name: 'agent-bus-cleanup',
      script: 'cleanup.sh',
      interpreter: 'bash',
      cwd: '/home/ted/repos/personal/agent-bus',
      cron_restart: '50 3 * * *',
      autorestart: false,
      watch: false,
    },
  ],
};
