# Kirk — a dark-themed fork of Jethro PMM 2.39.0 for umbrelOS.
# Kirk = the upstream Jethro 2.39.0 release archive (with PHP dependencies)
# plus our custom dark/teal stylesheet, loaded via Jethro's built-in
# jethro-<version>-custom.css hook. No source tree is stored in this repo;
# the fork's file additions are the stylesheet, tab favicon and icon
# sprites copied in below (plus one sed for the tab title).
FROM php:8.2-apache

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      libfreetype6-dev libjpeg62-turbo-dev libpng-dev libzip-dev curl unzip \
 && docker-php-ext-configure gd --with-freetype --with-jpeg \
 && docker-php-ext-install -j"$(nproc)" gd gettext zip exif pdo_mysql mysqli \
 && apt-get purge -y --auto-remove \
      libfreetype6-dev libjpeg62-turbo-dev libpng-dev libzip-dev \
 && rm -rf /var/lib/apt/lists/*

# Larger uploads (member photos, documents) than PHP's 2M/8M defaults.
COPY kirk-php.ini "$PHP_INI_DIR/conf.d/zz-kirk.ini"

COPY entrypoint.sh /usr/local/bin/kirk-entrypoint
RUN chmod +x /usr/local/bin/kirk-entrypoint

ARG JETHRO_VERSION=2.39.0
ADD https://github.com/tbar0970/jethro-pmm/releases/download/v${JETHRO_VERSION}/jethro-${JETHRO_VERSION}.zip /tmp/jethro.zip

# The zip is flat (no top-level folder): extract straight into the docroot.
RUN rm -rf /var/www/html/* \
 && unzip -q /tmp/jethro.zip -d /var/www/html \
 && rm /tmp/jethro.zip \
 # Uploads must live in persistent storage: files/ becomes a bind mount
 # target created by the entrypoint (remove the archived copy now).
 && rm -rf /var/www/html/files \
 && chown -R www-data:www-data /var/www/html

# The fork: dark/teal theme, loaded automatically by Jethro's custom-CSS hook.
# The custom-CSS filename only changes with the upstream JETHRO_VERSION, not
# with Kirk releases, so browsers must not cache it long — no-cache headers
# for stylesheets below.
COPY jethro-2.39.0-custom.css /var/www/html/
# Logo for the header (referenced by the stylesheet, left of the app title).
COPY icon.png /var/www/html/kirk-logo.png
COPY kirk-glyphicons-teal.png /var/www/html/kirk-glyphicons-teal.png
# Monochrome light-teal replacements for upstream's full-color document
# toolbar/folder-tree icons (minimalist line art, drawn to match Kirk).
COPY folder_add.png folder_delete.png folder_edit.png document_new.png document_upload.png folder.png /var/www/html/resources/img/
# Browser tab: no <link rel=icon> exists upstream, so the tab favicon is the
# root /favicon.ico — overwrite it with the Kirk cross. The <title> hardcodes
# 'Jethro - ' before SYSTEM_NAME; strip it so the tab reads just 'Kirk'.
# The stylesheet href gets a Kirk-revision query (?v=N) so every Kirk release
# is a brand-new URL — browsers and intermediaries can never serve a stale
# theme, even though the custom-CSS filename itself only changes upstream.
COPY kirk-favicon.png /var/www/html/kirk-favicon.png
COPY favicon.ico /var/www/html/favicon.ico
ARG KIRK_REV=26
RUN sed -i "s/if (!defined('IS_PUBLIC')) echo 'Jethro - ';//" \
         /var/www/html/templates/head.template.php \
 && sed -i "s#customCSSFile; ?>\"#customCSSFile.\"?v=${KIRK_REV}\"; ?>\"#" \
         /var/www/html/templates/head.template.php \
 && sed -i '\#apple-touch-icon#a <link rel="icon" type="image/png" href="<?php echo BASE_URL; ?>/kirk-favicon.png"/>' \
         /var/www/html/templates/head.template.php

RUN a2enmod headers
COPY kirk-cache.conf "$APACHE_CONFDIR/conf-available/kirk-cache.conf"
RUN a2enconf kirk-cache

# Silence the Apache ServerName warning.
RUN echo "ServerName localhost" > "$APACHE_CONFDIR/conf-available/servername.conf" \
 && a2enconf servername

EXPOSE 80

# The Apache master stays root to manage its workers (standard php:apache
# supervision); workers run as www-data, which owns /data/conf.php and
# /data/files via the entrypoint below.
ENTRYPOINT ["kirk-entrypoint"]
CMD ["apache2-foreground"]

