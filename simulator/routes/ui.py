"""
Minimalist operator UI for creating and mutating tickets during a demo.

Every mutation done here touches `sys_updated_on` on the underlying ticket,
so the XSIAM ServiceNow pack's `get-modified-remote-data` picks it up on
its next mirror poll.
"""
from flask import Blueprint, render_template, request, redirect, url_for, flash, Response
from urllib.parse import urlparse

from auth import require_basic_auth
from config import Config
from generators import incidents, journal

ui_bp = Blueprint('ui', __name__, url_prefix='/ui')


def _flash_username():
    return request.authorization.username if request.authorization else 'admin'


@ui_bp.route('', methods=['GET'])
@ui_bp.route('/', methods=['GET'])
@require_basic_auth
def index():
    tickets = incidents.list_tickets()
    # Attach journal entries for the expand-details view
    tickets_with_journal = []
    for t in tickets:
        entries = journal.query_entries(element_id=t['sys_id'], limit=100, offset=0)
        tickets_with_journal.append({
            'ticket': t,
            'journal': list(reversed(entries)),  # newest first for display
        })
    return render_template(
        'tickets.html',
        rows=tickets_with_journal,
        instance=Config.INSTANCE_NAME,
        ticket_type=Config.TICKET_TYPE,
        state_labels=incidents.STATE_LABELS,
        closed_states=incidents.CLOSED_STATES,
        current_user=_flash_username(),
    )


@ui_bp.route('/tickets', methods=['POST'])
@require_basic_auth
def create_ticket():
    short = (request.form.get('short_description') or '').strip()
    desc = (request.form.get('description') or '').strip()
    state = request.form.get('state', incidents.STATE_NEW)
    correlation_id = (request.form.get('correlation_id') or '').strip()

    if not short:
        flash('Le champ "sujet" est obligatoire.', 'error')
        return redirect(url_for('ui.index'))

    if state not in incidents.STATE_LABELS:
        state = incidents.STATE_NEW

    t = incidents.create_ticket({
        'short_description': short,
        'description': desc,
        'state': state,
        'correlation_id': correlation_id,
        'correlation_display': correlation_id or '',
        'caller_id': _flash_username(),
    })
    flash(f"Ticket {t['number']} créé.", 'success')
    return redirect(url_for('ui.index'))


@ui_bp.route('/tickets/<sys_id>/close', methods=['POST'])
@require_basic_auth
def close(sys_id):
    user = _flash_username()
    t = incidents.close_ticket(
        sys_id,
        close_code='Solved (Permanently)',
        close_notes=f'Closed via simulator UI by {user}',
        user=user,
    )
    if t:
        flash(f"Ticket {t['number']} fermé.", 'success')
    else:
        flash('Ticket introuvable.', 'error')
    return redirect(url_for('ui.index'))


@ui_bp.route('/tickets/<sys_id>/reopen', methods=['POST'])
@require_basic_auth
def reopen(sys_id):
    user = _flash_username()
    t = incidents.reopen_ticket(sys_id, user=user)
    if t:
        flash(f"Ticket {t['number']} rouvert.", 'success')
    else:
        flash('Ticket introuvable.', 'error')
    return redirect(url_for('ui.index'))


@ui_bp.route('/tickets/<sys_id>/comment', methods=['POST'])
@require_basic_auth
def comment(sys_id):
    text = (request.form.get('text') or '').strip()
    kind = request.form.get('kind', 'work_notes')
    if kind not in ('comments', 'work_notes'):
        kind = 'work_notes'
    if not text:
        flash('Le contenu du commentaire est vide.', 'error')
        return redirect(url_for('ui.index'))

    user = _flash_username()
    t = incidents.update_ticket(sys_id, {kind: text}, user=user)
    if t:
        label = 'commentaire' if kind == 'comments' else 'work note'
        flash(f"{label.capitalize()} ajouté à {t['number']}.", 'success')
    else:
        flash('Ticket introuvable.', 'error')
    return redirect(url_for('ui.index'))


@ui_bp.route('/logout', methods=['GET'])
def logout():
    """
    Force the browser to drop the Basic Auth credentials it cached by
    returning a 401 with a different realm. Not perfect (browsers vary),
    but does the job for most demos.
    """
    return Response(
        'Vous êtes déconnecté. Fermez cet onglet ou rechargez la page pour vous reconnecter.',
        status=401,
        headers={'WWW-Authenticate': 'Basic realm="logged-out"'},
    )
