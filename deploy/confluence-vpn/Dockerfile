ARG CISCO_BASE_IMAGE
FROM ${CISCO_BASE_IMAGE}

RUN apt-get -o Acquire::Retries=2 update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends tigervnc-standalone-server xauth \
    && rm -rf /var/lib/apt/lists/* \
    && test -x /usr/bin/Xtigervnc
