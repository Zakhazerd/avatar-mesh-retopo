"""Finds the over-dense areas of the configured body region, for the "do more there?" question before building
(live Blender, read-only):

    ns = {"__name__": "density_report", "CONFIG": optional config path}
    exec(open(path, encoding="utf-8").read(), ns); result = ns["report"]

Areas: with a rig, the vertices grouped by their main deform bone; without one, by material and UV island. For each
area: triangles, median edge length, density against the region's median, about how many edges go around its
curves (pi / median flatness, retopo_lib.density_metrics; ~24 reads smooth), whether a keep mask covers it, its
centre (to find it, or for a "point" view), the area spec build_region.py takes, and a suggestion:
  "extra pass"        at least 2x denser than the region and still 16+ edges around after one more pass:
                      candidates to ask the user about
  "dense, curved"     dense, but another pass would leave under 16 edges around: ask, with close-ups
  "kept, very dense"  a keep mask holds it at full detail although it's 3x+ denser than the region
Sorted by the triangles an extra pass could save (about 45% of the area's).
"""
import os

import bpy
import numpy as np

HERE = r"<set me: the folder holding these scripts>"
bns = {"__name__": "build_region"}
if globals().get("CONFIG"):
    bns["CONFIG"] = globals()["CONFIG"]
exec(open(os.path.join(HERE, "build_region.py"), encoding="utf-8").read(), bns)  # functions only (no ONLY)
CFG, rlib = bns["CFG"], bns["rlib"]
MIN_AROUND_AFTER = 16  # edges around a curve after one more pass that still read as smooth

obj = bpy.data.objects[CFG["BODY"]]
me = obj.data
region = bns["region_mask"](obj)
kept = np.zeros(len(me.vertices), bool)
for spec in CFG["KEEP_MASKS"]:
    kept |= bns["keep_mask"](obj, spec)
co, edge, flat = rlib["density_metrics"](me)
med = float(np.median(edge[region & ~kept]))
arm = next((m.object for m in obj.modifiers if m.type == "ARMATURE" and m.object), None)
if arm is not None:
    bones = {g.index: g.name for g in obj.vertex_groups if g.name in arm.data.bones}
    label = []
    for v in me.vertices:
        gs = [(g.weight, bones[g.group]) for g in v.groups if g.group in bones]
        label.append(max(gs)[1] if gs else "(unweighted)")
    label = np.array(label, dtype=object)

    def spec_of(name, idx):
        return ("bones", [name])
else:
    mats = [s.material.name if s.material else None for s in obj.material_slots]
    vmat = np.empty(len(me.vertices), dtype=object)
    for p in me.polygons:
        vmat[list(p.vertices)] = mats[p.material_index]
    island = rlib["uv_islands"](me)
    label = np.array([f"{m} / island {i}" for m, i in zip(vmat, island)], dtype=object)

    def spec_of(name, idx):
        return ("island", None, [round(float(x), 4) for x in co[idx[0]]])
face_label = np.array([label[p.vertices[0]] for p in me.polygons], dtype=object)
face_tris = np.array([p.loop_total - 2 for p in me.polygons])
rows = []
for name in sorted(set(label[region])):
    idx = np.where((label == name) & region)[0]
    if len(idx) < 20:
        continue
    tris = int(face_tris[face_label == name].sum())
    e = float(np.median(edge[idx]))
    dens = (med / e) ** 2
    around = float(np.pi / max(np.median(flat[idx]), 1e-3))  # edges around its curves, about
    is_kept = float(np.mean(kept[idx])) > 0.5
    if is_kept:
        suggest = "kept, very dense" if dens >= 3 else "kept"
    else:
        suggest = "extra pass" if dens >= 2 and around / 2 >= MIN_AROUND_AFTER else \
            "dense, curved" if dens >= 2 else ""
    rows.append({"area": name, "tris": tris, "median_edge_mm": round(e * 1000, 2), "density_x": round(dens, 1),
                 "edges_around": round(around), "kept": is_kept, "suggest": suggest,
                 "centre": [round(float(x), 3) for x in co[idx].mean(0)], "spec": spec_of(name, idx),
                 "extra_pass_saves": int(tris * 0.45) if suggest in ("extra pass", "kept, very dense") else 0})
rows.sort(key=lambda r: -r["extra_pass_saves"])
report = {"region_median_edge_mm": round(med * 1000, 2), "areas": len(rows),
          "candidates": [r for r in rows if r["suggest"] == "extra pass"][:20],
          "dense_but_curved": [r for r in rows if r["suggest"] == "dense, curved"][:12],
          "kept_very_dense": [r for r in rows if r["suggest"] == "kept, very dense"][:12]}
