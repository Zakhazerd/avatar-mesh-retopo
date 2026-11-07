"""Variant C: remesh every pattern piece in its own flattened (near-isometric) space.

Exec in Blender with the retopo_lib namespace in `ns`, the Source in `src` (flattened, with locators) and
optionally `h` (target edge length in metres).

1. Chains: open edges, T-junctions, UV seams and creases (sharp edges) are split into chains at corners
   and junctions, then resampled at even arc length. Chain samples lie on original edges, so pieces that
   share a seam share the same new vertices (no cracks) and creases (rolled hems) stay crisp.
2. Each piece gets a square grid of points at spacing h, aligned with its texture UV map's grain, kept away from
   chains, and a constrained Delaunay triangulation of grid + chain samples. Triangles are then joined
   into quads where they make good ones.
3. Every new vertex and corner is a barycentric point on an original triangle of the same piece, so
   positions, shape keys, weights, UVs, colours and normals come from the original at that point.
4. Folds the flattening can't hold (flipped or overlapping triangles, inconsistent winding), plus one ring
   around them, are frozen: those original faces are kept as they are, and chains touching them keep
   every original vertex.

Body regions (build_quad_region.py): pass `remesh` (a bool per source face). Only those faces are rebuilt; every
other face, from any connected part, is copied as it is, and the border keeps every original vertex. `pieces`
(a piece number per face) splits islands into areas rebuilt at different spacings, and `h` may then be a dict
{piece: spacing}; a chain shared by two pieces is sampled at the finer one.
"""
import math

import bmesh
import numpy as np
from mathutils import Vector
from mathutils.bvhtree import BVHTree
from mathutils.geometry import delaunay_2d_cdt
from mathutils.kdtree import KDTree

CORNER_ANGLE = 35.0  # chain vertices turning more than this (degrees) stay as corners
SNAP = 0.2  # chain samples within this fraction of an edge from an original vertex use that vertex
GRID_CLEAR = 0.55  # grid points stay this many h away from chains
FREEZE_RINGS = 0  # overlapping triangles are frozen on both sides already
JOIN_SHAPE = 40.0  # degrees, bmesh join_triangles shape threshold
INSIDE_TOL = 1e-6  # FlatUV units (~1.3 um); the 2D BVH works in float32, so inside points read ~1e-8 away


def pattern_spec(src, ns, h, extra_frozen=(), remesh=None, pieces=None):
    """extra_frozen: original faces to keep as they are too (where an earlier pass made layers intersect).
    remesh: faces to rebuild (None: the main part, whose other parts the caller rebuilds, e.g. buttons).
    pieces: a piece number per face, to split islands further (areas of one island rebuilt at different
    spacings share its flattening; the borders between them become chains). None: the islands."""
    bm = src.bm
    flat = src.flat
    isl = src.island if pieces is None else np.asarray(pieces)
    if remesh is None:
        scope = np.array([src.comp[f.verts[0].index] == src.main for f in bm.faces])
        remesh = scope
    else:
        scope = np.ones(len(bm.faces), bool)

    def piece_h(i):
        return h[i] if isinstance(h, dict) else h
    main_faces = [f for f in bm.faces if scope[f.index]]
    main_isl = sorted({int(isl[f.index]) for f in main_faces if remesh[f.index]})

    # ---------------- frozen faces (folds and overlaps are found per island, in its flattening)
    tri_isl = isl[src.tri_face]
    tri_src_isl = src.island[src.tri_face]
    a2 = flat[src.tri_loops]
    e2 = a2[:, 1:] - a2[:, :1]
    s2 = 0.5 * (e2[:, 0, 0] * e2[:, 1, 1] - e2[:, 0, 1] * e2[:, 1, 0])
    if pieces is None:
        scale = src.scale
    else:  # metres per FlatUV unit, per piece
        a3 = src.co[src.loop_vert[src.tri_loops]]
        area3 = 0.5 * np.linalg.norm(np.cross(a3[:, 1] - a3[:, 0], a3[:, 2] - a3[:, 0]), axis=1)
        scale = {i: math.sqrt(area3[tri_isl == i].sum() / max(np.abs(s2[tri_isl == i]).sum(), 1e-30)) for i in main_isl}
    frozen = set()
    for i in sorted({int(src.island[f.index]) for f in main_faces if remesh[f.index]}):
        m = tri_src_isl == i
        sign = np.sign(s2[m].sum())
        frozen |= set(src.tri_face[m & (np.sign(s2) != sign)].tolist())
        tree, tris = src.loc[i]
        cent = a2[tris].mean(1)
        for k, t in enumerate(tris):
            hits = tree.find_nearest_range(Vector((cent[k, 0], cent[k, 1], 0.0)), INSIDE_TOL)
            if len(hits) > 1:
                frozen |= {int(src.tri_face[tris[hh[2]]]) for hh in hits}
    for e in bm.edges:
        if len(e.link_faces) == 2 and src.edge_class[e.index] in (0, 4):
            l1, l2 = e.link_loops
            if l1.vert == l2.vert:
                frozen |= {f.index for f in e.link_faces}
    frozen |= set(extra_frozen)
    frozen |= set(np.nonzero(scope & ~remesh)[0].tolist())
    frozen = {f for f in frozen if scope[f]}
    for _ in range(FREEZE_RINGS):
        grow = set()
        for fi in frozen:
            for v in bm.faces[fi].verts:
                grow |= {g.index for g in v.link_faces}
        frozen |= grow
    is_frozen = np.zeros(len(bm.faces), bool)
    is_frozen[list(frozen)] = True

    # locators over the faces that get remeshed, per piece
    nf_loc = {}
    for i in main_isl:
        tris = np.nonzero((tri_isl == i) & ~is_frozen[src.tri_face])[0]
        if not len(tris):
            continue
        p = flat[src.tri_loops[tris]].reshape(-1, 2)
        nf_loc[i] = (BVHTree.FromPolygons([Vector((x, y, 0.0)) for x, y in p],
                                          [(3 * k, 3 * k + 1, 3 * k + 2) for k in range(len(tris))],
                                          all_triangles=True, epsilon=0.0), tris)

    def locate(i, p, nudge_to=None):
        tree, tris = nf_loc[i]
        q = p if nudge_to is None else p + (np.asarray(nudge_to) - p) * 1e-3
        hit = tree.find_nearest(Vector((q[0], q[1], 0.0)))
        t = int(tris[hit[2]])
        a, b, c = flat[src.tri_loops[t]]
        return t, ns["barycentric"](p, a, b, c), hit[3]

    def fl(f, v):
        return flat[src.face_loop_at(f, v)]

    # ---------------- chains
    def live_faces(e):
        return [f for f in e.link_faces if not is_frozen[f.index] and scope[f.index]]

    def edge_sig(e):
        lf = live_faces(e)
        if not lf:
            return None
        touches = len(lf) < len(e.link_faces)
        c = int(src.edge_class[e.index])
        if c == 0 and not touches:
            if len({int(isl[f.index]) for f in lf}) == 1:
                return None
            c = 6  # border between two pieces of one island
        elif c == 0:
            c = 5  # border of a frozen zone
        return (c, bool(src.sharp[e.index]), frozenset(int(isl[f.index]) for f in lf), touches)

    sig = {e.index: s for e in bm.edges if (s := edge_sig(e)) is not None}
    cedges = {}
    for ei in sig:
        e = bm.edges[ei]
        for v in e.verts:
            cedges.setdefault(v.index, []).append(ei)

    def turn(v, ea, eb):
        a = bm.edges[ea].other_vert(v).co
        b = bm.edges[eb].other_vert(v).co
        d1 = (v.co - a).normalized()
        d2 = (b - v.co).normalized()
        return math.degrees(d1.angle(d2, 0.0))

    node = set()
    for vi, es in cedges.items():
        if len(es) != 2 or sig[es[0]] != sig[es[1]] or turn(bm.verts[vi], es[0], es[1]) > CORNER_ANGLE:
            node.add(vi)

    chains = []
    used = set()

    def walk(v0, e0):
        verts, edges = [v0], [e0]
        used.add(e0)
        v = bm.edges[e0].other_vert(bm.verts[v0]).index
        while v not in node:
            nxt = [x for x in cedges[v] if x not in used]
            if not nxt:
                break
            verts.append(v)
            edges.append(nxt[0])
            used.add(nxt[0])
            v = bm.edges[nxt[0]].other_vert(bm.verts[v]).index
        verts.append(v)
        return verts, edges

    for vi in sorted(node):
        for ei in cedges[vi]:
            if ei not in used:
                chains.append(walk(vi, ei))
    for ei in sig:  # closed loops without a node: start at their sharpest vertex
        if ei not in used:
            e = bm.edges[ei]
            v0 = e.verts[0].index
            node.add(v0)
            chains.append(walk(v0, ei))

    # ---------------- spec vertices for chain samples
    spec = ns["Spec"]()
    vgid = {}

    def vert_gid(vi):
        if vi not in vgid:
            vgid[vi] = spec.add_vertex([(vi, 1.0)])
        return vgid[vi]

    chain_samples = []  # per chain: list of (edge position i, t, gid)
    for verts, edges in chains:
        s = sig[edges[0]]
        lens = [bm.edges[e].calc_length() for e in edges]
        cum = np.concatenate([[0.0], np.cumsum(lens)])
        L = cum[-1]
        samples = [(0, 0.0, vert_gid(verts[0]))]
        if s[3]:  # touches a frozen zone: keep every vertex
            targets = list(cum[1:-1])
        else:
            n = max(1, int(round(L / min(piece_h(i) for i in s[2]))))
            targets = [j * L / n for j in range(1, n)]
        for st in targets:
            i = min(int(np.searchsorted(cum, st, side="right")) - 1, len(edges) - 1)
            t = (st - cum[i]) / lens[i] if lens[i] > 0 else 0.0
            if t < SNAP:
                g, t = vert_gid(verts[i]), 0.0
            elif t > 1 - SNAP:
                g, i, t = vert_gid(verts[i + 1]), i, 1.0
            else:
                g = spec.add_vertex([(verts[i], 1 - t), (verts[i + 1], t)])
            if g != samples[-1][2]:
                samples.append((i, t, g))
        last = vert_gid(verts[-1])
        if last != samples[-1][2]:
            samples.append((len(edges) - 1, 1.0, last))
        chain_samples.append(samples)
        if s[1] and len(samples) > 1:
            for a, b in zip(samples, samples[1:]):
                spec.sharp.add((a[2], b[2]))

    # ---------------- per piece: chain sides, grid, CDT, quads
    stats = {"frozen_faces": int(is_frozen.sum()), "chains": len(chains), "pieces": {}}
    pending = []  # (piece, new vertex ids, corners); oriented per piece once all vertices exist
    for I in main_isl:
        if I not in nf_loc:
            continue
        pts, pgid, key = [], [], {}

        def add_pt(p, g):
            k = (round(p[0] * 1e7), round(p[1] * 1e7))
            if k not in key:
                key[k] = len(pts)
                pts.append(np.array(p, float))
                pgid.append(g)
            return key[k]

        cons = []
        for (verts, edges), samples in zip(chains, chain_samples):
            if I not in sig[edges[0]][2]:
                continue
            # sides of this chain in piece I, followed edge by edge through matching corner coordinates
            e0 = bm.edges[edges[0]]
            starts = []
            for f in live_faces(e0):
                if isl[f.index] != I:
                    continue
                ab = (fl(f, bm.verts[verts[0]]), fl(f, bm.verts[verts[1]]))
                if not any(np.abs(ab[0] - x[0]).max() < 1e-7 and np.abs(ab[1] - x[1]).max() < 1e-7 for x in starts):
                    starts.append(ab)
            for ab in starts:
                side = [ab]
                for k in range(1, len(edges)):
                    e = bm.edges[edges[k]]
                    prev_end = side[-1][1]
                    nxt = None
                    for f in live_faces(e):
                        if isl[f.index] != I:
                            continue
                        a = fl(f, bm.verts[verts[k]])
                        if np.abs(a - prev_end).max() < 1e-6:
                            nxt = (a, fl(f, bm.verts[verts[k + 1]]))
                            break
                    if nxt is None:
                        break
                    side.append(nxt)
                if len(side) != len(edges):
                    stats.setdefault("broken_sides", 0)
                    stats["broken_sides"] += 1
                    continue
                ids = []
                for i, t, g in samples:
                    A, B = side[i]
                    ids.append(add_pt(A + (B - A) * t, g))
                cons += [(a, b) for a, b in zip(ids, ids[1:]) if a != b]
        n_chain_pts = len(pts)

        # grid aligned with the texture UV map's grain (the first UV map, whatever it's called)
        loops = np.nonzero(np.isin(src.loop_face, [f.index for f in main_faces if isl[f.index] == I and not is_frozen[f.index]]))[0]
        X = flat[loops] - flat[loops].mean(0)
        Y = src.uvs[src.main_uv][loops] - src.uvs[src.main_uv][loops].mean(0)
        M = np.linalg.lstsq(X, Y, rcond=None)[0].T  # uv = M @ flat
        d = np.linalg.solve(M, np.array([1.0, 0.0]))
        ax = d / np.linalg.norm(d)
        ay = np.array([-ax[1], ax[0]])
        h2 = piece_h(I) / scale[I]
        P = flat[loops]
        u, w = P @ ax, P @ ay
        kd_pts = []
        for a, b in cons:
            pa, pb = pts[a], pts[b]
            n = max(1, int(np.linalg.norm(pb - pa) / (h2 / 8)))
            kd_pts += [pa + (pb - pa) * (j / n) for j in range(n + 1)]
        kd = KDTree(len(kd_pts))
        for j, p in enumerate(kd_pts):
            kd.insert((p[0], p[1], 0.0), j)
        kd.balance()
        tree = nf_loc[I][0]
        grid = []
        for gi in range(int(math.floor(u.min() / h2)), int(math.ceil(u.max() / h2)) + 1):
            for gj in range(int(math.floor(w.min() / h2)), int(math.ceil(w.max() / h2)) + 1):
                p = ax * gi * h2 + ay * gj * h2
                q = Vector((p[0], p[1], 0.0))
                hit = tree.find_nearest(q)
                if hit[3] > INSIDE_TOL:
                    continue
                if kd.find(q)[2] < GRID_CLEAR * h2:
                    continue
                grid.append(p)
        for p in grid:
            t, bw, _ = locate(I, p)
            g = spec.add_vertex([(int(src.loop_vert[src.tri_loops[t][k]]), bw[k]) for k in range(3)])
            add_pt(p, g)

        # constrained Delaunay, keep triangles inside the remeshed part of the piece
        vin = [Vector((p[0], p[1])) for p in pts]
        vout, eout, fout, ov, oe, of = delaunay_2d_cdt(vin, cons, [], 0, 1e-9 * h2, True)
        out_g = []
        for k, o in enumerate(ov):
            out_g.append(pgid[o[0]] if o else None)
        tris = []
        for f in fout:
            if any(out_g[k] is None for k in f):
                continue
            a, b, c = (vout[k] for k in f)
            if abs((b - a).cross(c - a)) < 1e-6 * h2 * h2:  # three samples in a row on one chain: no face
                continue
            c = sum((vout[k] for k in f), Vector((0, 0))) / 3
            if tree.find_nearest(Vector((c.x, c.y, 0.0)))[3] > INSIDE_TOL:
                continue
            tris.append(f)
        # quads: join triangles in 2D, never across a chain
        tb = bmesh.new()
        lid = tb.verts.layers.int.new("lid")
        bv = []
        for k, p in enumerate(vout):
            v = tb.verts.new((p.x, p.y, 0.0))
            v[lid] = k
            bv.append(v)
        for f in tris:
            try:
                tb.faces.new([bv[k] for k in f])
            except ValueError:
                pass
        cons_out = set()
        for k, (a, b) in enumerate(eout):
            if oe[k] and any(x < len(cons) for x in oe[k]):
                cons_out.add((min(a, b), max(a, b)))
        for e in tb.edges:
            a, b = e.verts[0][lid], e.verts[1][lid]
            e.seam = (min(a, b), max(a, b)) in cons_out
        bmesh.ops.join_triangles(tb, faces=list(tb.faces), cmp_seam=True, cmp_sharp=False, cmp_uvs=False,
                                 cmp_vcols=False, cmp_materials=False, angle_face_threshold=math.pi,
                                 angle_shape_threshold=math.radians(JOIN_SHAPE))
        n_faces = 0
        for f in tb.faces:
            ks = [v[lid] for v in f.verts]
            p2 = [np.array((vout[k].x, vout[k].y)) for k in ks]
            ctr = sum(p2) / len(p2)
            corners = []
            for p in p2:
                t, bw, _ = locate(I, p, ctr)
                corners.append([(int(src.tri_loops[t][k]), bw[k]) for k in range(3)])
            pending.append((I, [out_g[k] for k in ks], corners))
            n_faces += 1
        tb.free()
        stats["pieces"][int(I)] = {"chain_pts": n_chain_pts, "grid_pts": len(grid), "faces": n_faces,
                                   "merged_inputs": sum(1 for o in ov if len(o) > 1)}

    # ---------------- orientation: one winding per piece, by majority against the source faces
    vidx, vw = ns["_pad"](spec.vsrc)
    co = ns["interp"](src.co, vidx, vw)
    votes = {}
    for I, vs, corners in pending:
        ci, cw = ns["_pad"](corners)
        n_src = (src.face_normal[src.loop_face[ci]] * cw[..., None]).sum((0, 1))
        p = co[vs]
        n_geo = np.cross(p[1:-1] - p[0], p[2:] - p[0]).sum(0)
        votes[I] = votes.get(I, 0.0) + float(np.sign(n_geo @ n_src))
    for I, vs, corners in pending:
        if votes[I] < 0:
            vs, corners = vs[::-1], corners[::-1]
        spec.add_face(vs, corners, fixed=True)
    stats["piece_votes"] = {int(k): v for k, v in votes.items()}

    # ---------------- frozen faces, copied as they are (spec.copied: new face -> source face)
    spec.copied = {}
    for fi in sorted(frozen):
        f = bm.faces[fi]
        spec.copied[len(spec.faces)] = fi
        spec.add_face([vert_gid(v.index) for v in f.verts], [[(src.face_loop_at(f, v), 1.0)] for v in f.verts], fixed=True)
        for e in f.edges:
            if src.sharp[e.index]:
                spec.sharp.add((vert_gid(e.verts[0].index), vert_gid(e.verts[1].index)))
    return spec, stats


spec_c, info_c = pattern_spec(src, ns, h, globals().get("extra_frozen", ()), globals().get("remesh"),
                              globals().get("pieces"))
