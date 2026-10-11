# LAN for WoT 0.8.2 offline battles

Work in progress fork of [fr3asikx/WoT-0.8.2-offline-Battles](https://github.com/fr3asikx/WoT-0.8.2-offline-Battles).
Goal: several players in one offline battle, one PC acting as the host.

## Status

| Step | What | State |
|------|------|-------|
| 1 | UDP transport, host/guest handshake, ping | done |
| 2.1 | Host starts a battle, guests start the same map with the same bot line-up | done |
| 2.2 | Players see each other's tanks (position, hull, turret, gun) | testing |
| 2.3 | Guest bots follow the host's simulation | todo |
| 3 | Shooting, damage, deaths decided by the host | todo |
| 4 | Play over the internet (WireGuard / port forward) | todo |

## Setup

Config file `<game folder>/offhangar_user/lan.json` (created on first start):

```json
{"role": "host", "host_ip": "127.0.0.1", "port": 27015, "name": "Igor"}
```

- `role`: `host`, `guest` or `off`
- `host_ip`: the host's address (guests only)
- `port`: UDP port the host listens on (open it in the firewall on the host)

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

Not synced yet: shots, damage, deaths, and the bots' own movement (each client
still runs its own bot AI).

## Two clients on one PC

The client refuses a second instance (`wot_client_mutex`).
`tools/wot_lan_test.ps1` starts the host, closes that mutex with Sysinternals
`handle64.exe`, then starts the guest from a second copy of the game folder.
Run it from an elevated PowerShell.
