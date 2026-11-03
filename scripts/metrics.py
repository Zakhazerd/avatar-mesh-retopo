"""Measures how far each variant is from the original, in background Blender on a scratch copy. Object and bone
names come from avatar_config.py (next to this file):

    blender.exe -b <scratch copy.blend> --python metrics.py -- <job.json>

job.json: {"out": result.json, "reference": original object, "objects": [variants], "keys": [shape keys],
           "poses": ["rest", "armsup", "bent", "legbend", "fist"]}

For every state (rest, each shape key at 1, each pose) and variant, with all modifiers evaluated:
- var->orig: distance from each variant vertex to the original surface (mm);
- orig->var: distance from each original vertex to the variant surface (mm), i.e. detail the variant lost;
- inside body: garment vertices more than 0.5 mm inside the body (the config's BODY; poses only, evaluated in
  the same pose); "body": false skips it (when the compared objects are body duplicates);
- with "cover" (a garment, or a list of them, e.g. the config's GARMENTS): surface samples (vertices, face
  centres, edge midpoints) more than 0.3 mm outside that object, i.e. poking through it (a list: a count each);
- with "worst": n, the n original vertices that lost the most (orig->var), with their positions.
"config" names another avatar's config file. Without an armature, only rest and the shape-key states are measured.
Never saves the .blend.
"""
import json
import os
import sys

import bpy
import numpy as np
from mathutils import Vector
from mathutils.bvhtree import BVHTree

job = json.loads(open(sys.argv[sys.argv.index("--") + 1], encoding="utf-8").read())
HERE = os.path.dirname(os.path.abspath(__file__))
CFG = {}
exec(open(job.get("config") or os.path.join(HERE, "avatar_config.py"), encoding="utf-8").read(), CFG)
vl = bpy.context.view_layer


def _show_collections(lc):  # the live file keeps the retopo collections hidden; show them so they get evaluated
    lc.hide_viewport = False
    for c in lc.children:
        _show_collections(c)


_show_collections(bpy.context.view_layer.layer_collection)
for o in bpy.context.scene.objects:  # measure what the FBX gets: Multires at level 0 (the export drops it)
    for m in o.modifiers:
        if m.type == "MULTIRES":
            m.levels = m.render_levels = 0
_ref = bpy.data.objects[job["reference"]]
_rigged = _ref if any(m.type == "ARMATURE" for m in _ref.modifiers) else bpy.data.objects[CFG["BODY"]]
arm = next((m.object for m in _rigged.modifiers if m.type == "ARMATURE" and m.object), None)  # None: rest only
POSE_NS = {"CFG": CFG}
exec(open(os.path.join(HERE, "poses.py"), encoding="utf-8").read(), POSE_NS)
names = [job["reference"]] + job["objects"]


def evaluated(o):
    dg = bpy.context.evaluated_depsgraph_get()
    ev = o.evaluated_get(dg)
    me = ev.to_mesh()
    co = np.array([tuple(ev.matrix_world @ v.co) for v in me.vertices])
    polys = [tuple(p.vertices) for p in me.polygons]
    ev.to_mesh_clear()
    return co, polys


def stats(d):
    d = np.asarray(d) * 1000
    return {"p50": round(float(np.median(d)), 3), "p95": round(float(np.percentile(d, 95)), 3),
            "p99": round(float(np.percentile(d, 99)), 3), "max": round(float(d.max()), 2)}


def distances(pts, co, polys):
    tree = BVHTree.FromPolygons([Vector(c) for c in co], polys)
    return [tree.find_nearest(Vector(p))[3] for p in pts]


def inside_body(pts, body_tree):
    n = 0
    for p in pts:
        loc, nrm, _, dist = body_tree.find_nearest(Vector(p))
        if loc is not None and dist < 0.03 and (Vector(p) - loc).dot(nrm) < -0.0005:
            n += 1
    return n


def surface_samples(co, polys):
    """Vertices plus face centres and edge midpoints: poke-through between vertices shows up too."""
    pts = [tuple(c) for c in co]
    seen = set()
    for p in polys:
        pts.append(tuple(np.mean([co[i] for i in p], axis=0)))
        for a, b in zip(p, p[1:] + p[:1]):
            k = (min(a, b), max(a, b))
            if k not in seen:
                seen.add(k)
                pts.append(tuple((co[a] + co[b]) / 2))
    return pts


def outside_cover(pts, tree):
    n = 0
    for p in pts:
        loc, nrm, _, dist = tree.find_nearest(Vector(p))
        if loc is not None and dist < 0.01 and (Vector(p) - loc).dot(nrm) > 0.0003:
            n += 1
    return n


def set_keys(value_of):
    for nm in names:
        ks = bpy.data.objects[nm].data.shape_keys
        for kb in ks.key_blocks[1:]:
            kb.value = value_of(kb.name)


# shape keys start from the reference's own values in the file (the cover is fitted to those)
base = {kb.name: kb.value for kb in bpy.data.objects[job["reference"]].data.shape_keys.key_blocks}
out = {}
states = [("rest", "rest", None)] + [(f"key {k}", "rest", k) for k in job.get("keys", [])] + \
         [(f"pose {p}", p, None) for p in job.get("poses", []) if p != "rest" and arm is not None]
for label, pose, key in states:
    if arm is not None:
        POSE_NS["set_pose"](arm, vl, pose)
    set_keys(lambda n: 1.0 if n == key else base.get(n, 0.0))
    vl.update()
    ref_co, ref_polys = evaluated(bpy.data.objects[job["reference"]])
    body_tree = None
    cover_list = job.get("cover") or []
    covers = {}
    for cname in [cover_list] if isinstance(cover_list, str) else cover_list:
        cco, cpolys = evaluated(bpy.data.objects[cname])
        covers[cname] = BVHTree.FromPolygons([Vector(c) for c in cco], cpolys)

    def cover_counts(co, polys):
        pts = surface_samples(co, polys)
        counts = {c: outside_cover(pts, t) for c, t in covers.items()}
        return counts[cover_list] if isinstance(cover_list, str) else counts
    if key is None and job.get("body", True) and CFG["BODY"] in bpy.data.objects:
        bco, bpolys = evaluated(bpy.data.objects[CFG["BODY"]])
        body_tree = BVHTree.FromPolygons([Vector(c) for c in bco], bpolys)
    row = {job["reference"]: {}}
    if body_tree:
        row[job["reference"]]["inside_body"] = inside_body(ref_co, body_tree)
    if covers:
        row[job["reference"]]["outside_cover"] = cover_counts(ref_co, ref_polys)
    for nm in job["objects"]:
        co, polys = evaluated(bpy.data.objects[nm])
        lost = np.asarray(distances(ref_co, co, polys))
        r = {"var->orig": stats(distances(co, ref_co, ref_polys)), "orig->var": stats(lost)}
        if job.get("worst"):
            r["worst"] = [(round(float(lost[i]) * 1000, 2), [round(float(x), 3) for x in ref_co[i]])
                          for i in np.argsort(-lost)[:job["worst"]]]
        if body_tree:
            r["inside_body"] = inside_body(co, body_tree)
        if covers:
            r["outside_cover"] = cover_counts(co, polys)
        row[nm] = r
    out[label] = row
    print("METRICS", label)
json.dump(out, open(job["out"], "w"), indent=1)
print("METRICS_DONE")
