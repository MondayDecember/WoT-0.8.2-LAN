# -*- coding: utf-8 -*-
'''Game side of the bot brain (tools/bot_brain.py).

WHY
    The obstacle feelers are the most expensive thing a bot does: about 25
    wg_collideSegment rays per decision, hundreds per frame, all on the game's
    one Python thread. The brain is a separate Python 3 program next to the
    game. It answers the feeler question ("is the way ahead clear, and if not,
    which way to turn") from a height map, on its own CPU core, while the game
    keeps drawing.

HOW
    The geometry is static, so the game measures the ground height around
    the bots ONCE (a short vertical ray per 2 m cell, only where bots
    actually drive), keeps it per map in offhangar_user/nav_dump/<map>.hc,
    and streams new cells to the brain. After a few battles on a map almost
    nothing is left to measure and the bots' feelers cost the game nothing.

    Everything degrades to the old behaviour: no brain running, brain not
    answering, a cell not measured yet -> the bot casts its own rays that
    decision, exactly as before.

No BigWorld imports: the world queries arrive as callbacks, so this module
runs (and is tested) on a desktop Python 2.6+ or 3.
'''

import json
import math
import os
import socket
import struct
import time
from array import array

PORT = 27017
CELL = 2.0            # height cell (m)
WCELL_MUL = 2         # water cell = 2 height cells (4 m)
UNKNOWN = -30000.0    # never measured
VOID = -20000.0       # measured: no ground (outside the map) -> blocked
SCAN_R = 14.0         # measure this far around a bot (feelers reach ~13 m)
RESCAN_MOVE = 4.0     # re-scan around a bot after it moved this far
MAX_CELLS = 600000    # cap; a bigger arena gets bigger cells
MAGIC = b'OFFHHC1\n'
ALIVE_S = 3.0
ANSWER_MAX_AGE = 0.5


def _tobytes(a):
    return a.tostring() if hasattr(a, 'tostring') else a.tobytes()


def _frombytes(a, b):
    if hasattr(a, 'fromstring'):
        a.fromstring(b)
    else:
        a.frombytes(b)


class HeightCache(object):
    '''Ground heights on a 2 m grid over the arena, filled in where bots go.'''

    def __init__(self, map_name, bounds, cell=CELL):
        x0, z0, x1, z1 = [float(v) for v in bounds]
        if x1 < x0:
            x0, x1 = x1, x0
        if z1 < z0:
            z0, z1 = z1, z0
        area = max(1.0, (x1 - x0) * (z1 - z0))
        cell = max(cell, math.sqrt(area / float(MAX_CELLS)))
        self.map_name = map_name
        self.x0, self.z0, self.x1, self.z1 = x0, z0, x1, z1
        self.cell = cell
        self.nx = int(math.ceil((x1 - x0) / cell)) + 1
        self.nz = int(math.ceil((z1 - z0) / cell)) + 1
        self.wcell = cell * WCELL_MUL
        self.wnx = int(math.ceil((x1 - x0) / self.wcell)) + 1
        self.wnz = int(math.ceil((z1 - z0) / self.wcell)) + 1
        self.h = array('f', [UNKNOWN]) * (self.nx * self.nz)
        self.w = bytearray(self.wnx * self.wnz)      # 0 unknown, 1 dry, 2 water
        self.queued = bytearray(self.nx * self.nz)
        self.queue = []
        self.known = 0
        self.dirty = False
        self.new_h = []
        self.new_w = []

    def header(self):
        return {'name': self.map_name, 'x0': self.x0, 'z0': self.z0, 'x1': self.x1,
                'z1': self.z1, 'cell': self.cell, 'nx': self.nx, 'nz': self.nz,
                'wcell': self.wcell, 'wnx': self.wnx, 'wnz': self.wnz}

    def fits(self, hd):
        try:
            return (hd['nx'] == self.nx and hd['nz'] == self.nz and hd['wnx'] == self.wnx
                    and abs(hd['x0'] - self.x0) < 0.5 and abs(hd['z0'] - self.z0) < 0.5
                    and abs(hd['cell'] - self.cell) < 0.01)
        except Exception:
            return False

    # -- disk ---------------------------------------------------------------
    def save(self, path):
        d = os.path.dirname(path)
        if d and not os.path.isdir(d):
            try:
                os.makedirs(d)
            except OSError:
                pass
        tmp = path + '.tmp'
        f = open(tmp, 'wb')
        try:
            head = json.dumps(self.header()).encode('utf-8')
            f.write(MAGIC)
            f.write(struct.pack('<I', len(head)))
            f.write(head)
            f.write(_tobytes(self.h))
            f.write(bytes(self.w))
        finally:
            f.close()
        try:
            if os.path.exists(path):
                os.remove(path)
        except OSError:
            pass
        os.rename(tmp, path)
        self.dirty = False

    def load(self, path):
        '''Take a saved cache if it was made for this exact grid.'''
        if not os.path.isfile(path):
            return False
        f = open(path, 'rb')
        try:
            if f.read(len(MAGIC)) != MAGIC:
                return False
            hl = struct.unpack('<I', f.read(4))[0]
            hd = json.loads(f.read(hl).decode('utf-8'))
            if not self.fits(hd):
                return False
            h = array('f')
            _frombytes(h, f.read(self.nx * self.nz * 4))
            w = bytearray(f.read(self.wnx * self.wnz))
        finally:
            f.close()
        if len(h) != self.nx * self.nz or len(w) != self.wnx * self.wnz:
            return False
        self.h = h
        self.w = w
        self.known = sum(1 for v in h if v != UNKNOWN)
        return True

    # -- measuring ------------------------------------------------------------
    def want_around(self, x, z, y):
        '''Queue the unmeasured cells within SCAN_R of (x, z); y is the bot's
        height, which centres the measuring ray.'''
        c = self.cell
        r = int(SCAN_R / c) + 1
        cx = int((x - self.x0) / c)
        cz = int((z - self.z0) / c)
        r2 = (SCAN_R / c + 0.5) ** 2
        nx, nz = self.nx, self.nz
        h, q, queue = self.h, self.queued, self.queue
        added = 0
        for iz in range(max(0, cz - r), min(nz, cz + r + 1)):
            dz2 = (iz - cz) * (iz - cz)
            row = iz * nx
            for ix in range(max(0, cx - r), min(nx, cx + r + 1)):
                if (ix - cx) * (ix - cx) + dz2 > r2:
                    continue
                i = row + ix
                if h[i] == UNKNOWN and not q[i]:
                    q[i] = 1
                    queue.append((i, y))
                    added += 1
        return added

    def measure(self, ground, water, budget):
        '''Measure up to `budget` queued cells.

        ground(x, z, top, bottom) -> y or None   (None = nothing there)
        water(x, y, z) -> depth in m, < 0 when dry
        The ray runs from 80 m above to 60 m below the bot that asked for the
        cell: short rays are cheap, a 2 km ray through a city is not. Only
        when that window finds nothing does a full-height ray decide.'''
        done = 0
        queue = self.queue
        while queue and done < budget:
            i, ref_y = queue.pop()
            self.queued[i] = 0
            if self.h[i] != UNKNOWN:
                continue
            ix, iz = i % self.nx, i // self.nx
            x = self.x0 + ix * self.cell
            z = self.z0 + iz * self.cell
            y = ground(x, z, ref_y + 80.0, ref_y - 60.0)
            done += 1
            if y is None:
                y = ground(x, z, 1000.0, -1000.0)
                done += 1
            hv = VOID if y is None else float(y)
            self.h[i] = hv
            self.known += 1
            self.dirty = True
            self.new_h.append((i, round(hv, 2)))
            wi = (iz // WCELL_MUL) * self.wnx + (ix // WCELL_MUL)
            if self.w[wi] == 0 and hv != VOID:
                wet = 1
                try:
                    if water(x, hv, z) > 0.8:
                        wet = 2
                except Exception:
                    pass
                self.w[wi] = wet
                self.new_w.append((wi, wet))
                done += 1
        return done


class BrainLink(object):
    '''UDP link to tools/bot_brain.py on this PC.'''

    def __init__(self, port=PORT, clock=time.time):
        self.clock = clock
        self.addr = ('127.0.0.1', port)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(('127.0.0.1', 0))
        self.sock.setblocking(False)
        self.last_hi = 0.0
        self.last_hello = 0.0
        self.brain_version = None
        self.cache = None
        self.cache_path = None
        self.gen = 0
        self.map_ok_gen = -1
        self.last_map_sent = 0.0
        self.out_q = []
        self.answers = {}
        self.last_scan = {}
        self.stats = {'used': 0, 'fallback': 0, 'asked': 0, 'measured': 0}

    # -- state ----------------------------------------------------------------
    def alive(self):
        return self.clock() - self.last_hi < ALIVE_S

    def ready(self):
        return self.cache is not None and self.alive() and self.map_ok_gen == self.gen

    def set_map(self, map_name, bounds, cache_path):
        '''New battle: a fresh height cache for this map (loaded if saved).'''
        self.save()
        self.gen += 1
        self.cache = HeightCache(map_name, bounds)
        self.cache_path = cache_path
        try:
            self.cache.load(cache_path)
        except Exception:
            pass
        self.answers = {}
        self.last_scan = {}
        self.out_q = []
        self.last_map_sent = 0.0

    def end_map(self):
        self.save()
        self.cache = None
        self.answers = {}

    def save(self):
        c = self.cache
        if c is not None and c.dirty and self.cache_path:
            try:
                c.save(self.cache_path)
                return True
            except Exception:
                return False
        return False

    # -- per bot ----------------------------------------------------------------
    def ask(self, eid, x, y, z, yaw, turn_sign):
        '''Queue one feeler question; also schedules measuring around the bot.'''
        c = self.cache
        if c is None:
            return
        ls = self.last_scan.get(eid)
        if ls is None or (x - ls[0]) ** 2 + (z - ls[1]) ** 2 > RESCAN_MOVE * RESCAN_MOVE:
            self.last_scan[eid] = (x, z)
            c.want_around(x, z, y)
        self.out_q.append((int(eid), x, y, z, yaw, 1 if turn_sign > 0 else -1))
        self.stats['asked'] += 1

    def take(self, eid):
        '''The brain's newest answer for this bot, once.

        Returns (True, steer_yaw_or_None) for a usable answer, None when there
        is none (the caller casts its own rays).'''
        a = self.answers.pop(eid, None)
        if a is None:
            self.stats['fallback'] += 1
            return None
        t, v = a
        if self.clock() - t > ANSWER_MAX_AGE or v == '?':
            self.stats['fallback'] += 1
            return None
        self.stats['used'] += 1
        return (True, v)

    # -- once a frame -------------------------------------------------------------
    def frame(self, ground, water, budget):
        '''Measure, send what is due, read what arrived.'''
        now = self.clock()
        c = self.cache
        if c is not None and c.queue and budget > 0 and self.alive():
            self.stats['measured'] += c.measure(ground, water, budget)
        if now - self.last_hello >= 1.0:
            self.last_hello = now
            self._send({'t': 'hello', 'v': 1})
        if c is not None and self.map_ok_gen != self.gen and now - self.last_map_sent > 1.0 and self.alive():
            self.last_map_sent = now
            self.save()
            hd = c.header()
            hd.update({'t': 'map', 'gen': self.gen,
                       'file': os.path.abspath(self.cache_path) if self.cache_path else None})
            self._send(hd)
        if c is not None and self.map_ok_gen == self.gen:
            # Binary, not JSON: Python 2.6's json encoder is pure Python and
            # cost ~1 ms a frame here (tick_profile.txt); struct is C.
            while c.new_h or c.new_w:
                hs, c.new_h = c.new_h[:1000], c.new_h[1000:]
                ws, c.new_w = c.new_w[:1000], c.new_w[1000:]
                flat = []
                for i, v in hs:
                    flat.append(i)
                    flat.append(v)
                for i, v in ws:
                    flat.append(i)
                    flat.append(v)
                self._send_raw(b'C' + struct.pack('<IHH', self.gen, len(hs), len(ws))
                               + struct.pack('<' + 'if' * len(hs) + 'iB' * len(ws), *flat))
            q = self.out_q
            while q:
                part, q = q[:200], q[200:]
                flat = []
                for r in part:
                    flat.extend(r)
                self._send_raw(b'Q' + struct.pack('<IH', self.gen, len(part))
                               + struct.pack('<' + 'iffffb' * len(part), *flat))
        self.out_q = []
        self._poll()

    def _send_raw(self, data):
        try:
            self.sock.sendto(data, self.addr)
        except Exception:
            pass

    def _send(self, msg):
        try:
            self.sock.sendto(json.dumps(msg, separators=(',', ':')).encode('utf-8'), self.addr)
        except Exception:
            pass

    def _poll(self):
        for _ in range(200):
            try:
                data, addr = self.sock.recvfrom(65535)
            except Exception:
                return
            if data[:1] == b'A':
                try:
                    gen, n = struct.unpack_from('<IH', data, 1)
                    if gen != self.gen:
                        continue
                    vals = struct.unpack_from('<' + 'iBf' * n, data, 7)
                except Exception:
                    continue
                now = self.clock()
                ans = self.answers
                for k in range(0, 3 * n, 3):
                    kind = vals[k + 1]
                    ans[vals[k]] = (now, None if kind == 0 else (vals[k + 2] if kind == 1 else '?'))
                continue
            try:
                m = json.loads(data.decode('utf-8'))
            except Exception:
                continue
            t = m.get('t')
            if t == 'hi':
                self.last_hi = self.clock()
                self.brain_version = m.get('v')
            elif t == 'mapok':
                if m.get('gen') == self.gen:
                    self.map_ok_gen = self.gen
                    # cells measured before the brain had the map: resend all
                    c = self.cache
                    if c is not None and m.get('need_all'):
                        c.new_h = [(i, round(v, 2)) for i, v in enumerate(c.h) if v != UNKNOWN]
                        c.new_w = [(i, v) for i, v in enumerate(c.w) if v]
            elif t == 'a':
                if m.get('gen') == self.gen:
                    now = self.clock()
                    for r in m.get('r', ()):
                        self.answers[int(r[0])] = (now, r[1])

    def status(self):
        c = self.cache
        return ('brain %s | map %s | cells %s/%s | queue %s | asked %d used %d fallback %d measured %d'
                % ('alive' if self.alive() else 'not running',
                   ('ok' if self.ready() else 'waiting') if c is not None else '-',
                   c.known if c else 0, (c.nx * c.nz) if c else 0, len(c.queue) if c else 0,
                   self.stats['asked'], self.stats['used'], self.stats['fallback'],
                   self.stats['measured']))
