# WoT 0.8.2 LAN - network link between a host and its guests.
#
# Step 1:   transport (UDP, hello/welcome, ping).
# Step 2.1: shared battle start - map and bot line-up come from the host.
# Step 2.2: players see each other. Each guest gets a line-up slot on the
#           host's team; on every client the other players are spawned as
#           "remote" tanks that are driven by the poses they send (20 Hz)
#           instead of the bot AI.
#
# Status goes to python.log (lines starting with [LAN]) and to
# offhangar_user/lanlink.txt.
#
# Config: <game folder>/offhangar_user/lan.json (created on first run):
#   role     "host" or "guest" ("off" disables the mod)
#   host_ip  address of the host (guest only)
#   port     UDP port the host listens on
#   name     nickname shown to the other players
#
# Python 2.6 (the client's interpreter): no dict/set comprehensions,
# str.format needs explicit indexes.

import json
import os
import socket
import sys
import time
import zlib

import BigWorld

PROTO = 'WOTLAN3'
USER_DIR = 'offhangar_user'
CFG_FILE = os.path.join(USER_DIR, 'lan.json')
STATUS_FILE = os.path.join(USER_DIR, 'lanlink.txt')
DEBUG_FILE = os.path.join(USER_DIR, 'lan_debug.log')
SLOW_FRAME = 0.25      # a gap between two ticks longer than this gets logged

DEFAULT_CFG = {
	'role': 'host',
	'host_ip': '127.0.0.1',
	'port': 27015,
	'name': 'Player',
}

TICK = 0.0             # poll every frame
PING_EVERY = 1.0       # seconds between pings
PEER_TIMEOUT = 5.0     # seconds of silence before a peer is dropped
RESEND_EVERY = 0.3     # reliable messages: resend interval
RESEND_FOR = 10.0      # reliable messages: give up after
COMPRESS_OVER = 900    # bytes of JSON above which a packet is zlib-compressed
MAX_PACKET = 8000      # hard cap after compression
POSE_EVERY = 0.05      # own tank pose: 20 per second
POSE_STALE = 2.0       # a remote pose older than this is not applied
BOTS_EVERY = 1.0 / 15  # host: bot states to the guests, 15 per second

HOST_ID = 'host'


def _log(msg):
	try:
		if isinstance(msg, unicode):
			msg = msg.encode('utf-8')
		print '[LAN] %s' % msg
	except Exception:
		pass


_dbg_count = [0]


def dbg(msg):
	'''Append one line to lan_debug.log and close the file right away, so the
	line is on disk even if the client hangs or is killed a moment later.'''
	_dbg_count[0] += 1
	if _dbg_count[0] > 20000:
		return
	try:
		if isinstance(msg, unicode):
			msg = msg.encode('utf-8')
		f = open(DEBUG_FILE, 'a')
		try:
			f.write('%s %s\n' % (time.strftime('%H:%M:%S'), msg))
		finally:
			f.close()
	except Exception:
		pass


def _s(v):
	'''JSON gives unicode; the client's APIs want byte strings.'''
	if isinstance(v, unicode):
		return v.encode('utf-8')
	return v


def _load_cfg():
	cfg = dict(DEFAULT_CFG)
	try:
		if not os.path.isdir(USER_DIR):
			os.makedirs(USER_DIR)
		if os.path.isfile(CFG_FILE):
			f = open(CFG_FILE, 'r')
			try:
				user = json.load(f)
			finally:
				f.close()
			for k in user:
				cfg[str(k)] = user[k]
		else:
			f = open(CFG_FILE, 'w')
			try:
				json.dump(DEFAULT_CFG, f, indent=4)
			finally:
				f.close()
			_log('created %s with defaults' % CFG_FILE)
	except Exception as e:
		_log('config error %r, using defaults' % (e,))
	cfg['role'] = str(cfg.get('role', 'host')).lower()
	cfg['port'] = int(cfg.get('port', 27015))
	cfg['name'] = _s(cfg.get('name', 'Player'))
	return cfg


def _sock_code(e):
	try:
		return e.args[0]
	except Exception:
		return None


def _r(v, n=3):
	return round(float(v), n)


class Peer(object):
	def __init__(self, addr, name, pid):
		self.addr = addr
		self.name = name
		self.pid = pid           # player id inside the session: 'host', 'g1', 'g2'...
		self.last_seen = time.time()
		self.rtt_ms = None
		self.rtt_min = None
		self.rtt_max = None
		self.pings_sent = 0
		self.pongs_got = 0
		self.veh = None          # tank this peer took into the current battle
		self.veh_bid = None


class LanLink(object):
	def __init__(self, cfg):
		self.cfg = cfg
		self.role = cfg['role']
		self.name = cfg['name']
		self.my_id = HOST_ID if self.role == 'host' else None
		self.my_veh = None
		self.sock = None
		self.host_addr = None
		self.peers = {}          # addr -> Peer
		self.next_gid = 1
		self.seq = 0
		self.sent_at = {}        # ping seq -> send time
		self.last_ping = 0.0
		self.last_hello = 0.0
		self.last_status = 0.0
		self.started = time.time()
		self.errors = 0
		# reliable delivery
		self.rid = 0
		self.pending = {}        # (addr, rid) -> [msg, next_send, give_up_at]
		self.seen_rids = {}      # addr -> {rid: time}
		# battle sync
		self.battle_id = None    # id of the LAN battle in progress (host or guest)
		self.in_lan_battle = False
		self.pending_start = False
		self.pending_start_at = 0.0
		self.forced_map = None   # guest: map to use for the next battle
		self.plan = None         # full line-up of the current battle (wire form, 7-tuples)
		self.plan_bid = None
		self.my_spawn = None     # (x, z, yaw) of this player's slot, from the line-up
		self.my_index = None     # this player's job index in the line-up (entity id 1000+index elsewhere)
		self.remote_eids = {}    # pid -> entity id of that player's tank on THIS client
		self.slots = {}          # pid -> line-up slot on the host's team (1, 2, ...)
		# poses
		self.last_pose_sent = 0.0
		self.poses = {}          # pid -> (recv_time, msg)
		self.poses_in = 0
		self.poses_out = 0
		# bots (host: last send; guest: last snapshot from the host)
		self.last_bots_sent = 0.0
		self.bots = {}           # eid -> state list
		self.bots_n = 0
		self.bots_time = 0.0
		self.bots_in = 0
		self.bots_out = 0
		self.last_event = ''
		# frame watch
		self.last_tick = 0.0
		self.frames = 0
		self.frame_max = 0.0
		self.last_beat = 0.0
		self.tick_sum = 0.0
		self.tick_n = 0
		self.tick_max = 0.0

	# --- socket ---------------------------------------------------------

	def start(self):
		s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
		if self.role == 'host':
			s.bind(('0.0.0.0', self.cfg['port']))
		else:
			s.bind(('0.0.0.0', 0))
		s.setblocking(0)
		self.sock = s
		try:
			open(DEBUG_FILE, 'w').close()
		except Exception:
			pass
		_log('started as %s, bound 0.0.0.0:%d' % (self.role, s.getsockname()[1]))
		dbg('started as %s %s' % (self.role, self.name))
		if self.role == 'guest':
			self.host_addr = (str(self.cfg['host_ip']), self.cfg['port'])
			_log('will connect to %s:%d' % self.host_addr)
		BigWorld.callback(TICK, self.tick)

	def send(self, addr, msg):
		msg['p'] = PROTO
		try:
			data = json.dumps(msg)
			if len(data) > COMPRESS_OVER:
				data = 'Z' + zlib.compress(data, 6)
			if len(data) > MAX_PACKET:
				_log('packet too big (%d bytes, t=%s), dropped' % (len(data), msg.get('t')))
				return
			self.sock.sendto(data, addr)
		except socket.error as e:
			# 10054 = WSAECONNRESET after an ICMP port-unreachable: harmless for UDP
			if _sock_code(e) != 10054:
				self.errors += 1
				if self.errors < 20:
					_log('send error %r' % (e,))

	def send_reliable(self, addr, msg):
		self.rid += 1
		msg['rid'] = self.rid
		now = time.time()
		self.pending[(addr, self.rid)] = [msg, now + RESEND_EVERY, now + RESEND_FOR]
		self.send(addr, dict(msg))

	def broadcast_reliable(self, msg):
		for addr in self.peers.keys():
			self.send_reliable(addr, dict(msg))

	def recv_all(self):
		out = []
		while True:
			try:
				data, addr = self.sock.recvfrom(65535)
			except socket.error as e:
				if _sock_code(e) == 10054:
					continue
				break
			try:
				if data[:1] == 'Z':
					data = zlib.decompress(data[1:])
				msg = json.loads(data)
			except Exception:
				continue
			if not isinstance(msg, dict) or msg.get('p') != PROTO:
				continue
			out.append((msg, addr))
		return out

	# --- protocol -------------------------------------------------------

	def handle(self, msg, addr):
		t = msg.get('t')
		now = time.time()
		peer = self.peers.get(addr)
		if peer is not None:
			peer.last_seen = now

		# Until the handshake is done only handshake traffic counts. A reliable
		# message must not be acked by a client that cannot act on it yet, or
		# the sender stops resending and the message is lost for good.
		if peer is None and t not in ('hello', 'welcome', 'ping', 'pong', 'ack'):
			return

		# reliable: ack every copy, act on the first one only
		rid = msg.get('rid')
		if rid is not None and t != 'ack':
			self.send(addr, {'t': 'ack', 'rid': rid})
			seen = self.seen_rids.setdefault(addr, {})
			if rid in seen:
				return
			seen[rid] = now

		if t == 'ack':
			self.pending.pop((addr, msg.get('rid')), None)

		elif t == 'hello' and self.role == 'host':
			if peer is None:
				peer = Peer(addr, _s(msg.get('name', '?')), 'g%d' % self.next_gid)
				self.next_gid += 1
				self.peers[addr] = peer
				self.seen_rids.pop(addr, None)
				_log('guest joined: %s (%s) from %s:%d' % (peer.name, peer.pid, addr[0], addr[1]))
			self.send(addr, {'t': 'welcome', 'name': self.name, 'id': peer.pid})

		elif t == 'welcome' and self.role == 'guest':
			if peer is None:
				peer = Peer(addr, _s(msg.get('name', '?')), HOST_ID)
				self.peers[addr] = peer
				self.my_id = _s(msg.get('id'))
				_log('connected to host: %s at %s:%d, my id %s' % (peer.name, addr[0], addr[1], self.my_id))

		elif t == 'ping':
			self.send(addr, {'t': 'pong', 's': msg.get('s')})

		elif t == 'pong' and peer is not None:
			sent = self.sent_at.pop(msg.get('s'), None)
			if sent is not None:
				rtt = (now - sent) * 1000.0
				peer.rtt_ms = rtt
				peer.pongs_got += 1
				if peer.rtt_min is None or rtt < peer.rtt_min:
					peer.rtt_min = rtt
				if peer.rtt_max is None or rtt > peer.rtt_max:
					peer.rtt_max = rtt

		elif t == 'bye' and peer is not None:
			_log('peer left: %s' % peer.name)
			del self.peers[addr]

		elif t == 'start' and self.role == 'guest' and peer is not None:
			self.on_start(msg)

		elif t == 'joined' and self.role == 'host' and peer is not None:
			peer.veh = _s(msg.get('veh'))
			peer.veh_bid = msg.get('bid')
			self.last_event = '%s is in battle %s on %s' % (peer.name, peer.veh_bid, peer.veh)
			_log(self.last_event)
			dbg(self.last_event)

		elif t == 'plan' and self.role == 'guest' and peer is not None:
			self.set_plan(self._jobs_from_wire(msg.get('jobs') or []), msg.get('bid'))
			self.last_event = 'plan received: %d tanks (battle %s), my slot %s' % (len(self.plan), self.plan_bid, self.my_spawn)
			_log(self.last_event)
			dbg(self.last_event)

		elif t == 'need_plan' and self.role == 'host' and peer is not None:
			if self.plan is not None and msg.get('bid') == self.plan_bid:
				self.send(addr, {'t': 'plan', 'bid': self.plan_bid, 'jobs': self._jobs_to_wire(self.plan)})

		elif t == 'bots' and self.role == 'guest' and peer is not None:
			if msg.get('bid') != self.battle_id:
				return
			n = msg.get('n', 0)
			if n <= self.bots_n and self.bots_n - n < 100000:
				return   # older than what we have
			self.bots_n = n
			self.bots_time = now
			self.bots_in += 1
			for st in msg.get('b') or []:
				try:
					self.bots[int(st[0])] = st
				except Exception:
					pass

		elif t == 'pose' and peer is not None:
			if msg.get('bid') != self.battle_id:
				return
			pid = _s(msg.get('id'))
			if pid is None or pid == self.my_id:
				return
			prev = self.poses.get(pid)
			if prev is not None and msg.get('n', 0) <= prev[1].get('n', 0) and prev[1].get('n', 0) - msg.get('n', 0) < 1000:
				return   # out of order / duplicate
			self.poses[pid] = (now, msg)
			self.poses_in += 1
			if self.role == 'host':
				# relay a guest's pose to the other guests
				for a, p in self.peers.items():
					if a != addr:
						self.send(a, dict(msg))

	# --- battle sync: start -------------------------------------------------

	def on_enqueued(self, map_id, map_name, veh_name):
		'''Called by offline_battle when this client starts a battle.'''
		self.my_veh = veh_name
		self.poses = {}
		self.bots = {}
		self.bots_n = 0
		self.my_spawn = None
		if self.role == 'host':
			self.plan = None
			self.plan_bid = None
			if not self.peers:
				self.in_lan_battle = False
				self.battle_id = None
				return
			self.battle_id = '%d' % int(time.time() * 1000)
			self.in_lan_battle = True
			# line-up slots for the guests: slot 0 is the host, guests follow
			self.slots = {}
			pids = [p.pid for p in self.peers.values()]
			pids.sort()
			for i in range(len(pids)):
				self.slots[pids[i]] = i + 1
			self.broadcast_reliable({'t': 'start', 'bid': self.battle_id,
			                         'map_id': map_id, 'map_name': map_name, 'slots': self.slots})
			self.last_event = 'battle %s started on %s, sent to %d guest(s)' % (self.battle_id, map_name, len(self.peers))
			_log(self.last_event)
			dbg(self.last_event)
		else:
			# a guest battle is a LAN battle only if the host started it
			self.in_lan_battle = self.pending_start and time.time() - self.pending_start_at < 20.0
			self.pending_start = False
			if not self.in_lan_battle:
				self.battle_id = None
				_log('local battle (not started by host)')
				return
			for addr in self.peers.keys():
				self.send_reliable(addr, {'t': 'joined', 'bid': self.battle_id, 'veh': veh_name, 'name': self.name})

	def on_start(self, msg):
		bid = msg.get('bid')
		if bid == self.battle_id:
			return
		self.battle_id = bid
		if self.plan_bid != bid:
			self.plan = None
			self.plan_bid = None
		self.slots = {}
		for k, v in (msg.get('slots') or {}).items():
			self.slots[_s(k)] = int(v)
		self.forced_map = msg.get('map_id')
		if self.forced_map is None:
			self.forced_map = msg.get('map_name')
		self.last_event = 'host started battle %s on %s' % (bid, msg.get('map_name'))
		_log(self.last_event)
		dbg(self.last_event)
		BigWorld.callback(0.0, self._start_local_battle)

	def _start_local_battle(self):
		try:
			player = BigWorld.player()
			if player is None or not getattr(player, 'isOffline', False):
				_log('cannot start: not in the offline hangar')
				return
			ob = sys.modules.get('gui.mods.offhangar.offline_battle')
			if ob is None:
				_log('cannot start: offline_battle module not loaded')
				return
			self.pending_start = True
			self.pending_start_at = time.time()
			ob.start_offline_random_from_hangar(player, 0)
		except Exception as e:
			self.pending_start = False
			_log('start failed %r' % (e,))

	def take_forced_map(self):
		'''Guest: map the host picked, consumed once by the battle builder.'''
		m = self.forced_map
		self.forced_map = None
		return m

	# --- battle sync: line-up ------------------------------------------------
	#
	# A line-up job is (team, x, z, yaw, vehicle, remote_id, display_name).
	# remote_id None = an ordinary bot. Otherwise the tank belongs to a player
	# and is driven by that player's poses on every other client.

	def host_make_plan(self, jobs, own_job):
		'''Host: the bots have just been rolled. Hand team slots to the guests
		that are in this battle, add the host's own tank, send it all out and
		return the jobs the host itself must spawn.'''
		if self.role != 'host' or not self.in_lan_battle:
			return list(jobs)
		full = []
		for j in jobs:
			full.append((j[0], j[1], j[2], j[3], j[4], None, None))
		team = own_job[0]
		by_slot = {}
		for p in self.peers.values():
			if p.veh and p.veh_bid == self.battle_id and p.pid in self.slots:
				by_slot[self.slots[p.pid]] = p
		# the team's bot jobs fill slots 1, 2, ... in order (slot 0 is the host)
		gi = 0
		ti = 0
		for i in range(len(full)):
			j = full[i]
			if j[0] != team:
				continue
			ti += 1
			g = by_slot.get(ti)
			if g is not None:
				full[i] = (j[0], j[1], j[2], j[3], g.veh, g.pid, g.name)
				gi += 1
		if gi < len(by_slot):
			_log('not enough team slots for all guests (%d of %d placed)' % (gi, len(by_slot)))
		full.append((own_job[0], own_job[1], own_job[2], own_job[3], own_job[4], HOST_ID, self.name))
		self.set_plan(full, self.battle_id)
		self.broadcast_reliable({'t': 'plan', 'bid': self.battle_id, 'jobs': self._jobs_to_wire(full)})
		self.last_event = 'plan sent: %d tanks, %d guest(s) placed (battle %s)' % (len(full), gi, self.battle_id)
		_log(self.last_event)
		return self.local_jobs()

	def set_plan(self, full, bid):
		self.plan = list(full)
		self.plan_bid = bid
		self.my_spawn = None
		self.my_index = None
		self.remote_eids = {}
		for i in range(len(self.plan)):
			j = self.plan[i]
			if j[5] is not None and j[5] == self.my_id:
				self.my_spawn = (j[1], j[2], j[3])
				self.my_index = i
			elif j[5] is not None:
				self.remote_eids[j[5]] = 1000 + i

	def local_jobs(self):
		'''Jobs this client spawns: everything except its own tank. Each job
		gets its index in the full line-up as an 8th field, so a tank has the
		same entity id (1000 + index) on every client even though each client
		skips a different job (its own tank).'''
		out = []
		plan = self.plan or []
		for i in range(len(plan)):
			j = plan[i]
			if j[5] is not None and j[5] == self.my_id:
				continue
			out.append((j[0], j[1], j[2], j[3], j[4], j[5], j[6], i))
		return out

	def guest_plan(self):
		'''Guest: jobs to spawn for the current LAN battle, or None if the
		line-up has not arrived yet (asks the host again).'''
		if self.role != 'guest' or not self.in_lan_battle:
			return None
		if self.plan is not None and self.plan_bid == self.battle_id:
			return self.local_jobs()
		for addr in self.peers.keys():
			self.send(addr, {'t': 'need_plan', 'bid': self.battle_id})
		return None

	def guest_slot(self):
		'''Guest: its line-up slot on the host's team, None outside a LAN battle.'''
		if self.role != 'guest' or not self.in_lan_battle:
			return None
		return self.slots.get(self.my_id)

	# --- poses ----------------------------------------------------------------

	def send_pose(self, pos, ypr, turret_yaw, gun_pitch, speed):
		if not self.in_lan_battle or self.my_id is None or not self.peers:
			return
		now = time.time()
		if now - self.last_pose_sent < POSE_EVERY:
			return
		self.last_pose_sent = now
		self.seq += 1
		msg = {'t': 'pose', 'bid': self.battle_id, 'id': self.my_id, 'n': self.seq,
		       'x': [_r(pos[0]), _r(pos[1]), _r(pos[2])],
		       'r': [_r(ypr[0], 4), _r(ypr[1], 4), _r(ypr[2], 4)],
		       'ty': _r(turret_yaw, 4), 'gp': _r(gun_pitch, 4), 'v': _r(speed, 2)}
		for addr in self.peers.keys():
			self.send(addr, dict(msg))
		self.poses_out += 1

	def note_tick(self, dt):
		'''Battle tick duration of one frame (from offline_battle).'''
		self.tick_sum += dt
		self.tick_n += 1
		if dt > self.tick_max:
			self.tick_max = dt

	def bots_due(self):
		'''Host: time to send the next bot snapshot?'''
		if not self.peers:
			return False
		now = time.time()
		if now - self.last_bots_sent < BOTS_EVERY:
			return False
		self.last_bots_sent = now
		return True

	def send_bots(self, states):
		'''Host: one snapshot of all bots (compressed when large).'''
		self.seq += 1
		msg = {'t': 'bots', 'bid': self.battle_id, 'n': self.seq, 'b': states}
		for addr in self.peers.keys():
			self.send(addr, dict(msg))
		self.bots_out += 1

	def local_killer(self, killer, my_vid):
		'''Turn a killer id from another client into this client's id: the
		host's own tank travels as 'host', a player's tank as 1000+index -
		which on that player's own client is the player vehicle.'''
		if killer == HOST_ID or killer == u'host':
			return self.remote_eids.get(HOST_ID, -1)
		try:
			killer = int(killer)
		except Exception:
			return -1
		if self.my_index is not None and killer == 1000 + self.my_index:
			return my_vid
		return killer

	def bot_state(self, eid):
		'''Guest: the host's latest state of a bot, None if unknown or stale.'''
		if time.time() - self.bots_time > POSE_STALE:
			return None
		return self.bots.get(eid)

	def remote_pose(self, pid):
		'''Latest pose of a remote player, or None when none / too old.'''
		entry = self.poses.get(pid)
		if entry is None or time.time() - entry[0] > POSE_STALE:
			return None
		return entry[1]

	# --- helpers ----------------------------------------------------------

	def _jobs_to_wire(self, jobs):
		out = []
		for j in jobs:
			out.append([j[0], j[1], j[2], j[3], j[4], j[5], j[6]])
		return out

	def _jobs_from_wire(self, jobs):
		out = []
		for j in jobs:
			rid = None
			name = None
			if len(j) > 6:
				rid = _s(j[5])
				name = _s(j[6])
			out.append((int(j[0]), float(j[1]), float(j[2]), float(j[3]), _s(j[4]), rid, name))
		return out

	# --- main loop --------------------------------------------------------

	def tick(self):
		try:
			self._tick()
		except Exception as e:
			_log('tick error %r' % (e,))
		BigWorld.callback(TICK, self.tick)

	def _tick(self):
		now = time.time()
		if self.last_tick:
			gap = now - self.last_tick
			if gap > self.frame_max:
				self.frame_max = gap
			if gap > SLOW_FRAME:
				dbg('slow frame %.2fs' % gap)
		self.last_tick = now
		self.frames += 1
		if self.in_lan_battle and now - self.last_beat >= 1.0:
			dbg('beat fps=%d max_frame=%.0fms tick avg=%.0fms max=%.0fms peers=%d bots=%d poses=%d' % (
				self.frames / max(0.001, now - self.last_beat) if self.last_beat else 0,
				self.frame_max * 1000.0,
				(self.tick_sum / self.tick_n * 1000.0) if self.tick_n else -1, self.tick_max * 1000.0,
				len(self.peers), len(self.bots), len(self.poses)))
			self.last_beat = now
			self.frames = 0
			self.frame_max = 0.0
			self.tick_sum = 0.0
			self.tick_n = 0
			self.tick_max = 0.0
		for msg, addr in self.recv_all():
			self.handle(msg, addr)

		if self.role == 'guest' and not self.peers and now - self.last_hello >= 1.0:
			self.last_hello = now
			self.send(self.host_addr, {'t': 'hello', 'name': self.name})

		for key in list(self.pending.keys()):
			entry = self.pending.get(key)
			if entry is None:
				continue
			msg, next_send, give_up = entry
			if now >= give_up or key[0] not in self.peers:
				del self.pending[key]
				continue
			if now >= next_send:
				entry[1] = now + RESEND_EVERY
				self.send(key[0], dict(msg))

		if now - self.last_ping >= PING_EVERY:
			self.last_ping = now
			for addr, peer in self.peers.items():
				self.seq += 1
				self.sent_at[self.seq] = now
				peer.pings_sent += 1
				self.send(addr, {'t': 'ping', 's': self.seq})
			for s in list(self.sent_at.keys()):
				if now - self.sent_at[s] > PEER_TIMEOUT:
					del self.sent_at[s]
			for addr in list(self.seen_rids.keys()):
				seen = self.seen_rids[addr]
				for r in list(seen.keys()):
					if now - seen[r] > 60.0:
						del seen[r]

		for addr, peer in list(self.peers.items()):
			if now - peer.last_seen > PEER_TIMEOUT:
				_log('peer timed out: %s' % peer.name)
				dbg('peer timed out: %s' % peer.name)
				del self.peers[addr]

		if now - self.last_status >= 2.0:
			self.last_status = now
			self.write_status()

	def write_status(self):
		lines = ['role: %s  name: %s  id: %s  uptime: %ds' % (self.role, self.name, self.my_id, int(time.time() - self.started))]
		if self.role == 'guest':
			lines.append('host: %s:%d' % self.host_addr)
		if not self.peers:
			lines.append('peers: none' + (' (waiting for host...)' if self.role == 'guest' else ' (waiting for guests...)'))
		for addr, p in self.peers.items():
			lost = 0
			if p.pings_sent:
				lost = 100.0 * (p.pings_sent - p.pongs_got) / p.pings_sent
			rtt = '-' if p.rtt_ms is None else '%.1f ms (min %.1f, max %.1f)' % (p.rtt_ms, p.rtt_min, p.rtt_max)
			lines.append('peer %s [%s] %s:%d  rtt %s  loss %.0f%%' % (p.name, p.pid, addr[0], addr[1], rtt, lost))
		lines.append('lan battle: %s  id: %s  my tank: %s  slots: %s' % (
			'yes' if self.in_lan_battle else 'no', self.battle_id, self.my_veh, self.slots))
		lines.append('poses: sent %d, received %d' % (self.poses_out, self.poses_in))
		lines.append('bot snapshots: sent %d, received %d, bots known %d' % (self.bots_out, self.bots_in, len(self.bots)))
		for pid in sorted(self.poses.keys()):
			age = time.time() - self.poses[pid][0]
			lines.append('  %s: last pose %.1fs ago at %s' % (pid, age, self.poses[pid][1].get('x')))
		if self.last_event:
			lines.append('last: %s' % self.last_event)
		try:
			out = '\n'.join(lines) + '\n'
			if isinstance(out, unicode):
				out = out.encode('utf-8')
			f = open(STATUS_FILE, 'w')
			try:
				f.write(out)
			finally:
				f.close()
		except Exception:
			pass


g_lanLink = None


def get():
	'''The running link, or None when LAN is off.'''
	return g_lanLink


def _init():
	global g_lanLink
	cfg = _load_cfg()
	if cfg['role'] not in ('host', 'guest'):
		_log('disabled (role=%s)' % cfg['role'])
		return
	g_lanLink = LanLink(cfg)
	g_lanLink.start()


try:
	_init()
except Exception as e:
	_log('init failed %r' % (e,))
