"""HTTP Basic Auth guard for both the REST API and the /ui web UI."""
from functools import wraps
from flask import request, jsonify, Response
from config import Config


def require_basic_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        auth = request.authorization
        if not auth or auth.username != Config.AUTH_USERNAME or auth.password != Config.AUTH_PASSWORD:
            # ServiceNow-style JSON error for API clients; browsers just re-prompt
            body = {'error': {'message': 'User Not Authenticated', 'detail': 'Required to provide Auth information'},
                    'status': 'failure'}
            return Response(
                response=jsonify(body).get_data(),
                status=401,
                mimetype='application/json',
                headers={'WWW-Authenticate': 'Basic realm="ServiceNow Simulator"'},
            )
        return f(*args, **kwargs)
    return decorated
