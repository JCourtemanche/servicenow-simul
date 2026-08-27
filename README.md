# ServiceNow XSIAM Case-Mirroring Simulator

Simulateur Python/Flask de l'API REST ServiceNow pour démontrer le **case mirroring bidirectionnel** avec Cortex XSIAM (pack ServiceNow v2) — sans instance ServiceNow réelle.

Deux surfaces exposées :

- **`/api/now/...`** — endpoints ServiceNow REST Table que le pack XSIAM appelle (Basic Auth).
- **`/ui`** — interface web minimaliste (Basic Auth, mêmes credentials) pour créer un faux case, le fermer/rouvrir, ou y ajouter des comments/work_notes.

Basé sur le template [xsiam-simulator-template](https://github.com/JCourtemanche/xsiam-simulator-template) + le pattern stateful de [gravityzone-mock](https://github.com/JCourtemanche/gravityzone-mock). Utilise [xsiam-shared-personas](https://github.com/JCourtemanche/xsiam-shared-personas) pour la cohérence des utilisateurs Business Corp entre simulateurs.

---

## Lancer en local

```bash
cd simulator
pip install -r requirements.txt
python app.py
# → http://localhost:8080/ui   (login: admin / admin)
```

Sanity checks :

```bash
curl -u admin:admin 'http://localhost:8080/api/now/table/incident?sysparm_limit=2'
curl -u admin:admin -X POST -H "Content-Type: application/json" \
     -d '{"short_description":"Test","state":"1"}' \
     http://localhost:8080/api/now/table/incident
```

---

## Déployer sur GCP Cloud Run

```bash
# Optionnel : override les creds avant le déploiement
export AUTH_USERNAME=jc
export AUTH_PASSWORD=change-me-in-real-life
bash deploy-cloudrun.sh
```

Service Cloud Run `servicenow-simulator`, région `europe-west1`. Le script affiche l'URL, les creds, et un rappel de la config XSIAM à saisir.

---

## Configurer le pack ServiceNow v2 dans XSIAM

Marketplace → **ServiceNow v2** → nouvelle instance :

| Champ | Valeur |
|---|---|
| Server URL | `<URL Cloud Run>/` (avec le slash final) |
| Username | valeur de `AUTH_USERNAME` |
| Password | valeur de `AUTH_PASSWORD` |
| Use OAuth 2.0 / JWT | **décoché** |
| ServiceNow API Version | *(vide)* |
| Ticket type | `incident` |
| Instance Name | doit matcher `SNOW_INSTANCE_NAME` (défaut `demo-instance`) |
| Fetch incidents | **coché** (pour tester le mirror-in initial) |
| Mirroring Direction | Incoming And Outgoing |
| Close Mirrored XSIAM Incident | coché |
| Close Mirrored ServiceNow Ticket | coché |

Cliquer **Test** → doit répondre 200. Si ça échoue, vérifier que l'URL se termine par `/` et que le mot de passe est correct.

---

## Scénario de démo (mirroring bidirectionnel)

1. **Créer côté ServiceNow (mirror-in)** :
   - Ouvrir `<URL>/ui`, cliquer *Créer le ticket*, choisir statut *Ouvert*, laisser correlation_id vide.
   - Attendre ≤ 1 min → un incident XSIAM apparaît (nom = short_description).

2. **Commentaire depuis XSIAM (mirror-out)** :
   - Sur l'incident XSIAM créé, aller dans War Room, taguer un message avec `Note` (pour work_note) ou `Comment`.
   - Retourner sur `<URL>/ui`, déplier *Détails ticket & journal* du ticket concerné → le message apparaît.

3. **Commentaire depuis ServiceNow (mirror-in)** :
   - Dans `<URL>/ui`, sur la ligne du ticket, taper un message dans le champ inline (choisir `work_note` ou `comment`), cliquer `+`.
   - Attendre ≤ 1 min → visible côté XSIAM dans la War Room.

4. **Fermeture** :
   - Depuis `<URL>/ui` : clic *Fermer* → l'incident XSIAM se ferme au prochain poll.
   - Depuis XSIAM : fermer l'incident → le ticket ServiceNow passe à `state=7` (voir la table `/ui`).

---

## Endpoints implémentés

| Méthode | Path | Description |
|---|---|---|
| GET | `/api/now/table/incident` | Query (sysparm_query, sysparm_limit, sysparm_offset, sysparm_fields) |
| GET | `/api/now/table/incident/<sys_id>` | Get single ticket |
| POST | `/api/now/table/incident` | Create ticket |
| PATCH | `/api/now/table/incident/<sys_id>` | Update (`comments`/`work_notes` → journal) |
| DELETE | `/api/now/table/incident/<sys_id>` | Delete |
| GET | `/api/now/table/sys_journal_field` | Journal (comments/work_notes) |
| GET | `/api/now/attachment` | Toujours `[]` (MVP) |
| POST | `/oauth_token.do` | Stub OAuth (fake tokens) |
| GET | `/ui`, `/ui/logout`, `POST /ui/tickets`, `POST /ui/tickets/<id>/close|reopen|comment` | Interface web |
| GET | `/health`, `/` | Health check + service info |

Query parser (`sysparm_query`) : couvre les 4 patterns réellement envoyés par le pack : `sys_updated_on>...`, `number=INC...`, `element_id=<id>^element=...^ORelement=...^sys_created_on>...`, `table_sys_id=...`.

---

## Hors scope

- Tables autres que `incident` (`problem`, `sc_task`, `sn_si_incident`, CMDB…).
- Attachments réels (téléchargement/upload).
- OAuth/JWT complet (stub uniquement).
- `sysparm_display_value=all` (le pack marche sans côté simulateur).
- Persistance disque : restart Cloud Run = reset (suffit pour une démo).
