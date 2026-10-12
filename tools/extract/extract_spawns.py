#!/usr/bin/env python3
"""Extract retail spawn points and base control points from WoT 0.8.2 map packages.

Every 0.8.2 map ships the server's own SpawnPoint user-data-objects in its
chunk files (spaces/<map>/*.chunk, packed XML): team, preferred vehicle class
('<any>' or 'SPG') and a visibilityMask whose bit N means gameplay type N
(constants.ARENA_GAMEPLAY_NAMES: ctf=0, domination=1, assault=2, ...).
Chunk positions are local: world = chunk grid index * 100 m + translation.

    python3 extract_spawns.py <folder with *.pkg> <out.json>
"""
import json, math, os, sys, zipfile, collections
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', 'scripts', 'offhangar_tools'))
import packedxml as p

GAMEPLAY = ('ctf', 'domination', 'assault', 'escort', 'ctf2', 'domination2', 'assault2')


def chunk_xz(name):
    s = name.split('/')[-1][:8]
    gx, gz = int(s[:4], 16), int(s[4:8], 16)
    if gx >= 0x8000: gx -= 0x10000
    if gz >= 0x8000: gz -= 0x10000
    return gx, gz


def modes(mask):
    return [g for i, g in enumerate(GAMEPLAY) if mask & (1 << i)]


def extract(pkg):
    z = zipfile.ZipFile(pkg)
    spawns, bases, udo_types = [], [], collections.Counter()
    for n in z.namelist():
        if not n.endswith('.chunk'):
            continue
        gx, gz = chunk_xz(n)
        try:
            r = p.parse(z.read(n))
        except Exception:
            continue
        if not isinstance(r, dict):
            continue
        for u in r.get('UserDataObject', []) or []:
            t = str((u.get('type') or ['?'])[0])
            udo_types[t] += 1
            if t != 'SpawnPoint':
                continue
            tr = u['transform'][0]
            pr = (u.get('properties') or [{}])[0]
            spawns.append({'team': int(pr.get('team', [0])[0]),
                           'cls': str(pr.get('preferableType', ['<any>'])[0]),
                           'modes': modes(int(u.get('visibilityMask', [0xFFFFFFFF])[0])),
                           'x': round(gx * 100 + tr[9], 2), 'y': round(tr[10], 2), 'z': round(gz * 100 + tr[11], 2),
                           'yaw': round(math.atan2(tr[6], tr[8]), 4)})
        for c in r.get('ControlPoint', []) or []:
            tr = c['transform'][0]
            bases.append({'team': int((c.get('team') or [0])[0]), 'baseID': int((c.get('baseID') or [0])[0]),
                          'radius': float((c.get('radius') or [0])[0]),
                          'modes': modes(int(c.get('visibilityMask', [0xFFFFFFFF])[0])),
                          'x': round(gx * 100 + tr[9], 2), 'y': round(tr[10], 2), 'z': round(gz * 100 + tr[11], 2)})
    return spawns, bases, udo_types


def main():
    src, out = sys.argv[1], sys.argv[2]
    doc = {}
    for f in sorted(os.listdir(src)):
        if not f.endswith('.pkg') or not f[:2].isdigit():
            continue
        name = f[:-4]
        spawns, bases, types = extract(os.path.join(src, f))
        doc[name] = {'spawns': [s for s in spawns if 'ctf' in s['modes']],
                     'bases': [b for b in bases if 'ctf' in b['modes']]}
        c = collections.Counter((s['team'], s['cls']) for s in doc[name]['spawns'])
        print('%-22s ctf spawns %s | ctf bases %d of %d | udo %s'
              % (name, dict(c), len(doc[name]['bases']), len(bases), dict(types)))
    for v in doc.values():
        for s in v['spawns']:
            del s['modes']
        for b in v['bases']:
            del b['modes']
    with open(out, 'w') as fh:
        json.dump(doc, fh, separators=(',', ':'), sort_keys=True)


if __name__ == '__main__':
    main()
