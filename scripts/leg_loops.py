"""Method A for a body part: join triangles into quads, then dissolve every other clean edge loop, keeping all
faces quads. Works in place on a bmesh of a full duplicate, so every attribute, the weights and all shape keys of
the kept vertices stay exactly as they were.

    ns = {"__name__": "leg_loops"}; exec(open(path, encoding="utf-8").read(), ns)
    info = ns["reduce_region"](dup_obj, region_vertex_mask, passes=1, join=True)

ends="closed" (default) dissolves closed loops (rings) only: that's the one case where every merged face stays
a quad. An open loop ending at a 3-edge pole turns one neighbouring quad into a triangle ("v3"), and one
ending anywhere else leaves a 5-sided face ("any"). Never dissolved: edges leaving the region, UV seams or UV-map
splits (every map), sharp edges, material borders, colour borders (a channel jump > 0.1 in any colour layer, like
nail masks), jumps in any other corner attribute (float, vector, int: a garment's "body UV" or a skin mask),
edges between faces wound opposite ways, non-manifold edges.

Loops are picked face-disjoint (no two dissolved loops touch the same face), so every vertex a loop removes is
removed by that loop alone and its guards checked it against neighbours that stay. Only the loops' own vertices
are dissolved afterwards: vertices that already had 2 edges (a hair strand's tip) and loose vertices stay.
"""
import bmesh
import bpy
import numpy as np

COLOR_JUMP = 0.1
ATTR_JUMP = 1e-6


def _attr_layers(bm):
    """Corner layers that aren't UVs or colours (float, vector, int), without internal and retopo_* ones."""
    return [(k, lay) for k in ("float", "float_vector", "int")
            for name, lay in getattr(bm.loops.layers, k).items() if not name.startswith((".", "retopo"))]


def _protected_edges(bm, region, color_layers=None):
    """color_layers: names of the colour layers whose jumps protect an edge; None = every colour layer."""
    uv_layers = list(bm.loops.layers.uv.values())
    cols = [lay for coll in (bm.loops.layers.color, bm.loops.layers.float_color) for name, lay in coll.items()
            if not name.startswith(".") and (color_layers is None or name in color_layers)]
    attrs = _attr_layers(bm)
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
        if e.link_loops[0].vert is e.link_loops[1].vert:  # the two faces wind opposite ways (layered parts)
            prot.add(e.index)
            continue
        for v in e.verts:
            l1 = next(l for l in f1.loops if l.vert == v)
            l2 = next(l for l in f2.loops if l.vert == v)
            if any((l1[u].uv - l2[u].uv).length > 1e-6 for u in uv_layers) or \
                    any(max(abs(a - b) for a, b in zip(l1[c], l2[c])) > COLOR_JUMP for c in cols) or \
                    any((max(abs(a - b) for a, b in zip(l1[lay], l2[lay])) if k == "float_vector"
                         else abs(l1[lay] - l2[lay])) > ATTR_JUMP for k, lay in attrs):
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
    """Every other loop: two loops are neighbours when they touch the same face. That covers parallel loops one
    quad apart, and also loops that cross or meet at a pole: removing two of those together removes vertices no
    guard checked (a crossing vertex loses all four edges) and leaves holes and 20-37-sided faces.
    2-colour the neighbour graph breadth-first; dissolve one colour, never two neighbours."""
    by_face = {}
    for i, es in enumerate(loops):
        for e in es:
            for f in e.link_faces:
                by_face.setdefault(f.index, set()).add(i)
    nbr = [set() for _ in loops]
    for owners in by_face.values():
        for i in owners:
            nbr[i] |= owners - {i}
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


def _topology(bm):
    """Open edges, edges with 3+ faces, wire edges and loose vertices: a pass must leave these counts as they were
    (reduce_region reports any change per pass as "topology_changed")."""
    out = {"open": 0, "nonmanifold": 0, "wire": 0}
    for e in bm.edges:
        k = len(e.link_faces)
        if k == 0:
            out["wire"] += 1
        elif k == 1:
            out["open"] += 1
        elif k > 2:
            out["nonmanifold"] += 1
    out["loose_verts"] = sum(1 for v in bm.verts if not v.link_edges)
    return out


def reduce_region(obj, region_verts, passes=1, join=True, join_shape=40.0, write=True, greedy=True, ends="closed", guard=None,
                  color_layers=None):
    """region_verts: bool array over the mesh's vertices. With write=False nothing is written back (a dry run).
    guard: a loop filter or a list of them (a loop is dissolved only if all keep it); a filter with a .bind(bm)
    gets the bmesh before each pass. color_layers: see _protected_edges. Returns counts; a pass's
    "topology_changed" should be empty."""
    import math
    me = obj.data
    bm = bmesh.new()
    bm.from_mesh(me)
    inr = bm.verts.layers.int.new("retopo_region")
    for v in bm.verts:
        v[inr] = int(region_verts[v.index])
    info = {"passes": []}

    def region_faces():
        bm.faces.index_update()  # bmesh ops leave element indices stale
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
        bm.verts.index_update()
        bm.edges.index_update()
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
        topo0 = _topology(bm)
        cand = {v for e in rem for v in e.verts}  # only the loops' own vertices may go
        bmesh.ops.dissolve_edges(bm, edges=rem, use_verts=False, use_face_split=False)
        two = [v for v in cand if v.is_valid and v[inr] and len(v.link_edges) == 2 and len(v.link_faces) == 2
               and all(all(x[inr] for x in f.verts) for f in v.link_faces)]  # never touch faces outside the region
        bmesh.ops.dissolve_verts(bm, verts=two, use_face_split=False, use_boundary_tear=False)
        loose = [v for v in cand if v.is_valid and not v.link_edges]
        bmesh.ops.delete(bm, geom=loose, context="VERTS")
        region = region_faces()
        topo1 = _topology(bm)
        info["passes"].append({"clean_loops": len(loops), "dissolved_loops": len(pick), "edges": len(rem),
                               "faces": len(region), "tris": sum(len(bm.faces[i].verts) - 2 for i in region),
                               "ngons": sum(1 for i in region if len(bm.faces[i].verts) > 4),
                               "max_sides": max((len(bm.faces[i].verts) for i in region), default=0),
                               "tri_faces": sum(1 for i in region if len(bm.faces[i].verts) == 3),
                               "topology_changed": {k: topo1[k] - topo0[k] for k in topo0 if topo1[k] != topo0[k]}})
    bm.verts.layers.int.remove(inr)
    if write:
        bm.to_mesh(me)
        me.update()
    bm.free()
    return info


def set_custom_normals(me, normals):
    """normals_split_custom_set where a zero vector really means "auto" (Reset Vectors). In Blender 5.2 a zero
    vector leaves the corner's existing custom normal as it was when the mesh already has custom normals (tested
    on 5.2.3: a mesh tilted 98 degrees stayed 98 degrees off); on a mesh without them it means auto. So the old
    custom normals are cleared first. Note that normals_split_custom_set also marks edges sharp where it has to
    split a fan, and auto normals respect sharp edges."""
    cn = me.attributes.get("custom_normal")
    if cn is not None:
        me.attributes.remove(cn)
    me.normals_split_custom_set([tuple(x) for x in normals])


def auto_corner_normals(me):
    """The corner normals a true Reset Vectors would give on me (a copy with its custom normals cleared), for
    checks. Don't build the reference with normals_split_custom_set(zeros) on a copy: in Blender 5.2 that changes
    nothing, so a check against it always passes."""
    ref = me.copy()
    cn = ref.attributes.get("custom_normal")
    if cn is not None:
        ref.attributes.remove(cn)
    out = np.empty(len(ref.loops) * 3)
    ref.corner_normals.foreach_get("vector", out)
    bpy.data.meshes.remove(ref)
    return out.reshape(-1, 3)


def reset_vectors(me, loop_mask):
    """Reset Vectors (custom normals back to auto) on the corners in loop_mask; other corners keep theirs."""
    cur = np.empty(len(me.loops) * 3)
    me.corner_normals.foreach_get("vector", cur)
    cur = cur.reshape(-1, 3)
    cur[loop_mask] = 0.0
    set_custom_normals(me, cur)


def cover_guard(cover_obj, margin=0.0008, reach=0.012, side="over"):
    """A loop filter: rejects a loop if, at any of its vertices, the surface left after dissolving it (the
    midpoint of the vertex's two neighbours across the loop) would rise more than (gap to cover - margin)
    above the vertex, i.e. poke through cover_obj (a stocking). Uses the cover's rest shape (mesh plus its
    active shape keys, no Armature), in the same space as the body (both objects sit at the origin).
    side="under": cover_obj lies under the reduced mesh instead (the body under a garment being reduced), and the
    loop is rejected if the surface would sink into it."""
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
    sign = -1.0 if side == "under" else 1.0

    def ok(edges):
        es = {e.index for e in edges}
        for v in {v for e in edges for v in e.verts}:
            cross = [e.other_vert(v) for e in v.link_edges if e.index not in es]
            if len(cross) != 2:
                continue
            n = v.normal * sign
            rise = ((cross[0].co + cross[1].co) / 2 - v.co).dot(n)
            if rise <= 0:
                continue
            hit = tree.ray_cast(v.co + n * 1e-5, n, reach)
            if hit[0] is not None and rise > hit[3] - margin:
                return False
        return True
    ok.label = ("under " if side == "under" else "cover ") + cover_obj.name
    return ok


def _merged_corners(e):
    """For a loop edge e = (v, w): the corners of its two faces next to v and w that are not on e."""
    v, w = e.verts
    out = []
    for f in e.link_faces:
        ls = list(f.loops)
        cv = next((l.link_loop_prev.vert if l.link_loop_next.vert is w else l.link_loop_next.vert) for l in ls if l.vert is v)
        cw = next((l.link_loop_prev.vert if l.link_loop_next.vert is v else l.link_loop_next.vert) for l in ls if l.vert is w)
        out.append((cv.co, cw.co))
    return out


def _merged_gap(pt, nrm, a1, a2, b2, b1, diag):
    """Signed height (along nrm) of the merged face (a1, a2, b2, b1) over pt, triangulated along a1-b2 (diag 0)
    or a2-b1 (diag 1): the nearer of its two triangles."""
    from mathutils.geometry import closest_point_on_tri
    t = ((a1, a2, b2), (a1, b2, b1)) if diag == 0 else ((a1, a2, b1), (a2, b2, b1))
    q1 = closest_point_on_tri(pt, *t[0])
    q2 = closest_point_on_tri(pt, *t[1])
    q = q1 if (q1 - pt).length <= (q2 - pt).length else q2
    return (q - pt).dot(nrm)


def orig_guard(obj, limit=0.002):
    """A loop filter measured against the ORIGINAL surface, so losses don't add up over passes (LOD2, extra
    passes): a pass's shape guard only sees the current mesh, and one stocking vertex lost 3.8 mm over four 1.5 mm
    passes. Make it from the unreduced duplicate (its vertex positions are the original surface). Each pass,
    every original vertex is given to the current face nearest to it; a loop is rejected if the face its edge's two
    faces merge into (either triangulation) would lie more than limit from one of those vertices, along the faces'
    normal."""
    from mathutils import Vector
    from mathutils.bvhtree import BVHTree
    me = obj.data
    co = np.empty(len(me.vertices) * 3)
    me.vertices.foreach_get("co", co)
    pts = [Vector(p) for p in co.reshape(-1, 3)]
    state = {}

    def bind(bm):
        bm.faces.ensure_lookup_table()
        tree = BVHTree.FromBMesh(bm)
        under = {}
        for k, pt in enumerate(pts):
            loc, nrm, fi, dist = tree.find_nearest(pt, 0.02)
            if loc is not None:
                under.setdefault(fi, []).append(k)
        state["under"] = under

    def ok(edges):
        under = state["under"]
        for e in edges:
            f1, f2 = e.link_faces
            ks = under.get(f1.index, []) + under.get(f2.index, [])
            if not ks:
                continue
            (a1, b1), (a2, b2) = _merged_corners(e)
            nrm = f1.normal + f2.normal
            if nrm.length < 1e-9:
                continue
            nrm.normalize()
            for k in ks:
                if max(abs(_merged_gap(pts[k], nrm, a1, a2, b2, b1, d)) for d in (0, 1)) > limit:
                    return False
        return True
    ok.bind = bind
    ok.label = "original surface"
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
            for name, lay in getattr(bm.loops.layers, k).items() if not name.startswith((".", "retopo"))]

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


def _joined(a, b):
    return any(e.other_vert(a) is b for e in a.link_edges)


def _cut_exists(verts, split):
    """True if one of the split's cuts joins two corners that already share an edge elsewhere."""
    m = len(verts)
    for q in split:
        for k in range(4):
            i, j = q[k], q[(k + 1) % 4]
            if (j - i) % m not in (1, m - 1) and _joined(verts[i], verts[j]):
                return True
    return False


def _chord_exists(f):
    """True if two corners of f that aren't neighbours around it already share an edge (a fold or a thin strip):
    triangulating f could then reuse that edge and give it a third face."""
    vs = list(f.verts)
    n = len(vs)
    return any(_joined(vs[i], vs[j]) for i in range(n) for j in range(i + 2, n) if not (i == 0 and j == n - 1))


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
                if _cut_exists(verts, s):
                    continue  # face_split would reuse that edge and give it a third face
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
    back into quads where they make good ones. Same vertices, same triangle count, corners keep their data.
    Faces with a chord that already exists as an edge are left as they are (see _chord_exists)."""
    import math
    made = []
    for f in faces:
        if not f.is_valid or _chord_exists(f):
            continue
        tris = bmesh.ops.triangulate(bm, faces=[f], quad_method="BEAUTY", ngon_method="BEAUTY")["faces"]
        out = bmesh.ops.join_triangles(bm, faces=tris, cmp_seam=True, cmp_sharp=True, cmp_uvs=True, cmp_vcols=True,
                                       cmp_materials=True, angle_face_threshold=math.radians(join_angle),
                                       angle_shape_threshold=math.radians(join_angle))
        made += [x for x in out["faces"] if x.is_valid] + [t for t in tris if t.is_valid]
    return list({id(x): x for x in made if x.is_valid}.values())
