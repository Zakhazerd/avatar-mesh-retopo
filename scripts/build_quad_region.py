"""Builds the full-quad level of a body region ("Quad") as a full duplicate of the body in the "<BODY> Retopo"
collection. Live or background Blender. Everything avatar-specific comes from avatar_config.py.

Experimental, and only on request: on an artist-made body it lost to the loop levels (59,425 tris against LOD3's
46,648) with patchwork topology, half of the new faces triangles. Run it only when the user asks for it by name.

    exec(open(path, encoding="utf-8").read(), {"__name__": "build_quad_region", "DEV": 0.0015})

The region and its keep masks are build_region.py's. The region is cut into islands at every border nothing may be
interpolated across: UV seams in every UV map, jumps in a corner colour or corner attribute, material borders.
Each island is flattened and rebuilt as quad grids aligned with its UV grain (variant_c_pattern.py), finer where
it curves tightly. A vertex allows the coarsest spacing h = H_MAX * STEP^k for which

  k_max * h^2 / 4 <= DEV   and   k_max * h * N_MIN <= 2*pi

with k_max its largest principal curvature read at the scale of h: a quadric fitted, in the vertex's normal frame,
to the vertices within max(h / sqrt(2), 2.5 x its edges) that face the same way. So a quad's centre sits at most
DEV off the original surface (the counterpart of the loop levels' shape guard), no curve gets fewer than N_MIN
edges around, and mesh noise or dents smaller than a quad don't count. Each face takes the finest spacing its
corners allow, then the finest of its neighbours' (fine areas grow by a ring). Connected areas of one island at
one spacing are the pieces; the borders between them are sampled at the finer spacing. A face stays exactly as it
is when its spacing would be under GAIN x its own edges (fingers, toes, nails: an even grid can't be coarser
there), and so does a piece smaller than MIN_CELLS grid cells that is finer than everything around it.

Settings (exec namespace): DEV (default 0.0015 m), N_MIN (12), H_MAX (0.03 m), STEP (0.9), GAIN (1.3),
MIN_CELLS (6), TAG ("Quad"), DRY (True: report the plan and a budget table only, build nothing), CONFIG (another
avatar's config file).

Data: every vertex and corner outside the rebuilt pieces is the original one, exactly (weights too). New vertices
lie on the original surface and take every shape key, weight (limited to 4 bones, normalised), UV, colour and
attribute from the original triangle under them. Normals: Reset Vectors on the region; region corners on the
border ring take the outside corners' normal, outside corners keep theirs (as build_region.py). Leaves an index
map (new vertex -> source vertex, -1 for new ones) in driver_namespace "retopo_region_orig_<TAG>". The body is only
read. Never saves the .blend.
"""
import math
import time

import bmesh
import bpy
import numpy as np
from mathutils.kdtree import KDTree

HERE = r"<set me: the folder holding these scripts>"
DEV = globals().get("DEV", 0.0015)
N_MIN = globals().get("N_MIN", 12)
H_MAX = globals().get("H_MAX", 0.03)
STEP = globals().get("STEP", 0.9)
GAIN = globals().get("GAIN", 1.3)
MIN_CELLS = globals().get("MIN_CELLS", 6)
JOIN_SHAPE = globals().get("JOIN_SHAPE", 40.0)  # degrees, the triangle join pass
TAG = globals().get("TAG", "Quad")
DRY = globals().get("DRY", False)

t0 = time.time()
bns = {"__name__": "build_region_lib"}
if globals().get("CONFIG"):
    bns["CONFIG"] = CONFIG
exec(open(HERE + r"\build_region.py", encoding="utf-8").read(), bns)  # no ONLY: only defines its functions
CFG, rlib = bns["CFG"], bns["rlib"]
rlib["COLLECTION"] = f"{CFG['BODY']} Retopo"
body = bpy.data.objects[CFG["BODY"]]
mask0 = bns["region_mask"](body)
for keep in CFG["KEEP_MASKS"]:
    mask0 &= ~bns["keep_mask"](body, keep)


def allowed_spacing(src, verts, dev):
    """Per vertex: the coarsest spacing H_MAX * STEP^k its curvature allows at that scale (see the top)."""
    co = src.co
    n = len(co)
    vn = np.empty(n * 3)
    src.me.vertex_normals.foreach_get("vector", vn)
    vn = vn.reshape(-1, 3)
    kd = KDTree(n)
    for i, p in enumerate(co):
        kd.insert(p, i)
    kd.balance()
    edge = np.array([sum(e.calc_length() for e in v.link_edges) / max(len(v.link_edges), 1) for v in src.bm.verts])
    ladder = [H_MAX * STEP ** k for k in range(200) if H_MAX * STEP ** k > 2e-4]

    def ok(v, h):
        idx = np.array([i for _, i, _ in kd.find_range(co[v], max(h / math.sqrt(2), 2.5 * edge[v]))])
        idx = idx[vn[idx] @ vn[v] > 0.3]
        if len(idx) < 7:
            return False
        nrm = vn[v]
        t1 = np.cross(nrm, (1.0, 0.0, 0.0) if abs(nrm[0]) < 0.9 else (0.0, 1.0, 0.0))
        t1 /= np.linalg.norm(t1)
        t2 = np.cross(nrm, t1)
        d = co[idx] - co[v]
        x, y, z = d @ t1, d @ t2, d @ nrm
        c = np.linalg.lstsq(np.stack([np.ones_like(x), x, y, x * x, x * y, y * y], 1), z, rcond=None)[0]
        k = np.abs(np.linalg.eigvalsh(np.array([[2 * c[3], c[4]], [c[4], 2 * c[5]]]))).max()
        return k * h * h / 4 <= dev and k * h * N_MIN <= 2 * math.pi

    hv = np.full(n, H_MAX)
    for v in verts:
        lo, hi = 0, len(ladder) - 1
        while lo < hi:
            mid = (lo + hi) // 2
            if ok(v, ladder[mid]):
                hi = mid
            else:
                lo = mid + 1
        hv[v] = ladder[lo]
    return hv


def plan(src, region_face, nb, area, hv):
    """A piece per face (-1: not rebuilt), each piece's spacing, the spacing level per face and a triangle
    estimate, from the vertices' allowed spacing hv."""
    nf = len(area)
    level = np.full(nf, -2)  # -2 outside the region, -1 stays as it is, k >= 0: spacing H_MAX * STEP**k
    for f in src.bm.faces:
        if region_face[f.index]:
            k = int(round(np.log(min(hv[v.index] for v in f.verts) / H_MAX) / np.log(STEP)))
            edge = sum(e.calc_length() for e in f.edges) / len(f.edges)
            level[f.index] = k if H_MAX * STEP ** k >= GAIN * edge else -1
    grown = level.copy()
    for f in np.nonzero(level >= 0)[0]:
        grown[f] = max([level[f]] + [level[g] for g in nb[f] if level[g] >= 0])
    level = grown
    for _ in range(8):  # small pieces join a finer neighbour, or stay as they are when nothing around is finer
        comp, comps = np.full(nf, -1), []
        for f in np.nonzero(level >= 0)[0]:
            if comp[f] >= 0:
                continue
            comp[f], members, st = len(comps), [f], [f]
            while st:
                x = st.pop()
                for g in nb[x]:
                    if comp[g] < 0 and level[g] == level[x]:
                        comp[g] = len(comps)
                        members.append(g)
                        st.append(g)
            comps.append(members)
        changed = False
        for c, members in enumerate(comps):
            k = level[members[0]]
            if area[members].sum() >= MIN_CELLS * (H_MAX * STEP ** k) ** 2:
                continue
            finer = [level[g] for f in members for g in nb[f] if comp[g] != c and level[g] > k]
            level[members] = min(finer) if finer else -1
            changed = True
        if not changed:
            break
    hmap = {c: H_MAX * STEP ** level[m[0]] for c, m in enumerate(comps)}
    est = sum(2 * area[m].sum() / hmap[c] ** 2 for c, m in enumerate(comps))
    est += sum(len(src.bm.faces[f].verts) - 2 for f in np.nonzero(level == -1)[0])
    return comp, hmap, level, int(est)


src = rlib["Source"](body, seams="all")
report = {"region_verts": int(mask0.sum()), "skipped_attrs": src.skipped_attrs}
try:
    region_face = np.array([all(mask0[v.index] for v in f.verts) for f in src.bm.faces])
    nb = [[g.index for e in f.edges for g in e.link_faces if g.index != f.index and src.island[g.index] == src.island[f.index]]
          for f in src.bm.faces]
    area = np.array([f.calc_area() for f in src.bm.faces])
    verts = sorted({v.index for f in src.bm.faces if region_face[f.index] for v in f.verts})
    outside_tris = int(sum(len(f.verts) - 2 for f in src.bm.faces if not region_face[f.index]))
    report.update(outside_tris=outside_tris,
                  region_tris=int(sum(len(f.verts) - 2 for f in src.bm.faces if region_face[f.index])),
                  budget={round(d * 1000, 1): outside_tris + plan(src, region_face, nb, area,
                                                                  allowed_spacing(src, verts, d))[3]
                          for d in globals().get("BUDGET_DEVS", ())})
    pieces, hmap, level, est = plan(src, region_face, nb, area, allowed_spacing(src, verts, DEV))
    report.update(rebuilt_pieces=len(hmap), kept_region_faces=int((level == -1).sum()), estimate=outside_tris + est,
                  levels={round(H_MAX * STEP ** k * 1000, 1): [int((level == k).sum()), round(float(area[level == k].sum()) * 1e4)]
                          for k in sorted(set(level[level >= 0].tolist()))})  # spacing mm: faces, area cm2
    if not DRY:
        src.flatten(rlib["get_collection"]())
        src.build_locators()
        report["flatten_s"] = round(time.time() - t0, 1)
        g = {"ns": rlib, "src": src, "h": hmap, "remesh": pieces >= 0, "pieces": pieces}
        exec(open(HERE + r"\variant_c_pattern.py", encoding="utf-8").read(), g)
        spec, info = g["spec_c"], g["info_c"]
        report["pattern"] = {k: v for k, v in info.items() if k not in ("pieces", "piece_votes")}
        o = rlib["write_variant"](src, f"{CFG['REGION_NAME']} {TAG}", spec, limit_all=False)
        me = o.data

        # normals: Reset Vectors on the region, the outside keeps its own, the border ring takes the outside's
        new_region = np.ones(len(spec.faces), bool)
        for nf, sf in spec.copied.items():
            new_region[nf] = region_face[sf]
        border = {}
        for nf, sf in spec.copied.items():
            if not new_region[nf]:
                for v, corner in zip(spec.faces[nf], spec.csrc[nf]):
                    border.setdefault(v, src.normals[corner[0][0]])
        normals, ring = [], 0
        for nf, (vs, cs) in enumerate(zip(spec.faces, spec.csrc)):
            for v, corner in zip(vs, cs):
                if not new_region[nf]:
                    normals.append(tuple(src.normals[corner[0][0]]))
                elif v in border:
                    normals.append(tuple(border[v]))
                    ring += 1
                else:
                    normals.append((0.0, 0.0, 0.0))
        # join the rebuilt part's triangle pairs into quads where they make good ones (nothing moves; never across
        # a UV, colour, material or sharp border). The target normals ride along as a corner attribute.
        tn = me.attributes.new("retopo_normal", "FLOAT_VECTOR", "CORNER")
        tn.data.foreach_set("vector", np.array(normals).ravel())
        cf = me.attributes.new("retopo_copied", "BOOLEAN", "FACE")
        cf.data.foreach_set("value", np.isin(np.arange(len(spec.faces)), list(spec.copied)).tolist())
        bm = bmesh.new()
        bm.from_mesh(me)
        lay = bm.faces.layers.bool["retopo_copied"]
        tris = [f for f in bm.faces if len(f.verts) == 3 and not f[lay]]
        n_tris = len(tris)
        bmesh.ops.join_triangles(bm, faces=tris, cmp_seam=False, cmp_sharp=True, cmp_uvs=True, cmp_vcols=True,
                                 cmp_materials=True, angle_face_threshold=math.radians(40),
                                 angle_shape_threshold=math.radians(JOIN_SHAPE))
        bm.to_mesh(me)
        bm.free()
        nrm = np.empty(len(me.loops) * 3)
        me.attributes["retopo_normal"].data.foreach_get("vector", nrm)
        rebuilt = np.empty(len(me.polygons), bool)
        me.attributes["retopo_copied"].data.foreach_get("value", rebuilt)
        rebuilt = ~rebuilt
        me.attributes.remove(me.attributes["retopo_normal"])
        me.attributes.remove(me.attributes["retopo_copied"])
        me.normals_split_custom_set(nrm.reshape(-1, 3).tolist())
        orig = np.array([p[0][0] if len(p) == 1 or all(w == 0 for _, w in p[1:]) else -1 for p in spec.vsrc])
        bpy.app.driver_namespace["retopo_region_orig_" + TAG] = orig
        sides = np.array([p.loop_total for p in me.polygons])
        report.update(name=o.name, tris=int((sides - 2).sum()), corners_border_ring=ring, joined_from_tris=n_tris,
                      faces_rebuilt=int(rebuilt.sum()), rebuilt_quads=int((sides[rebuilt] == 4).sum()),
                      rebuilt_tris=int((sides[rebuilt] == 3).sum()), rebuilt_ngons=int((sides[rebuilt] > 4).sum()),
                      faces_copied=len(spec.copied), new_verts=int((orig < 0).sum()),
                      write=rlib["WRITE_INFO"].get(o.name))
        o.active_shape_key_index = body.active_shape_key_index
        layer = bpy.context.view_layer.layer_collection.children.get(rlib["COLLECTION"])
        if layer:
            layer.hide_viewport = True
finally:
    src.free()
report["seconds"] = round(time.time() - t0, 1)
print("QUAD_BUILT" if not DRY else "QUAD_DRY", report)
