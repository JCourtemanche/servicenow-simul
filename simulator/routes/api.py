"""
ServiceNow REST Table API — the endpoints the XSIAM ServiceNow v2 pack calls.

Envelope: `{"result": <object|array>}` on success.
Errors:   HTTP 4xx/5xx with `{"error": {"message": "...", "detail": "..."}, "status": "failure"}`.
"""
import re
import logging
from flask import Blueprint, jsonify, request

from auth import require_basic_auth
from config import Config
from generators import incidents, journal
from generators.base import parse_snow_datetime

api_bp = Blueprint('api', __name__, url_prefix='/api/now')
_log = logging.getLogger(__name__)


# `sysparm_*` and other reserved keys — anything NOT in this set found in
# request.args on a GET /table/<x> is treated as a direct field filter.
# This is what the pack sends via `client.get(...)` when number/custom_fields
# is set (see integration-ServiceNowv2.yml line 2832-2840).
_RESERVED_PARAMS = {
    'sysparm_query', 'sysparm_limit', 'sysparm_offset', 'sysparm_fields',
    'sysparm_display_value', 'sysparm_exclude_reference_link',
    'sysparm_view', 'sysparm_no_count', 'sysparm_input_display_value',
    'sysparm_suppress_pagination_header', 'sysparm_query_category',
    'sysparm_query_no_domain',
}

# When a broken/mangled URL puts a real path fragment inside a query value
# (e.g. `sysparm_limit=2/api/now/table/incident/INC0000001`), try to recover
# the target sys_id or number from any such value.
_EMBEDDED_TARGET_RE = re.compile(r'/table/[^/]+/([A-Za-z0-9_-]+)')


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
    raw = request.args.get(name, default)
    try:
        return int(raw)
    except (TypeError, ValueError):
        _log.warning("Non-integer %s=%r received, falling back to %r", name, raw, default)
        return default


def _is_supported_ticket_table(table):
    return table == Config.TICKET_TYPE


def _bare_field_filters():
    """
    Return {field: value} for any GET query params that are NOT the reserved
    sysparm_* set. Pack `Client.get()` at line 2837 does this for `number`
    and line 2839-2840 for `custom_fields`.
    """
    return {k: v for k, v in request.args.items() if k not in _RESERVED_PARAMS}


def _resolve_target_from_body_or_url(body):
    """
    Recover a sys_id or number when a client PATCHes/DELETEs on the
    collection endpoint instead of `/table/<x>/<sys_id>`.

    Checks (in order):
      1. body['sys_id'] / body['number']
      2. embedded '/table/<x>/<target>' inside any query-param value
    Returns a ticket dict if a match is found, else None.
    """
    if isinstance(body, dict):
        if body.get('sys_id'):
            t = incidents.get_by_sys_id(body['sys_id'])
            if t:
                return t
        if body.get('number'):
            t = incidents.get_by_number(body['number'])
            if t:
                return t

    for v in request.args.values():
        m = _EMBEDDED_TARGET_RE.search(v or '')
        if not m:
            continue
        target = m.group(1)
        t = incidents.get_by_sys_id(target) or incidents.get_by_number(target)
        if t:
            return t
    return None


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
    bare_filters = _bare_field_filters()

    # sys_journal_field — comments/work_notes store
    if table == 'sys_journal_field':
        parsed = _parse_journal_query(sysparm_query)
        rows = journal.query_entries(
            element_id=parsed.get('element_id') or bare_filters.get('element_id'),
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

    # Direct-param shortcut: `?number=INC0000001` (pack v2 line 2837, when
    # the caller passed `number=...` to servicenow-get-ticket) — must return
    # ONLY that ticket, else the caller picks result[0] and mirrors the
    # wrong record.
    if 'number' in bare_filters:
        t = incidents.get_by_number(bare_filters['number'])
        return _ok([t] if t else [])

    if 'sys_id' in bare_filters:
        t = incidents.get_by_sys_id(bare_filters['sys_id'])
        return _ok([t] if t else [])

    # `sysparm_query=number=INC0000001` short-circuit for completeness
    m = re.match(r'^number=([\w-]+)$', sysparm_query.strip())
    if m:
        t = incidents.get_by_number(m.group(1))
        return _ok([t] if t else [])

    # Generic filter: apply sysparm_query AND every remaining bare field
    fields = sysparm_fields or None
    results = incidents.query_tickets(sysparm_query, limit=1000, offset=0, fields=None)
    if bare_filters:
        results = [t for t in results if all(str(t.get(k, '')) == v for k, v in bare_filters.items())]
    page = results[offset:offset + limit]
    if fields:
        wanted = [f.strip() for f in fields.split(',') if f.strip()]
        page = [{f: t.get(f, '') for f in wanted} for t in page]
    return _ok(page)


@api_bp.route('/table/<table>/<sys_id>', methods=['GET'])
@require_basic_auth
def get_record(table, sys_id):
    if not _is_supported_ticket_table(table):
        _log.info("get_record: unsupported table=%s, returning 404", table)
        return _err('No Record found', 404)
    t = incidents.get_by_sys_id(sys_id)
    if t:
        _log.info("get_record: matched by sys_id=%s → number=%s", sys_id, t.get('number'))
        return _ok(t)
    # Fallback: the caller may have passed a `number` (INC…) where sys_id
    # was expected. Happens with servicenow-get-ticket / update-ticket
    # id="INC…" (pack `Client.get/update(record_id="INC…")`).
    t = incidents.get_by_number(sys_id)
    if t:
        _log.warning("get_record: sys_id=%s not found, matched via number fallback → number=%s sys_id=%s",
                     sys_id, t.get('number'), t.get('sys_id'))
        return _ok(t)
    _log.warning("get_record: sys_id=%s not found (neither by sys_id nor by number), returning 404", sys_id)
    return _err('No Record found', 404)


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

    # Try sys_id, then number (see get_record fallback rationale)
    if incidents.get_by_sys_id(sys_id):
        _log.info("update_record: matched by sys_id=%s", sys_id)
        t = incidents.update_ticket(sys_id, dict(body), user=user)
    else:
        by_number = incidents.get_by_number(sys_id)
        if by_number:
            _log.warning("update_record: sys_id=%s not found, matched via number fallback → number=%s sys_id=%s",
                         sys_id, by_number.get('number'), by_number.get('sys_id'))
            t = incidents.update_ticket(by_number['sys_id'], dict(body), user=user)
        else:
            _log.warning("update_record: sys_id=%s not found (neither by sys_id nor by number)", sys_id)
            t = None

    if not t:
        return _err('No Record found', 404)
    return _ok(t)


@api_bp.route('/table/<table>/<sys_id>', methods=['DELETE'])
@require_basic_auth
def delete_record(table, sys_id):
    if not _is_supported_ticket_table(table):
        return _err(f"Table '{table}' not supported by this simulator", 400)
    target_sys_id = sys_id if incidents.get_by_sys_id(sys_id) else \
        (incidents.get_by_number(sys_id) or {}).get('sys_id')
    if target_sys_id and incidents.delete_ticket(target_sys_id):
        return _ok({})
    return _err('No Record found', 404)


# ---------------------------------------------------------------------------
# Collection-level PATCH/DELETE fallback for clients with URL-construction
# bugs (e.g. XSIAM pack v2 when a nested command wrapper corrupts the URL).
# ---------------------------------------------------------------------------

@api_bp.route('/table/<table>', methods=['PATCH', 'PUT', 'DELETE'])
@require_basic_auth
def collection_mutation_fallback(table):
    if not _is_supported_ticket_table(table):
        return _err(f"Table '{table}' not supported by this simulator", 400)
    body = request.get_json(force=True, silent=True) or {}
    target = _resolve_target_from_body_or_url(body)
    if not target:
        _log.warning(
            "%s on collection %s could not resolve a target ticket. args=%r body-keys=%s",
            request.method, table, dict(request.args), list(body.keys()) if isinstance(body, dict) else '?',
        )
        return _err(
            f"{request.method} on collection endpoint requires a sys_id in the URL. "
            "Include /<sys_id> in the path, or a sys_id/number field in the body.",
            405,
        )

    _log.warning(
        "Recovered target ticket %s (%s) from malformed %s /table/%s (args=%r)",
        target.get('number'), target.get('sys_id'), request.method, table, dict(request.args),
    )
    if request.method == 'DELETE':
        incidents.delete_ticket(target['sys_id'])
        return _ok({})
    user = request.authorization.username if request.authorization else 'admin'
    t = incidents.update_ticket(target['sys_id'], dict(body), user=user)
    return _ok(t)


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
