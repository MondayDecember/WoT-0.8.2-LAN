# LAN for WoT 0.8.2 offline battles

Work in progress fork of [fr3asikx/WoT-0.8.2-offline-Battles](https://github.com/fr3asikx/WoT-0.8.2-offline-Battles).
Goal: several players in one offline battle, one PC acting as the host.

## Status

| Step | What | State |
|------|------|-------|
| 1 | UDP transport, host/guest handshake, ping | done |
| 2.1 | Host starts a battle, guests start the same map with the same bot line-up | done |
| 2.2 | Players see each other's tanks (position, hull, turret, gun) | done |
| 2.3 | Guest bots follow the host's simulation | done (verify on two PCs) |
| 3 | Shooting, damage, deaths decided by the host | testing |
| 3+ | Capture bar, results, stats, chat, lobby, launcher, module crits | testing |
| 4 | Play over the internet (WireGuard / port forward) | todo |

## Setup

Config file `<game folder>/offhangar_user/lan.json` (created on first start):

```json
{"role": "host", "host_ip": "127.0.0.1", "port": 27015, "name": "Igor", "key": "secret"}
```

- `role`: `host`, `guest` or `off`
- `host_ip`: the host's address (guests only)
- `port`: UDP port the host listens on (open it in the firewall on the host)
- `name`: your nickname, also used as your name in battle unless
  `config.json` has a `nickname` of its own
- `key`: optional session password, the same on the host and every guest;
  the host ignores guests with a wrong or missing key
- `uid`: your permanent player id, written on first start - leave it alone

A guest connected to a host cannot start a battle itself (the hangar says
so); the host's Battle button starts it for everyone. Without a host a guest
plays ordinary solo battles.

Status of the link: `<game folder>/offhangar_user/lanlink.txt`.

## How a LAN battle starts

1. Guests sit in the hangar, connected to the host (`lanlink.txt` shows the peer).
2. The host presses Battle. Map and battle id go to every guest; each guest's
   client starts a battle on that map by itself.
3. When the host rolls its bot line-up, the list (team, slot position, yaw,
   tank) goes to the guests, and they spawn exactly those bots in the same
   order, so the bot ids (1000+n) match on every client.

A guest that presses Battle on its own gets an ordinary local battle.

## How players see each other

- At battle start the host hands out line-up slots on its team: slot 0 is the
  host, guests get 1, 2, ... in order of joining. Each guest stands in its slot.
- When the line-up is rolled, a guest's slot job carries that guest's tank and
  a remote id; the host's own tank is added as an extra job with id `host`.
  Every client spawns all jobs except its own tank.
- A remote tank is skipped by the bot AI and by bot targeting on that client;
  it is driven by the poses its owner sends (position, hull yaw/pitch/roll,
  turret yaw, gun pitch, speed - 20 per second). The host relays guest poses to
  the other guests.

## Shared bots

Bots are simulated on the host only. About 15 times a second the host sends
every bot's state (position, hull, turret, gun, speed, hit points, alive,
spotted by the team, killer). On a guest the bot AI, routes and nav grid are
switched off; bots follow those states. A bot the host reports dead goes to
0 HP and the guest's normal kill path makes the wreck. Enemy visibility on the
guest follows the host's team spotting.

Entity ids are 1000 + the job's index in the shared line-up on every client.
Killer ids are translated: the host's own tank travels as `host`, a player's
tank as its line-up id (which on that player's client is the player vehicle).

## Shooting

- Every shot fired on any client (a bot on the host, or a player) goes out
  as a `shot` event: shooter, shell index, muzzle position, velocity and the
  point where it struck a tank. The others play the muzzle flash, the recoil
  and the tracer up to that point. The host relays guests' shots.
- Bots on the host target guests' tanks like any other tank. The damage those
  shots do is computed on the host, against the guest's tank there.
- Every bot snapshot also carries each player's hit points, alive flag and
  killer (`pl`). A guest takes its own hit points from there (only ever
  downward) and dies through its normal death path; remote players' tanks
  follow the same numbers.
- A guest's own shell is resolved on the guest (it sees the same bots in the
  same places). When it damages a bot, the amount goes to the host (`dmg`,
  reliable) and the host applies it to the real bot, credited to that guest's
  tank. The guest keeps its lowered hit points for up to 1.5 s until the
  host's snapshot catches up.
- Damage a player takes locally (falls, rams) travels in its pose (`hp`) and
  lowers its tank on the host.

## End of battle

The host decides: team wipe, timer and base capture are evaluated on the
host only. Its outcome (winner, reason) goes to every guest (`end`,
reliable) and the guest finishes the battle the same way. While a guest is
in a LAN battle and still hears its host, its own end checks and base
capture are off; if the host disappears, the guest falls back to its own.

Guests see the host's base capture bar (points ride on the bot snapshot).

At the end the host also sends its battle ledger; the guest loads it (ids
mapped) before its results screen is built, so every player's damage, hits
and frags there are the host's numbers.

## Damage to a guest's tank beyond hit points

The snapshot's player entries carry the host-side tank's destroyed modules,
downed crew and fire. A guest applies only what is NEW since the previous
snapshot (damage panel, crew voice, mobility flags, fire); repair, medkits
and extinguishers then run on the guest as usual.

## Chat

- F5 / F6 / F7 in a LAN battle: quick messages to everyone
  ("Атакую!", "Нужна помощь!", "Отступаем!").
- Typed chat: the client's broadcast chat command is forwarded (the action
  id comes from chat_shared; every chat command is logged to lan_debug.log
  so this can be verified in game).
- Incoming lines: battle chat in battle, notifications in the hangar.

## Stats

The host books every LAN battle into `offhangar_user/lan_stats.json` by
player uid: battles, wins, draws, damage, frags, survived, last played.
The launcher's Статистика tab shows it.

## Launcher

Copy `tools/WoT-LAN-Launcher.ps1` and `tools/WoT LAN.cmd` into the game
folder and start `WoT LAN.cmd`: nickname, host / join / solo, host IP, port,
password, lobby and room; it writes `lan.json` and starts the game, and shows
the link status while open.

Not synced yet: hit sparks at the target on other clients, module damage
that is not a full destruction (yellow modules) on a guest.

## Lobby (optional): online list and joining by room name

`tools/lobby_server.py` is a tiny UDP server (Python 3, standard library,
stores nothing on disk). Run it on any always-on machine with a public IP
and forward **UDP 27016** to it (`tools/lobby_server.cmd` on Windows, or
`python3 lobby_server.py` on Linux).

In `lan.json` (or the launcher) of everyone:

```json
"lobby": "<lobby address>:27016", "room": "tanki"
```

- Everyone registers every 5 s; the hangar's notification list says who is
  online, who came and who left (`X в сети`, `X вышел`), hosts with their room.
- A guest with a `room` and no `host_ip` asks the lobby for that room's host
  and gets its public and LAN addresses - no IP to type in. The host's game
  port must still be reachable (forwarded) for guests outside its LAN.
- The lobby only hands the address to a guest whose key hash matches the
  host's; the key itself never leaves the clients.

## Playing over the internet

With a public (white) IP on the host's router:

1. Router: forward **UDP 27015** to the host PC's LAN address.
2. Host PC: allow it in Windows Firewall (Private profile is not enough for
   traffic from outside - use Any, or the profile of the active network):

   ```powershell
   New-NetFirewallRule -DisplayName "WoT 0.8.2 LAN (internet)" -Direction Inbound -Protocol UDP -LocalPort 27015 -Program "C:\World_of_Tanks_0.08.02.00.00_RU_0349_SD\WorldOfTanks.exe" -Action Allow -Profile Any
   ```

3. Host `lan.json`: set a `key`.
4. Each friend's `lan.json`: `"role": "guest"`, `"host_ip": "<host's public IP>"`,
   the same `key`, their own `name`.

Without a public IP: put everyone in one WireGuard network and use the host's
WireGuard address as `host_ip`.

Over the internet expect 30-100 ms ping: movement is smoothed, shots and
damage are decided by the host, so a guest may see a hit register a moment
after the tracer lands.

## Two clients on one PC

The client refuses a second instance (`wot_client_mutex`).
`tools/wot_lan_test.ps1` starts the host, closes that mutex with Sysinternals
`handle64.exe`, then starts the guest from a second copy of the game folder.
Run it from an elevated PowerShell.
