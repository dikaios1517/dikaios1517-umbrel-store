#!/usr/bin/env python3
"""Wisp management GUI + transparent relay proxy (stdlib only).

Listens on 0.0.0.0:3000. Routes, in order:
  1. websocket upgrades        -> transparent proxy to 127.0.0.1:7777
  2. NIP-11 (Accept: application/nostr+json) -> proxy
  3. /api/*                    -> local JSON API (config read/write, status, restart)
  4. "/"                       -> the GUI page
  5. anything else (/metrics, other paths) -> proxy

Proxied requests get client-supplied X-Forwarded-For stripped and a real
X-Forwarded-For injected, so wisp's trust_proxy + trusted_proxies=127.0.0.1
keeps per-IP limits keyed to the actual client.
"""

import json
import os
import signal
import socket
import socketserver
import threading
import time
import tomllib

import purge

RELAY_HOST = '127.0.0.1'
RELAY_PORT = 7777
GUI_PORT = int(os.environ.get('GUI_PORT', '3000'))
CONFIG_PATH = os.environ.get('WISP_CONFIG', '/data/wisp.toml')
WISP_PID_FILE = os.environ.get('WISP_PID_FILE', '/tmp/wisp.pid')
HIDDEN_SERVICE = os.environ.get('APP_HIDDEN_SERVICE', '')
HTML_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'index.html')


def relay_alive():
    try:
        with socket.create_connection((RELAY_HOST, RELAY_PORT), timeout=1):
            return True
    except OSError:
        return False


def wisp_pid():
    try:
        with open(WISP_PID_FILE) as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None


def restart_relay():
    pid = wisp_pid()
    if pid is None:
        return False, 'relay pid not found; use the app Restart button in umbrelOS'
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    for _ in range(50):
        if not relay_alive():
            break
        time.sleep(0.2)
    else:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    return True, 'restart signal sent; the supervisor will bring the relay back up'


def read_config():
    with open(CONFIG_PATH, 'rb') as f:
        return tomllib.load(f)


def render_toml(data):
    """wisp's TOML parser only understands flat scalar values under [section]
    headers (everything else it knows is a scalar; csv lists are plain quoted
    strings), so rendering nested dicts of scalars is lossless for everything
    wisp itself can read.

    wisp takes the value between the first two double quotes LITERALLY and
    does no backslash unescaping, so strings containing quotes or newlines
    cannot be represented — reject them instead of silently corrupting."""
    for section, values in data.items():
        if not isinstance(values, dict):
            continue  # wisp has no top-level scalars; ignore anything odd
        for key, value in values.items():
            if isinstance(value, str) and ('"' in value or '\n' in value):
                raise ValueError(
                    f'{section}.{key}: quotes and newlines are not allowed in settings values'
                )
    out = []
    for section, values in data.items():
        if not isinstance(values, dict):
            continue
        out.append(f'[{section}]')
        for key, value in values.items():
            if value is None:
                continue
            if isinstance(value, bool):
                out.append(f'{key} = {"true" if value else "false"}')
            elif isinstance(value, (int, float)):
                out.append(f'{key} = {value}')
            elif isinstance(value, str):
                out.append(f'{key} = "{value}"')
            elif isinstance(value, list):
                items = ', '.join(f'"{v}"' for v in value)
                out.append(f'{key} = [{items}]')
        out.append('')
    return '\n'.join(out)


def write_config(data):
    text = render_toml(data)
    tmp = CONFIG_PATH + '.tmp'
    with open(tmp, 'w') as f:
        f.write(text)
    os.replace(tmp, CONFIG_PATH)


class Handler:
    def __init__(self, sock, addr):
        self.sock = sock
        self.addr = addr

    def respond(self, status, body, content_type='application/json'):
        reason = {200: 'OK', 400: 'Bad Request', 404: 'Not Found', 500: 'Internal Server Error'}.get(status, 'OK')
        payload = body if isinstance(body, bytes) else json.dumps(body).encode()
        head = (f'HTTP/1.1 {status} {reason}\r\n'
                f'Content-Type: {content_type}\r\n'
                f'Content-Length: {len(payload)}\r\n'
                f'Cache-Control: no-store\r\n'
                f'Connection: close\r\n\r\n').encode()
        self.sock.sendall(head + payload)

    def read_request(self):
        buf = b''
        while b'\r\n\r\n' not in buf:
            chunk = self.sock.recv(65536)
            if not chunk:
                return None
            buf += chunk
            if len(buf) > 1048576:
                return None
        head, _, rest = buf.partition(b'\r\n\r\n')
        lines = head.decode('latin-1').split('\r\n')
        method, path, _ = lines[0].split(' ', 2)
        headers = {}
        for line in lines[1:]:
            if ':' in line:
                k, v = line.split(':', 1)
                headers[k.strip().lower()] = v.strip()
        clen = int(headers.get('content-length', '0') or '0')
        while len(rest) < clen:
            chunk = self.sock.recv(65536)
            if not chunk:
                break
            rest += chunk
        return method, path, headers, rest

    def proxy(self, method, path, headers, rest):
        """Transparent proxy to the relay, injecting the real client IP."""
        try:
            upstream = socket.create_connection((RELAY_HOST, RELAY_PORT), timeout=5)
        except OSError:
            self.respond(502, {'error': 'relay is not running'})
            return
        lines = [f'{method} {path} HTTP/1.1']
        skip = {'x-forwarded-for', 'x-real-ip'}
        for k, v in headers.items():
            if k not in skip:
                lines.append(f'{k}: {v}')
        lines.append(f'X-Forwarded-For: {self.addr[0]}')
        try:
            upstream.sendall(('\r\n'.join(lines) + '\r\n\r\n').encode('latin-1') + rest)
            self.sock.settimeout(None)
            upstream.settimeout(None)
            pipe(self.sock, upstream)
        finally:
            try:
                upstream.close()
            except OSError:
                pass

    def route(self):
        req = self.read_request()
        if req is None:
            return
        method, path, headers, rest = req
        path_only = path.split('?', 1)[0]

        if headers.get('upgrade', '').lower() == 'websocket' or \
           'application/nostr+json' in headers.get('accept', ''):
            self.proxy(method, path, headers, rest)
            return

        if path_only.startswith('/api/'):
            self.api(method, path_only, headers, rest)
            return

        if path_only in ('/', '/index.html') and method == 'GET':
            try:
                with open(HTML_PATH, 'rb') as f:
                    self.respond(200, f.read(), 'text/html; charset=utf-8')
            except OSError:
                self.respond(500, {'error': 'GUI page missing'})
            return

        self.proxy(method, path, headers, rest)

    def api(self, method, path, headers, rest):
        try:
            if path == '/api/status' and method == 'GET':
                pid = wisp_pid()
                self.respond(200, {
                    'relayRunning': relay_alive(),
                    'pid': pid,
                    'onion': (HIDDEN_SERVICE if HIDDEN_SERVICE and
                              not HIDDEN_SERVICE.startswith(('not-', 'notyetset')) else None),
                    'lan': 'ws://umbrel:7777',
                })
            elif path == '/api/config' and method == 'GET':
                self.respond(200, read_config())
            elif path == '/api/config' and method == 'POST':
                data = json.loads(rest.decode('utf-8') or '{}')
                if not isinstance(data, dict):
                    raise ValueError('config must be a JSON object')
                write_config(data)
                self.respond(200, {'ok': True, 'restartNeeded': True})
            elif path == '/api/restart' and method == 'POST':
                ok, msg = restart_relay()
                self.respond(200 if ok else 500, {'ok': ok, 'message': msg})
            elif path == '/api/purge' and method == 'GET':
                self.respond(200, purge_scheduler.status)
            elif path == '/api/purge' and method == 'POST':
                self.respond(200, purge_scheduler.run_now())
            else:
                self.respond(404, {'error': 'not found'})
        except Exception as e:  # noqa: BLE001 - surfaced to the GUI
            self.respond(500, {'error': str(e)})


def pipe(a, b):
    """Bidirectional socket pipe until either side closes."""
    import select
    sockets = [a, b]
    try:
        while True:
            r, _, x = select.select(sockets, [], sockets, 60)
            if x or not r:
                break
            for s in r:
                data = s.recv(65536)
                if not data:
                    return
                (b if s is a else a).sendall(data)
    except OSError:
        pass


class ThreadedTCPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def process_request(self, request, client_address):
        t = threading.Thread(target=lambda: self.handle(request, client_address), daemon=True)
        t.start()

    def handle(self, request, client_address):
        try:
            Handler(request, client_address).route()
        except OSError:
            pass
        finally:
            try:
                request.close()
            except OSError:
                pass


if __name__ == '__main__':
    server = ThreadedTCPServer(('0.0.0.0', GUI_PORT), ThreadedTCPServer)

    purge_scheduler = purge.PurgeScheduler(read_config)
    purge_scheduler.start()

    server.serve_forever()
