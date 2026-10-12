# What is inside the WoT 0.8.2 client (notes from reading it)

Read on 2026-10-12 from the RU 0.8.2 #349 client: all 486 Python modules of
`res/scripts` and `res_bw/scripts` decompiled (485 cleanly, about 115,000
lines; the decompiled source is not part of this repository), the entity and
user-data-object definitions, `arena_defs`, the graphics settings in
`misc.pkg`, every map package's chunk files, and the strings of
`WorldOfTanks.exe`.

## How the client is built

- BigWorld engine 2012 (`WorldOfTanks.exe`, 32-bit, DirectX 9) with Python
  2.6 compiled in. Rendering, collision, Umbra occlusion culling, chunk
  streaming and sound are C++; the game logic the client has is Python.
- The battle itself is decided on the server: hits, damage, spotting, base
  capture, bot behaviour. The client mostly shows what the server says.
  That is why the offline mod has to recreate all of it in Python.
- Entities: `Account` (hangar), `PlayerAvatar` (the player in battle),
  `Vehicle`, `AreaDestructibles`, `ClientArena`, `TeamBase` and others
  (`res/scripts/entity_defs`).
- The offline mod runs the battle inside the `Account` entity, without a
  `PlayerAvatar`. Everything `PlayerAvatar.onBecomePlayer` / `onEnterWorld`
  normally sets up for a battle space is therefore skipped unless the mod does
  it itself (see below).

Biggest modules: `common/items/vehicles.py` (vehicle descriptors, 3400
lines), `client/avatar.py` (the player in battle, 2300), `control_modes.py`
(arcade / sniper / artillery cameras and aiming, 2650),
`vehicleappearance.py` (models, tracks, effects, 1800),
`gui/scaleform/battle.py` (battle interface, 2000),
`common/physics_shared.py` (vehicle physics constants and formulas).

## Findings that matter for this mod

1. **Real spawn points.** Every map's chunks contain the server's
   `SpawnPoint` objects: team, preferred class (`heavyTank`, `mediumTank`,
   `lightTank`, `AT-SPG`, `SPG`, `<any>`), facing, and a visibility mask
   saying for which gameplay type they are (bit 0 ctf = standard battle,
   1 encounter, 2 assault, 4 ctf2). Every map has 30 standard-battle points
   per team. The upstream mod never had them (arena_defs only carry base
   positions for ctf) and lined the tanks up in a grid around the flag.
   Extracted for all 33 maps into `offhangar/spawn_points.json`
   (`tools/extract/extract_spawns.py`); used when `retail_spawns` is on.
2. **The "heap of bases".** The chunks also hold the bases (`ControlPoint`)
   of all gameplay types - Malinovka has 6 for 2 used in a standard battle.
   The server normally tells the client which gameplay type is played; the
   exe has the native `BigWorld.wg_setSpaceItemsVisibilityMask` for it. The
   offline battle never set it, so every flag of every mode was drawn.
   `retail_space_setup` sets the standard-battle mask.
3. **Umbra occlusion culling** is switched on by `PlayerAvatar.onEnterWorld`
   (`wg_setUmbraEnabled(arenaType.umbraEnabled)`, 1 on every map). The
   offline battle skipped it as well; `retail_space_setup` switches it on.
4. **No navigation mesh in the client.** Chunks only carry a `navmeshDirty`
   flag; the navmesh was server data. Bot navigation must keep measuring the
   map itself (nav grid, height cache).
5. **Bot leftovers from 2012.** The client defines `PatrolNode`,
   `SimplePatrolNode` and `AutoBotSpawnPoint` user data objects (patrol
   graphs with links, radius, wait time) - WG's internal test bots - but no
   0.8.2 map contains any.
6. **Terrain height maps** are inside every `.cdata` (`terrain2/heights`),
   and static models are listed per chunk: a future tool could build the
   bots' obstacle map offline instead of measuring it in battle.
7. **Graphics presets** are in `misc.pkg` `system/data/graphics_settings_presets.xml`
   (maximum, high, medium, low, minimum); the launcher writes them.
8. Handy natives found in the exe / scripts: `wg_collideSegment`,
   `wg_collideWater`, `wg_getSpaceBounds`, `wg_setSpaceItemsVisibilityMask`,
   `wg_setUmbraEnabled`, `wg_setBatchingEnabled`, `wg_enableTreeHiding`,
   `wg_setReducedFpsMode`, `wg_getPreferencesFilePath`.
