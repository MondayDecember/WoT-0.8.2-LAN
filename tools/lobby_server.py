#!/usr/bin/env python3
"""WoT 0.8.2 LAN lobby: who is online, and where a room's host can be reached.

Run it on any always-on machine with a public IP (or inside your WireGuard
network) and forward UDP 27016 to it:

    python3 lobby_server.py            # port 27016
    python3 lobby_server.py --port 27100

Python 3 standard library only. Nothing is stored on disk.

Clients (mod_lanlink.py with "lobby": "<this host>:27016" in lan.json):
  reg   every few seconds: uid, name, role, room, game port, state, key hash,
        LAN addresses. The lobby records the public address it came from.
  list  -> every registered player: name, role, room, state (no addresses)
  find  room + key hash -> the room host's public address and LAN addresses.
        Only a guest with the same key hash gets them; the lobby never sees
        the key itself.
"""

import argparse
import json
import socket
import time

PROTO = 'WOTLOBBY1'
STALE = 20.0          # seconds without a reg before a player is dropped
MAX_PACKET = 8000


def log(msg):
    print(time.strftime('%H:%M:%S'), msg, flush=True)


class Lobby(object):
    def __init__(self, port):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(('0.0.0.0', port))
        self.sock.settimeout(1.0)
        self.players = {}      # uid -> dict(..., addr, seen)
        self.port = port

    def send(self, addr, msg):
        msg['p'] = PROTO
        data = json.dumps(msg).encode('utf-8')
        if len(data) > MAX_PACKET:
            return
        try:
            self.sock.sendto(data, addr)
        except OSError:
            pass

    def expire(self):
        now = time.time()
        for uid in list(self.players):
            if now - self.players[uid]['seen'] > STALE:
                p = self.players.pop(uid)
                log('offline: %s (%s)' % (p['name'], p['role']))

    def handle(self, msg, addr):
        t = msg.get('t')
        if t == 'reg':
            uid = str(msg.get('uid') or '')[:32]
            if not uid:
                return
            known = uid in self.players
            entry = {
                'uid': uid,
                'name': str(msg.get('name') or '?')[:32],
                'role': str(msg.get('role') or '?')[:8],
                'room': str(msg.get('room') or '')[:32],
                'state': str(msg.get('state') or '')[:16],
                'kh': str(msg.get('kh') or '')[:64],
                'port': int(msg.get('port') or 0),
                'lan': [str(a) for a in (msg.get('lan') or [])][:4],
                'addr': addr,
                'seen': time.time(),
            }
            self.players[uid] = entry
            if not known:
                log('online: %s (%s%s) from %s:%d' % (entry['name'], entry['role'],
                    ', room ' + entry['room'] if entry['room'] else '', addr[0], addr[1]))
            self.send(addr, {'t': 'regok'})
        elif t == 'list':
            out = []
            for p in sorted(self.players.values(), key=lambda p: p['name'].lower()):
                out.append({'uid': p['uid'], 'name': p['name'], 'role': p['role'],
                            'room': p['room'], 'state': p['state']})
            self.send(addr, {'t': 'list', 'players': out[:60]})
        elif t == 'find':
            room = str(msg.get('room') or '')
            kh = str(msg.get('kh') or '')
            host = None
            for p in self.players.values():
                if p['role'] == 'host' and p['room'] and p['room'] == room:
                    host = p
                    break
            if host is None:
                self.send(addr, {'t': 'found', 'room': room, 'err': 'no host'})
                return
            if host['kh'] and host['kh'] != kh:
                self.send(addr, {'t': 'found', 'room': room, 'err': 'key'})
                log('find %s from %s:%d refused: wrong key' % (room, addr[0], addr[1]))
                return
            port = host['port'] or 27015
            addrs = [[host['addr'][0], port]]
            for ip in host['lan']:
                if ip and ip != host['addr'][0] and not ip.startswith('127.'):
                    addrs.append([ip, port])
            self.send(addr, {'t': 'found', 'room': room, 'host': host['name'], 'addrs': addrs})

    def run(self):
        log('lobby listening on UDP %d' % self.port)
        last_expire = 0.0
        while True:
            try:
                data, addr = self.sock.recvfrom(65535)
                try:
                    msg = json.loads(data.decode('utf-8'))
                except Exception:
                    msg = None
                if isinstance(msg, dict) and msg.get('p') == PROTO:
                    self.handle(msg, addr)
            except socket.timeout:
                pass
            except OSError:
                pass
            if time.time() - last_expire > 2.0:
                last_expire = time.time()
                self.expire()


def main():
    ap = argparse.ArgumentParser(description='WoT 0.8.2 LAN lobby')
    ap.add_argument('--port', type=int, default=27016)
    args = ap.parse_args()
    try:
        Lobby(args.port).run()
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()
