#!/bin/bash
# Usage: bash deploy-cloudrun.sh
# Deploys the ServiceNow XSIAM Case-Mirroring Simulator to Cloud Run,
# with optional Firestore persistence (STORAGE_BACKEND=firestore).

set -e

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

PROJECT_ID=$(gcloud config get-value project 2>/dev/null)
REGION="europe-west1"
SERVICE_NAME="servicenow-simulator"
REPO_NAME="servicenow-simulator"

# Demo credentials — override with env vars before running to change them.
AUTH_USERNAME="${AUTH_USERNAME:-admin}"
AUTH_PASSWORD="${AUTH_PASSWORD:-admin}"
SNOW_INSTANCE_NAME="${SNOW_INSTANCE_NAME:-demo-instance}"
SEED_COUNT="${SEED_COUNT:-5}"

# Persistence backend. Set STORAGE_BACKEND=memory (or unset) to skip the
# Firestore provisioning steps and run purely in-memory.
STORAGE_BACKEND="${STORAGE_BACKEND:-firestore}"
FIRESTORE_LOCATION="${FIRESTORE_LOCATION:-eur3}"     # multi-region eu (matches europe-west1 region)
FIRESTORE_DATABASE="${FIRESTORE_DATABASE:-(default)}"

echo -e "${GREEN}=== Déploiement Cloud Run - $SERVICE_NAME ===${NC}\n"
echo -e "${YELLOW}Project:${NC}      $PROJECT_ID"
echo -e "${YELLOW}Region:${NC}       $REGION"
echo -e "${YELLOW}Service:${NC}      $SERVICE_NAME"
echo -e "${YELLOW}Instance:${NC}     $SNOW_INSTANCE_NAME"
echo -e "${YELLOW}Persistence:${NC}  $STORAGE_BACKEND"
echo ""

echo -e "${YELLOW}[1/7] Activation des APIs...${NC}"
APIS="run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com storage.googleapis.com"
if [ "$STORAGE_BACKEND" = "firestore" ]; then
  APIS="$APIS firestore.googleapis.com"
fi
gcloud services enable $APIS
echo -e "${GREEN}✓ APIs activées${NC}\n"

echo -e "${YELLOW}[2/7] Configuration d'Artifact Registry...${NC}"
REPO_EXISTS=$(gcloud artifacts repositories list \
  --location=$REGION \
  --filter="name:$REPO_NAME" \
  --format="value(name)" 2>/dev/null)

if [ -z "$REPO_EXISTS" ]; then
  gcloud artifacts repositories create $REPO_NAME \
    --repository-format=docker \
    --location=$REGION \
    --description="$SERVICE_NAME images" \
    --quiet
  echo -e "${GREEN}✓ Repository créé${NC}"
else
  echo -e "${GREEN}✓ Repository existe déjà${NC}"
fi
echo ""

# --- Firestore setup (skipped when STORAGE_BACKEND != firestore) ---
if [ "$STORAGE_BACKEND" = "firestore" ]; then
  echo -e "${YELLOW}[3/7] Provisioning Firestore (Native mode)...${NC}"
  DB_EXISTS=$(gcloud firestore databases list \
    --format="value(name)" 2>/dev/null | grep -F "/databases/${FIRESTORE_DATABASE}" || true)
  if [ -z "$DB_EXISTS" ]; then
    echo "Création de la base $FIRESTORE_DATABASE en région $FIRESTORE_LOCATION..."
    gcloud firestore databases create \
      --database="$FIRESTORE_DATABASE" \
      --location="$FIRESTORE_LOCATION" \
      --type=firestore-native \
      --quiet
    echo -e "${GREEN}✓ Firestore DB créée${NC}"
  else
    echo -e "${GREEN}✓ Firestore DB existe déjà${NC}"
  fi

  # Grant datastore.user to the Cloud Run runtime service account (default
  # is the Compute Engine default SA when no dedicated runtime SA is set).
  PROJECT_NUMBER=$(gcloud projects describe "$PROJECT_ID" --format="value(projectNumber)")
  RUNTIME_SA="${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"
  echo "Grant roles/datastore.user au SA runtime ($RUNTIME_SA)..."
  gcloud projects add-iam-policy-binding "$PROJECT_ID" \
    --member="serviceAccount:$RUNTIME_SA" \
    --role="roles/datastore.user" \
    --condition=None \
    --quiet >/dev/null
  echo -e "${GREEN}✓ IAM configuré${NC}\n"
else
  echo -e "${YELLOW}[3/7] Firestore skipped (STORAGE_BACKEND=$STORAGE_BACKEND)${NC}\n"
fi

echo -e "${YELLOW}[4/7] Vérification du répertoire...${NC}"
if [ ! -f "deployment/Dockerfile" ]; then
  echo -e "${RED}ERREUR: Dockerfile non trouvé. Lancez ce script depuis la racine du projet.${NC}"
  exit 1
fi
echo -e "${GREEN}✓ Dockerfile trouvé${NC}\n"

echo -e "${YELLOW}[5/7] Construction de l'image Docker...${NC}"
echo "Cela peut prendre 2-3 minutes..."
gcloud builds submit --config cloudbuild.yaml
IMAGE_PATH="${REGION}-docker.pkg.dev/$PROJECT_ID/$REPO_NAME/$REPO_NAME:latest"
echo -e "${GREEN}✓ Image construite: $IMAGE_PATH${NC}\n"

echo -e "${YELLOW}[6/7] Déploiement sur Cloud Run...${NC}"
ENV_VARS="AUTH_USERNAME=$AUTH_USERNAME,AUTH_PASSWORD=$AUTH_PASSWORD"
ENV_VARS="$ENV_VARS,SNOW_INSTANCE_NAME=$SNOW_INSTANCE_NAME,SEED_COUNT=$SEED_COUNT,DEBUG=False"
ENV_VARS="$ENV_VARS,STORAGE_BACKEND=$STORAGE_BACKEND,FIRESTORE_DATABASE=$FIRESTORE_DATABASE"

gcloud run deploy $SERVICE_NAME \
  --image $IMAGE_PATH \
  --platform managed \
  --region $REGION \
  --allow-unauthenticated \
  --memory 512Mi \
  --cpu 1 \
  --timeout 300 \
  --min-instances 1 \
  --max-instances 1 \
  --concurrency 80 \
  --set-env-vars "$ENV_VARS"
# NB: min=max=1 on purpose. Even with Firestore persistence, a single
# instance keeps the in-memory cache authoritative and avoids the cost
# of round-tripping Firestore on every read.

echo -e "${YELLOW}[7/7] Configuration de l'accès public...${NC}"
gcloud run services add-iam-policy-binding $SERVICE_NAME \
  --region=$REGION \
  --member=allUsers \
  --role=roles/run.invoker \
  --project=$PROJECT_ID \
  --quiet 2>/dev/null && PUBLIC_ACCESS=true || PUBLIC_ACCESS=false

SERVICE_URL=$(gcloud run services describe $SERVICE_NAME \
  --region $REGION \
  --format 'value(status.url)')

echo ""
echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}✓ Déploiement réussi !${NC}"
echo -e "${GREEN}========================================${NC}"
echo ""
echo -e "${YELLOW}URL du service:${NC}     ${GREEN}${SERVICE_URL}${NC}"
echo -e "${YELLOW}Interface web:${NC}      ${GREEN}${SERVICE_URL}/ui${NC}"
echo -e "${YELLOW}Login:${NC}              ${AUTH_USERNAME} / ${AUTH_PASSWORD}"
echo -e "${YELLOW}Persistence:${NC}        ${STORAGE_BACKEND} (Firestore DB: ${FIRESTORE_DATABASE})"
echo ""
echo -e "${YELLOW}Tests de validation:${NC}"
echo "  Health check:"
echo "    curl ${SERVICE_URL}/health"
echo "  Query incidents (auth requis):"
echo "    curl -u ${AUTH_USERNAME}:${AUTH_PASSWORD} '${SERVICE_URL}/api/now/table/incident?sysparm_limit=2'"
echo ""
echo -e "${YELLOW}Configuration XSIAM (pack ServiceNow v2):${NC}"
echo "  Server URL:    ${SERVICE_URL}/"
echo "  Username:      ${AUTH_USERNAME}"
echo "  Password:      ${AUTH_PASSWORD}"
echo "  Use OAuth:     unchecked"
echo "  Instance Name: ${SNOW_INSTANCE_NAME}"
echo "  Ticket type:   incident"
echo ""
echo -e "${YELLOW}Commandes utiles:${NC}"
echo "  Voir les logs:      gcloud run services logs read $SERVICE_NAME --region $REGION"
echo "  Supprimer service:  gcloud run services delete $SERVICE_NAME --region $REGION"
if [ "$STORAGE_BACKEND" = "firestore" ]; then
  echo "  Wipe Firestore DB:  gcloud firestore databases delete $FIRESTORE_DATABASE"
fi
echo ""
