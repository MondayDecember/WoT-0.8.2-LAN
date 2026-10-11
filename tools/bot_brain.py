#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Bot brain for WoT 0.8.2 LAN: answers the bots' obstacle feelers outside the game.

The game (offhangar/brain_link.py) sends, per bot decision, the bot's position,
hull yaw and which way it wants to turn. This program checks the same rays the
game used to cast - two heights (0.7 m / 7 m ahead and 1.5 m / 12 m ahead),
left track / centre / right track, slope and water - against a 2 m ground
height map the game measures and streams in. It runs on its own CPU core, so
the game's single Python thread no longer pays for the feelers.

Answer per bot: a steering yaw (go round), null (way ahead is clear) or "?"
(part of the area is not measured yet - the game casts its own rays then).

Python 3, standard library only. Started by the launcher; one instance per PC
(a second one finds the port taken and exits). Exits after 30 minutes without
a word from the game.

    python bot_brain.py [--dir <game folder>] [--port 27017] [--selftest]
"""

import argparse
import json
import math
import os
import socket
import struct
import sys
import time
from array import array

VERSION = 1
UNKNOWN = -30000.0
VOID = -20000.0
MAGIC = b'OFFHHC1\n'
IDLE_EXIT_S = 30 * 60

# Must match the in-game feeler block (offline_battle.py, "MULTI-RAY SENSORS").
STEP = 0.25
RAY_PROFILES = ((0.7, 7.0), (1.5, 12.0))
MARGINS = (2.2, 1.6)
UP_RATIO = 0.45
DOWN_RATIO = 0.7
WATER_DEPTH = 0.8   # the game stores wet cells already thresholded


class Unknown(Exception):
    pass


class HeightMap(object):
    def __init__(self, hd):
        self.name = hd['name']
        self.x0 = float(hd['x0'])
        self.z0 = float(hd['z0'])
        self.cell = float(hd['cell'])
        self.nx = int(hd['nx'])
        self.nz = int(hd['nz'])
        self.wcell = float(hd['wcell'])
        self.wnx = int(hd['wnx'])
        self.wnz = int(hd['wnz'])
        self.h = array('f', [UNKNOWN]) * (self.nx * self.nz)
        self.w = bytearray(self.wnx * self.wnz)

    def load_file(self, path):
        try:
            with open(path, 'rb') as f:
                if f.read(len(MAGIC)) != MAGIC:
                    return False
                hl = struct.unpack('<I', f.read(4))[0]
                hd = json.loads(f.read(hl).decode('utf-8'))
                if int(hd['nx']) != self.nx or int(hd['nz']) != self.nz or int(hd['wnx']) != self.wnx:
                    return False
                h = array('f')
                h.frombytes(f.read(self.nx * self.nz * 4))
                w = bytearray(f.read(self.wnx * self.wnz))
            if len(h) != len(self.h) or len(w) != len(self.w):
                return False
            self.h, self.w = h, w
            return True
        except Exception:
            return False

    def height(self, x, z):
        """Ground height at (x, z): nearest of the 4 surrounding cells that is
        HIGHEST - a wall between two samples must not slip through."""
        fx = (x - self.x0) / self.cell
        fz = (z - self.z0) / self.cell
        ix = int(math.floor(fx))
        iz = int(math.floor(fz))
        if ix < 0 or iz < 0 or ix + 1 >= self.nx or iz + 1 >= self.nz:
            return VOID
        h = self.h
        nx = self.nx
        i = iz * nx + ix
        best = None
        for j in (i, i + 1, i + nx, i + nx + 1):
            v = h[j]
            if v == UNKNOWN:
                raise Unknown()
            if v == VOID:
                return VOID
            if best is None or v > best:
                best = v
        return best

    def wet(self, x, z):
        ix = int((x - self.x0) / self.wcell + 0.5)
        iz = int((z - self.z0) / self.wcell + 0.5)
        if ix < 0 or iz < 0 or ix >= self.wnx or iz >= self.wnz:
            return False
        v = self.w[iz * self.wnx + ix]
        if v == 0:
            raise Unknown()
        return v == 2


def blocked(hm, x, y, z, fy, hw):
    """One feeler direction, same tests as the game's ray version."""
    sin_fy = math.sin(fy)
    cos_fy = math.cos(fy)
    for h, dist in RAY_PROFILES:
        dx = x + sin_fy * dist
        dz = z + cos_fy * dist
        dy = hm.height(dx, dz)
        if dy == VOID:
            return True
        ydiff = dy - y
        if ydiff > dist * UP_RATIO or ydiff < -dist * DOWN_RATIO:
            return True
        if hm.wet(dx, dz):
            return True
        n = int(dist)
        for ox in (-hw, 0.0, hw):
            sx = x + cos_fy * ox
            sz = z - sin_fy * ox
            for k in range(1, n + 1):
                t = k / dist
                px = sx + sin_fy * dist * t
                pz = sz + cos_fy * dist * t
                g = hm.height(px, pz)
                if g == VOID:
                    return True
                line = y + h + (dy - y) * t
                if g > line:
                    return True
    return False


def feelers(hm, x, y, z, yaw, sign):
    """Steering yaw around an obstacle, None when the way is clear.
    Raises Unknown when the answer needs cells not measured yet."""
    angles = [0.0]
    for i in range(1, 6):
        if sign > 0:
            angles.extend([i * STEP, -i * STEP])
        else:
            angles.extend([-i * STEP, i * STEP])
    for hw in MARGINS:
        center_blocked = False
        best = None
        for a in angles:
            hit = blocked(hm, x, y, z, yaw + a, hw)
            if a == 0.0:
                if hit:
                    center_blocked = True
                else:
                    break
            elif not hit:
                best = a
                break
        if not center_blocked:
            return None
        if best is not None:
            return yaw + best
    return None


class Brain(object):
    def __init__(self, port, status_path=None):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(('127.0.0.1', port))
        self.sock.settimeout(1.0)
        self.maps = {}           # client addr -> (gen, HeightMap)
        self.status_path = status_path
        self.last_msg = time.time()
        self.n_q = 0
        self.n_unknown = 0
        self.busy = 0.0
        self.t_stat = time.time()

    def send(self, addr, msg):
        try:
            self.sock.sendto(json.dumps(msg, separators=(',', ':')).encode('utf-8'), addr)
        except OSError:
            pass

    def handle(self, m, addr):
        t = m.get('t')
        if t == 'hello':
            cur = self.maps.get(addr)
            self.send(addr, {'t': 'hi', 'v': VERSION, 'map': cur[1].name if cur else None})
        elif t == 'map':
            hm = HeightMap(m)
            loaded = bool(m.get('file')) and hm.load_file(m['file'])
            self.maps[addr] = (m.get('gen'), hm)
            self.send(addr, {'t': 'mapok', 'gen': m.get('gen'), 'need_all': not loaded})
        elif t == 'cells':
            cur = self.maps.get(addr)
            if cur is None or cur[0] != m.get('gen'):
                return
            hm = cur[1]
            n = len(hm.h)
            for i, v in m.get('h', ()):
                if 0 <= i < n:
                    hm.h[i] = v
            nw = len(hm.w)
            for i, v in m.get('w', ()):
                if 0 <= i < nw:
                    hm.w[i] = v
        elif t == 'q':
            cur = self.maps.get(addr)
            if cur is None or cur[0] != m.get('gen'):
                return
            hm = cur[1]
            out = []
            for eid, x, y, z, yaw, sign in m.get('b', ()):
                self.n_q += 1
                try:
                    r = feelers(hm, x, y, z, yaw, sign)
                    out.append([eid, None if r is None else round(r, 4)])
                except Unknown:
                    self.n_unknown += 1
                    out.append([eid, '?'])
            self.send(addr, {'t': 'a', 'gen': m.get('gen'), 'r': out})

    def write_status(self):
        now = time.time()
        if now - self.t_stat < 5.0:
            return
        span = now - self.t_stat
        line = ('%s  bot brain v%d | %.0f answers/s, %.0f%% not measured yet | busy %.0f%% of one core | maps: %s\n'
                % (time.strftime('%H:%M:%S'), VERSION, self.n_q / span,
                   100.0 * self.n_unknown / max(1, self.n_q), 100.0 * self.busy / span,
                   ', '.join(v[1].name for v in self.maps.values()) or '-'))
        self.n_q = self.n_unknown = 0
        self.busy = 0.0
        self.t_stat = now
        if self.status_path:
            try:
                with open(self.status_path, 'w', encoding='utf-8') as f:
                    f.write(line)
            except OSError:
                pass

    def run(self):
        while True:
            try:
                data, addr = self.sock.recvfrom(65535)
            except socket.timeout:
                data = None
            except OSError:
                data = None
            now = time.time()
            if data:
                self.last_msg = now
                t0 = time.perf_counter()
                try:
                    self.handle(json.loads(data.decode('utf-8')), addr)
                except Exception:
                    pass
                self.busy += time.perf_counter() - t0
            elif now - self.last_msg > IDLE_EXIT_S:
                return
            self.write_status()


def selftest():
    hd = {'name': 'test', 'x0': -50.0, 'z0': -50.0, 'cell': 2.0, 'nx': 51, 'nz': 51,
          'wcell': 4.0, 'wnx': 26, 'wnz': 26}
    hm = HeightMap(hd)
    for iz in range(hm.nz):
        for ix in range(hm.nx):
            x = hm.x0 + ix * hm.cell
            z = hm.z0 + iz * hm.cell
            # a house 10 m ahead of the origin (z 10..20, x -6..6), 8 m tall
            hm.h[iz * hm.nx + ix] = 8.0 if (10.0 <= z <= 20.0 and -6.0 <= x <= 6.0) else 0.0
    for i in range(len(hm.w)):
        hm.w[i] = 1
    assert feelers(hm, 0.0, 0.0, -30.0, 0.0, 1) is None, 'open road must be clear'
    r = feelers(hm, 0.0, 0.0, 2.0, 0.0, 1)
    assert r is not None and abs(r) > 0.2, 'house ahead must make it turn: %r' % (r,)
    r2 = feelers(hm, 0.0, 0.0, 2.0, 0.0, -1)
    assert r2 is not None and r2 < 0 < r, 'turn side follows the wanted turn'
    hm.h[int((-24.0 - hm.z0) / 2) * hm.nx + int((-30.0 - hm.x0) / 2)] = UNKNOWN
    try:
        feelers(hm, -30.0, 0.0, -30.0, 0.0, 1)
        raise AssertionError('unknown cell must raise')
    except Unknown:
        pass
    t0 = time.perf_counter()
    for _ in range(300):
        feelers(hm, 0.0, 0.0, 2.0, 0.0, 1)
    ms = (time.perf_counter() - t0) / 300 * 1000.0
    print('selftest ok: house -> turn %.2f rad / %.2f rad, worst-case decision %.2f ms' % (r, r2, ms))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--dir', default=None, help='game folder (status goes to offhangar_user/brain_status.txt)')
    ap.add_argument('--port', type=int, default=27017)
    ap.add_argument('--selftest', action='store_true')
    a = ap.parse_args()
    if a.selftest:
        selftest()
        return
    status = None
    if a.dir:
        ud = os.path.join(a.dir, 'offhangar_user')
        if os.path.isdir(ud):
            status = os.path.join(ud, 'brain_status.txt')
    try:
        brain = Brain(a.port, status)
    except OSError:
        sys.exit(0)        # already running
    brain.run()


if __name__ == '__main__':
    main()
