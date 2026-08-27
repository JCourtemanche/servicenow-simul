"""
Optional persistence backend for the ticket store.

Selected via env `STORAGE_BACKEND`:
  * `memory` (default) — everything stays in-process; state is lost on redeploy
    or Cloud Run cold start. Fine for local dev.
  * `firestore` — writes through to Google Cloud Firestore (Native mode).
    Requires the runtime service account to have `roles/datastore.user`
    on the target GCP project.

The module fails soft: if Firestore is selected but the SDK / creds / DB
aren't available, we log a warning and fall back to memory silently so
the simulator never crashes at boot.

Collections used (only two):
  * `sn_tickets`         — one doc per ticket, doc_id = ticket sys_id
  * `sn_journal`         — one doc per comment/work_note, doc_id = row sys_id
  * `sn_counters/number` — single doc storing {next: <int>} for INC counter
"""
import logging
import os

_log = logging.getLogger(__name__)

_TICKETS_COL = 'sn_tickets'
_JOURNAL_COL = 'sn_journal'
_COUNTERS_COL = 'sn_counters'
_NUMBER_DOC = 'number'

_client = None
_backend = None  # 'memory' or 'firestore'; set by init()


def init(backend='memory', project_id='', database='(default)'):
    """Initialize the persistence layer once at app startup."""
    global _client, _backend
    _backend = 'memory'

    if backend != 'firestore':
        _log.info("persistence: backend=memory (no external storage)")
        return

    try:
        from google.cloud import firestore
    except ImportError:
        _log.warning("persistence: google-cloud-firestore not installed; falling back to memory")
        return

    try:
        kwargs = {}
        if project_id:
            kwargs['project'] = project_id
        if database and database != '(default)':
            kwargs['database'] = database
        _client = firestore.Client(**kwargs)
        # Cheap round-trip to confirm the DB really exists and IAM works
        _ = _client.collection(_COUNTERS_COL).document(_NUMBER_DOC).get()
        _backend = 'firestore'
        _log.info("persistence: backend=firestore project=%s db=%s",
                  _client.project, database)
    except Exception as exc:
        _log.warning(
            "persistence: firestore init failed (%s); falling back to memory. "
            "Check that (1) the Firestore API is enabled, (2) a database exists "
            "in native mode, (3) the runtime SA has roles/datastore.user.",
            exc,
        )
        _client = None


def is_firestore():
    return _backend == 'firestore'


# ---------------------------------------------------------------------------
# Write-through operations — no-op when backend is memory
# ---------------------------------------------------------------------------

def save_ticket(sys_id, ticket):
    if _client is None:
        return
    try:
        _client.collection(_TICKETS_COL).document(sys_id).set(dict(ticket))
    except Exception as exc:
        _log.error("persistence.save_ticket(%s) failed: %s", sys_id, exc)


def delete_ticket(sys_id):
    if _client is None:
        return
    try:
        _client.collection(_TICKETS_COL).document(sys_id).delete()
    except Exception as exc:
        _log.error("persistence.delete_ticket(%s) failed: %s", sys_id, exc)


def save_journal_entry(entry):
    if _client is None:
        return
    try:
        _client.collection(_JOURNAL_COL).document(entry['sys_id']).set(dict(entry))
    except Exception as exc:
        _log.error("persistence.save_journal_entry(%s) failed: %s", entry.get('sys_id'), exc)


def save_counter(next_value):
    if _client is None:
        return
    try:
        _client.collection(_COUNTERS_COL).document(_NUMBER_DOC).set({'next': int(next_value)})
    except Exception as exc:
        _log.error("persistence.save_counter(%s) failed: %s", next_value, exc)


# ---------------------------------------------------------------------------
# Bulk load — called once at startup to warm the in-memory caches
# ---------------------------------------------------------------------------

def load_all():
    """
    Return (tickets_by_sysid, journal_rows, next_number).

    On memory backend or on Firestore error, returns empty defaults so the
    caller can seed instead.
    """
    if _client is None:
        return {}, [], 1

    try:
        tickets = {doc.id: doc.to_dict() for doc in _client.collection(_TICKETS_COL).stream()}
        journal_rows = [doc.to_dict() for doc in _client.collection(_JOURNAL_COL).stream()]
        counter_doc = _client.collection(_COUNTERS_COL).document(_NUMBER_DOC).get()
        next_num = int((counter_doc.to_dict() or {}).get('next', 1)) if counter_doc.exists else 1
        _log.info("persistence.load_all: %d tickets, %d journal rows, counter@%d",
                  len(tickets), len(journal_rows), next_num)
        return tickets, journal_rows, next_num
    except Exception as exc:
        _log.error("persistence.load_all failed: %s", exc)
        return {}, [], 1


def clear_all():
    """Wipe Firestore collections. Used only on explicit reseed."""
    if _client is None:
        return
    try:
        for col in (_TICKETS_COL, _JOURNAL_COL):
            for doc in _client.collection(col).list_documents():
                doc.delete()
        _client.collection(_COUNTERS_COL).document(_NUMBER_DOC).delete()
    except Exception as exc:
        _log.error("persistence.clear_all failed: %s", exc)
