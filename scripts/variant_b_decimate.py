"""Variant B: Blender's collapse decimate, then redistribute.

Exec in Blender with the retopo_lib namespace in `ns`, the flattened Source in `src`, and `ratio`.

1. A temporary object holds another copy of the garment (no buttons, no shape keys) with the FlatUV layer
   and two vertex groups: chain vertices (open edges, T-junctions, seams) and crease vertices.
   A Decimate modifier (collapse) protected by the chain group gives the new topology; the decimator
   interpolates FlatUV, so every new corner knows where it sits on the original surface.
2. Redistribute: interior vertices (not on a chain or crease) are relaxed toward the centre of their
   neighbours in FlatUV space, staying inside their pattern piece.
3. Every vertex and corner is then re-sampled from the original at its FlatUV point (barycentric), so
   shape keys, weights, UVs, colours and normals all come from the original surface.
The temporary object and meshes are deleted afterwards.
"""
import bmesh
import bpy
import numpy as np
from mathutils import Vector
from mathutils.bvhtree import BVHTree

PROTECT_FACTOR = 10.0
RELAX_ITERS = 10
RELAX_STEP = 0.5


def decimate_spec(src, ns, ratio):
    col = ns["get_collection"]()
    me = src.me.copy()
    tmp = bpy.data.objects.new(src.obj.name + " Retopo decimate temp", me)
    col.objects.link(tmp)
    dme = None
    try:
        tmp.shape_key_clear()
        fu = me.uv_layers.new(name="FlatUV")
        fu.data.foreach_set("uv", src.flat.ravel())
        cls = src.edge_class
        chain_v, crease_v = set(), set()
        for e in src.bm.edges:
            if cls[e.index] in (1, 2, 3):
                chain_v |= {v.index for v in e.verts}
            elif cls[e.index] == 4:
                crease_v |= {v.index for v in e.verts}
        gp = tmp.vertex_groups.new(name="retopo_chain")
        gc = tmp.vertex_groups.new(name="retopo_crease")
        gp.add(sorted(chain_v), 1.0, "REPLACE")
        gc.add(sorted(crease_v - chain_v), 1.0, "REPLACE")
        bm = bmesh.new()
        bm.from_mesh(me)
        bmesh.ops.delete(bm, geom=[v for v in bm.verts if src.comp[v.index] != src.main], context="VERTS")
        bm.to_mesh(me)
        bm.free()
        main_tris = sum(p.loop_total - 2 for p in me.polygons)
        mod = tmp.modifiers.new("retopo_decimate", "DECIMATE")
        mod.decimate_type = "COLLAPSE"
        mod.ratio = ratio
        mod.use_collapse_triangulate = True
        mod.vertex_group = gp.name  # the group says where collapsing is allowed, so invert it to protect chains
        mod.invert_vertex_group = True
        mod.vertex_group_factor = PROTECT_FACTOR
        dg = bpy.context.evaluated_depsgraph_get()
        dme = bpy.data.meshes.new_from_object(tmp.evaluated_get(dg), preserve_all_data_layers=True, depsgraph=dg)

        nv = len(dme.vertices)
        L = len(dme.loops)
        lv = np.empty(L, np.int64)
        dme.loops.foreach_get("vertex_index", lv)
        fl = np.empty(L * 2)
        dme.uv_layers["FlatUV"].data.foreach_get("uv", fl)
        fl = fl.reshape(-1, 2)
        ls = np.empty(len(dme.polygons), np.int64)
        dme.polygons.foreach_get("loop_start", ls)
        lt = np.empty(len(dme.polygons), np.int64)
        dme.polygons.foreach_get("loop_total", lt)
        wc = np.zeros(nv)
        wk = np.zeros(nv)
        for v in dme.vertices:
            for g in v.groups:
                name = tmp.vertex_groups[g.group].name
                if name == "retopo_chain":
                    wc[v.index] = g.weight
                elif name == "retopo_crease":
                    wk[v.index] = g.weight
    finally:
        bpy.data.objects.remove(tmp)
        bpy.data.meshes.remove(me)

    # which piece each corner sits in: one BVH over all pieces
    tri_isl = src.island[src.tri_face]
    main_tris_idx = np.nonzero(np.isin(src.loop_vert[src.tri_loops[:, 0]], np.nonzero(src.comp == src.main)[0]))[0]
    p = src.flat[src.tri_loops[main_tris_idx]].reshape(-1, 2)
    gtree = BVHTree.FromPolygons([Vector((x, y, 0.0)) for x, y in p],
                                 [(3 * k, 3 * k + 1, 3 * k + 2) for k in range(len(main_tris_idx))],
                                 all_triangles=True, epsilon=0.0)
    corner_isl = np.empty(L, np.int64)
    for i in range(L):
        hit = gtree.find_nearest(Vector((fl[i, 0], fl[i, 1], 0.0)))
        corner_isl[i] = tri_isl[main_tris_idx[hit[2]]]

    # free vertices: all corners at one FlatUV point, not on a chain or crease, not on an open edge
    bm = bmesh.new()
    bm.from_mesh(dme)
    bm.verts.ensure_lookup_table()
    corners_of = [[] for _ in range(nv)]
    for i in range(L):
        corners_of[lv[i]].append(i)
    free = np.zeros(nv, bool)
    for v in bm.verts:
        cs = corners_of[v.index]
        if not cs or wc[v.index] > 0.5 or wk[v.index] > 0.5 or any(e.is_boundary for e in v.link_edges):
            continue
        if np.abs(fl[cs] - fl[cs[0]]).max() > 1e-6 or len(set(corner_isl[cs].tolist())) > 1:
            continue
        free[v.index] = True
    pos = np.array([fl[corners_of[i][0]] if corners_of[i] else (0.0, 0.0) for i in range(nv)])
    nbrs = [[e.other_vert(v).index for e in v.link_edges] for v in bm.verts]
    vert_isl = np.array([corner_isl[corners_of[i][0]] if corners_of[i] else -1 for i in range(nv)])

    def nb_pos(i, j):
        """FlatUV of neighbour j as seen from free vertex i's piece."""
        if free[j]:
            return pos[j]
        for c in corners_of[j]:
            if corner_isl[c] == vert_isl[i]:
                return fl[c]
        return None

    # piece orientation (majority of triangle signs in FlatUV) and incident faces per vertex
    def sign2(a, b, c):
        return np.sign((b - a)[0] * (c - a)[1] - (b - a)[1] * (c - a)[0])

    maj = {}
    for f in range(len(ls)):
        k = corner_isl[ls[f]]
        maj[k] = maj.get(k, 0) + sign2(*fl[ls[f]:ls[f] + 3])
    faces_of = [[] for _ in range(nv)]
    for f in range(len(ls)):
        for c in range(ls[f], ls[f] + lt[f]):
            faces_of[lv[c]].append((f, c))

    def safe(i, target):
        """True if moving vertex i to target folds no more adjacent triangles than before (folds the
        decimator left may get fixed, never made)."""
        before = after = 0
        for f, c in faces_of[i]:
            want = np.sign(maj[corner_isl[c]])
            before += sign2(*fl[ls[f]:ls[f] + 3]) != want
            after += sign2(*[fl[x] if x != c else target for x in range(ls[f], ls[f] + 3)]) != want
        return after <= before

    moved = rejected = 0
    for _ in range(RELAX_ITERS):
        moved = 0
        for i in np.nonzero(free)[0]:
            ps = [q for j in nbrs[i] if (q := nb_pos(i, j)) is not None]
            if len(ps) < 3:
                continue
            step = RELAX_STEP
            for _try in range(3):
                target = pos[i] + step * (np.mean(ps, axis=0) - pos[i])
                tree, _ = src.loc[vert_isl[i]]
                if tree.find_nearest(Vector((target[0], target[1], 0.0)))[3] < 1e-6 and safe(i, target):
                    pos[i] = target
                    for c in corners_of[i]:
                        fl[c] = target
                    moved += 1
                    break
                step *= 0.5
            else:
                rejected += 1
    sgn = []
    for f in range(len(ls)):
        a, b, c = fl[ls[f]], fl[ls[f] + 1], fl[ls[f] + 2]
        sgn.append((corner_isl[ls[f]], np.sign((b - a)[0] * (c - a)[1] - (b - a)[1] * (c - a)[0])))
    folds = sum(1 for k, sg in sgn if sg != np.sign(maj[k]))

    # spec: everything re-sampled from the original at its FlatUV point
    spec = ns["Spec"]()
    for i in range(nv):
        c = corners_of[i][0]
        t, w, _ = src.locate(corner_isl[c], fl[c])
        spec.add_vertex([(int(src.loop_vert[src.tri_loops[t][k]]), w[k]) for k in range(3)])
    sharp_v = wk > 0.5
    for f in bm.faces:
        cs = list(range(ls[f.index], ls[f.index] + lt[f.index]))
        ctr = {}
        for c in cs:
            ctr.setdefault(corner_isl[c], []).append(fl[c])
        corners = []
        for c in cs:
            t, w, _ = src.locate(corner_isl[c], fl[c], np.mean(ctr[corner_isl[c]], axis=0))
            corners.append([(int(src.tri_loops[t][k]), w[k]) for k in range(3)])
        spec.add_face([int(lv[c]) for c in cs], corners, fixed=True)  # the decimator keeps the winding
    for e in bm.edges:  # an edge between two crease vertices on the same original crease stays sharp
        a, b = e.verts
        if sharp_v[a.index] and sharp_v[b.index]:
            spec.sharp.add((a.index, b.index))
    bm.free()
    info = {"main_tris_before": main_tris, "tris_after": sum(int(x) - 2 for x in lt), "free_verts": int(free.sum()),
            "verts": nv, "moved_last_iter": moved, "rejected_moves": rejected, "folded_tris": folds}
    bpy.data.meshes.remove(dme)
    return spec, info


spec_b, info_b = decimate_spec(src, ns, ratio)
