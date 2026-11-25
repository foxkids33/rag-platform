FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    RAG_VPN_ISOLATED=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates curl iproute2 openconnect \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/confluence-vpn
COPY route_hook.py /opt/confluence-vpn/route_hook.py
RUN chmod 0555 /opt/confluence-vpn/route_hook.py

# Run only in a dedicated network namespace, with --cap-drop=ALL,
# --cap-add=NET_ADMIN and --device=/dev/net/tun. Never use host networking.
# No endpoint or credentials are baked into this image. The caller supplies
# --script=/opt/confluence-vpn/route_hook.py and --passwd-on-stdin at runtime.
ENTRYPOINT ["openconnect"]
CMD ["--help"]
