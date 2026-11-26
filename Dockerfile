#FROM mcr.microsoft.com/playwright/python:v1.62.0-jammy
#
#USER root

# Объединяем установку системных пакетов в один слой
#RUN apt-get update && \
#    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
#    tini \
#    libreoffice \
#    libreoffice-java-common \
#    default-jre-headless \
#    fonts-dejavu \
#    fonts-liberation \
#    && apt-get clean && \
#    rm -rf /var/lib/apt/lists/*

#ENV HOME=/tmp
#WORKDIR /home/user/stroy-resurs/mvp

# Сначала копируем зависимости для изоляции кэша
#COPY requirements.txt .
#RUN pip install --no-cache-dir --upgrade pip && \
#    pip install --no-cache-dir -r requirements.txt

# Копируем остальной исходный код
#COPY . .


#ENTRYPOINT ["/usr/bin/tini", "--"]

#------------------------------------------

FROM docker:29.9.2-cli AS docker_cli

FROM mcr.microsoft.com/playwright/python:v1.62.0-jammy

USER root

# Установка tini
RUN apt-get update && \
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends tini && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

ENTRYPOINT ["/usr/bin/tini", "--"]

# LibreOffice + Java (Playwright-образ уже содержит большинство графических библиотек)
RUN apt-get update && \
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    libreoffice \
    libreoffice-java-common \
    default-jre-headless \
    fonts-dejavu \
    fonts-liberation \
    && apt-get clean && \
    rm -rf /var/lib/apt/lists/*

ENV HOME=/tmp

WORKDIR /home/user/stroy-resurs/mvp

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Изолированный слой панели: не инвалидирует кэш тяжёлых зависимостей пайплайна.
COPY dashboard-requirements.txt .
RUN pip install --no-cache-dir -r dashboard-requirements.txt

COPY --from=docker_cli /usr/local/bin/docker /usr/local/bin/docker
COPY --from=docker_cli /usr/local/libexec/docker/cli-plugins/ /usr/local/libexec/docker/cli-plugins/

COPY . .


# Pinned Calibri-compatible fonts for browser rendering of fixed-height text blocks.
COPY browser_fonts/fonts/ /usr/local/share/fonts/stroy-resurs/
ENV FONTCONFIG_FILE=/home/user/stroy-resurs/mvp/browser_fonts/fonts.conf
RUN fc-cache -f \
    && fc-match Calibri \
    && fc-match Calibri | grep -q Carlito
