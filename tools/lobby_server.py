#!/usr/bin/env python3
"""WoT 0.8.2 LAN lobby and account server.

Accounts, friends, platoons, who is online, where a host can be reached,
and stats across battles. Run it on an always-on machine with a public IP
and forward UDP 27016 to it:

    python3 lobby_server.py                 # UDP 27016, data in ./wotlan_data
    python3 lobby_server.py --port 27100 --data /srv/wotlan

Python 3 standard library only. State is kept in <data>/state.json, written
atomically after every change.

Protocol: one JSON object per UDP datagram, field "p" = "WOTLOBBY1". A
request may carry "q" (any id); the reply echoes it.

Accounts (passwords never travel or stay in clear: the client sends
sha256("wotlan:" + login.lower() + ":" + password), the server keeps a salted
PBKDF2 of that):
  register {login, pw, name}            -> {ok} | {err}
  login    {login, pw}                  -> {ok, token, uid, name}
  logout   {token}
  rename   {token, name}

Presence (every few seconds from the game and from the launcher):
  reg {token | uid, name, client, role, room, port, state, kh, lan}
      -> regok {uid, name, platoon, notes[]}     notes = pending notifications

Friends:   friends {token} -> {friends[], incoming[], outgoing[]}
           friend_add {token, name} / friend_accept {token, uid}
           friend_decline {token, uid} / friend_remove {token, uid}
Platoon:   pl_state {token} -> {platoon}
           pl_invite {token, uid} / pl_accept {token, leader} / pl_decline {token, leader}
           pl_leave {token} / pl_kick {token, uid}
Rooms:     list -> {players[]}
           find {room, kh, token?} -> {addrs[] | err}
               a platoon room "pl:<leader uid>" is open to its members
Stats:     stats_add {token, battle: {winner, rows: [[uid, name, team, damage, frags, alive]]}}
           stats -> {players[]}
"""

import argparse
import base64
import hashlib
import hmac
import json
import os
import secrets
import socket
import time

PROTO = 'WOTLOBBY1'
STALE = 20.0             # seconds without a reg before a client counts as gone
MAX_PACKET = 8000
PLATOON_MAX = 5
TOKEN_TTL = 30 * 24 * 3600


def log(msg):
    print(time.strftime('%H:%M:%S'), msg, flush=True)


def pbkdf2(secret, salt):
    return base64.b64encode(hashlib.pbkdf2_hmac('sha256', secret.encode('utf-8'), salt, 120000)).decode('ascii')


def clean_name(s, limit=24):
    s = str(s or '').strip()
    s = ''.join(ch for ch in s if ch.isprintable() and ch not in '<>&"\'')
    return s[:limit]


class Store(object):
    """accounts, friendships, platoons, stats - all in one JSON file."""

    def __init__(self, path):
        self.path = path
        self.data = {'accounts': {}, 'by_login': {}, 'tokens': {}, 'friends': {},
                     'requests': {}, 'stats': {}, 'battles': 0}
        if os.path.isfile(path):
            with open(path, 'r', encoding='utf-8') as f:
                loaded = json.load(f)
            self.data.update(loaded)

    def save(self):
        tmp = self.path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(self.data, f, ensure_ascii=False, indent=1, sort_keys=True)
        os.replace(tmp, self.path)


class Server(object):
    def __init__(self, port, data_dir):
        if not os.path.isdir(data_dir):
            os.makedirs(data_dir)
        self.store = Store(os.path.join(data_dir, 'state.json'))
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(('0.0.0.0', port))
        self.sock.settimeout(1.0)
        self.port = port
        self.presence = {}      # uid -> {client: {...entry...}}
        self.platoons = {}      # leader uid -> {'members': [uid...], 'invited': {uid: time}}
        self.member_of = {}     # uid -> leader uid
        self.notes = {}         # uid -> {'game': [text], 'launcher': [text]}

    # ------------------------------------------------------------ helpers
    @property
    def d(self):
        return self.store.data

    def send(self, addr, msg, req=None):
        msg['p'] = PROTO
        if req is not None and 'q' in req:
            msg['q'] = req['q']
        data = json.dumps(msg, ensure_ascii=False).encode('utf-8')
        if len(data) > MAX_PACKET:
            return
        try:
            self.sock.sendto(data, addr)
        except OSError:
            pass

    def account_of(self, req):
        tok = str(req.get('token') or '')
        if not tok:
            return None
        t = self.d['tokens'].get(tok)
        if not t or time.time() - t['t'] > TOKEN_TTL:
            return None
        return self.d['accounts'].get(t['uid'])

    def name_of(self, uid):
        a = self.d['accounts'].get(uid)
        if a:
            return a['name']
        for entry in self.presence.get(uid, {}).values():
            return entry.get('name', '?')
        return '?'

    def note(self, uid, text):
        '''A notification for both the player's game and launcher.'''
        box = self.notes.setdefault(uid, {'game': [], 'launcher': []})
        for q in box.values():
            q.append(text)
            del q[:-20]

    def status_of(self, uid):
        clients = self.presence.get(uid) or {}
        game = clients.get('game')
        if game:
            return game.get('state') or 'hangar'
        if clients.get('launcher'):
            return 'launcher'
        return 'offline'

    def platoon_info(self, uid):
        leader = self.member_of.get(uid)
        if leader is None and uid in self.platoons:
            leader = uid
        if leader is None:
            return None
        pl = self.platoons.get(leader)
        if pl is None:
            return None
        return {'leader': leader, 'leader_name': self.name_of(leader),
                'members': [{'uid': m, 'name': self.name_of(m), 'status': self.status_of(m)} for m in pl['members']],
                'invited': [{'uid': u, 'name': self.name_of(u)} for u in pl['invited']],
                'room': 'pl:' + leader,
                # members show this to the leader's game instead of the room key;
                # only members ever receive it (in their authenticated regok)
                'secret': pl.get('secret', '')}

    def friends_of(self, uid):
        return self.d['friends'].setdefault(uid, [])

    # ------------------------------------------------------------ expiry
    def expire(self):
        now = time.time()
        for uid in list(self.presence):
            clients = self.presence[uid]
            for c in list(clients):
                if now - clients[c]['seen'] > STALE:
                    del clients[c]
            if not clients:
                del self.presence[uid]
                log('offline: %s' % self.name_of(uid))
                for f in self.friends_of(uid):
                    if f in self.presence:
                        self.note(f, '%s вышел' % self.name_of(uid))
        for leader in list(self.platoons):
            pl = self.platoons[leader]
            for u in list(pl['invited']):
                if now - pl['invited'][u] > 120:
                    del pl['invited'][u]

    def disband_if_alone(self, leader):
        pl = self.platoons.get(leader)
        if pl and len(pl['members']) <= 1 and not pl['invited']:
            for m in pl['members']:
                self.member_of.pop(m, None)
            del self.platoons[leader]

    def leave_platoon(self, uid):
        leader = self.member_of.get(uid)
        if leader is None:
            return
        pl = self.platoons.get(leader)
        self.member_of.pop(uid, None)
        if pl is None:
            return
        if uid in pl['members']:
            pl['members'].remove(uid)
        if uid == leader:
            # the leader leaves: the platoon goes with it
            for m in pl['members']:
                self.member_of.pop(m, None)
                self.note(m, 'Взвод распущен: %s вышел' % self.name_of(uid))
            del self.platoons[leader]
        else:
            for m in pl['members']:
                self.note(m, '%s покинул взвод' % self.name_of(uid))
            self.disband_if_alone(leader)

    # ------------------------------------------------------------ handlers
    def h_register(self, req, addr):
        login = str(req.get('login') or '').strip().lower()
        pw = str(req.get('pw') or '')
        name = clean_name(req.get('name') or login)
        if not (3 <= len(login) <= 24) or not login.replace('_', '').replace('-', '').isalnum():
            return {'err': 'Логин: 3-24 символа, буквы, цифры, _ и -'}
        if len(pw) != 64:
            return {'err': 'bad password hash'}
        if login in self.d['by_login']:
            return {'err': 'Такой логин уже занят'}
        if len(name) < 2:
            return {'err': 'Ник слишком короткий'}
        for a in self.d['accounts'].values():
            if a['name'].lower() == name.lower():
                return {'err': 'Такой ник уже занят'}
        uid = secrets.token_hex(6)
        salt = secrets.token_bytes(16)
        self.d['accounts'][uid] = {'uid': uid, 'login': login, 'name': name,
                                   'salt': base64.b64encode(salt).decode('ascii'),
                                   'hash': pbkdf2(pw, salt), 'created': time.strftime('%Y-%m-%d %H:%M')}
        self.d['by_login'][login] = uid
        self.store.save()
        log('registered: %s (%s)' % (name, login))
        return {'ok': 1, 'uid': uid, 'name': name}

    def h_login(self, req, addr):
        login = str(req.get('login') or '').strip().lower()
        uid = self.d['by_login'].get(login)
        a = self.d['accounts'].get(uid) if uid else None
        if a is None:
            return {'err': 'Неверный логин или пароль'}
        salt = base64.b64decode(a['salt'])
        if not hmac.compare_digest(pbkdf2(str(req.get('pw') or ''), salt), a['hash']):
            log('login failed: %s from %s' % (login, addr[0]))
            return {'err': 'Неверный логин или пароль'}
        tok = secrets.token_hex(16)
        self.d['tokens'][tok] = {'uid': uid, 't': time.time()}
        # forget expired tokens
        now = time.time()
        for k in [k for k, v in self.d['tokens'].items() if now - v['t'] > TOKEN_TTL]:
            del self.d['tokens'][k]
        self.store.save()
        log('login: %s' % a['name'])
        return {'ok': 1, 'token': tok, 'uid': uid, 'name': a['name']}

    def h_logout(self, req, addr):
        tok = str(req.get('token') or '')
        if self.d['tokens'].pop(tok, None) is not None:
            self.store.save()
        return {'ok': 1}

    def h_rename(self, req, addr):
        a = self.account_of(req)
        if a is None:
            return {'err': 'auth'}
        name = clean_name(req.get('name'))
        if len(name) < 2:
            return {'err': 'Ник слишком короткий'}
        for other in self.d['accounts'].values():
            if other['uid'] != a['uid'] and other['name'].lower() == name.lower():
                return {'err': 'Такой ник уже занят'}
        a['name'] = name
        self.store.save()
        return {'ok': 1, 'name': name}

    def h_reg(self, req, addr):
        a = self.account_of(req)
        if a is not None:
            uid, name = a['uid'], a['name']
        else:
            if req.get('token'):
                return {'t': 'regok', 'err': 'auth'}
            uid = str(req.get('uid') or '')[:32]
            name = clean_name(req.get('name') or '?')
            if not uid:
                return None
        client = 'launcher' if req.get('client') == 'launcher' else 'game'
        clients = self.presence.setdefault(uid, {})
        was_online = bool(clients)
        clients[client] = {
            'uid': uid, 'name': name, 'account': a is not None,
            'role': str(req.get('role') or '')[:8], 'room': str(req.get('room') or '')[:40],
            'state': str(req.get('state') or '')[:16], 'kh': str(req.get('kh') or '')[:64],
            'port': int(req.get('port') or 0), 'lan': [str(x) for x in (req.get('lan') or [])][:4],
            'addr': addr, 'seen': time.time()}
        if not was_online:
            log('online: %s (%s) from %s:%d' % (name, client, addr[0], addr[1]))
            if a is not None:
                for f in self.friends_of(uid):
                    if f in self.presence:
                        self.note(f, '%s в сети' % name)
        box = self.notes.get(uid)
        notes = []
        if box:
            notes = box.get(client, [])
            box[client] = []
        return {'t': 'regok', 'uid': uid, 'name': name, 'account': a is not None,
                'platoon': self.platoon_info(uid), 'notes': notes}

    def h_list(self, req, addr):
        out = []
        for uid, clients in self.presence.items():
            e = clients.get('game') or clients.get('launcher')
            out.append({'uid': uid, 'name': e['name'], 'role': e['role'], 'room': e['room'],
                        'state': self.status_of(uid)})
        out.sort(key=lambda e: e['name'].lower())
        return {'t': 'list', 'players': out[:60]}

    def h_find(self, req, addr):
        room = str(req.get('room') or '')
        kh = str(req.get('kh') or '')
        host = None
        for uid, clients in self.presence.items():
            g = clients.get('game')
            if g and g['role'] == 'host' and g['room'] and g['room'] == room:
                host = g
                break
        if host is None:
            return {'t': 'found', 'room': room, 'err': 'no host'}
        allowed = not host['kh'] or host['kh'] == kh
        if not allowed and room.startswith('pl:'):
            a = self.account_of(req)
            leader = room[3:]
            if a is not None and (self.member_of.get(a['uid']) == leader):
                allowed = True
        if not allowed:
            log('find %s from %s:%d refused: wrong key' % (room, addr[0], addr[1]))
            return {'t': 'found', 'room': room, 'err': 'key'}
        port = host['port'] or 27015
        addrs = [[host['addr'][0], port]]
        for ip in host['lan']:
            if ip and ip != host['addr'][0] and not ip.startswith('127.'):
                addrs.append([ip, port])
        return {'t': 'found', 'room': room, 'host': host['name'], 'addrs': addrs}

    def h_friends(self, req, addr):
        a = self.account_of(req)
        if a is None:
            return {'err': 'auth'}
        uid = a['uid']
        fr = [{'uid': f, 'name': self.name_of(f), 'status': self.status_of(f)} for f in self.friends_of(uid)]
        fr.sort(key=lambda e: (e['status'] == 'offline', e['name'].lower()))
        inc = [{'uid': u, 'name': self.name_of(u)} for u in self.d['requests'].get(uid, [])]
        out = [{'uid': t, 'name': self.name_of(t)} for t, reqs in self.d['requests'].items() if uid in reqs]
        return {'t': 'friends', 'friends': fr, 'incoming': inc, 'outgoing': out,
                'platoon': self.platoon_info(uid)}

    def h_friend_add(self, req, addr):
        a = self.account_of(req)
        if a is None:
            return {'err': 'auth'}
        name = clean_name(req.get('name')).lower()
        target = None
        for other in self.d['accounts'].values():
            if other['name'].lower() == name or other['login'] == name:
                target = other
                break
        if target is None:
            return {'err': 'Игрок не найден'}
        me, them = a['uid'], target['uid']
        if me == them:
            return {'err': 'Это вы'}
        if them in self.friends_of(me):
            return {'err': 'Уже в друзьях'}
        # they already asked us: accept straight away
        if them in self.d['requests'].get(me, []):
            return self.h_friend_accept({'token': req.get('token'), 'uid': them}, addr)
        reqs = self.d['requests'].setdefault(them, [])
        if me not in reqs:
            reqs.append(me)
        self.store.save()
        self.note(them, '%s хочет добавить вас в друзья' % a['name'])
        return {'ok': 1}

    def h_friend_accept(self, req, addr):
        a = self.account_of(req)
        if a is None:
            return {'err': 'auth'}
        me, them = a['uid'], str(req.get('uid') or '')
        reqs = self.d['requests'].get(me, [])
        if them not in reqs:
            return {'err': 'Нет такой заявки'}
        reqs.remove(them)
        for x, y in ((me, them), (them, me)):
            fl = self.friends_of(x)
            if y not in fl:
                fl.append(y)
        self.store.save()
        self.note(them, '%s принял заявку в друзья' % a['name'])
        return {'ok': 1}

    def h_friend_decline(self, req, addr):
        a = self.account_of(req)
        if a is None:
            return {'err': 'auth'}
        reqs = self.d['requests'].get(a['uid'], [])
        them = str(req.get('uid') or '')
        if them in reqs:
            reqs.remove(them)
            self.store.save()
        return {'ok': 1}

    def h_friend_remove(self, req, addr):
        a = self.account_of(req)
        if a is None:
            return {'err': 'auth'}
        me, them = a['uid'], str(req.get('uid') or '')
        for x, y in ((me, them), (them, me)):
            fl = self.friends_of(x)
            if y in fl:
                fl.remove(y)
        self.store.save()
        return {'ok': 1}

    def h_pl_state(self, req, addr):
        a = self.account_of(req)
        if a is None:
            return {'err': 'auth'}
        invites = [{'leader': l, 'name': self.name_of(l)} for l, pl in self.platoons.items() if a['uid'] in pl['invited']]
        return {'t': 'pl', 'platoon': self.platoon_info(a['uid']), 'invites': invites}

    def h_pl_invite(self, req, addr):
        a = self.account_of(req)
        if a is None:
            return {'err': 'auth'}
        me, them = a['uid'], str(req.get('uid') or '')
        if them not in self.friends_of(me):
            return {'err': 'Пригласить можно только друга'}
        if them not in self.presence:
            return {'err': 'Друг не в сети'}
        leader = self.member_of.get(me)
        if leader is not None and leader != me:
            return {'err': 'Приглашает командир взвода'}
        pl = self.platoons.get(me)
        if pl is None:
            pl = {'members': [me], 'invited': {}, 'secret': secrets.token_hex(8)}
            self.platoons[me] = pl
            self.member_of[me] = me
        if them in pl['members']:
            return {'err': 'Уже во взводе'}
        if len(pl['members']) + len(pl['invited']) >= PLATOON_MAX:
            return {'err': 'Взвод полон'}
        pl['invited'][them] = time.time()
        self.note(them, '%s приглашает вас во взвод' % a['name'])
        return {'ok': 1, 'platoon': self.platoon_info(me)}

    def h_pl_accept(self, req, addr):
        a = self.account_of(req)
        if a is None:
            return {'err': 'auth'}
        me, leader = a['uid'], str(req.get('leader') or '')
        pl = self.platoons.get(leader)
        if pl is None or me not in pl['invited']:
            return {'err': 'Приглашение устарело'}
        if self.member_of.get(me) not in (None, leader):
            self.leave_platoon(me)
        del pl['invited'][me]
        if len(pl['members']) >= PLATOON_MAX:
            return {'err': 'Взвод полон'}
        pl['members'].append(me)
        self.member_of[me] = leader
        for m in pl['members']:
            if m != me:
                self.note(m, '%s вступил во взвод' % a['name'])
        return {'ok': 1, 'platoon': self.platoon_info(me)}

    def h_pl_decline(self, req, addr):
        a = self.account_of(req)
        if a is None:
            return {'err': 'auth'}
        leader = str(req.get('leader') or '')
        pl = self.platoons.get(leader)
        if pl and a['uid'] in pl['invited']:
            del pl['invited'][a['uid']]
            self.note(leader, '%s отклонил приглашение' % a['name'])
            self.disband_if_alone(leader)
        return {'ok': 1}

    def h_pl_leave(self, req, addr):
        a = self.account_of(req)
        if a is None:
            return {'err': 'auth'}
        self.leave_platoon(a['uid'])
        return {'ok': 1}

    def h_pl_kick(self, req, addr):
        a = self.account_of(req)
        if a is None:
            return {'err': 'auth'}
        pl = self.platoons.get(a['uid'])
        them = str(req.get('uid') or '')
        if pl is None or them not in pl['members'] or them == a['uid']:
            return {'err': 'Нельзя'}
        self.leave_platoon(them)
        self.note(them, 'Вас исключили из взвода')
        return {'ok': 1}

    def h_stats_add(self, req, addr):
        a = self.account_of(req)
        if a is None:
            return {'err': 'auth'}
        b = req.get('battle') or {}
        bid = str(b.get('id') or '')
        seen = self.d.setdefault('battle_ids', [])
        if bid and bid in seen:
            return {'ok': 1}
        if bid:
            seen.append(bid)
            del seen[:-500]
        winner = int(b.get('winner') or 0)
        self.d['battles'] = int(self.d.get('battles', 0)) + 1
        for row in b.get('rows') or []:
            try:
                uid, name, team, damage, frags, alive = row[:6]
            except Exception:
                continue
            uid = str(uid)
            if uid not in self.d['accounts']:
                continue   # stats only for registered players
            st = self.d['stats'].setdefault(uid, {'battles': 0, 'wins': 0, 'draws': 0, 'damage': 0, 'frags': 0, 'survived': 0})
            st['battles'] += 1
            if winner == 0:
                st['draws'] += 1
            elif winner == int(team):
                st['wins'] += 1
            st['damage'] += int(damage)
            st['frags'] += int(frags)
            st['survived'] += 1 if alive else 0
            st['last'] = time.strftime('%Y-%m-%d %H:%M')
        self.store.save()
        log('battle booked by %s: %d players' % (a['name'], len(b.get('rows') or [])))
        return {'ok': 1}

    def h_stats(self, req, addr):
        out = []
        for uid, st in self.d['stats'].items():
            row = dict(st)
            row['name'] = self.name_of(uid)
            row['uid'] = uid
            out.append(row)
        out.sort(key=lambda r: -r.get('damage', 0))
        return {'t': 'stats', 'players': out[:60]}

    # ------------------------------------------------------------ loop
    def handle(self, req, addr):
        t = req.get('t')
        fn = getattr(self, 'h_' + str(t), None)
        if fn is None:
            return
        try:
            res = fn(req, addr)
        except Exception as e:
            log('error in %s: %r' % (t, e))
            res = {'err': 'server error'}
        if res is not None:
            if 't' not in res:
                res['t'] = str(t) + '_r'
            self.send(addr, res, req)

    def run(self):
        log('lobby listening on UDP %d, %d account(s)' % (self.port, len(self.d['accounts'])))
        last_expire = 0.0
        while True:
            try:
                data, addr = self.sock.recvfrom(65535)
                try:
                    req = json.loads(data.decode('utf-8'))
                except Exception:
                    req = None
                if isinstance(req, dict) and req.get('p') == PROTO:
                    self.handle(req, addr)
            except socket.timeout:
                pass
            except OSError:
                pass
            if time.time() - last_expire > 2.0:
                last_expire = time.time()
                self.expire()


def main():
    ap = argparse.ArgumentParser(description='WoT 0.8.2 LAN lobby and account server')
    ap.add_argument('--port', type=int, default=27016)
    ap.add_argument('--data', default=os.path.join(os.path.dirname(os.path.abspath(__file__)), 'wotlan_data'))
    args = ap.parse_args()
    try:
        Server(args.port, args.data).run()
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()
