# Gunicorn configuration for advisorAPP
# Usage: gunicorn -c gunicorn.conf.py app:app

bind = "0.0.0.0:8888"
workers = 4
worker_class = "sync"
timeout = 120
keepalive = 5
accesslog = "-"
errorlog = "-"
loglevel = "info"
preload_app = True
