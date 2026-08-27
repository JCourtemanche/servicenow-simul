FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*

COPY simulator/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY simulator/ .

EXPOSE 8080

# Single worker on purpose — the ticket store is in-process memory, so
# multiple workers would each seed independently and diverge state
# (see also --max-instances 1 in deploy-cloudrun.sh).
CMD ["gunicorn", "-b", "0.0.0.0:8080", "-w", "1", "--threads", "8", "--timeout", "120", "app:app"]
