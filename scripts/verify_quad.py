"""Background Blender on a scratch copy holding the build: checks a build_quad_region.py version against the body.

    blender -b <scratch.blend> --python verify_quad.py -- <version name> <index map json> <out json>

The index map is the build's driver_namespace "retopo_region_orig_<TAG>" saved as a JSON list (new vertex ->
source vertex, -1 for new ones). Compare health with the source's own counts: boundary and non-manifold edges
should match it, degenerate faces be 0.

Exact part (vertices with a source vertex): positions, every shape key, weights, point attributes; corner values at
those vertices within 1e-5 of one of the source's corner values there; every face outside the region present with
the same corners. New part: weights (influences, sums), face orientation against the source surface, mesh
health, faces by sides. Normals: outside corners against the source, region corners against a true reset, the
border seam."""
import json
import sys

import bmesh
import bpy
import numpy as np
from mathutils import Vector
from mathutils.bvhtree import BVHTree

name, orig_path, out_path = sys.argv[sys.argv.index("--") + 1:][:3]
HERE = r"<set me: the folder holding these scripts>"
bns = {"__name__": "build_region_lib"}
exec(open(HERE + r"\build_region.py", encoding="utf-8").read(), bns)
CFG = bns["CFG"]
bo, vo = bpy.data.objects[CFG["BODY"]], bpy.data.objects[name]
b, v = bo.data, vo.data
oi = np.array(json.load(open(orig_path)))
mask0 = bns["region_mask"](bo)
for keep in CFG["KEEP_MASKS"]:
    mask0 &= ~bns["keep_mask"](bo, keep)
ex = oi >= 0
out = {"verts": len(v.vertices), "exact_verts": int(ex.sum()), "new_verts": int((~ex).sum())}


def co(me):
    a = np.empty(len(me.vertices) * 3)
    me.vertices.foreach_get("co", a)
    return a.reshape(-1, 3)


out["positions_exact"] = bool(np.array_equal(co(v)[ex], co(b)[oi[ex]]))
bad = []
for kb in b.shape_keys.key_blocks:
    kv = v.shape_keys.key_blocks.get(kb.name)
    a, c = np.empty(len(b.vertices) * 3, np.float32), np.empty(len(v.vertices) * 3, np.float32)
    kb.data.foreach_get("co", a)
    kv.data.foreach_get("co", c)
    if not np.array_equal(c.reshape(-1, 3)[ex], a.reshape(-1, 3)[oi[ex]]) or kb.value != kv.value or \
            kb.relative_key.name != kv.relative_key.name or (kb.slider_min, kb.slider_max) != (kv.slider_min, kv.slider_max):
        bad.append(kb.name)
out["keys"] = {"source": len(b.shape_keys.key_blocks), "variant": len(v.shape_keys.key_blocks), "differ_on_exact": bad[:10],
               "n_differ": len(bad)}
wb = [sorted((g.group, round(g.weight, 7)) for g in x.groups) for x in b.vertices]
out["weights_exact"] = sum(1 for i in np.nonzero(ex)[0] if sorted((g.group, round(g.weight, 7)) for g in v.vertices[i].groups) != wb[oi[i]])
arm = next(m.object for m in bo.modifiers if m.type == "ARMATURE")
deform = {i for i, g in enumerate(vo.vertex_groups) if g.name in arm.data.bones and arm.data.bones[g.name].use_deform}
infl = [[g.weight for g in v.vertices[i].groups if g.group in deform and g.weight > 1e-5] for i in np.nonzero(~ex)[0]]
out["new_weights"] = {"max_influences": max((len(x) for x in infl), default=0),
                      "sum_min": round(min((sum(x) for x in infl), default=1.0), 4),
                      "sum_max": round(max((sum(x) for x in infl), default=1.0), 4), "no_weight": sum(1 for x in infl if not x)}


def attr(me, a):
    n = {"POINT": len(me.vertices), "EDGE": len(me.edges), "FACE": len(me.polygons), "CORNER": len(me.loops)}[a.domain]
    w = {"FLOAT2": 2, "FLOAT_VECTOR": 3, "FLOAT_COLOR": 4, "BYTE_COLOR": 4, "INT16_2D": 2, "INT32_2D": 2}.get(a.data_type, 1)
    prop = {"FLOAT_VECTOR": "vector", "FLOAT2": "vector", "FLOAT_COLOR": "color", "BYTE_COLOR": "color"}.get(a.data_type, "value")
    arr = np.empty(n * w, bool if a.data_type == "BOOLEAN" else np.float64)
    a.data.foreach_get(prop, arr)
    return arr.reshape(n, w).astype(np.float64)


lv_b = np.empty(len(b.loops), np.int64)
b.loops.foreach_get("vertex_index", lv_b)
lv_v = np.empty(len(v.loops), np.int64)
v.loops.foreach_get("vertex_index", lv_v)
attrs = {}
for a in b.attributes:
    if a.name.startswith(".") or a.name in ("position", "custom_normal") or a.domain == "EDGE":
        continue
    av = v.attributes.get(a.name)
    if av is None or av.domain != a.domain or av.data_type != a.data_type:
        attrs[a.name] = "missing"
        continue
    xb, xv = attr(b, a), attr(v, av)
    if a.domain == "POINT":
        attrs[a.name] = "exact" if np.array_equal(xv[ex], xb[oi[ex]]) else "differs on exact vertices"
    elif a.domain == "CORNER":
        have = {}
        for i, vert in enumerate(lv_b):
            have.setdefault(int(vert), []).append(xb[i])
        miss = 0
        for i, vert in enumerate(lv_v):
            o = oi[vert]
            if o >= 0 and min(np.abs(xv[i] - h).max() for h in have[int(o)]) > 1e-5:
                miss += 1
        attrs[a.name] = "ok" if miss == 0 else f"{miss} corners at exact vertices with new values"
out["attributes"] = attrs


def cycles(me, vmap=None):
    d = {}
    for p in me.polygons:
        vs = [int(vmap[x]) if vmap is not None else x for x in p.vertices]
        k = vs.index(min(vs))
        d[tuple(vs[k:] + vs[:k])] = p.index
    return d


cb, cv = cycles(b), cycles(v, oi)
outside = [c for c in cb if not all(mask0[x] for x in c)]
out["outside_faces"] = {"count": len(outside), "missing": sum(1 for c in outside if c not in cv)}
out["region_faces_copied"] = sum(1 for c in cb if all(mask0[x] for x in c) and c in cv)

# normals
nb_ = np.empty(len(b.loops) * 3)
b.corner_normals.foreach_get("vector", nb_)
nb_ = nb_.reshape(-1, 3)
nv_ = np.empty(len(v.loops) * 3)
v.corner_normals.foreach_get("vector", nv_)
nv_ = nv_.reshape(-1, 3)
outside_set = set(cv[c] for c in outside if c in cv)
worst = 0.0
for c in outside:
    if c not in cv:
        continue
    pb, pv = b.polygons[cb[c]], v.polygons[cv[c]]
    lb = {b.loops[li].vertex_index: li for li in pb.loop_indices}
    for li in pv.loop_indices:
        worst = max(worst, float(np.degrees(np.arccos(np.clip(nv_[li] @ nb_[lb[int(oi[v.loops[li].vertex_index])]], -1, 1)))))
out["outside_normals_max_deg"] = round(worst, 4)
ref = v.copy()
ref.normals_split_custom_set([(0.0, 0.0, 0.0)] * len(ref.loops))
nr = np.empty(len(ref.loops) * 3)
ref.corner_normals.foreach_get("vector", nr)
nr = nr.reshape(-1, 3)
bpy.data.meshes.remove(ref)
border_verts = {v.loops[li].vertex_index for f in outside_set for li in v.polygons[f].loop_indices}
reg_dev, ring_seam = [], 0.0
for p in v.polygons:
    if p.index in outside_set:
        continue
    for li in p.loop_indices:
        if v.loops[li].vertex_index in border_verts:
            continue
        reg_dev.append(float(np.degrees(np.arccos(np.clip(nv_[li] @ nr[li], -1, 1)))))
seam = {}
for li in range(len(v.loops)):
    seam.setdefault(int(lv_v[li]), []).append(nv_[li])
for vert in border_verts:
    ns_ = seam[vert]
    ring_seam = max(ring_seam, max(float(np.degrees(np.arccos(np.clip(x @ y, -1, 1)))) for x in ns_ for y in ns_))
out["region_normals_vs_true_reset_deg"] = {"max": round(max(reg_dev), 4), "p99": round(float(np.percentile(reg_dev, 99)), 4)}
out["border_ring_spread_deg_max"] = round(ring_seam, 3)

# orientation against the source surface, mesh health, sides
bco = co(b)
btree = BVHTree.FromPolygons([Vector(x) for x in bco], [tuple(p.vertices) for p in b.polygons])
vco = co(v)
flipped = 0
for p in v.polygons:
    if p.index in outside_set:
        continue
    hit = btree.find_nearest(Vector(p.center))
    if hit[1] is not None and hit[1].dot(p.normal) < 0:
        flipped += 1
out["faces_against_source_surface"] = flipped
bm = bmesh.new()
bm.from_mesh(v)
bmb = bmesh.new()
bmb.from_mesh(b)
out["health"] = {"nonmanifold_edges": sum(1 for e in bm.edges if len(e.link_faces) > 2),
                 "source_nonmanifold_edges": sum(1 for e in bmb.edges if len(e.link_faces) > 2),
                 "boundary_edges": sum(1 for e in bm.edges if e.is_boundary),
                 "source_boundary_edges": sum(1 for e in bmb.edges if e.is_boundary),
                 "loose_verts": sum(1 for x in bm.verts if not x.link_faces),
                 "degenerate_faces": sum(1 for f in bm.faces if f.calc_area() < 1e-10),
                 "wire_edges": sum(1 for e in bm.edges if e.is_wire)}
bm.free()
bmb.free()
sides = np.array([p.loop_total for p in v.polygons])
region_new = np.array([p.index not in outside_set for p in v.polygons])
out["tris"] = int((sides - 2).sum())
out["region_faces_by_sides"] = {"3": int((sides[region_new] == 3).sum()), "4": int((sides[region_new] == 4).sum()),
                                "5+": int((sides[region_new] > 4).sum())}
out["materials_same"] = [s.material for s in bo.material_slots] == [s.material for s in vo.material_slots]
out["groups_same"] = [g.name for g in bo.vertex_groups] == [g.name for g in vo.vertex_groups]
out["modifiers"] = [(m.name, m.type, getattr(m, "total_levels", None), getattr(getattr(m, "object", None), "name", None))
                    for m in vo.modifiers]
json.dump(out, open(out_path, "w"), indent=1)
print("VERIFY_QUAD", json.dumps(out)[:1500])
