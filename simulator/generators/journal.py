"""
In-memory store for the ServiceNow `sys_journal_field` table.

Every row represents a single comment or work_note entry on a ticket.
The XSIAM ServiceNow pack's `get-remote-data` command reads from this table
(not from the ticket's `comments` field directly) to pull mirror-in updates.

Writes to `comments` / `work_notes` on a ticket PATCH append rows here.
"""
import threading
from datetime import datetime
from .base import snow_sys_id, snow_datetime, parse_snow_datetime

_JOURNAL = []
_LOCK = threading.Lock()


def add_entry(element_id, element, value, user='admin'):
    """
    Append a journal row. `element` must be 'comments' or 'work_notes'.
    Returns the row dict.
    """
    row = {
        'sys_id': snow_sys_id(),
        'sys_created_on': snow_datetime(),
        'sys_created_by': user,
        'element_id': element_id,
        'element': element,
        'value': value,
        'name': 'incident',
    }
    with _LOCK:
        _JOURNAL.append(row)
    return row


def query_entries(element_id=None, elements=None, created_after=None, limit=10, offset=0):
    """
    Filter journal rows by ticket sys_id, element type (comments/work_notes),
    and creation date. `elements` is a list; pass None for no filter.
    `created_after` is a datetime or None.
    """
    with _LOCK:
        rows = list(_JOURNAL)
    if element_id:
        rows = [r for r in rows if r['element_id'] == element_id]
    if elements:
        rows = [r for r in rows if r['element'] in elements]
    if created_after:
        rows = [r for r in rows if (parse_snow_datetime(r['sys_created_on']) or datetime.min) > created_after]
    return rows[offset:offset + limit]


def reset():
    """Test helper — clear the journal."""
    with _LOCK:
        _JOURNAL.clear()
