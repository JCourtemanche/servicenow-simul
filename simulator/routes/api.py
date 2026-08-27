"""
ServiceNow REST Table API — the endpoints the XSIAM ServiceNow v2 pack calls.

Envelope: `{"result": <object|array>}` on success.
Errors:   HTTP 4xx/5xx with `{"error": {"message": "...", "detail": "..."}, "status": "failure"}`.
"""
import re
from flask import Blueprint, jsonify, request

from auth import require_basic_auth
from config import Config
from generators import incidents, journal
from generators.base import parse_snow_datetime

api_bp = Blueprint('api', __name__, url_prefix='/api/now')


# ---------------------------------------------------------------------------
# Response helpers
# ---------------------------------------------------------------------------

def _ok(data, status=200):
    return jsonify({'result': data}), status


def _err(message, status=400, detail=None):
    return jsonify({
        'error': {'message': message, 'detail': detail or message},
        'status': 'failure',
    }), status


def _int_param(name, default):
    try:
        return int(request.args.get(name, default))
    except (TypeError, ValueError):
        return default


def _is_supported_ticket_table(table):
    return table == Config.TICKET_TYPE


# ---------------------------------------------------------------------------
# Table CRUD — /api/now/table/<table_name>
# ---------------------------------------------------------------------------

@api_bp.route('/table/<table>', methods=['GET'])
@require_basic_auth
def query_table(table):
    limit = max(1, min(_int_param('sysparm_limit', 10), 1000))
    offset = max(0, _int_param('sysparm_offset', 0))
    sysparm_query = request.args.get('sysparm_query', '') or ''
    sysparm_fields = request.args.get('sysparm_fields', '') or ''

    # sys_journal_field — comments/work_notes store
    if table == 'sys_journal_field':
        parsed = _parse_journal_query(sysparm_query)
        rows = journal.query_entries(
            element_id=parsed.get('element_id'),
            elements=parsed.get('elements'),
            created_after=parsed.get('created_after'),
            limit=limit,
            offset=offset,
        )
        return _ok(rows)

    # Only the configured ticket type is truly backed by a store; anything
    # else returns an empty list so unrelated integration commands don't blow up.
    if not _is_supported_ticket_table(table):
        return _ok([])

    # `number=INC0000001` short-circuit — returns a list of one, per SNOW behaviour
    m = re.match(r'^number=([\w-]+)$', sysparm_query.strip())
    if m:
        t = incidents.get_by_number(m.group(1))
        return _ok([t] if t else [])

    fields = sysparm_fields or None
    return _ok(incidents.query_tickets(sysparm_query, limit=limit, offset=offset, fields=fields))


@api_bp.route('/table/<table>/<sys_id>', methods=['GET'])
@require_basic_auth
def get_record(table, sys_id):
    if not _is_supported_ticket_table(table):
        return _err('No Record found', 404)
    t = incidents.get_by_sys_id(sys_id)
    if not t:
        return _err('No Record found', 404)
    return _ok(t)


@api_bp.route('/table/<table>', methods=['POST'])
@require_basic_auth
def create_record(table):
    if not _is_supported_ticket_table(table):
        return _err(f"Table '{table}' not supported by this simulator", 400)
    body = request.get_json(force=True, silent=True) or {}
    user = request.authorization.username if request.authorization else 'admin'
    body.setdefault('opened_by', user)
    t = incidents.create_ticket(body)
    return _ok(t, status=201)


@api_bp.route('/table/<table>/<sys_id>', methods=['PATCH', 'PUT'])
@require_basic_auth
def update_record(table, sys_id):
    if not _is_supported_ticket_table(table):
        return _err(f"Table '{table}' not supported by this simulator", 400)
    body = request.get_json(force=True, silent=True) or {}
    user = request.authorization.username if request.authorization else 'admin'
    t = incidents.update_ticket(sys_id, dict(body), user=user)
    if not t:
        return _err('No Record found', 404)
    return _ok(t)


@api_bp.route('/table/<table>/<sys_id>', methods=['DELETE'])
@require_basic_auth
def delete_record(table, sys_id):
    if not _is_supported_ticket_table(table):
        return _err(f"Table '{table}' not supported by this simulator", 400)
    if incidents.delete_ticket(sys_id):
        return _ok({})
    return _err('No Record found', 404)


# ---------------------------------------------------------------------------
# Attachments — /api/now/attachment (list only; MVP)
# ---------------------------------------------------------------------------

@api_bp.route('/attachment', methods=['GET'])
@require_basic_auth
def list_attachments():
    return _ok([])


@api_bp.route('/attachment/<sys_id>', methods=['GET', 'DELETE'])
@require_basic_auth
def attachment_by_id(sys_id):
    if request.method == 'DELETE':
        return _ok({})
    return _err('No Record found', 404)


# ---------------------------------------------------------------------------
# sys_journal_field query parser
# ---------------------------------------------------------------------------

_JOURNAL_ELEMENT_RE = re.compile(r'element=([\w_]+)')
_JOURNAL_ELEMENT_ID_RE = re.compile(r'element_id=([a-f0-9]+)')
_JOURNAL_CREATED_RE = re.compile(r'sys_created_on>([\d\- :]+)')


def _parse_journal_query(sysparm_query):
    """
    The pack sends queries like:
      element_id=<sys_id>^element=comments^ORelement=work_notes^sys_created_on><date>
    We extract the 3 filter dimensions independently.
    """
    result = {'element_id': None, 'elements': None, 'created_after': None}
    if not sysparm_query:
        return result

    el_id_m = _JOURNAL_ELEMENT_ID_RE.search(sysparm_query)
    if el_id_m:
        result['element_id'] = el_id_m.group(1)

    elements = _JOURNAL_ELEMENT_RE.findall(sysparm_query)
    if elements:
        result['elements'] = elements

    created_m = _JOURNAL_CREATED_RE.search(sysparm_query)
    if created_m:
        result['created_after'] = parse_snow_datetime(created_m.group(1).strip())

    return result
