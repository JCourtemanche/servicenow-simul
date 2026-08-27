"""
ServiceNow XSIAM Case-Mirroring Simulator.

Exposes:
  * `/api/now/...` — the ServiceNow REST Table API endpoints hit by the
    Cortex XSIAM ServiceNow v2 pack (Basic Auth).
  * `/ui`         — a minimal browser UI to create tickets, close/reopen
    them, and add comments/work_notes (Basic Auth).
"""
import logging
import secrets
from flask import Flask, jsonify, request

from config import Config
from routes.api import api_bp
from routes.ui import ui_bp
from generators import incidents, journal, persistence
from generators.base import reset_counter
from generators.incidents import seed_tickets


def _bootstrap_state(logger):
    """
    Initialize the persistence layer, then either rehydrate the in-memory
    caches from Firestore (if there's saved state) or run seed_tickets().
    """
    persistence.init(
        backend=Config.STORAGE_BACKEND,
        project_id=Config.GCP_PROJECT_ID,
        database=Config.FIRESTORE_DATABASE,
    )

    tickets, journal_rows, next_num = persistence.load_all()
    if tickets:
        incidents.load_from_persistence(tickets)
        journal.load_from_persistence(journal_rows)
        reset_counter(next_num)
        logger.info("Restored %d tickets + %d journal rows from persistence (counter@%d)",
                    len(tickets), len(journal_rows), next_num)
        return

    if persistence.is_firestore():
        logger.info("Firestore is empty — seeding %d tickets and writing to Firestore",
                    Config.SEED_COUNT)
    else:
        logger.info("Memory backend — seeding %d tickets (no persistence)", Config.SEED_COUNT)
    seed_tickets(Config.SEED_COUNT)


def create_app():
    app = Flask(__name__)
    app.config['SECRET_KEY'] = Config.FLASK_SECRET or secrets.token_hex(16)

    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    )
    logger = logging.getLogger(__name__)
    logger.info("Starting ServiceNow XSIAM Simulator (instance=%s, table=%s)",
                Config.INSTANCE_NAME, Config.TICKET_TYPE)

    app.register_blueprint(api_bp)
    app.register_blueprint(ui_bp)

    _req_log = logging.getLogger('request')

    @app.before_request
    def _log_request():
        # Skip the noisy UI static/paths but keep everything API-side
        if request.path.startswith('/api/') or request.path.startswith('/oauth_token'):
            _req_log.info("→ %s %s?%s", request.method, request.path, request.query_string.decode('latin-1'))

    @app.after_request
    def _log_response(response):
        if request.path.startswith('/api/') or request.path.startswith('/oauth_token'):
            _req_log.info("← %s %s → %s", request.method, request.path, response.status_code)
        return response

    _bootstrap_state(logger)

    @app.route('/health', methods=['GET'])
    def health():
        return jsonify({
            'status': 'healthy',
            'service': 'servicenow-simulator',
            'version': '1.0.0',
            'instance': Config.INSTANCE_NAME,
        }), 200

    @app.route('/', methods=['GET'])
    def root():
        return jsonify({
            'service': 'ServiceNow XSIAM Case-Mirroring Simulator',
            'version': '1.0.0',
            'instance': Config.INSTANCE_NAME,
            'ticket_type': Config.TICKET_TYPE,
            'auth': 'HTTP Basic Auth',
            'endpoints': {
                'GET  /api/now/table/{table}':             'Query records (sysparm_query, sysparm_limit, sysparm_offset, sysparm_fields)',
                'GET  /api/now/table/{table}/{sys_id}':    'Get single record',
                'POST /api/now/table/{table}':             'Create record',
                'PATCH /api/now/table/{table}/{sys_id}':   'Update record (comments/work_notes go to the journal)',
                'DELETE /api/now/table/{table}/{sys_id}':  'Delete record',
                'GET  /api/now/table/sys_journal_field':   'Query comments/work_notes for a ticket',
                'GET  /api/now/attachment':                'Attachments — always [] in this simulator',
                'POST /oauth_token.do':                    'OAuth2 stub (returns fake tokens)',
                'GET  /ui':                                'Operator web UI (create/close/reopen tickets)',
                'GET  /health':                            'Health check',
            },
        }), 200

    @app.route('/oauth_token.do', methods=['POST'])
    def oauth_token():
        """Stub for the ServiceNow OAuth2 endpoint — returns fake tokens.
        Only hit if the user checks 'Use OAuth' in the XSIAM pack config."""
        return jsonify({
            'access_token': 'simulator-fake-access-token',
            'refresh_token': 'simulator-fake-refresh-token',
            'token_type': 'Bearer',
            'expires_in': 1800,
            'scope': 'useraccount',
        }), 200

    @app.errorhandler(404)
    def not_found(_error):
        return jsonify({'error': {'message': 'Endpoint not found', 'detail': request.path},
                        'status': 'failure'}), 404

    @app.errorhandler(500)
    def internal_error(error):
        logger.error("Internal server error: %s", error)
        return jsonify({'error': {'message': 'Internal server error', 'detail': str(error)},
                        'status': 'failure'}), 500

    return app


app = create_app()


if __name__ == '__main__':
    app.run(host=Config.HOST, port=Config.PORT, debug=Config.DEBUG)
