# WoT 0.8.2 LAN - step 1: transport test.
#
# Two clients find each other over UDP and exchange pings while sitting in the
# hangar. Nothing in the battle is touched yet. Results go to python.log
# (lines starting with [LAN]) and to offhangar_user/lanlink.txt.
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
import time

import BigWorld

PROTO = 'WOTLAN1'
USER_DIR = 'offhangar_user'
CFG_FILE = os.path.join(USER_DIR, 'lan.json')
STATUS_FILE = os.path.join(USER_DIR, 'lanlink.txt')

DEFAULT_CFG = {
	'role': 'host',
	'host_ip': '127.0.0.1',
	'port': 27015,
	'name': 'Player',
}

TICK = 0.0           # poll every frame
PING_EVERY = 1.0     # seconds between pings
PEER_TIMEOUT = 5.0   # seconds of silence before a peer is dropped
MAX_PACKET = 1400


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
				cfg[k] = user[k]
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
		self.peers = {}          # addr -> Peer
		self.seq = 0
		self.sent_at = {}        # seq -> send time
		self.last_ping = 0.0
		self.last_hello = 0.0
		self.last_status = 0.0
		self.started = time.time()
		self.errors = 0

	# --- socket ---------------------------------------------------------

	def start(self):
		s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
		if self.role == 'host':
			s.bind(('0.0.0.0', self.cfg['port']))
		else:
			s.bind(('0.0.0.0', 0))
		s.setblocking(0)
		self.sock = s
		_log('started as %s, bound %s:%d' % (self.role, '0.0.0.0', s.getsockname()[1]))
		if self.role == 'guest':
			self.host_addr = (str(self.cfg['host_ip']), self.cfg['port'])
			_log('will connect to %s:%d' % self.host_addr)
		BigWorld.callback(TICK, self.tick)

	def send(self, addr, msg):
		msg['p'] = PROTO
		try:
			data = json.dumps(msg)
			if len(data) > MAX_PACKET:
				_log('packet too big (%d), dropped' % len(data))
				return
			self.sock.sendto(data, addr)
		except socket.error as e:
			# WSAECONNRESET (10054) on Windows UDP after an ICMP unreachable: harmless
			if getattr(e, 'errno', None) not in (10054,) and (e.args and e.args[0]) != 10054:
				self.errors += 1
				if self.errors < 20:
					_log('send error %r' % (e,))

	def recv_all(self):
		out = []
		while True:
			try:
				data, addr = self.sock.recvfrom(4096)
			except socket.error as e:
				code = e.args[0] if e.args else None
				if code == 10054:
					continue
				break
			try:
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

		if t == 'hello' and self.role == 'host':
			if peer is None:
				peer = Peer(addr, msg.get('name', '?'))
				self.peers[addr] = peer
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

		if now - self.last_ping >= PING_EVERY:
			self.last_ping = now
			for addr, peer in self.peers.items():
				self.seq += 1
				self.sent_at[self.seq] = now
				peer.pings_sent += 1
				self.send(addr, {'t': 'ping', 's': self.seq})
			# forget pings that never came back
			for s in list(self.sent_at.keys()):
				if now - self.sent_at[s] > PEER_TIMEOUT:
					del self.sent_at[s]

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
		try:
			f = open(STATUS_FILE, 'w')
			try:
				out = '\n'.join(lines) + '\n'
				if isinstance(out, unicode):
					out = out.encode('utf-8')
				f.write(out)
			finally:
				f.close()
		except Exception:
			pass


g_lanLink = None


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
