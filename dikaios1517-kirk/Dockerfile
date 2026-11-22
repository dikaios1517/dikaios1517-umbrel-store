# Kirk — a dark-themed fork of Jethro PMM 2.39.0 for umbrelOS.
# Kirk = the upstream Jethro 2.39.0 release archive (with PHP dependencies)
# plus our custom dark/teal stylesheet, loaded via Jethro's built-in
# jethro-<version>-custom.css hook. No source tree is stored in this repo;
# the fork's only file addition is the stylesheet copied in below.
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
COPY jethro-2.39.0-custom.css /var/www/html/
# Logo for the header (referenced by the stylesheet, left of the app title).
COPY icon.png /var/www/html/kirk-logo.png

# Silence the Apache ServerName warning.
RUN echo "ServerName localhost" > "$APACHE_CONFDIR/conf-available/servername.conf" \
 && a2enconf servername

EXPOSE 80

# The Apache master stays root to manage its workers (standard php:apache
# supervision); workers run as www-data, which owns /data/conf.php and
# /data/files via the entrypoint below.
ENTRYPOINT ["kirk-entrypoint"]
CMD ["apache2-foreground"]

