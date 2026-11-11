FROM python:3.12-alpine

LABEL org.opencontainers.image.source="https://github.com/shipyard/harness-plugin" \
      org.opencontainers.image.description="Shipyard plugin for Harness CI: wait for a Shipyard environment and export its URL and bypass token" \
      org.opencontainers.image.licenses="MIT"

# Unbuffered so Harness streams the "waiting..." lines as they happen.
ENV PYTHONUNBUFFERED=1

COPY plugin.py /opt/shipyard/plugin.py

# Harness runs plugin images with their own entrypoint.
ENTRYPOINT ["python", "/opt/shipyard/plugin.py"]
