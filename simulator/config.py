import os


class Config:
    DEBUG = os.environ.get('DEBUG', 'True').lower() == 'true'
    HOST = os.environ.get('HOST', '0.0.0.0')
    PORT = int(os.environ.get('PORT', 8080))

    # HTTP Basic Auth — same creds protect both the REST API and the /ui web UI
    AUTH_USERNAME = os.environ.get('AUTH_USERNAME', 'admin')
    AUTH_PASSWORD = os.environ.get('AUTH_PASSWORD', 'admin')

    # ServiceNow-specific
    TICKET_TYPE = os.environ.get('SNOW_TICKET_TYPE', 'incident')
    SEED_COUNT = int(os.environ.get('SEED_COUNT', 5))
    INSTANCE_NAME = os.environ.get('SNOW_INSTANCE_NAME', 'demo-instance')

    # Flask session — used for /ui flash messages
    FLASK_SECRET = os.environ.get('FLASK_SECRET', '')
