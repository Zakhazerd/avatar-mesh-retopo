"""Method A for a body part: join triangles into quads, then dissolve every other clean edge loop, keeping all
faces quads. Works in place on a bmesh of a full duplicate, so every attribute, the weights and all shape keys of
the kept vertices stay exactly as they were.

    ns = {"__name__": "leg_loops"}; exec(open(path, encoding="utf-8").read(), ns)
    info = ns["reduce_region"](dup_obj, region_vertex_mask, passes=1, join=True)

ends="closed" (default) dissolves closed loops (rings) only: that's the one case where every merged face stays
a quad. An open loop ending at a 3-edge pole turns one neighbouring quad into a triangle ("v3"), and one
ending anywhere else leaves a 5-sided face ("any"). Never dissolved: edges leaving the region, UV seams or UV-map splits (both maps),
sharp edges, material borders, colour borders (a channel jump > 0.1 in any colour layer, like nail masks),
non-manifold edges.
"""
import bmesh
import bpy
import numpy as np

COLOR_JUMP = 0.1


def _protected_edges(bm, region, color_layers=None):
    """color_layers: names of the colour layers whose jumps protect an edge; None = every colour layer."""
    uv_layers = list(bm.loops.layers.uv.values())
    cols = [lay for coll in (bm.loops.layers.color, bm.loops.layers.float_color) for name, lay in coll.items()
            if not name.startswith(".") and (color_layers is None or name in color_layers)]
    seam_flag = bm.edges.layers.bool.get("uv_seam") if hasattr(bm.edges.layers, "bool") else None
    prot = set()
    for e in bm.edges:
        lf = e.link_faces
        if len(lf) != 2 or not all(f.index in region for f in lf) or e.seam or not e.smooth:
            prot.add(e.index)
            continue
        if seam_flag is not None and e[seam_flag]:
            prot.add(e.index)
            continue
        f1, f2 = lf
        if f1.material_index != f2.material_index:
            prot.add(e.index)
            continue
        for v in e.verts:
            l1 = next(l for l in f1.loops if l.vert == v)
            l2 = next(l for l in f2.loops if l.vert == v)
            if any((l1[u].uv - l2[u].uv).length > 1e-6 for u in uv_layers) or \
                    any(max(abs(a - b) for a, b in zip(l1[c], l2[c])) > COLOR_JUMP for c in cols):
                prot.add(e.index)
                break
    return prot


def _clean_loops(bm, region, prot, ends="closed"):
    def regular(v):
        return len(v.link_edges) == 4 and all(len(f.verts) == 4 and f.index in region for f in v.link_faces) \
            and len(v.link_faces) == 4

    def next_edge(e, v):
        fs = set(e.link_faces)
        for x in v.link_edges:
            if x is not e and not (set(x.link_faces) & fs):
                return x
        return None

    def end_ok(v):
        return len(v.link_edges) == 3 and len(v.link_faces) == 3 and all(len(f.verts) == 4 and f.index in region
                                                                         for f in v.link_faces)

    ends_rule = ends
    used, loops = set(), []
    for e0 in bm.edges:
        if e0.index in used or e0.index in prot or len(e0.link_faces) != 2:
            continue
        edges, seen, ends, closed = [e0], {e0.index}, [], False
        for direction in (0, 1):
            e, v = e0, e0.verts[direction]
            while True:
                if not regular(v):
                    ends.append(v)
                    break
                n = next_edge(e, v)
                if n is None:
                    ends.append(v)
                    break
                if n.index in seen:
                    closed = True
                    break
                seen.add(n.index)
                if direction == 0:
                    edges.append(n)
                else:
                    edges.insert(0, n)
                e, v = n, n.other_vert(v)
            if closed:
                break
        used |= seen
        if any(x.index in prot for x in edges):
            continue
        if closed or (ends_rule != "closed" and len(ends) == 2 and ends[0] != ends[1]
                      and (ends_rule == "any" or all(end_ok(v) for v in ends))):
            loops.append(edges)
    return loops


def _pick_alternate(loops, greedy=True):
    """Every other loop: two loops are neighbours when a quad has one edge in each as opposite sides.
    2-colour the neighbour graph breadth-first; dissolve one colour, never two neighbours."""
    owner = {}
    for i, es in enumerate(loops):
        for e in es:
            owner[e.index] = i
    nbr = [set() for _ in loops]
    for i, es in enumerate(loops):
        for e in es:
            for f in e.link_faces:
                if len(f.verts) != 4:
                    continue
                ls = list(f.loops)
                k = next(j for j, l in enumerate(ls) if l.edge == e)
                o = ls[(k + 2) % 4].edge.index
                if o in owner and owner[o] != i:
                    nbr[i].add(owner[o])
    if greedy:
        chosen, blocked = [], set()
        for i in sorted(range(len(loops)), key=lambda i: -len(loops[i])):
            if i not in blocked:
                chosen.append(i)
                blocked |= nbr[i] | {i}
        return chosen
    color = [-1] * len(loops)
    for s_ in range(len(loops)):
        if color[s_] != -1:
            continue
        color[s_] = 0
        queue = [s_]
        while queue:
            i = queue.pop(0)
            for j in sorted(nbr[i]):
                if color[j] == -1:
                    color[j] = 1 - color[i]
                    queue.append(j)
    pick = [i for i in range(len(loops)) if color[i] == 1 and all(color[j] == 0 for j in nbr[i])]
    return pick


def reduce_region(obj, region_verts, passes=1, join=True, join_shape=40.0, write=True, greedy=True, ends="closed", guard=None,
                  color_layers=None):
    """region_verts: bool array over the mesh's vertices. With write=False nothing is written back (a dry run).
    guard: a loop filter or a list of them (a loop is dissolved only if all keep it); a filter with a .bind(bm)
    gets the bmesh before each pass. color_layers: see _protected_edges. Returns counts."""
    import math
    me = obj.data
    bm = bmesh.new()
    bm.from_mesh(me)
    inr = bm.verts.layers.int.new("retopo_region")
    for v in bm.verts:
        v[inr] = int(region_verts[v.index])
    info = {"passes": []}

    def region_faces():
        bm.faces.ensure_lookup_table()
        return {f.index for f in bm.faces if all(v[inr] for v in f.verts)}

    guards = [] if guard is None else list(guard) if isinstance(guard, (list, tuple)) else [guard]
    region = region_faces()
    info["before"] = {"faces": len(region), "tris": sum(len(bm.faces[i].verts) - 2 for i in region),
                      "tri_faces": sum(1 for i in region if len(bm.faces[i].verts) == 3)}
    if join:
        tri = [bm.faces[i] for i in region if len(bm.faces[i].verts) == 3]
        bmesh.ops.join_triangles(bm, faces=tri, cmp_seam=True, cmp_sharp=True, cmp_uvs=True, cmp_vcols=True,
                                 cmp_materials=True, angle_face_threshold=math.radians(40),
                                 angle_shape_threshold=math.radians(join_shape))
        region = region_faces()
        info["after_join_tri_faces"] = sum(1 for i in region if len(bm.faces[i].verts) == 3)
    for _ in range(passes):
        bm.edges.ensure_lookup_table()
        bm.normal_update()
        prot = _protected_edges(bm, region, color_layers)
        loops = _clean_loops(bm, region, prot, ends)
        if guards:
            for g in guards:
                if hasattr(g, "bind"):
                    g.bind(bm)
            blocked, kept = {}, []
            for es in loops:
                no = next((g for g in guards if not g(es)), None)
                if no is None:
                    kept.append(es)
                else:
                    label = getattr(no, "label", "guard")
                    blocked[label] = blocked.get(label, 0) + 1
            info.setdefault("guard_blocked", []).append(blocked)
            loops = kept
        pick = _pick_alternate(loops, greedy)
        rem = [e for i in pick for e in loops[i]]
        bmesh.ops.dissolve_edges(bm, edges=rem, use_verts=False, use_face_split=False)
        two = [v for v in bm.verts if v[inr] and len(v.link_edges) == 2 and len(v.link_faces) == 2
               and all(all(x[inr] for x in f.verts) for f in v.link_faces)]  # never touch faces outside the region
        bmesh.ops.dissolve_verts(bm, verts=two, use_face_split=False, use_boundary_tear=False)
        loose = [v for v in bm.verts if not v.link_edges]
        bmesh.ops.delete(bm, geom=loose, context="VERTS")
        region = region_faces()
        info["passes"].append({"clean_loops": len(loops), "dissolved_loops": len(pick), "edges": len(rem),
                               "faces": len(region), "tris": sum(len(bm.faces[i].verts) - 2 for i in region),
                               "ngons": sum(1 for i in region if len(bm.faces[i].verts) > 4),
                               "tri_faces": sum(1 for i in region if len(bm.faces[i].verts) == 3)})
    bm.verts.layers.int.remove(inr)
    if write:
        bm.to_mesh(me)
        me.update()
    bm.free()
    return info


def reset_vectors(me, loop_mask):
    """Reset Vectors (custom normals back to auto) on the corners in loop_mask; other corners keep theirs."""
    cur = np.empty(len(me.loops) * 3)
    me.corner_normals.foreach_get("vector", cur)
    cur = cur.reshape(-1, 3)
    cur[loop_mask] = 0.0
    me.normals_split_custom_set([tuple(x) for x in cur])


def cover_guard(cover_obj, margin=0.0008, reach=0.012):
    """A loop filter: rejects a loop if, at any of its vertices, the surface left after dissolving it (the
    midpoint of the vertex's two neighbours across the loop) would rise more than (gap to cover - margin)
    above the vertex, i.e. poke through cover_obj (a stocking). Uses the cover's rest shape (mesh plus its
    active shape keys, no Armature), in the same space as the body (both objects sit at the origin)."""
    from mathutils import Vector
    from mathutils.bvhtree import BVHTree
    cm = cover_obj.data
    n = len(cm.vertices)
    co = np.empty(n * 3)
    cm.vertices.foreach_get("co", co)
    co = co.reshape(-1, 3)
    if cm.shape_keys and cm.shape_keys.use_relative:  # rest shape = Basis + active keys, whatever the pose is
        kd = {k.name: k for k in cm.shape_keys.key_blocks}
        for k in cm.shape_keys.key_blocks[1:]:
            if k.value and not k.mute:
                a, r = np.empty(n * 3), np.empty(n * 3)
                k.data.foreach_get("co", a)
                kd[k.relative_key.name].data.foreach_get("co", r)
                co += k.value * (a - r).reshape(-1, 3)
    mw = cover_obj.matrix_world
    tree = BVHTree.FromPolygons([mw @ Vector(c) for c in co], [tuple(p.vertices) for p in cm.polygons])

    def ok(edges):
        es = {e.index for e in edges}
        for v in {v for e in edges for v in e.verts}:
            cross = [e.other_vert(v) for e in v.link_edges if e.index not in es]
            if len(cross) != 2:
                continue
            n = v.normal
            rise = ((cross[0].co + cross[1].co) / 2 - v.co).dot(n)
            if rise <= 0:
                continue
            hit = tree.ray_cast(v.co + n * 1e-5, n, reach)
            if hit[0] is not None and rise > hit[3] - margin:
                return False
        return True
    ok.label = "cover " + cover_obj.name
    return ok


def _across(v, es):
    """The vertex's two neighbours across the loop and where it sits between them (0..1), or None at loop ends."""
    cross = [e.other_vert(v) for e in v.link_edges if e.index not in es]
    if len(cross) != 2:
        return None
    a, b = cross
    ab = b.co - a.co
    ll = ab.length_squared
    t = 0.5 if ll == 0 else min(1.0, max(0.0, (v.co - a.co).dot(ab) / ll))
    return a, b, t


def shape_guard(max_loss=0.002, max_bend=None):
    """A loop filter: rejects a loop if any of its vertices lies more than max_loss from the straight line
    between its two neighbours across the loop, i.e. dissolving it would flatten a curve (calf, breast, butt)
    by more than that. With max_bend (degrees), also when the surface bends more than that across the loop at
    one of its vertices: small sharp features (the nipple's base, knuckles, toes) lose little in mm but a lot
    in look."""
    import math
    cos_bend = math.cos(math.radians(max_bend)) if max_bend else None

    def ok(edges):
        es = {e.index for e in edges}
        for v in {v for e in edges for v in e.verts}:
            ac = _across(v, es)
            if ac is not None:
                a, b, t = ac
                if max_loss is not None and (v.co - a.co.lerp(b.co, t)).length > max_loss:
                    return False
                if cos_bend is not None:
                    d1, d2 = v.co - a.co, b.co - v.co
                    if d1.length > 0 and d2.length > 0 and d1.normalized().dot(d2.normalized()) < cos_bend:
                        return False
        return True
    ok.label = "shape"
    return ok


def key_guard(obj, names, tol=0.001):
    """A loop filter: rejects a loop if, for any shape key in names, a vertex's key offset differs by more than
    tol from the offset interpolated between its neighbours across the loop, i.e. the key would lose detail
    there (a nipple or garter squeeze). Reads the key offsets from obj's mesh by original vertex index, so the
    mesh needs the int point attribute "retopo_orig" (original index) before the bmesh is made."""
    me = obj.data
    n = len(me.vertices)
    kb = me.shape_keys.key_blocks
    deltas = np.empty((len(names), n, 3), np.float32)
    a, r = np.empty(n * 3), np.empty(n * 3)
    for i, nm in enumerate(names):
        kb[nm].data.foreach_get("co", a)
        kb[nm].relative_key.data.foreach_get("co", r)
        deltas[i] = (a - r).reshape(-1, 3)
    state = {}

    def bind(bm):
        state["orig"] = bm.verts.layers.int["retopo_orig"]

    def ok(edges):
        lay = state["orig"]
        es = {e.index for e in edges}
        rows = []
        for v in {v for e in edges for v in e.verts}:
            ac = _across(v, es)
            if ac is not None:
                rows.append((v[lay], ac[0][lay], ac[1][lay], ac[2]))
        if not rows:
            return True
        iv, ia, ib, t = (np.array(x) for x in zip(*rows))
        t = t[None, :, None]
        err = deltas[:, iv] - ((1 - t) * deltas[:, ia] + t * deltas[:, ib])
        return float(np.sqrt((err ** 2).sum(-1)).max()) <= tol
    ok.bind = bind
    ok.label = "keys"
    return ok


def _corner_values(bm):
    """A function giving a loop's UVs, colours and other corner data (not the custom normal) as a tuple."""
    lays = [(k, lay) for k in ("uv", "color", "float_color", "float", "int", "float_vector")
            for name, lay in getattr(bm.loops.layers, k).items() if not name.startswith(".")]

    def values(lp):
        out = []
        for k, lay in lays:
            x = lp[lay]
            out.append(tuple(x.uv) if k == "uv" else tuple(x) if k in ("color", "float_color", "float_vector") else x)
        return tuple(out)
    return values


def _quad_dev(pts, n):
    """Worst corner's distance from 90 degrees, or None if the quad isn't convex against normal n."""
    import math
    worst = 0.0
    for k in range(4):
        a, b, c = pts[k - 1], pts[k], pts[(k + 1) % 4]
        u, w = a - b, c - b
        if (b - a).cross(c - b).dot(n) <= 0 or u.length == 0 or w.length == 0:
            return None
        worst = max(worst, abs(math.degrees(u.angle(w)) - 90.0))
    return worst


def _splits(m):
    """Ways to cut an m-gon (6 or 8 corners) into quads, as lists of corner-index quads."""
    if m == 6:
        out = [[(i, i + 1, i + 2, i + 3), (i + 3, i + 4, i + 5, i)] for i in range(3)]
    else:
        out = [[(i, i + 1, i + 2, i + 3), (i + 4, i + 5, i + 6, i + 7), (i, i + 3, i + 4, i + 7)] for i in range(4)]
        out += [[(i, i + 1, i + 2, i + 3), (i, i + 3, i + 4, i + 5), (i, i + 5, i + 6, i + 7)] for i in range(8)]
    return [[tuple(x % m for x in q) for q in s] for s in out]


def pair_pentagons(bm, in_region, max_dev=55.0, passes=3):
    """Turns pentagons into quads without adding triangles or moving anything: a pentagon and a neighbouring
    triangle (6 corners) become 2 quads, two neighbouring pentagons (8 corners) become 3 quads. Only across an
    edge that could be dissolved (smooth, no seam, same material, the same corner data on both sides), and only
    when every new quad is convex with all corners within max_dev degrees of square; the best-shaped cut wins.
    in_region(face) -> bool. Corners keep the data they had. Returns counts per pass."""
    values = _corner_values(bm)
    done = []
    for _ in range(passes):
        bm.normal_update()
        cands = []
        for e in bm.edges:
            lf = e.link_faces
            if len(lf) != 2 or e.seam or not e.smooth:
                continue
            p, x = sorted(lf, key=lambda f: -len(f.verts))
            if len(p.verts) != 5 or len(x.verts) not in (3, 5) or p.material_index != x.material_index \
                    or not (in_region(p) and in_region(x)):
                continue
            lp = next(l for l in p.loops if l.edge == e)
            lx = next(l for l in x.loops if l.edge == e)
            if lx.vert is not lp.link_loop_next.vert:
                continue  # the two faces wind opposite ways
            if values(lp) != values(lx.link_loop_next) or values(lp.link_loop_next) != values(lx):
                continue  # corner data differs across the edge: it's a seam in disguise
            ring = [lp.link_loop_next]
            while len(ring) < len(p.verts):
                ring.append(ring[-1].link_loop_next)
            l = lx.link_loop_next.link_loop_next
            while l.vert is not lp.link_loop_next.vert:
                ring.append(l)
                l = l.link_loop_next
            verts = [r.vert for r in ring]
            if len(set(verts)) != len(verts) or len(verts) != len(p.verts) + len(x.verts) - 2:
                continue  # the faces touch along more than this edge
            n = (p.normal + x.normal).normalized()
            best = None
            for s in _splits(len(verts)):
                devs = [_quad_dev([verts[i].co for i in q], n) for q in s]
                if None in devs:
                    continue
                if max(devs) <= max_dev and (best is None or max(devs) < best[0]):
                    best = (max(devs), s)
            if best:
                cands.append((best[0], p, x, verts, best[1]))
        used, made = set(), {"pent+tri": 0, "pent+pent": 0}
        for dev, p, x, verts, s in sorted(cands, key=lambda c: c[0]):
            if p in used or x in used:
                continue
            used |= {p, x}
            kind = "pent+tri" if len(x.verts) == 3 else "pent+pent"
            f = bmesh.utils.face_join([p, x])
            if f is None:
                continue
            # cut along each quad side that isn't a side of the joined face
            m = len(verts)
            diags = {tuple(sorted((q[k], q[(k + 1) % 4]))) for q in s for k in range(4)
                     if (q[(k + 1) % 4] - q[k]) % m not in (1, m - 1)}
            pieces = [f]
            for i, j in sorted(diags):
                vi, vj = verts[i], verts[j]
                face = next(g for g in pieces if g.is_valid and vi in g.verts and vj in g.verts and len(g.verts) > 4)
                new_face, _ = bmesh.utils.face_split(face, vi, vj)
                pieces.append(new_face)
            made[kind] += 1
        done.append(made)
        if not any(made.values()):
            break
    return done


def fix_ngons(bm, faces, join_angle=60.0):
    """Re-splits n-gons without moving anything: each face is triangulated (beauty) and its triangles joined
    back into quads where they make good ones. Same vertices, same triangle count, corners keep their data."""
    import math
    made = []
    for f in faces:
        if not f.is_valid:
            continue
        tris = bmesh.ops.triangulate(bm, faces=[f], quad_method="BEAUTY", ngon_method="BEAUTY")["faces"]
        out = bmesh.ops.join_triangles(bm, faces=tris, cmp_seam=True, cmp_sharp=True, cmp_uvs=True, cmp_vcols=True,
                                       cmp_materials=True, angle_face_threshold=math.radians(join_angle),
                                       angle_shape_threshold=math.radians(join_angle))
        made += [x for x in out["faces"] if x.is_valid] + [t for t in tris if t.is_valid]
    return list({id(x): x for x in made if x.is_valid}.values())
