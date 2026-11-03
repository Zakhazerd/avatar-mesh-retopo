"""Builds reduction levels of a body region as full duplicates of the body in the "<BODY> Retopo" collection
(live Blender). Everything avatar-specific comes from avatar_config.py.

    exec(open(path, encoding="utf-8").read(), {"__name__": "build_region", "ONLY": ["LOD3 keys"]})

ONLY names the levels to (re)build; without it nothing is built. CONFIG can name another avatar's config file
(default: avatar_config.py next to this script). LEVELS can be passed in the same way to build
other combinations. Default levels: "LOD1", "LOD2", "LOD3" ignore the shape keys (the Basis is the shape we want);
"LOD1 keys", "LOD2 keys", "LOD3 keys" also keep every loop whose removal would change a shape key by more than 1 mm.
A level's "extra" (or EXTRA in the exec namespace, for every level built) lists (area, passes): more passes in
over-dense areas after the main pass, with the same loop rule and guards (areas: see area_mask).

Region: by bone weight, by material or the whole mesh (see REGION_MODE in the config), minus the KEEP_MASKS,
which stay at full detail. Loop rules as in leg_loops.py: LOD3 dissolves any clean loop once, LOD1 closed rings once, LOD2 closed rings
twice (all quads). A loop is kept when removing it would
  - let the skin poke through one of the GARMENTS (0.8 mm margin, their rest shapes);
  - change a shape key by more than 1 mm ("keys" levels; the HIDE_KEY_PREFIXES keys are not guarded);
  - flatten the surface by more than the level's shape limit (2 mm).
Then n-gons in the region with 6+ sides or a bad shape are re-split (beauty triangulate + join, nothing moves),
pentagons are paired with neighbouring triangles/pentagons into quads where that makes good quads, and Reset
Vectors runs on the region's corners. Region corners on the border ring take the outside corners' normal (no
seam), and every corner outside keeps its original custom normal. Each duplicate also loses its Multires higher
levels. The body is only read. Never saves the .blend.
"""
import math

import bmesh
import bpy
import numpy as np

HERE = r"<set me: the folder holding these scripts>"
CFG = {}
exec(open(globals().get("CONFIG") or HERE + r"\avatar_config.py", encoding="utf-8").read(), CFG)
SOURCE = CFG["BODY"]
COLLECTION = f"{SOURCE} Retopo"
# shape: shape-guard limit (m); keys: guard the shape keys; ends/passes: the loop rule
DEFAULT_LEVELS = {
    "LOD1": dict(shape=0.002, keys=False, ends="closed", passes=1),
    "LOD2": dict(shape=0.002, keys=False, ends="closed", passes=2),
    "LOD3": dict(shape=0.002, keys=False, ends="any", passes=1),
    "LOD1 keys": dict(shape=0.002, keys=True, ends="closed", passes=1),
    "LOD2 keys": dict(shape=0.002, keys=True, ends="closed", passes=2),
    "LOD3 keys": dict(shape=0.002, keys=True, ends="any", passes=1),
}
LEVELS = globals().get("LEVELS") or DEFAULT_LEVELS
LEVELS = {k: v for k, v in LEVELS.items() if k in globals().get("ONLY", ())}
KEY_TOL = 0.001
DRY = globals().get("DRY", False)  # True: counts only, the duplicate's mesh is left unreduced

lib = {"__name__": "leg_loops"}
exec(open(HERE + r"\leg_loops.py", encoding="utf-8").read(), lib)
rlib = {"__name__": "retopo_lib"}
exec(open(HERE + r"\retopo_lib.py", encoding="utf-8").read(), rlib)


def material_mask(obj, names):
    """Vertices of any face that uses one of these materials."""
    slots = {i for i, s in enumerate(obj.material_slots) if s.material and s.material.name in names}
    out = np.zeros(len(obj.data.vertices), bool)
    for p in obj.data.polygons:
        if p.material_index in slots:
            out[list(p.vertices)] = True
    return out


def region_mask(obj):
    """The region's vertices. "all": the whole mesh; "materials": the faces of REGION_MATERIALS; "below"/"under":
    by bone weight, where only vertex groups that are bones count (avatars carry transfer and mask groups that
    would pull other parts in)."""
    if CFG["REGION_MODE"] == "all":
        return np.ones(len(obj.data.vertices), bool)
    if CFG["REGION_MODE"] == "materials":
        return material_mask(obj, CFG["REGION_MATERIALS"])
    arm = next(m.object for m in obj.modifiers if m.type == "ARMATURE")
    root = arm.data.bones[CFG["REGION_ROOT_BONE"]]
    tree = {root.name} | {b.name for b in root.children_recursive} | set(CFG["REGION_EXTRA_BONES"])
    in_tree = {g.index for g in obj.vertex_groups if g.name in tree}
    bone = {g.index for g in obj.vertex_groups if g.name in arm.data.bones}
    n = len(obj.data.vertices)
    wr, wt = np.zeros(n), np.zeros(n)
    for v in obj.data.vertices:
        for g in v.groups:
            if g.group in bone:
                wt[v.index] += g.weight
                if g.group in in_tree:
                    wr[v.index] += g.weight
    return wr <= 0.5 * wt if CFG["REGION_MODE"] == "below" else wr > 0.5 * wt


def keep_mask(obj, spec):
    """Vertices of a KEEP_MASKS entry: ("color", layer, channel, threshold) - any corner (or the point) of a colour
    attribute above threshold -, ("group", vertex group, threshold) or ("material", material name)."""
    me = obj.data
    if spec[0] == "material":
        return material_mask(obj, [spec[1]])
    if spec[0] == "group":
        gi = obj.vertex_groups[spec[1]].index
        return np.array([any(g.group == gi and g.weight > spec[2] for g in v.groups) for v in me.vertices])
    _, name, channel, threshold = spec
    a = me.attributes[name]
    n = len(me.loops) if a.domain == "CORNER" else len(me.vertices)
    c = np.empty(n * 4)
    a.data.foreach_get("color", c)
    hit = c.reshape(-1, 4)[:, channel] > threshold
    if a.domain != "CORNER":
        return hit
    lv = np.empty(len(me.loops), np.int64)
    me.loops.foreach_get("vertex_index", lv)
    out = np.zeros(len(me.vertices), bool)
    out[lv[hit]] = True
    return out


def area_mask(obj, spec):
    """Vertices of an area for extra passes: any KEEP_MASKS kind, or ("bones", [names]) - more than half their
    bone weight on these bones -, ("box", [x0, y0, z0], [x1, y1, z1]) in object space, or ("island", uv map or
    None, [x, y, z]) - the UV island of the vertex nearest that point."""
    me = obj.data
    if spec[0] in ("color", "group", "material"):
        return keep_mask(obj, spec)
    co = np.empty(len(me.vertices) * 3)
    me.vertices.foreach_get("co", co)
    co = co.reshape(-1, 3)
    if spec[0] == "box":
        lo, hi = np.array(spec[1]), np.array(spec[2])
        return np.all((co >= lo) & (co <= hi), axis=1)
    if spec[0] == "island":
        island = rlib["uv_islands"](me, spec[1])
        return island == island[int(np.argmin(np.linalg.norm(co - np.array(spec[2]), axis=1)))]
    if spec[0] == "bones":
        arm = next(m.object for m in obj.modifiers if m.type == "ARMATURE")
        want = {g.index for g in obj.vertex_groups if g.name in set(spec[1])}
        bone = {g.index for g in obj.vertex_groups if g.name in arm.data.bones}
        w, t = np.zeros(len(me.vertices)), np.zeros(len(me.vertices))
        for v in me.vertices:
            for g in v.groups:
                if g.group in bone:
                    t[v.index] += g.weight
                    if g.group in want:
                        w[v.index] += g.weight
        return w > 0.5 * np.maximum(t, 1e-9)
    raise ValueError(f"unknown area {spec!r}")


def region_keys(obj, mask, min_offset=0.0005):
    """Shape keys that move a region vertex by more than min_offset, without the hide keys."""
    me = obj.data
    n = len(me.vertices)
    kb = me.shape_keys.key_blocks
    a, r = np.empty(n * 3), np.empty(n * 3)
    out = []
    for k in kb[1:]:
        if k.name.startswith(tuple(CFG["HIDE_KEY_PREFIXES"])):
            continue
        k.data.foreach_get("co", a)
        k.relative_key.data.foreach_get("co", r)
        if np.linalg.norm((a - r).reshape(-1, 3)[mask], axis=1).max() > min_offset:
            out.append(k.name)
    return out


def corner_keys(me):
    """(vertex, sorted vertices of its face) for every corner: identifies untouched faces after the edit."""
    keys = []
    for p in me.polygons:
        vs = tuple(sorted(p.vertices))
        keys += [(me.loops[li].vertex_index, vs) for li in p.loop_indices]
    return keys


def clean_ngons(me, mask0):
    """Re-splits the region's n-gons with 6+ sides or a bad shape, joins leftover region triangles where they make
    good quads, then pairs pentagons into quads. Same vertices and triangle count; returns the reviews."""
    bm = bmesh.new()
    bm.from_mesh(me)
    lay = bm.verts.layers.int["retopo_orig"]

    def in_region(f):
        return all(mask0[v[lay]] for v in f.verts)
    bm.faces.index_update()
    before, bad = rlib["ngon_review"](bm)
    bad_ids = {b["face"] for b in bad}
    targets = [f for f in bm.faces if len(f.verts) > 4 and in_region(f) and (f.index in bad_ids or len(f.verts) >= 6)]
    lib["fix_ngons"](bm, targets)
    tris = [f for f in bm.faces if len(f.verts) == 3 and in_region(f)]
    bmesh.ops.join_triangles(bm, faces=tris, cmp_seam=True, cmp_sharp=True, cmp_uvs=True, cmp_vcols=True,
                             cmp_materials=True, angle_face_threshold=math.radians(40),
                             angle_shape_threshold=math.radians(40))
    paired = lib["pair_pentagons"](bm, in_region)
    bm.faces.index_update()
    after, bad_after = rlib["ngon_review"](bm)
    sides = {}
    for f in bm.faces:
        if in_region(f):
            k = min(len(f.verts), 5)
            sides[k] = sides.get(k, 0) + 1
    bm.to_mesh(me)
    me.update()
    bm.free()
    return {"fixed": len(targets), "paired": paired, "before": before, "after": after, "bad_after": bad_after,
            "region_faces_by_sides": {"tri": sides.get(3, 0), "quad": sides.get(4, 0), "5+": sides.get(5, 0)}}


if LEVELS:  # exec'd without ONLY (e.g. by density_report.py), it only defines the functions above
    src = bpy.data.objects[SOURCE]
    col = bpy.data.collections.get(COLLECTION)
    if col is None:
        col = bpy.data.collections.new(COLLECTION)
        bpy.context.scene.collection.children.link(col)
    mask0 = region_mask(src)
    kept_verts = 0
    for spec in CFG["KEEP_MASKS"]:
        keep = keep_mask(src, spec) & mask0
        kept_verts += int(keep.sum())
        mask0 &= ~keep
    KEYS = region_keys(src, mask0)
    covers = [bpy.data.objects[c] for c in CFG["GARMENTS"] if c in bpy.data.objects]
    report = {"region_verts": int(mask0.sum()), "kept_by_masks": kept_verts, "keys_guarded": len(KEYS)}
    for tag, kw in LEVELS.items():
        name = f"{SOURCE} Retopo {CFG['REGION_NAME']} {tag}"
        old = bpy.data.objects.get(name)
        if old is not None:
            assert COLLECTION in [c.name for c in old.users_collection]
            m = old.data
            bpy.data.objects.remove(old)
            if m.users == 0 and m != src.data:
                bpy.data.meshes.remove(m)
        o = src.copy()
        o.data = src.data.copy()
        assert o.data != src.data
        col.objects.link(o)
        o.name = name
        o.data.name = f"{src.data.name} Retopo {CFG['REGION_NAME']} {tag}"
        me = o.data
        cn0 = np.empty(len(me.loops) * 3)
        me.corner_normals.foreach_get("vector", cn0)
        cn0 = cn0.reshape(-1, 3)
        keys0 = corner_keys(me)
        orig = me.attributes.new("retopo_orig", "INT", "POINT")
        orig.data.foreach_set("value", list(range(len(me.vertices))))
        for m in [m for m in o.modifiers if m.type == "MULTIRES"]:
            with bpy.context.temp_override(object=o, active_object=o, selected_objects=[o]):
                bpy.ops.object.multires_higher_levels_delete(modifier=m.name)
        guards = [lib["cover_guard"](c) for c in covers]
        if kw["keys"]:
            guards.append(lib["key_guard"](o, KEYS, KEY_TOL))
        if kw["shape"]:
            guards.append(lib["shape_guard"](kw["shape"]))
        info = lib["reduce_region"](o, mask0, passes=kw["passes"], join=True, ends=kw["ends"], guard=guards,
                                    write=not DRY, color_layers=CFG["COLOR_BORDER_LAYERS"])
        row = {"guard_blocked": info.get("guard_blocked"), "region": info["passes"][-1], "region_before": info["before"]}
        # extra passes in over-dense areas (density_report.py finds them): the same loop rule and guards
        for spec, n_passes in (list(kw.get("extra", [])) + list(globals().get("EXTRA", []))) if not DRY else []:
            oi_now = np.empty(len(me.vertices), np.int64)
            me.attributes["retopo_orig"].data.foreach_get("value", oi_now)
            area = (area_mask(src, spec) & mask0)[oi_now]
            einfo = lib["reduce_region"](o, area, passes=n_passes, join=False, ends=kw["ends"], guard=guards,
                                         color_layers=CFG["COLOR_BORDER_LAYERS"])
            row.setdefault("extra", []).append({"area": spec, "passes": n_passes, "before": einfo["before"],
                                                "after": einfo["passes"][-1], "blocked": einfo.get("guard_blocked")})
        if not DRY:
            row["ngons"] = clean_ngons(me, mask0)
            # Reset Vectors on the region; every other corner gets its original normal back. Region corners on the
            # border ring take the outside corners' normal there, so the region and its surroundings shade as one.
            oi = np.empty(len(me.vertices), np.int64)
            me.attributes["retopo_orig"].data.foreach_get("value", oi)
            lookup = dict(zip(keys0, cn0))
            faces = [(p, tuple(sorted(int(oi[v]) for v in p.vertices)), all(mask0[oi[v]] for v in p.vertices))
                     for p in me.polygons]
            border = {}
            for p, vs, region_face in faces:
                if not region_face:
                    for v in p.vertices:
                        border.setdefault(int(oi[v]), lookup.get((int(oi[v]), vs)))
            normals, kept, reset, ring = [], 0, 0, 0
            for p, vs, region_face in faces:
                for li in p.loop_indices:
                    ov = int(oi[me.loops[li].vertex_index])
                    key = (ov, vs)
                    if not region_face and key in lookup:
                        normals.append(tuple(lookup[key]))
                        kept += 1
                    elif region_face and border.get(ov) is not None:
                        normals.append(tuple(border[ov]))
                        ring += 1
                    else:
                        normals.append((0.0, 0.0, 0.0))
                        reset += 1
            me.normals_split_custom_set(normals)
            row["corners_border_ring"] = ring
            bpy.app.driver_namespace["retopo_region_orig_" + tag] = oi.copy()  # new vertex -> source vertex, for checks
            row.update(corners_kept=kept, corners_reset=reset)
        me.attributes.remove(me.attributes["retopo_orig"])
        o.active_shape_key_index = src.active_shape_key_index
        row["body_tris"] = sum(p.loop_total - 2 for p in me.polygons)
        report[name] = row
    bpy.context.view_layer.layer_collection.children[COLLECTION].hide_viewport = True
    print("REGION_BUILT", report)
