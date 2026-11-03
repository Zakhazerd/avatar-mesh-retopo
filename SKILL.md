---
name: avatar-mesh-retopo
description: Lower the triangle count of a VRChat/avatar mesh in Blender (through a Blender MCP connection) without breaking shape keys, weights, UVs, vertex colours or normals. The user picks up to six versions (loop levels LOD1/LOD2/LOD3, each with or without shape-key protection), which can be built in parallel by agents. Covers backups and full duplicates, edge-loop dissolve that keeps clean quads, guards for clothing, shape keys, silhouette and small features, data-exact verification, n-gon cleanup, background comparison renders, metrics, and an in-Blender comparison with a posable rig. Use when asked to reduce an avatar's polycount, retopo a garment or body part, or compare mesh reduction methods.
---

# Avatar mesh retopo in Blender

A tested workflow for cutting triangles on skinned avatar meshes (clothing and bodies with hundreds of shape keys) so the result can ship to Unity/VRChat. It was worked out on one avatar:

- **A Marvelous Designer shirt:** 22,246 → 9,595 tris.
- **A body with 564 shape keys, neck down:** 62,686 → 45,086 tris. Every kept vertex has exactly its original data.

The scripts in `scripts/` are general. Everything avatar-specific is in **`scripts/avatar_config.py`**, which every script reads: object names, garments, the region, feature masks, hide keys, bone roles and facing direction, check textures. Set the `HERE` path at the top of the live-Blender scripts (`build_region.py`, `density_report.py`, `build_variants.py`, `setup_compare.py`; marked `<set me ...>`), and fill in the config before anything else (section 1).

## 0. Model: Opus 5.5 or better
- **Why it matters:** this workflow is long and check-heavy, with dozens of live-Blender steps, exactness proofs, n-gon reviews and reading renders. The mistakes it guards against are easy to miss: data that's no longer exact, n-gons reported as "five-sided" when they're 27-sided slivers, skin poking through a stocking, a normals seam.
- **Before starting, check which model you are.** If you're less capable than **Claude Opus 5.5** (for example a Sonnet or Haiku model, or an older Opus), tell the user plainly that the results may be wrong in ways that are hard to spot. Suggest they switch to Opus 5.5 or better first, and continue only if they say so.
- **Agents you dispatch need it too:** they must run on Opus 5.5 or better. A fork inherits your model; for a fresh agent, set the model explicitly.

## 0.5 Working with the user
- **"This" and "the selected":** when the user says "the selected faces", "this", or "look at what I have selected", read the current selection in Blender first.
- **A "why" is a question:** when they ask why something is the way it is, answer with the actual source (a measurement, the code, a guess) and how sure you are. Change nothing until they ask for a change.
- **Check before stating absolutes** ("always", "never", "impossible", "this will fail"): measure or test the claim first, say how sure you are, and ask whether it matches what they know.
- **Ask before saving a new rule** about how you work (a memory, a CLAUDE.md line, a hook). Propose the wording first.
- **Reach them where they are:** send renders and sheets as files (they may follow from another device), and show changes in Blender with a screenshot before any export.
- **Deleting:** remove versions or files only when the user says so. Many avatar makers keep old things "just in case".
- **Retopo is usually the last step:** it permanently lowers quality. Before cutting triangles, ask what the goal is. For a VRChat performance rank, material count and skinned-mesh count often matter more than the triangle count, so check those first.

## 1. Fill in the config, then ask the user
**First, the config.** Run `scripts/inspect_avatar.py` in the live Blender. It's read-only and lists the candidates:
- skinned meshes, with a guess at the body and the garments visible on the same rig;
- the bone roles, guessed from common naming schemes;
- the facing direction, from the feet;
- colour channels that look like masks (mostly 0/1, a small share);
- non-bone vertex groups and shape-key name prefixes.

Fill in `scripts/avatar_config.py` from that, and **confirm every value with the user**: the body, the garments, the region's root bone, the feature masks, the hide-key prefix, the bone roles and the front. The guesses are only guesses.

**Then ask these** before building anything (use AskUserQuestion if you have it):

1. **Region:** which object and part (a garment, one leg, the body from the neck down). For the body this sets `REGION_MODE` / `REGION_ROOT_BONE` in the config.
2. **Versions:** any of up to **six**. There are three levels of detail, each **with** or **without** shape-key protection. LOD0 is the original; LOD1–LOD3 are reduced versions to choose one from, not a runtime LOD group:

   | Level | Loop rule | Faces | On our body (neck down) |
   |---|---|---|---|
   | **LOD1** | closed rings, one pass | all quads | 56,090 tris (−11%), looks like the original, max loss 1.8 mm |
   | **LOD2** | closed rings, two passes | all quads | 53,258 (−15%), max 1.8 mm at rest, 4.7 mm bent |
   | **LOD3** | any clean loop, one pass | quads + clean pentagons + some triangles | 45,086 (−28%), max 4.7 mm, 8.5 mm in a deep sit |

   - **With shape-key protection:** a loop is kept wherever removing it would change any shape key by more than 1 mm. It costs a little: LOD3 went from 45,086 to 46,648.
   - **Without it:** the Basis is treated as the final shape. Keys still transfer exactly on every kept vertex, but they lose the detail of the removed loops. On our body, a garter-squeeze key lost up to 5–7 mm.
   - **Why rings reduce so little:** on a body, few closed rings stay unbroken inside the region (UV seams and protected edges cut most of the torso's), so LOD1/LOD2 mostly thin the arms, legs and fingers. On a tube-like part (one leg) LOD1/LOD2 work better.
3. **Parallel processing?** Ask whether to build the versions in parallel, with agents each doing one version while you do one. Tell them the cost:
   - each agent used ~370k tokens and ~5 minutes in our run;
   - each runs its own background Blender (2–3 GB of RAM).
   See section 5.
4. **Clothing to guard against:** every garment that covers the region (`GARMENTS`).
5. **Features to keep at full detail:** small sharp features such as nipples, areolae or lips (`KEEP_MASKS`). Ask whether a vertex-colour channel or vertex group already marks them (see 4.5 for why).
6. **Shape guard:** keep loops whose removal would flatten the surface by more than 2 mm (the default). Ask only if they want it tighter or off.
7. **Over-dense areas: do more there?** One rule everywhere doesn't notice parts that carry far more loops than their shape needs, so look before building:
   - **Find them:** run `scripts/density_report.py` (live, read-only).
     - It groups the region by main deform bone (no rig: by material and UV island).
     - For each area it gives the density against the region's median and roughly how many edges go around its curves (about π / flatness; ~24 reads smooth).
     - It suggests:
       - **"extra pass"** for areas at least 2× denser whose curves keep 16+ edges around after one more pass;
       - **"dense, curved"** for areas where another pass would start to facet them;
       - **"kept, very dense"** for keep masks holding very dense parts (nails were 90× the body's density on one avatar).
   - **Show them:** render the heat map (`render_views.py` mode `density`: blue half the median density, white at it, red 4× and up) with close-ups of each candidate.
   - **Ask per area,** with the triangles each would save. The answer goes into the level as `"extra": [(area spec, passes), ...]` (or `EXTRA` for all levels). The report prints each area's spec.
   - **A keep mask** comes out of `KEEP_MASKS` if the user wants a very dense kept part reduced too.
   - **The measure follows curves, not creases.** A nipple's base or a nail's edge can be flagged as safe and still soften, so decide those with close-ups, and re-check them after the build.
   - **On one body:** the report found the feet (2× the median density, 44 edges around) and the nipple islands (16×), and an extra pass there cut 884 more triangles. It also showed 3,500 kept nail triangles at 90× density.

## 2. Safety rules: follow all of them
1. **Back up before any edit.**
   - Save a copy: `bpy.ops.wm.save_as_mainfile(filepath=<Retopo Backups/name timestamp before X.blend>, copy=True)`.
   - Open that copy in background Blender and confirm the source object's fingerprint matches.
2. **Fingerprint the source first** with `fingerprint()` / `fingerprint_all()` in `retopo_lib.py`.
   - They hash positions, every shape key, UVs, colours, corners, weights, custom normals and vertex-group names.
   - Compare again at the end and report "unchanged on N entries".
   - Round-trip through JSON before comparing: lists and tuples otherwise count as different.
3. **Work on full duplicates only:** `o2 = o.copy(); o2.data = o.data.copy()`, in their own collection.
   - Use names that can never match what the export picks up (`Body Retopo Neck Down LOD3`).
   - Hide that collection in the main view layer.
4. **Read the source through a private copy** or read-only bmesh. Never `bm.to_mesh()` the original.
5. **Don't save the user's .blend unless they say so.** A duplicate of a body with 500+ shape keys is ~200 MB: delete the versions they don't pick before saving.
6. **Leave the user's state alone:** view, selection, hidden flags and pose. If you have to change something (a mode, a selection), say so.
7. **Ctrl+Z in Blender also undoes your script steps.** If deleted objects reappear or built ones vanish, someone pressed undo.
   - Check the scene state at the start of each step.
   - Redo what's needed and tell the user.
   - Verify that kept objects are still exactly as built.
8. **Never edit a mesh the user has open in Edit Mode.** That includes any object sharing the mesh, such as a lineup copy.
   - In Edit Mode, mesh data reads as empty (attributes of length 0).
   - Their edit session overwrites your change when they leave it.
   - Switch that object to Object Mode first (`bpy.ops.object.mode_set` with a context override) and tell them, or ask them to leave Edit Mode.
9. **Before editing a duplicate again, prove it's still what you built.** Compare `fingerprint_all` with the post-build scratch copy; only `.select_*` may differ.
10. **Don't keep Python ID references across MCP calls.** An undo invalidates them ("StructRNA has been removed"), so re-fetch by name in every call.
11. **Don't call `bpy.ops.render.opengl` through the MCP**; it can hang the server. Viewport screenshots are stale while Blender is minimised, so render in background Blender from `save_as_mainfile(copy=True)` scratch copies.
12. **Keep paid assets local.** Avatar files and renders never go to third-party services.
13. **Leave vertex colours and shape keys as they are.** Colour channels are usually shader masks (nails, skin, nipples); method A keeps them exactly, so never repaint one without asking. Edit shape-key data only in Object Mode: in Edit Mode the bmesh holds every key and writes them all back on exit, and changing the active key from Python there can write into the wrong key.
14. **Use absolute paths in shell commands.** In some Claude apps a `cd` moves the whole session's working folder.

## 3. Understand the mesh
- **Count triangles by region and material.** The dense part is often not where you'd guess. On our body, the leg tube had ~15 mm edges while a foot had 2.7 mm edges, and the head alone was 22,000 tris.
- **Take the region from bone weights only.**
  - Sum only the vertex groups that are bones of the armature. Avatars often carry non-bone groups (transfer groups, mask groups) that would pull face pieces into a "body" region.
  - **Neck down** = vertices with at most half their bone weight on Neck, Head and everything under them. The border is then a clean ring at the neck bone.
- **Inventory the parts:**
  - connected components (buttons, face overlays hidden in the Basis);
  - UV islands in every UV map;
  - edge classes: open edges, T-junctions, seams, sharp creases;
  - irregular vertices.
- **Look at it:** render wireframes and feature lines with `render_views.py` (modes `wire` / `features`).
- **List the shape keys that move the region,** with their max offset there. Separate the hide keys, which collapse the body under clothes (`HIDE_KEY_PREFIXES`, e.g. `Shrink_*`), from detail keys (breast, nipple, squeeze, heel, nails).
- **Note what covers the region.** A reduction can push skin through clothing.

## 4. Method A: edge-loop dissolve (exact data)
Every remaining vertex and corner is an original one, so positions, all shape keys, weights, UVs, colours and custom attributes stay exact; only the faces get bigger. Work in place on a bmesh of the duplicate (`bm.from_mesh` … `bm.to_mesh`): Blender carries all shape keys and attributes through. `build_region.py` runs all of this for the configured region and the chosen levels: `ONLY=["LOD1", "LOD3 keys", ...]`, or pass your own `LEVELS`. The building blocks are in `leg_loops.py`.

### 4.1 The steps
1. **Multires:** delete the duplicate's higher Multires levels first (`bpy.ops.object.multires_higher_levels_delete`); displacement doesn't fit merged faces.
2. **Store each vertex's original index** in an int point attribute (`retopo_orig`). It's for the checks and the normals, and it survives the bmesh round trip.
3. **Join triangle pairs into quads:** `bmesh.ops.join_triangles(..., cmp_seam, cmp_sharp, cmp_uvs, cmp_vcols, cmp_materials, 40°)`. It's exact and turns broken loops into continuous ones.
4. **Protect these edges:**
   - the region border;
   - UV seams and splits in **every** UV map;
   - sharp edges and material borders;
   - colour borders in any colour layer (e.g. nail masks; `COLOR_BORDER_LAYERS` narrows it);
   - non-manifold edges.
5. **Find loops** through regular vertices (4 edges, 4 quads).
   - `ends="closed"` takes rings only (LOD1/LOD2).
   - `ends="any"` also takes open loops (LOD3).
   - `ends="v3"` takes only open loops ending at 3-edge poles: triangles instead of pentagons.
6. **Run the guards** (4.2) and drop every loop one of them rejects.
7. **Pick every other loop:** a greedy maximum independent set, longest first. Two loops are neighbours when one quad has an edge of each as opposite sides; never remove two neighbours.
8. **Dissolve:** `dissolve_edges(use_verts=False)`, then dissolve the vertices left with 2 edges. Only dissolve those whose faces are **all** inside the region, so faces outside are never touched. Delete loose vertices.
9. **LOD2:** run a second pass of steps 5–8.
10. **Clean the n-gons** (4.3), **then** Reset Vectors (4.4). Do it in that order: re-splitting faces after the reset leaves stale normals. That happened once and was off by up to 33° with a 29° seam.

### 4.2 Guards (a loop is kept when removing it would…)
- **Clothing:** …poke the skin through a garment. At each loop vertex:
  1. take the midpoint of its two neighbours across the loop;
  2. measure how far it rises along the normal;
  3. ray-cast to the garment's rest shape (Basis + active keys, no Armature);
  4. reject the loop if the rise is more than the gap minus 0.8 mm.
  
  Guard against every garment over the region. On our body only the stocking and bra ever blocked loops (~180 loops).
- **Shape keys** (`key_guard`, when the user wants protection): …change a key's offset by more than 1 mm compared with the offset interpolated between the vertex's neighbours across the loop.
  - Leave out the hide keys (`HIDE_KEY_PREFIXES`).
  - It reads the offsets by original index, so it needs the `retopo_orig` attribute.
- **Shape** (`shape_guard`, 2 mm): …move a vertex more than 2 mm from the straight line between its neighbours across the loop. Without it, LOD3 lost 9–11 mm on the calves; with it, at most 4.7 mm.

### 4.3 N-gons
- **Where they come from:** an open loop leaves a 5-sided face at each end, or a triangle where it ends at a 3-edge pole. Loop ends that bunch up merge into 6- to 27-sided faces.
- **Review them** with `ngon_review()`: count by number of sides, and flag faces with 7+ sides, bent ones, concave ones and slivers. Compare with the source's own n-gons.
- **Re-split** every face with 6+ sides or a bad shape: triangulate it (beauty), then join the triangles back into quads where they make good ones (`fix_ngons`). After that, run a join pass on the region's triangles. Nothing moves and the triangle count stays the same.
- **Pentagon pairing** (`pair_pentagons`, built into `build_region.py`, same vertices and triangle count):
  - a pentagon + a neighbouring triangle (6 corners) becomes 2 quads;
  - two neighbouring pentagons (8 corners) become 3 quads;
  - only across an edge that could be dissolved, and only if every new quad is convex with corners within 55° of square.
  
  It's honest work but limited. Most loop-end pentagons are a quad with a vertex in the middle of one side, and paired cuts give bad quads (median 66° off square); others sit across UV seams or touch only quads. On our body it fixed 32 of 220.
- **Tell the user** that a pentagon costs the same 3 triangles in Unity. Pentagons only matter for editing and smooth deformation in Blender.

### 4.4 Normals: Reset Vectors on the region
- **Region corners:** `normals_split_custom_set` with zero vectors (the same as Mesh > Normals > Reset Vectors).
- **Outside corners keep their original normal.** Map them by (original vertex, sorted original vertices of its face); a face or neck usually has deliberate custom normals.
- **Border ring:** region corners on the border's vertices take the outside corners' normal. Otherwise the new auto normals there leave a seam (7° on our neck). Check the seam: the largest angle between region and outside corners at the same vertex. Ours was 0.04°, the same as the original.

### 4.5 Keep small sharp features at full detail
A millimetre guard can't protect a small feature. Thinning the nipple turned it into a cone while moving it less than 2 mm. A bend-angle guard would catch it but costs 2.8k–5.4k triangles across the body (fingers, toes). Instead, leave the feature's mask out of the region: our nipple and areola vertex-colour mask cost ~750 tris and keeps them exactly as they were. Look at close-ups of nipples, navel, fingers and toes in every version.

### 4.6 Irregular garments
For Marvelous Designer clothes (`variant_a_loops.py`), you can also dissolve open loops in both directions, with a union-find parity so neighbouring lines never both go. That gives larger cuts (the shirt: 22,246 → 9,595) but leaves n-gons and uneven density. Say so, and clean them as in 4.3.

## 5. Parallel processing (when the user said yes)
1. **Prepare the config and the levels yourself first:** every version is a named level (`LEVELS` + `ONLY=[...]` in `build_region.py`). Agents must not edit shared scripts or the config.
2. **Build one level yourself to validate the script,** then dispatch: one agent per version, and you take one.
   - Forks inherit your context, which is ideal.
   - Agents must run on Opus 5.5 or better (section 0).
3. **Render the reference columns (the original and any kept version) once yourself,** into their own folders. Each agent renders only its own object. `compose.py` takes several render folders separated by `;`.
4. **Rules for every agent:**
   - build only its own level and touch only its object;
   - never run the compare setup or removal, never change modes or the window's view layer, never save the .blend;
   - save its own scratch copy (`scratch_<level>.blend`);
   - run at most **one** background Blender at a time, and wait for ≥3 GB of free RAM before each launch;
   - if its object disappears (an undo), rebuild it once and say so;
   - report numbers first: tris, quads/tris/pentagons, guard blocks, exactness, normals and seam, losses at rest, in poses and in keys, poke-through vs the original, what it saw in its sheets, and file paths.
5. **Re-check every agent's claims yourself:** exactness from the live mesh, and metrics from its JSON. Then compose the combined sheets, set up the comparison, and finish with `show(name)`.

## 6. Verify: prove it, don't assume it
- **Exactness**, with the index map from the build (not by matching positions: coincident vertices with identical keys make that ambiguous):
  - positions, every shape key and the weights;
  - each corner's UVs, colours and float attributes, against the source corners of that vertex;
  - every face outside the region still present.
  
  Expect 0 differences and give the numbers. Only `custom_normal` should change.
- **Normals:**
  - region corners against auto: only the border ring may differ;
  - the border seam angle;
  - outside corners against the original.
  
  Compare against a **true reset**: a copy of the version's mesh with `normals_split_custom_set(zeros)`. Rebuilding the mesh with `from_pydata` gave a wrong reference on one body (33° off where the real reset matched within 0.05°).
- **Metrics in background Blender** (`metrics.py`): distance in both directions, with the worst spots, at rest, with relevant keys at 1, and in poses (`poses.py`: arms up, bent over, deep sit, fist).
  - Poke-through is counted per garment (a list of covers), sampling face centres and edge midpoints too.
  - Start keys from the file's own values.
- **N-gon review** (4.3) on every version **before** showing anything.
- **Normal maps:** look at the normal-map-only and UV-grid renders of each version. Fewer, bigger faces change the tangent space a normal map is decoded in, and Unity builds tangents from UV0 only (a map on a second UV map was baked in UV0's tangent space).
- **Checks that can see the defect:** set the pass thresholds before you look at the result, and compare the same fixed views before and after. For big changes (a whole body), an independent image-only review by a second agent can catch what you've stopped seeing.
- **Zero test after changing a script:** rebuild a version you already verified and expect it identical: the same faces, UVs, normals and metrics.
- **At the end:** re-check the source fingerprint and the backups.

## 7. Show the user (they decide)
- **Sheets:** `render_views.py` renders in background Blender: wire, `solid` (plain shaded, for meshes whose textures are missing), `density` (the heat map), own materials in Eevee, normal map, UV grid; whole-body, torso, chest, hips, hand, foot and pose views.
  - The framing comes from the config's bone roles and the posed vertices weighted to them, so it fits any humanoid. A view from above clips everything higher than the framed part.
  - Test-render the views once on a new avatar and look at them.
- **Composing:** `compose.py` lays the renders out as sheets: columns = the original + versions, rows = views. Pass a titles JSON with the triangle counts. Show nipples/navel/hands/feet close-ups and posed views.
- **In the user's Blender** (`setup_compare.py`):
  - `setup(name, [(object, label), ...])`, the source first, makes one view layer per version (skinned in place, so posing the real rig compares deformation; it shows just that version and the config's garments) plus a labelled lineup side by side (a layer showing only the lineup, so other avatars in the file stay out of it);
  - `add_rig(name, label)` gives a lineup copy its own posable copy of the armature, so posing it moves only that copy. Test it by bending one bone, then reset it.
  
  New collections and objects show up in every older view layer; `setup()` hides them there. Use a new name per comparison.
- **Finish on the comparison.** When you're done, call `show(name)` and leave the user looking at the multi-comparison: every window on the "Compare <name> Lineup" view layer, the 3D view turned to the avatar's front and framed on all versions side by side, with their labels. It keeps their selection and perspective, and outside Object Mode it only switches the layer. Say which view layers show each version in place.
- **Clean up** when they're done: `remove(name)` also deletes armature copies and labels.

## 7.5 Adopting a version and exporting (only when the user says so)
- **Show it in Blender first.** Never export until the user has picked a version and seen it there; send a screenshot with a short note of what changed.
- **Taking over the original's place:**
  - rename the original out of the way (e.g. "Body (before retopo)") and hide it; don't delete it unless asked;
  - give the version the original's **object name and mesh name**, so the exporter and Unity keep seeing the same mesh;
  - keep both until the export is checked.
- **The reduced duplicate keeps an empty Multires modifier.** Export with modifiers off, or remove it: on our setup, exporting with modifiers on silently dropped every shape key.
- **Unity limits:** weights are unchanged by method A, but the garment methods rebuild them, so check for at most 4 bone influences. Shape-key values outside 0–1 get clamped on import.
- **Check the export:** compare it with the previous export (vertex order changes between exports, so match vertices by position and UV, not index). Expect the same shape keys, bones and weights, and only the dropped vertices missing.

## 7.6 A body without a rig, or someone else's file
Tested on a friend's body sent as a bare mesh: no armature, no weights, no head, broken shape keys, missing textures.
- **Check what arrived.** `inspect_avatar.py` lists meshes without a rig, flags shape keys that move most of the mesh or by a large share of its size, and lists missing textures. Show the user what you found and ask before working around it. In our case, every key had its +X half on the mirrored positions of the −X half (a Symmetrize run with keys).
- **Its own config:** copy the template to `avatar_config_<name>.py` and pass it as `CONFIG` (live scripts) or `"config"` (background jobs).
  - Use `REGION_MODE = "all"` or `"materials"`, and keep small parts with `("material", name)` masks (e.g. nails).
  - Leave `BONES` empty.
- **No rig means:**
  - no pose tests: metrics measure rest (and keys, if they're sound);
  - close-ups are `"point"` views: pass `"extra_views"` in the render job, centred on landmarks you find on the mesh (the chest tip, the butt, hand and foot boxes);
  - render `solid` when the textures are missing.
- **Broken shape keys:** build without key protection and say the keys are carried over unchanged, so they're still broken. Fixing them is the owner's job.
- **The file:** build in the user's open file only if they agree, as hidden duplicates, and remind them to delete those before saving their own avatar's file.
- **On that body:** LOD1 kept the look almost entirely (51,494 → 40,950). LOD2 (36,550) softened the nipples. LOD3 (23,456) also made the top of the breast angular and left a shading facet. Small features need a keep mask even when the body has no colour mask: a UV island or a vertex group can serve.

## 8. Pitfalls we hit
- **"Looks the same" from an agent:** zoom into the feature crops yourself. One agent reported unchanged nipples that the close-up showed slightly softened.
- **Vertex groups:** their names live on the mesh, so `me.clear_geometry()` drops them.
- **Float32 BVH:** use tolerances near 1e-6, not 1e-9.
- **Coincident vertices:** carry source indices instead of matching positions.
- **Face orientation:** decide it from the *geometric* source normals, not custom normals.
- **Parallel background renders** need unique job files, or they race.
- **Ending an orphaned background Blender:** check its command line contains `-b` and your scratch path.
- **Eevee** compiles the materials per render; Workbench is much faster for wireframes.
- **Washed-out PNGs:** a scene whose display device is set for an HDR monitor (Rec.2100-PQ) writes PQ-encoded PNGs. `render_views.py` sets the display to sRGB; do the same for any screenshot meant for viewing.
- **MCP background tools:** an MCP's own "run in background" tools can time out even on quick reads. Running `blender.exe -b` from the shell takes seconds. Blender must be open for the live MCP, and a newly added MCP server needs the session's approval or a restart.
- **Selections aren't stored in the .blend:** to find a part again, use something that is (the vertices a shape key moves, a UV island, an attribute or colour mask).
- **Parts that share vertices with the skin** (nails, overlays): separate them by UV island and their shape keys, not by mesh connectivity.
- **Colour masks are per corner:** a vertex counts as masked if any of its corners is. Painting per vertex bleeds one island's colour onto its neighbour's border corners.
- **A wrong bend guard is expensive:** decide by looking at close-ups, not body-wide numbers.
- **Measure every claim:** when a render shows a difference, measure it before explaining it. The nipple looked flatter while every mm metric said "fine".

## Files in `scripts/`
| File | What it does |
|---|---|
| `avatar_config.py` | **Everything avatar-specific** (fill in first): body, garments, region, keep masks, hide keys, bone roles, front, check textures |
| `inspect_avatar.py` | Read-only scene report with candidates for the config |
| `density_report.py` | Read-only report of over-dense areas (extra-pass candidates, dense-but-curved, very dense kept parts) with their area specs |
| `build_region.py` | Builds the chosen levels of the body region: keep masks, guards, extra passes in chosen areas, n-gon cleanup, pentagon pairing, border-ring normals; leaves an index map for the checks |
| `leg_loops.py` | Method A building blocks: triangle join, protected edges, loop finder (closed/v3/any), greedy pick, dissolve; guards (`cover_guard`, `key_guard`, `shape_guard`); `fix_ngons`, `pair_pentagons`, regional Reset Vectors |
| `verify_region_variant.py` | Exactness check against the source, with the build's index map |
| `retopo_lib.py` | Fingerprints, `ngon_review`, the garment source snapshot, flattening and barycentric locator, `write_variant`, button rebuild, intersection checks |
| `variant_a_loops.py` / `variant_b_decimate.py` / `variant_c_pattern.py` / `build_variants.py` | Garment methods and their runner (the config's `GARMENT`) |
| `render_views.py`, `poses.py`, `compose.py` | Background renders framed by bone roles, test poses (arms up, bent over, deep sit, fist), sheets from several render folders |
| `metrics.py` | Distances with worst spots, shape-key and pose checks, poke-through per garment |
| `setup_compare.py` | Comparison view layers + lineup in the live file, `add_rig()`, `show()` (the finishing view), `remove()` |
