# WoT 0.8.2 LAN - network link between a host and its guests.
#
# Step 1: transport (UDP, hello/welcome, ping).
# Step 2.1: shared battle start. When the host presses "Battle", the map and
# the bot line-up are sent to every guest; the guest's client starts the same
# battle by itself and spawns the host's bots instead of rolling its own.
#
# Status goes to python.log (lines starting with [LAN]) and to
# offhangar_user/lanlink.txt.
#
# Config: <game folder>/offhangar_user/lan.json (created on first run):
#   role     "host" or "guest" ("off" disables the mod)
#   host_ip  address of the host (guest only)
#   port     UDP port the host listens on
#   name     nickname shown to the other side
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

PROTO = 'WOTLAN2'
USER_DIR = 'offhangar_user'
CFG_FILE = os.path.join(USER_DIR, 'lan.json')
STATUS_FILE = os.path.join(USER_DIR, 'lanlink.txt')

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


def _log(msg):
	try:
		if isinstance(msg, unicode):
			msg = msg.encode('utf-8')
		print '[LAN] %s' % msg
	except Exception:
		pass


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
	return cfg


def _sock_code(e):
	try:
		return e.args[0]
	except Exception:
		return None


class Peer(object):
	def __init__(self, addr, name):
		self.addr = addr
		self.name = name
		self.last_seen = time.time()
		self.rtt_ms = None
		self.rtt_min = None
		self.rtt_max = None
		self.pings_sent = 0
		self.pongs_got = 0


class LanLink(object):
	def __init__(self, cfg):
		self.cfg = cfg
		self.role = cfg['role']
		self.name = cfg.get('name', 'Player')
		self.sock = None
		self.host_addr = None
		self.peers = {}          # addr -> Peer
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
		self.plan = None         # host: last line-up sent; guest: line-up received
		self.plan_bid = None
		self.last_event = ''

	# --- socket ---------------------------------------------------------

	def start(self):
		s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
		if self.role == 'host':
			s.bind(('0.0.0.0', self.cfg['port']))
		else:
			s.bind(('0.0.0.0', 0))
		s.setblocking(0)
		self.sock = s
		_log('started as %s, bound 0.0.0.0:%d' % (self.role, s.getsockname()[1]))
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
				peer = Peer(addr, msg.get('name', '?'))
				self.peers[addr] = peer
				self.seen_rids.pop(addr, None)
				_log('guest joined: %s from %s:%d' % (peer.name, addr[0], addr[1]))
			self.send(addr, {'t': 'welcome', 'name': self.name})

		elif t == 'welcome' and self.role == 'guest':
			if peer is None:
				peer = Peer(addr, msg.get('name', '?'))
				self.peers[addr] = peer
				_log('connected to host: %s at %s:%d' % (peer.name, addr[0], addr[1]))

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

		elif t == 'plan' and self.role == 'guest' and peer is not None:
			self.plan = self._jobs_from_wire(msg.get('jobs') or [])
			self.plan_bid = msg.get('bid')
			self.last_event = 'plan received: %d bots (battle %s)' % (len(self.plan), self.plan_bid)
			_log(self.last_event)

		elif t == 'need_plan' and self.role == 'host' and peer is not None:
			if self.plan is not None and msg.get('bid') == self.plan_bid:
				self.send(addr, {'t': 'plan', 'bid': self.plan_bid, 'jobs': self._jobs_to_wire(self.plan)})

	# --- battle sync: host side ------------------------------------------

	def on_enqueued(self, map_id, map_name):
		'''Called by offline_battle when this client starts a battle.'''
		if self.role == 'host':
			self.plan = None
			self.plan_bid = None
			if not self.peers:
				self.in_lan_battle = False
				self.battle_id = None
				return
			self.battle_id = '%d' % int(time.time() * 1000)
			self.in_lan_battle = True
			self.broadcast_reliable({'t': 'start', 'bid': self.battle_id,
			                         'map_id': map_id, 'map_name': map_name})
			self.last_event = 'battle %s started on %s, sent to %d guest(s)' % (self.battle_id, map_name, len(self.peers))
			_log(self.last_event)
		else:
			# a guest battle is a LAN battle only if the host started it
			self.in_lan_battle = self.pending_start and time.time() - self.pending_start_at < 20.0
			self.pending_start = False
			if not self.in_lan_battle:
				self.battle_id = None
				_log('local battle (not started by host)')

	def host_set_plan(self, jobs):
		'''Host: the bot line-up for the current battle has been rolled.'''
		if self.role != 'host' or not self.in_lan_battle:
			return
		self.plan = list(jobs)
		self.plan_bid = self.battle_id
		self.broadcast_reliable({'t': 'plan', 'bid': self.battle_id, 'jobs': self._jobs_to_wire(self.plan)})
		self.last_event = 'plan sent: %d bots (battle %s)' % (len(self.plan), self.battle_id)
		_log(self.last_event)

	# --- battle sync: guest side ------------------------------------------

	def on_start(self, msg):
		bid = msg.get('bid')
		if bid == self.battle_id:
			return
		self.battle_id = bid
		if self.plan_bid != bid:
			self.plan = None
			self.plan_bid = None
		self.forced_map = msg.get('map_id')
		if self.forced_map is None:
			self.forced_map = msg.get('map_name')
		self.last_event = 'host started battle %s on %s' % (bid, msg.get('map_name'))
		_log(self.last_event)
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

	def guest_plan(self):
		'''Guest: line-up for the current LAN battle, or None if not here yet.'''
		if self.role != 'guest' or not self.in_lan_battle:
			return None
		if self.plan is not None and self.plan_bid == self.battle_id:
			return self.plan
		if self.host_addr is not None:
			for addr in self.peers.keys():
				self.send(addr, {'t': 'need_plan', 'bid': self.battle_id})
		return None

	# --- helpers ----------------------------------------------------------

	def _jobs_to_wire(self, jobs):
		out = []
		for j in jobs:
			out.append([j[0], j[1], j[2], j[3], j[4]])
		return out

	def _jobs_from_wire(self, jobs):
		out = []
		for j in jobs:
			vn = j[4]
			if isinstance(vn, unicode):
				vn = vn.encode('utf-8')
			out.append((int(j[0]), float(j[1]), float(j[2]), float(j[3]), vn))
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
				del self.peers[addr]

		if now - self.last_status >= 2.0:
			self.last_status = now
			self.write_status()

	def write_status(self):
		lines = ['role: %s  name: %s  uptime: %ds' % (self.role, self.name, int(time.time() - self.started))]
		if self.role == 'guest':
			lines.append('host: %s:%d' % self.host_addr)
		if not self.peers:
			lines.append('peers: none' + (' (waiting for host...)' if self.role == 'guest' else ' (waiting for guests...)'))
		for addr, p in self.peers.items():
			lost = 0
			if p.pings_sent:
				lost = 100.0 * (p.pings_sent - p.pongs_got) / p.pings_sent
			rtt = '-' if p.rtt_ms is None else '%.1f ms (min %.1f, max %.1f)' % (p.rtt_ms, p.rtt_min, p.rtt_max)
			lines.append('peer %s %s:%d  rtt %s  loss %.0f%%' % (p.name, addr[0], addr[1], rtt, lost))
		lines.append('lan battle: %s  id: %s' % ('yes' if self.in_lan_battle else 'no', self.battle_id))
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
