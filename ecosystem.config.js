module.exports = {
  apps: [
    {
      name: 'radiotify-redis',
      script: 'redis-server',
      args: '--daemonize no',
      interpreter: 'none',
      autorestart: true,
      watch: false,
    },
    {
      name: 'radiotify-backend',
      script: '/Users/alfian/project/radiotify/backend/venv/bin/python',
      args: 'run.py',
      cwd: '/Users/alfian/project/radiotify/backend',
      interpreter: 'none',
      autorestart: true,
      watch: false,
      env: {
        REDIS_URL: 'redis://localhost:6379/0',
      },
    },
    {
      name: 'radiotify-frontend',
      script: 'npm',
      args: 'run preview -- --port 3000 --host',
      cwd: '/Users/alfian/project/radiotify/frontend',
      interpreter: 'none',
      autorestart: true,
      watch: false,
    },
  ],
};
