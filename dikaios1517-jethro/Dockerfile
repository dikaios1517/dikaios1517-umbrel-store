# Jethro PMM on umbrelOS.
# The release archive ships with vendor/ (composer deps), so no composer step.
FROM php:8.2-apache

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      libfreetype6-dev libjpeg62-turbo-dev libpng-dev libzip-dev unzip curl \
 && docker-php-ext-configure gd --with-freetype --with-jpeg \
 && docker-php-ext-install -j"$(nproc)" gd gettext zip exif pdo_mysql mysqli \
 && apt-get purge -y --auto-remove \
      libfreetype6-dev libjpeg62-turbo-dev libpng-dev libzip-dev \
 && rm -rf /var/lib/apt/lists/*

# Larger uploads (member photos, documents) than PHP's 2M/8M defaults.
COPY jethro-php.ini "$PHP_INI_DIR/conf.d/zz-jethro.ini"

COPY entrypoint.sh /usr/local/bin/jethro-entrypoint
RUN chmod +x /usr/local/bin/jethro-entrypoint

ARG JETHRO_VERSION=2.39.0
ADD https://github.com/tbar0970/jethro-pmm/releases/download/v${JETHRO_VERSION}/jethro-${JETHRO_VERSION}.zip /tmp/jethro.zip

# The zip is flat (no top-level folder): extract straight into the docroot.
RUN rm -rf /var/www/html/* \
 && unzip -q /tmp/jethro.zip -d /var/www/html \
 && rm /tmp/jethro.zip \
 # Uploads must live in persistent storage: files/ becomes a symlink to
 # /data/files, created by the entrypoint (remove the archived copy now).
 && rm -rf /var/www/html/files \
 && chown -R www-data:www-data /var/www/html

# Silence the Apache ServerName warning.
RUN echo "ServerName localhost" > "$APACHE_CONFDIR/conf-available/servername.conf" \
 && a2enconf servername

EXPOSE 80

# The Apache master stays root to manage its workers (standard php:apache
# supervision); workers run as www-data, which owns /data/conf.php and
# /data/files via the entrypoint below.
ENTRYPOINT ["jethro-entrypoint"]
CMD ["apache2-foreground"]
