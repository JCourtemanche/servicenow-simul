"""
In-memory store of ServiceNow `incident` records + minimal query-parser.

The store is a `sys_id -> dict` map protected by a lock. Every mutation
touches `sys_updated_on` so the XSIAM `get-modified-remote-data` command
picks up the change on its next poll.

The query parser implements only the 4 sysparm_query patterns the XSIAM
ServiceNow v2 pack actually sends:
  * sys_updated_on><date>                       (mirror trigger)
  * element_id=<x>^element=comments^ORelement=work_notes^sys_created_on><date>
  * number=INC0000001                           (get-ticket by number)
  * table_sys_id=<sys_id>                       (attachments — trivial, no store)
"""
import random
import re
import threading
from datetime import datetime, timedelta

from . import journal, persistence
from .base import (
    snow_sys_id,
    snow_number,
    snow_datetime,
    parse_snow_datetime,
    reset_counter,
    random_user,
    random_malicious_ip,
    random_malicious_url,
)

try:
    from xsiam_shared import MALICIOUS_FILES
except ImportError:
    MALICIOUS_FILES = [{'name': 'malware.exe', 'hash': 'a' * 64}]


_TICKETS = {}
_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# State machine — values match ServiceNow `incident.state` numeric codes
# ---------------------------------------------------------------------------
STATE_NEW = '1'
STATE_IN_PROGRESS = '2'
STATE_ON_HOLD = '3'
STATE_AWAITING_CALLER = '4'
STATE_AWAITING_EVIDENCE = '5'
STATE_RESOLVED = '6'
STATE_CLOSED = '7'
STATE_CANCELED = '8'

CLOSED_STATES = {STATE_RESOLVED, STATE_CLOSED, STATE_CANCELED}

STATE_LABELS = {
    STATE_NEW: 'New',
    STATE_IN_PROGRESS: 'In Progress',
    STATE_ON_HOLD: 'On Hold',
    STATE_AWAITING_CALLER: 'Awaiting Caller',
    STATE_AWAITING_EVIDENCE: 'Awaiting Evidence',
    STATE_RESOLVED: 'Resolved',
    STATE_CLOSED: 'Closed',
    STATE_CANCELED: 'Canceled',
}


# ---------------------------------------------------------------------------
# Ticket factory
# ---------------------------------------------------------------------------

def _blank_ticket():
    """Empty record with every field the XSIAM pack expects to read."""
    return {
        'sys_id': '',
        'number': '',
        'sys_created_on': '',
        'sys_updated_on': '',
        'sys_updated_by': 'admin',
        'opened_at': '',
        'closed_at': '',
        'resolved_at': '',
        'short_description': '',
        'description': '',
        'state': STATE_NEW,
        'incident_state': STATE_NEW,
        'close_code': '',
        'close_notes': '',
        'assigned_to': '',
        'assignment_group': '',
        'caller_id': '',
        'opened_by': 'admin',
        'priority': '3',
        'urgency': '3',
        'severity': '3',
        'impact': '3',
        'category': 'inquiry',
        'subcategory': '',
        'active': 'true',
        'reopen_count': '0',
        'sys_tags': '',
        'correlation_id': '',
        'correlation_display': '',
        'contact_type': 'self-service',
    }


def create_ticket(fields=None):
    """Insert a new ticket. Returns the stored dict."""
    fields = fields or {}
    now = snow_datetime()
    ticket = _blank_ticket()
    ticket.update({
        'sys_id': snow_sys_id(),
        'number': snow_number(),
        'sys_created_on': now,
        'sys_updated_on': now,
        'opened_at': now,
        'active': 'true',
    })
    # Merge caller-provided fields, but never let them override sys_id/number
    for k, v in fields.items():
        if k in ('sys_id', 'number'):
            continue
        ticket[k] = v if v is not None else ''

    # If created already-closed, stamp the close/resolve timestamps
    if ticket.get('state') in CLOSED_STATES:
        _apply_close_side_effects(ticket, now)

    with _LOCK:
        _TICKETS[ticket['sys_id']] = ticket
    persistence.save_ticket(ticket['sys_id'], ticket)
    return ticket


def _apply_close_side_effects(ticket, now):
    ticket['closed_at'] = ticket.get('closed_at') or now
    ticket['resolved_at'] = ticket.get('resolved_at') or now
    ticket['active'] = 'false'
    ticket['close_code'] = ticket.get('close_code') or 'Solved (Permanently)'
    ticket['close_notes'] = ticket.get('close_notes') or 'Closed'
    ticket['incident_state'] = ticket['state']


def _apply_reopen_side_effects(ticket):
    ticket['closed_at'] = ''
    ticket['resolved_at'] = ''
    ticket['close_code'] = ''
    ticket['close_notes'] = ''
    ticket['active'] = 'true'
    ticket['incident_state'] = ticket['state']
    try:
        ticket['reopen_count'] = str(int(ticket.get('reopen_count') or '0') + 1)
    except ValueError:
        ticket['reopen_count'] = '1'


def update_ticket(sys_id, delta, user='admin'):
    """
    Apply a PATCH delta. `comments` / `work_notes` in the delta are
    appended to the journal (not stored on the ticket row) — mirrors real
    ServiceNow behaviour.
    Returns the updated ticket dict, or None if not found.
    """
    with _LOCK:
        ticket = _TICKETS.get(sys_id)
        if not ticket:
            return None

        previously_closed = ticket.get('state') in CLOSED_STATES

        # Journal fields — split out, never stored on the ticket
        for journal_field in ('comments', 'work_notes'):
            if journal_field in delta:
                value = delta.pop(journal_field)
                if value:
                    journal.add_entry(sys_id, journal_field, str(value), user=user)

        # Apply the remaining flat delta
        for k, v in delta.items():
            ticket[k] = v if v is not None else ''

        now = snow_datetime()
        ticket['sys_updated_on'] = now
        ticket['sys_updated_by'] = user

        now_closed = ticket.get('state') in CLOSED_STATES
        if now_closed and not previously_closed:
            _apply_close_side_effects(ticket, now)
        elif previously_closed and not now_closed:
            _apply_reopen_side_effects(ticket)

    # Persistence write is outside the lock to avoid blocking readers on
    # network I/O. The dict passed to Firestore is captured by-reference,
    # but the SDK snapshots it synchronously.
    persistence.save_ticket(ticket['sys_id'], ticket)
    return ticket


def close_ticket(sys_id, close_code='Solved (Permanently)', close_notes='', user='admin'):
    return update_ticket(
        sys_id,
        {'state': STATE_CLOSED, 'close_code': close_code, 'close_notes': close_notes},
        user=user,
    )


def reopen_ticket(sys_id, user='admin'):
    return update_ticket(sys_id, {'state': STATE_IN_PROGRESS}, user=user)


def delete_ticket(sys_id):
    with _LOCK:
        removed = _TICKETS.pop(sys_id, None)
    if removed:
        persistence.delete_ticket(sys_id)
    return removed


def get_by_sys_id(sys_id):
    with _LOCK:
        return _TICKETS.get(sys_id)


def get_by_number(number):
    with _LOCK:
        for t in _TICKETS.values():
            if t.get('number') == number:
                return t
    return None


def list_tickets():
    """Return tickets sorted newest-first (by sys_created_on desc)."""
    with _LOCK:
        return sorted(
            _TICKETS.values(),
            key=lambda t: t.get('sys_created_on', ''),
            reverse=True,
        )


def reset():
    """Clear the in-memory store only (does not touch persistence)."""
    with _LOCK:
        _TICKETS.clear()


def load_from_persistence(tickets_by_sysid):
    """Rehydrate the in-memory cache from a pre-loaded map (startup only)."""
    with _LOCK:
        _TICKETS.clear()
        _TICKETS.update(tickets_by_sysid)


def count_tickets():
    with _LOCK:
        return len(_TICKETS)


# ---------------------------------------------------------------------------
# Seed data
# ---------------------------------------------------------------------------

_SEED_TEMPLATES = [
    ('Suspicious outbound traffic detected', 'EDR flagged outbound connections to a known-malicious host from {host} ({ip}).'),
    ('Malware quarantined on endpoint', 'Antivirus quarantined {file} on {host}. User: {user}.'),
    ('Failed login burst', 'Auth logs show >20 failed logins for {user}@{domain} in the last 5 minutes.'),
    ('Phishing email reported by user', '{user} reported a phishing email with a link to {url}.'),
    ('Anomalous data-transfer volume', 'DLP flagged 1.2 GB uploaded from {host} to an external SaaS.'),
    ('Ransomware pre-cursor behaviour', 'Volume shadow copy deletion attempted on {host} by unknown process.'),
    ('Unauthorized privilege escalation', 'User {user} added themselves to Domain Admins on {domain}.'),
]


def seed_tickets(count=5, seed=42):
    """
    Insert `count` fake tickets. Idempotent — clears the store first.

    Uses a fixed RNG seed so every restart produces identical seed data.
    Without this, two Cloud Run workers would each generate different
    tickets (same numbers, different descriptions/users) and the UI would
    show inconsistent state across page refreshes.
    """
    reset()
    journal.reset()

    rng = random.Random(seed)
    from xsiam_shared import USERS  # local import to avoid cycles
    users = list(USERS) if USERS else [{'email': 'anon@business.org', 'hostname': 'BSNS-UNKNOWN', 'internal_ip': '192.168.1.1'}]

    now = datetime.utcnow()
    for i in range(count):
        template_short, template_desc = _SEED_TEMPLATES[i % len(_SEED_TEMPLATES)]
        user = users[i % len(users)]
        mal_file = MALICIOUS_FILES[i % len(MALICIOUS_FILES)] if MALICIOUS_FILES else {'name': 'evil.exe'}
        ctx = {
            'user': user.get('email', 'unknown'),
            'host': user.get('hostname', 'BSNS-HOST'),
            'ip': user.get('internal_ip') or '192.168.1.1',
            'file': mal_file.get('name', 'evil.exe'),
            'url': random_malicious_url(),
            'domain': 'business.org',
        }
        created = now - timedelta(hours=rng.randint(1, 72))
        priority = str(rng.choice([1, 2, 3, 3, 4]))

        state = STATE_NEW if i < max(1, count - 1) else STATE_IN_PROGRESS

        create_ticket({
            'short_description': template_short,
            'description': template_desc.format(**ctx),
            'state': state,
            'priority': priority,
            'urgency': priority,
            'severity': priority,
            'impact': priority,
            'caller_id': ctx['user'],
            'assigned_to': users[(i + 1) % len(users)].get('email', ''),
            'category': ['security', 'inquiry', 'network', 'software'][i % 4],
            'sys_created_on': snow_datetime(created),
            'sys_updated_on': snow_datetime(created),
            'opened_at': snow_datetime(created),
        })


# ---------------------------------------------------------------------------
# Minimal sysparm_query parser
# ---------------------------------------------------------------------------

_CLAUSE_RE = re.compile(r'^(\w+)(!=|>=|<=|LIKE|=|>|<)(.*)$')


def _match_clause(ticket, field, op, value):
    tval = ticket.get(field, '')
    # Date comparisons — try to parse both sides as datetime
    dt_ticket = parse_snow_datetime(str(tval)) if op in ('>', '<', '>=', '<=') else None
    dt_query = parse_snow_datetime(value) if op in ('>', '<', '>=', '<=') else None
    if dt_ticket and dt_query:
        if op == '>':  return dt_ticket > dt_query
        if op == '<':  return dt_ticket < dt_query
        if op == '>=': return dt_ticket >= dt_query
        if op == '<=': return dt_ticket <= dt_query

    # String comparisons (case-sensitive for =, insensitive for LIKE)
    tstr = str(tval)
    if op == '=':    return tstr == value
    if op == '!=':   return tstr != value
    if op == 'LIKE': return value.lower() in tstr.lower()
    if op == '>':    return tstr > value
    if op == '<':    return tstr < value
    if op == '>=':   return tstr >= value
    if op == '<=':   return tstr <= value
    return False


def match_query(ticket, sysparm_query):
    """
    Return True iff the ticket matches the sysparm_query mini-language.
    Groups joined by `^OR` are OR'd; clauses within a group are AND'd.
    Empty / None query matches everything.
    """
    if not sysparm_query:
        return True
    # ^OR is the group separator; ^ within a group is AND
    or_groups = sysparm_query.split('^OR')
    for group in or_groups:
        clauses = [c for c in group.split('^') if c]
        if not clauses:
            continue
        if all(_match_and_clause(ticket, c) for c in clauses):
            return True
    return False


def _match_and_clause(ticket, clause_str):
    m = _CLAUSE_RE.match(clause_str)
    if not m:
        return False
    field, op, value = m.group(1), m.group(2), m.group(3)
    return _match_clause(ticket, field, op, value)


def query_tickets(sysparm_query='', limit=100, offset=0, fields=None):
    """
    Filter, paginate, and optionally project. `fields` is a comma-separated
    string like 'sys_id,number' (ServiceNow's sysparm_fields).
    """
    all_tickets = list_tickets()
    matched = [t for t in all_tickets if match_query(t, sysparm_query)]
    page = matched[offset:offset + limit]
    if fields:
        wanted = [f.strip() for f in fields.split(',') if f.strip()]
        page = [{f: t.get(f, '') for f in wanted} for t in page]
    return page
