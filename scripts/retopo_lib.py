"""Shared tools for the retopo methods (garments and body regions). Run inside Blender:

    ns = {"__name__": "retopo_lib"}; exec(open(path, encoding="utf-8").read(), ns)

Safety: the source object and its mesh are only read, and only through a private copy of the mesh
(Source.me). Every variant is a full duplicate (obj.copy() + data.copy()) in the COLLECTION
collection (the caller sets it, e.g. "<source> Retopo"), named "<source> Retopo <tag>", so an exporter that picks
objects by exact name never sees them.

A variant is described by a Spec: for each new vertex, up to 3 (source vertex, weight) pairs; for each
new face corner, up to 3 (source loop, weight) pairs; and the edges to mark sharp. write_variant() turns
it into a mesh with the source's shape keys, weights, UV maps, colours and corner normals interpolated
from those pairs.
"""
import hashlib
import math

import bmesh
import bpy
import numpy as np
from mathutils import Vector
from mathutils.bvhtree import BVHTree

COLLECTION = "Retopo"  # the caller sets it after exec, e.g. ns["COLLECTION"] = f"{source} Retopo"
MAX_INFLUENCES = 4
WRITE_INFO = {}


# ---------------------------------------------------------------- fingerprint
def fingerprint(obj):
    """Hashes everything about a mesh object that a retopo could touch."""
    me = obj.data
    n, L = len(me.vertices), len(me.loops)

    def hh(a):
        return hashlib.sha256(np.ascontiguousarray(a).tobytes()).hexdigest()

    d = {}
    co = np.empty(n * 3, np.float32)
    me.vertices.foreach_get("co", co)
    d["co"] = hh(co)
    if me.shape_keys:
        for kb in me.shape_keys.key_blocks:
            a = np.empty(n * 3, np.float32)
            kb.data.foreach_get("co", a)
            d["key:" + kb.name] = hh(a)
    for u in me.uv_layers:
        a = np.empty(L * 2, np.float32)
        u.data.foreach_get("uv", a)
        d["uv:" + u.name] = hh(a)
    for c in me.color_attributes:
        a = np.empty(len(c.data) * 4, np.float32)
        c.data.foreach_get("color", a)
        d["col:" + c.name] = hh(a)
    lv = np.empty(L, np.int32)
    me.loops.foreach_get("vertex_index", lv)
    d["loops"] = hh(lv)
    w = [(v.index, g.group, g.weight) for v in me.vertices for g in v.groups]
    d["weights"] = hh(np.array(w, np.float64))
    d["n_weights"] = len(w)
    cn = me.attributes.get("custom_normal")
    if cn:
        a = np.empty(len(cn.data) * 2, np.int16)
        cn.data.foreach_get("value", a)
        d["custom_normal"] = hh(a)
    d["vgroup_names"] = hh(np.frombuffer("|".join(g.name for g in obj.vertex_groups).encode(), np.uint8))
    return d


def tri_count(obj):
    return sum(p.loop_total - 2 for p in obj.data.polygons)


# ---------------------------------------------------------------- source snapshot
class Source:
    """Read-only arrays and topology of the source garment, taken from a private copy of its mesh."""

    def __init__(self, obj):
        self.obj = obj
        me = self.me = obj.data.copy()
        me.name = obj.data.name + " (retopo read copy)"
        n, L = len(me.vertices), len(me.loops)
        self.n, self.L = n, L
        self.co = np.empty(n * 3, np.float64)
        me.vertices.foreach_get("co", self.co)
        self.co = self.co.reshape(-1, 3)
        self.keys = []  # (name, coords, settings)
        if me.shape_keys:
            for kb in me.shape_keys.key_blocks:
                a = np.empty(n * 3, np.float64)
                kb.data.foreach_get("co", a)
                self.keys.append((kb.name, a.reshape(-1, 3), {
                    "relative_key": kb.relative_key.name, "slider_min": kb.slider_min, "slider_max": kb.slider_max,
                    "value": kb.value, "vertex_group": kb.vertex_group, "interpolation": kb.interpolation,
                    "mute": kb.mute}))
            self.key_settings = {"use_relative": me.shape_keys.use_relative}
        self.groups = [g.name for g in obj.vertex_groups]
        self.weights = np.zeros((n, len(self.groups)), np.float64)
        for v in me.vertices:
            for g in v.groups:
                self.weights[v.index, g.group] = g.weight
        arm = next((m.object for m in obj.modifiers if m.type == "ARMATURE" and m.object), None)
        deform = {b.name for b in arm.data.bones if b.use_deform} if arm else set()
        self.deform_cols = np.array([i for i, g in enumerate(self.groups) if g in deform], np.int64)
        self.loop_vert = np.empty(L, np.int64)
        me.loops.foreach_get("vertex_index", self.loop_vert)
        self.uvs = {}
        for u in me.uv_layers:
            a = np.empty(L * 2, np.float64)
            u.data.foreach_get("uv", a)
            self.uvs[u.name] = a.reshape(-1, 2)
        self.uv_names = [u.name for u in me.uv_layers]
        self.active_uv = me.uv_layers.active.name
        self.colors = {}
        self.color_meta = []
        for c in me.color_attributes:
            a = np.empty(len(c.data) * 4, np.float64)
            c.data.foreach_get("color_srgb", a)
            self.colors[c.name] = a.reshape(-1, 4)
            self.color_meta.append((c.name, c.data_type, c.domain))
        self.active_color = me.color_attributes.active_color_name
        self.render_color = me.color_attributes[me.color_attributes.render_color_index].name
        cn = np.empty(L * 3, np.float64)
        me.corner_normals.foreach_get("vector", cn)
        self.normals = cn.reshape(-1, 3)
        self.loop_start = np.empty(len(me.polygons), np.int64)
        me.polygons.foreach_get("loop_start", self.loop_start)
        self.loop_total = np.empty(len(me.polygons), np.int64)
        me.polygons.foreach_get("loop_total", self.loop_total)
        self.loop_face = np.repeat(np.arange(len(me.polygons)), self.loop_total)
        fn = np.empty(len(me.polygons) * 3, np.float64)
        me.polygons.foreach_get("normal", fn)
        self.face_normal = fn.reshape(-1, 3)
        me.calc_loop_triangles()
        T = len(me.loop_triangles)
        self.tri_loops = np.empty(T * 3, np.int64)
        me.loop_triangles.foreach_get("loops", self.tri_loops)
        self.tri_loops = self.tri_loops.reshape(-1, 3)
        self.tri_face = np.empty(T, np.int64)
        me.loop_triangles.foreach_get("polygon_index", self.tri_face)
        self.sharp = np.zeros(len(me.edges), bool)
        if me.attributes.get("sharp_edge"):
            me.attributes["sharp_edge"].data.foreach_get("value", self.sharp)

        bm = self.bm = bmesh.new()
        bm.from_mesh(me)
        bm.verts.ensure_lookup_table()
        bm.edges.ensure_lookup_table()
        bm.faces.ensure_lookup_table()
        for f in bm.faces:  # bmesh keeps the mesh's loop order; check it so loop ids can be trusted
            l0 = f.loops[0]
            assert self.loop_vert[self.loop_start[f.index]] == l0.vert.index
        self._components()
        self._edge_classes()
        self._islands()

    def free(self):
        self.bm.free()
        if self.me.name in bpy.data.meshes:
            bpy.data.meshes.remove(self.me)

    def loop_id(self, f, l):
        """Mesh loop index of bmesh loop l in face f."""
        k = list(f.loops).index(l)
        return int(self.loop_start[f.index] + k)

    def face_loop_at(self, f, v):
        for k, l in enumerate(f.loops):
            if l.vert == v:
                return int(self.loop_start[f.index] + k)
        raise KeyError

    def _components(self):
        comp = np.full(self.n, -1, np.int64)
        c = 0
        for v in self.bm.verts:
            if comp[v.index] != -1:
                continue
            st = [v]
            comp[v.index] = c
            while st:
                x = st.pop()
                for e in x.link_edges:
                    y = e.other_vert(x)
                    if comp[y.index] == -1:
                        comp[y.index] = c
                        st.append(y)
            c += 1
        self.comp = comp
        self.main = int(np.bincount(comp).argmax())

    def _uv_disc(self, e):
        f1, f2 = e.link_faces
        uv = self.uvs["UVMap"]
        for v in e.verts:
            a = uv[self.face_loop_at(f1, v)]
            b = uv[self.face_loop_at(f2, v)]
            if np.abs(a - b).max() > 1e-6:
                return True
        return False

    def _edge_classes(self):
        """0 interior, 1 boundary, 2 non-manifold, 3 UV seam, 4 crease (sharp, not a seam)."""
        cls = np.zeros(len(self.bm.edges), np.int8)
        for e in self.bm.edges:
            nf = len(e.link_faces)
            if nf == 1:
                cls[e.index] = 1
            elif nf > 2:
                cls[e.index] = 2
            elif self._uv_disc(e):
                cls[e.index] = 3
            elif self.sharp[e.index]:
                cls[e.index] = 4
        self.edge_class = cls

    def _islands(self):
        """Faces joined across UV-continuous manifold edges (the Marvelous Designer pattern pieces)."""
        isl = np.full(len(self.bm.faces), -1, np.int64)
        k = 0
        for f in self.bm.faces:
            if isl[f.index] != -1:
                continue
            st = [f]
            isl[f.index] = k
            while st:
                x = st.pop()
                for e in x.edges:
                    if self.edge_class[e.index] in (0, 4):
                        for y in e.link_faces:
                            if isl[y.index] == -1:
                                isl[y.index] = k
                                st.append(y)
            k += 1
        self.island = isl
        self.n_islands = k

    # ------------------------------------------------------------ flattening
    def flatten(self, collection):
        """Near-isometric (minimum stretch) flattening of every pattern piece, as per-loop coords (L,2).
        Runs the unwrap on a temporary object holding another copy of the mesh; deletes it afterwards."""
        tmp_me = self.me.copy()
        tmp = bpy.data.objects.new(self.obj.name + " Retopo flatten temp", tmp_me)
        collection.objects.link(tmp)
        vl = bpy.context.view_layer
        prev_sel = [o for o in bpy.context.selected_objects]
        prev_act = vl.objects.active
        try:
            if tmp_me.shape_keys:
                tmp.active_shape_key_index = 0
            for o in prev_sel:
                o.select_set(False)
            tmp.select_set(True)
            vl.objects.active = tmp
            seams = (self.edge_class == 1) | (self.edge_class == 2) | (self.edge_class == 3)
            tmp_me.edges.foreach_set("use_seam", seams.tolist())
            fu = tmp_me.uv_layers.new(name="FlatUV")
            tmp_me.uv_layers.active = fu
            win = bpy.context.window_manager.windows[0]
            area = next(a for a in win.screen.areas if a.type == "VIEW_3D")
            region = next(r for r in area.regions if r.type == "WINDOW")
            with bpy.context.temp_override(window=win, area=area, region=region, active_object=tmp, object=tmp,
                                           selected_objects=[tmp], selected_editable_objects=[tmp]):
                bpy.ops.object.mode_set(mode="EDIT")
                bpy.ops.mesh.select_mode(type="FACE")
                bpy.ops.mesh.select_all(action="SELECT")
                bpy.ops.uv.unwrap(method="MINIMUM_STRETCH", fill_holes=True, correct_aspect=True, margin=0.002)
                bpy.ops.object.mode_set(mode="OBJECT")
            a = np.empty(self.L * 2, np.float64)
            tmp_me.uv_layers["FlatUV"].data.foreach_get("uv", a)
            self.flat = a.reshape(-1, 2)
        finally:
            if tmp.mode != "OBJECT":
                with bpy.context.temp_override(active_object=tmp, object=tmp):
                    bpy.ops.object.mode_set(mode="OBJECT")
            bpy.data.objects.remove(tmp)
            bpy.data.meshes.remove(tmp_me)
            for o in prev_sel:
                if o.name in bpy.data.objects:
                    o.select_set(True)
            vl.objects.active = prev_act
        self._island_scales()
        return self.flat

    def _island_scales(self):
        """Metres per FlatUV unit for each island, and flattening quality (stretch spread, flips)."""
        self.scale = np.ones(self.n_islands)
        self.flips = np.zeros(self.n_islands, np.int64)
        tri_isl = self.island[self.tri_face]
        a3 = self.co[self.loop_vert[self.tri_loops]]
        a2 = self.flat[self.tri_loops]
        area3 = 0.5 * np.linalg.norm(np.cross(a3[:, 1] - a3[:, 0], a3[:, 2] - a3[:, 0]), axis=1)
        e2 = a2[:, 1:] - a2[:, :1]
        area2s = 0.5 * (e2[:, 0, 0] * e2[:, 1, 1] - e2[:, 0, 1] * e2[:, 1, 0])
        self.stretch = {}
        for i in range(self.n_islands):
            m = tri_isl == i
            if not m.any():
                continue
            s2 = np.abs(area2s[m]).sum()
            self.scale[i] = math.sqrt(area3[m].sum() / s2) if s2 > 0 else 1.0
            # orientation: most triangles share one sign; the rest are flips
            sign = np.sign(area2s[m].sum())
            self.flips[i] = int((np.sign(area2s[m]) != sign).sum())
            r = np.sqrt(area3[m] / np.maximum(np.abs(area2s[m]) * self.scale[i] ** 2, 1e-20))
            self.stretch[i] = (float(np.percentile(r, 5)), float(np.percentile(r, 95)))

    def build_locators(self):
        """A 2D BVH per island over its loop triangles in FlatUV space."""
        self.loc = {}
        tri_isl = self.island[self.tri_face]
        for i in range(self.n_islands):
            tris = np.nonzero(tri_isl == i)[0]
            if not len(tris):
                continue
            p = self.flat[self.tri_loops[tris]].reshape(-1, 2)
            verts = [Vector((x, y, 0.0)) for x, y in p]
            polys = [(3 * k, 3 * k + 1, 3 * k + 2) for k in range(len(tris))]
            self.loc[i] = (BVHTree.FromPolygons(verts, polys, all_triangles=True, epsilon=0.0), tris)

    def locate(self, isl, p, nudge_to=None):
        """Source loop triangle and barycentric weights of FlatUV point p in island isl. With nudge_to, the
        triangle is picked at a point moved a hair toward nudge_to (to pick a side at creases and seams), but
        the weights are for p itself."""
        tree, tris = self.loc[isl]
        q = p if nudge_to is None else p + (np.asarray(nudge_to) - p) * 1e-3
        hit = tree.find_nearest(Vector((q[0], q[1], 0.0)))
        t = int(tris[hit[2]])
        a, b, c = self.flat[self.tri_loops[t]]
        w = barycentric(p, a, b, c)
        return t, w, hit[3]


def barycentric(p, a, b, c):
    v0, v1, v2 = b - a, c - a, p - a
    d00, d01, d11 = v0 @ v0, v0 @ v1, v1 @ v1
    d20, d21 = v2 @ v0, v2 @ v1
    den = d00 * d11 - d01 * d01
    if abs(den) < 1e-30:
        return np.array([1.0, 0.0, 0.0])
    v = (d11 * d20 - d01 * d21) / den
    w = (d00 * d21 - d01 * d20) / den
    bc = np.array([1.0 - v - w, v, w])
    bc = np.clip(bc, 0.0, None)
    return bc / bc.sum()


# ---------------------------------------------------------------- spec
class Spec:
    """New vertices and faces, with interpolation sources in the original mesh."""

    def __init__(self):
        self.vsrc = []  # per new vertex: list of (source vertex, weight)
        self.faces = []  # tuples of new vertex ids
        self.csrc = []  # per face: list (per corner) of lists of (source loop, weight)
        self.sharp = set()  # sorted new vertex pairs
        self.fixed = set()  # faces whose winding is already right (write_variant won't re-orient them)

    def add_vertex(self, pairs):
        self.vsrc.append(pairs)
        return len(self.vsrc) - 1

    def add_face(self, verts, corners, fixed=False):
        if fixed:
            self.fixed.add(len(self.faces))
        self.faces.append(tuple(verts))
        self.csrc.append(corners)

    def extend(self, other):
        off = len(self.vsrc)
        self.fixed |= {f + len(self.faces) for f in other.fixed}
        self.vsrc += other.vsrc
        self.faces += [tuple(v + off for v in f) for f in other.faces]
        self.csrc += other.csrc
        self.sharp |= {(a + off, b + off) for a, b in other.sharp}


def _pad(pairs_list):
    n = len(pairs_list)
    idx = np.zeros((n, 3), np.int64)
    w = np.zeros((n, 3), np.float64)
    for i, pairs in enumerate(pairs_list):
        for k, (j, x) in enumerate(pairs[:3]):
            idx[i, k] = j
            w[i, k] = x
    w /= w.sum(1, keepdims=True)
    return idx, w


def interp(data, idx, w):
    return (data[idx] * w[..., None]).sum(1)


# ---------------------------------------------------------------- writer
def get_collection():
    col = bpy.data.collections.get(COLLECTION)
    if col is None:
        col = bpy.data.collections.new(COLLECTION)
        bpy.context.scene.collection.children.link(col)
    return col


def write_variant(src, tag, spec):
    """Builds '<source> Retopo <tag>' as a full duplicate of the source and replaces its geometry with spec.
    Replaces an earlier variant with the same tag (only objects inside the retopo collection)."""
    col = get_collection()
    name = f"{src.obj.name} Retopo {tag}"
    assert name != src.obj.name
    old = bpy.data.objects.get(name)
    if old is not None:
        assert COLLECTION in [c.name for c in old.users_collection], name
        old_me = old.data
        bpy.data.objects.remove(old)
        if old_me.users == 0 and old_me != src.obj.data:
            bpy.data.meshes.remove(old_me)

    o2 = src.obj.copy()
    o2.data = src.obj.data.copy()
    assert o2.data != src.obj.data
    col.objects.link(o2)
    o2.name = name
    me = o2.data
    me.name = f"{src.obj.data.name} Retopo {tag}"
    o2.shape_key_clear()
    me.clear_geometry()
    for a in [a.name for a in me.attributes if not a.name.startswith(".") and a.name not in ("position",)]:
        if a in me.attributes:
            me.attributes.remove(me.attributes[a])
    # vertex group names live on the mesh, so clear_geometry() dropped them
    for g in src.groups:
        if g not in o2.vertex_groups:
            o2.vertex_groups.new(name=g)
    assert [g.name for g in o2.vertex_groups] == src.groups

    vidx, vw = _pad(spec.vsrc)
    co = interp(src.co, vidx, vw)
    # orient every face like the source faces its corners came from (geometric normals, not custom ones)
    faces, csrc, flipped = [], [], 0
    for fi, (f, cs) in enumerate(zip(spec.faces, spec.csrc)):
        if fi in spec.fixed:
            faces.append(f)
            csrc.append(cs)
            continue
        ci, cwf = _pad(cs)
        n_src = (src.face_normal[src.loop_face[ci]] * cwf[..., None]).sum((0, 1))
        p = co[list(f)]
        n_geo = np.cross(p[1:-1] - p[0], p[2:] - p[0]).sum(0)
        if n_geo @ n_src < 0:
            f, cs = tuple(reversed(f)), list(reversed(cs))
            flipped += 1
        faces.append(f)
        csrc.append(cs)
    spec.faces, spec.csrc = faces, csrc
    corners = [c for cs in spec.csrc for c in cs]
    cidx, cw = _pad(corners)
    me.from_pydata(co.tolist(), [], [list(f) for f in spec.faces])
    me.shade_smooth()
    WRITE_INFO[name] = {"faces_reoriented": flipped}

    for u in src.uv_names:
        layer = me.uv_layers.new(name=u)
        layer.data.foreach_set("uv", interp(src.uvs[u], cidx, cw).ravel())
    me.uv_layers.active = me.uv_layers[src.active_uv]
    for u in me.uv_layers:
        u.active_render = u.name == src.active_uv
    for cname, dtype, domain in src.color_meta:
        c = me.color_attributes.new(cname, dtype, domain)
        c.data.foreach_set("color_srgb", np.clip(interp(src.colors[cname], cidx, cw), 0, 1).ravel())
    me.color_attributes.active_color = me.color_attributes[src.active_color]
    me.color_attributes.render_color_index = [c.name for c in me.color_attributes].index(src.render_color)

    # sharp edges, then corner normals (the normal spaces depend on the sharp edges)
    ev = np.empty(len(me.edges) * 2, np.int64)
    me.edges.foreach_get("vertices", ev)
    ev = ev.reshape(-1, 2)
    lookup = {(min(a, b), max(a, b)): i for i, (a, b) in enumerate(ev)}
    sharp = np.zeros(len(me.edges), bool)
    for a, b in spec.sharp:
        i = lookup.get((min(a, b), max(a, b)))
        if i is not None:
            sharp[i] = True
    sa = me.attributes.new("sharp_edge", "BOOLEAN", "EDGE")
    sa.data.foreach_set("value", sharp.tolist())
    nrm = interp(src.normals, cidx, cw)
    nrm /= np.maximum(np.linalg.norm(nrm, axis=1, keepdims=True), 1e-12)
    me.normals_split_custom_set([tuple(x) for x in nrm])

    # weights: deform groups limited to the strongest MAX_INFLUENCES and normalised, others interpolated
    W = interp(src.weights, vidx, vw)
    dc = src.deform_cols
    D = W[:, dc]
    if D.shape[1] > MAX_INFLUENCES:
        cut = np.sort(D, axis=1)[:, -MAX_INFLUENCES][:, None]
        D = np.where(D >= cut, D, 0.0)
    s = D.sum(1, keepdims=True)
    D = np.where(s > 0, D / np.maximum(s, 1e-12), D)
    W[:, dc] = D
    for gi, g in enumerate(o2.vertex_groups):
        col_w = W[:, gi]
        nz = np.nonzero(col_w > 1e-5)[0]
        for vi in nz:
            g.add([int(vi)], float(col_w[vi]), "REPLACE")

    # shape keys
    if src.keys:
        for kname, kco, ks in src.keys:
            kb = o2.shape_key_add(name=kname, from_mix=False)
            kb.data.foreach_set("co", interp(kco, vidx, vw).ravel())
        kbs = me.shape_keys.key_blocks
        me.shape_keys.use_relative = src.key_settings["use_relative"]
        for kname, kco, ks in src.keys:
            kb = kbs[kname]
            kb.relative_key = kbs[ks["relative_key"]]
            kb.slider_min, kb.slider_max = ks["slider_min"], ks["slider_max"]
            kb.value = ks["value"]
            kb.vertex_group = ks["vertex_group"]
            kb.interpolation = ks["interpolation"]
            kb.mute = ks["mute"]
        o2.active_shape_key_index = 0
    me.update()
    return o2


# ---------------------------------------------------------------- buttons
def button_spec(src, segments_keep_every=2, rings_keep=(0, 1, 4, 5, 6, 8)):
    """Rebuilds every button (the small revolved components) with half the segments and fewer rings. Kept
    vertices are original vertices; each corner copies the original corner of the original face nearest the
    new face's centre, so data is exact."""
    spec = Spec()
    bm = src.bm
    for c in sorted(set(src.comp.tolist()) - {src.main}):
        verts = [v for v in bm.verts if src.comp[v.index] == c]
        centre = next(v for v in verts if len(v.link_edges) >= 12)
        dist = {centre.index: 0}
        frontier = [centre]
        while frontier:
            nxt = []
            for v in frontier:
                for e in v.link_edges:
                    w = e.other_vert(v)
                    if w.index not in dist:
                        dist[w.index] = dist[v.index] + 1
                        nxt.append(w)
            frontier = nxt
        nr = max(dist.values())
        # spokes: from each ring-1 vertex, follow radial edges outward
        ring1 = [e.other_vert(centre) for e in centre.link_edges]
        # order ring 1 around the circle
        order = [ring1[0]]
        while len(order) < len(ring1):
            cur = order[-1]
            nb = [e.other_vert(cur) for e in cur.link_edges if dist.get(e.other_vert(cur).index) == 1]
            nb = [w for w in nb if w not in order]
            if not nb:
                break
            order.append(nb[0])
        spokes = []
        for v in order:
            chain = [v]
            for r in range(2, nr + 1):
                cur = chain[-1]
                nxt = [e.other_vert(cur) for e in cur.link_edges if dist.get(e.other_vert(cur).index) == r]
                chain.append(nxt[0])
            spokes.append([centre] + chain)
        keep_s = list(range(0, len(spokes), segments_keep_every))
        rings = [r for r in rings_keep if r <= nr]
        vid = {}

        def nv(v):
            if v.index not in vid:
                vid[v.index] = spec.add_vertex([(v.index, 1.0)])
            return vid[v.index]

        def corner(v, face_verts):
            ctr = sum((x.co for x in face_verts), Vector()) / len(face_verts)
            f = min(v.link_faces, key=lambda f: (f.calc_center_median() - ctr).length)
            return [(src.face_loop_at(f, v), 1.0)]

        for si, s in enumerate(keep_s):
            s2 = keep_s[(si + 1) % len(keep_s)]
            for ri in range(len(rings) - 1):
                r0, r1 = rings[ri], rings[ri + 1]
                if r0 == 0:
                    fv = [centre, spokes[s][r1], spokes[s2][r1]]
                else:
                    fv = [spokes[s][r0], spokes[s][r1], spokes[s2][r1], spokes[s2][r0]]
                spec.add_face([nv(v) for v in fv], [corner(v, fv) for v in fv])
        # sharp rings
        for r in rings:
            if r == 0:
                continue
            for si, s in enumerate(keep_s):
                s2 = keep_s[(si + 1) % len(keep_s)]
                a, b = spokes[s][r], spokes[s2][r]
                e = next((e for e in a.link_edges if e.other_vert(a) == spokes[(s + 1) % len(spokes)][r]), None)
                if e is not None and src.sharp[e.index]:
                    spec.sharp.add((vid[a.index], vid[b.index]))
    return spec


# ---------------------------------------------------------------- checks
def mesh_stats(obj):
    me = obj.data
    bm = bmesh.new()
    bm.from_mesh(me)
    lens = np.array([e.calc_length() for e in bm.edges])
    areas = np.array([f.calc_area() for f in bm.faces])
    out = {
        "verts": len(me.vertices), "faces": len(me.polygons), "tris": tri_count(obj),
        "quads": sum(1 for p in me.polygons if p.loop_total == 4),
        "ngons": sum(1 for p in me.polygons if p.loop_total > 4),
        "boundary_edges": sum(1 for e in bm.edges if e.is_boundary),
        "nonmanifold_edges": sum(1 for e in bm.edges if len(e.link_faces) > 2),
        "loose_verts": sum(1 for v in bm.verts if not v.link_faces),
        "degenerate_faces": int((areas < 1e-9).sum()),
        "edge_mm": {"p5": round(float(np.percentile(lens, 5)) * 1000, 2),
                    "median": round(float(np.median(lens)) * 1000, 2),
                    "p95": round(float(np.percentile(lens, 95)) * 1000, 2)},
        "max_influences": max((sum(1 for g in v.groups if g.weight > 1e-4) for v in me.vertices), default=0),
        "shape_keys": len(me.shape_keys.key_blocks) if me.shape_keys else 0,
    }
    bm.free()
    return out


def intersections(me):
    """Pairs of loop triangles that cut through each other (not sharing a vertex), with their polygons and
    centres."""
    me.calc_loop_triangles()
    co = [v.co.copy() for v in me.vertices]
    tris = [tuple(t.vertices) for t in me.loop_triangles]
    polys = [t.polygon_index for t in me.loop_triangles]
    tree = BVHTree.FromPolygons(co, tris, all_triangles=True, epsilon=0.0)
    out = []
    for i, j in tree.overlap(tree):
        if i < j and not set(tris[i]) & set(tris[j]):
            c = (sum((co[k] for k in tris[i]), Vector()) + sum((co[k] for k in tris[j]), Vector())) / 6
            out.append((polys[i], polys[j], c))
    return out


def new_intersection_faces(src, obj, spec, radius=0.008):
    """Original faces under the variant's intersections that the original doesn't have within radius."""
    from mathutils.kdtree import KDTree
    orig = intersections(src.me)
    kd = KDTree(max(1, len(orig)))
    for k, (_, _, c) in enumerate(orig):
        kd.insert(c, k)
    kd.balance()
    faces = set()
    new = 0
    for pi, pj, c in intersections(obj.data):
        if orig and kd.find(c)[2] < radius:
            continue
        new += 1
        for p in (pi, pj):
            for corner in spec.csrc[p]:
                for loop, w in corner:
                    if w > 0:
                        faces.add(int(src.loop_face[loop]))
    return faces, new


def fingerprint_all(obj):
    """Like fingerprint(), but hashes every mesh attribute (internal ones too), every shape key, the weights,
    vertex group names, material slots and modifiers: for heavy sources like a body."""
    me = obj.data
    d = {}
    for a in me.attributes:
        dom = {"POINT": len(me.vertices), "EDGE": len(me.edges), "FACE": len(me.polygons), "CORNER": len(me.loops)}[a.domain]
        width = {"FLOAT": 1, "INT": 1, "BOOLEAN": 1, "INT8": 1, "FLOAT2": 2, "INT32_2D": 2, "INT16_2D": 2,
                 "FLOAT_VECTOR": 3, "FLOAT_COLOR": 4, "BYTE_COLOR": 4, "QUATERNION": 4}.get(a.data_type)
        if width is None:
            continue
        prop = {"FLOAT_VECTOR": "vector", "FLOAT2": "vector", "FLOAT_COLOR": "color", "BYTE_COLOR": "color",
                "QUATERNION": "value"}.get(a.data_type, "value")
        dt = {"BOOLEAN": bool, "INT": np.int32, "INT8": np.int8, "INT32_2D": np.int32, "INT16_2D": np.int16}.get(a.data_type, np.float32)
        arr = np.empty(dom * width, dt)
        a.data.foreach_get(prop, arr)
        d["attr:" + a.name] = hashlib.sha256(arr.tobytes()).hexdigest()
    n = len(me.vertices)
    if me.shape_keys:
        h = hashlib.sha256()
        for kb in me.shape_keys.key_blocks:
            arr = np.empty(n * 3, np.float32)
            kb.data.foreach_get("co", arr)
            h.update(kb.name.encode())
            h.update(arr.tobytes())
            h.update(np.array([kb.value, kb.slider_min, kb.slider_max], np.float32).tobytes())
        d["shape_keys"] = h.hexdigest()
        d["n_keys"] = len(me.shape_keys.key_blocks)
    w = [(v.index, g.group, g.weight) for v in me.vertices for g in v.groups]
    d["weights"] = hashlib.sha256(np.array(w, np.float64).tobytes()).hexdigest()
    d["vgroup_names"] = hashlib.sha256("|".join(g.name for g in obj.vertex_groups).encode()).hexdigest()
    d["materials"] = [s.material.name if s.material else None for s in obj.material_slots]
    d["modifiers"] = [(m.name, m.type) for m in obj.modifiers]
    d["counts"] = [n, len(me.edges), len(me.polygons), len(me.loops)]
    return d


def ngon_review(bm, bad_sides=7, max_bend=0.08, min_roundness=0.3):
    """Rates every face with more than 4 corners. A face is bad when it has bad_sides or more corners, bends out
    of its plane by more than max_bend x its size, has a concave corner, or is a sliver (isoperimetric
    roundness 4*pi*area/perimeter^2 below min_roundness; a square is 0.79, a 4:1 strip 0.5). Returns a summary
    and the bad faces with their reasons."""
    import math
    rows, bad = [], []
    for f in bm.faces:
        n = len(f.verts)
        if n <= 4:
            continue
        pts = [v.co for v in f.verts]
        ctr = f.calc_center_median()
        nrm = f.normal
        area = f.calc_area()
        size = math.sqrt(max(area, 1e-12))
        bend = max(abs((p - ctr).dot(nrm)) for p in pts) / size
        per = f.calc_perimeter()
        round_ = 4 * math.pi * area / (per * per) if per > 0 else 0.0
        concave = False
        for k in range(n):
            a, b, c = pts[k - 1], pts[k], pts[(k + 1) % n]
            if (b - a).cross(c - b).dot(nrm) < -1e-12:
                concave = True
                break
        why = []
        if n >= bad_sides:
            why.append(f"{n} sides")
        if bend > max_bend:
            why.append(f"bent {bend:.2f}")
        if concave:
            why.append("concave")
        if round_ < min_roundness:
            why.append(f"sliver {round_:.2f}")
        rows.append(n)
        if why:
            bad.append({"face": f.index, "sides": n, "centre": tuple(round(x, 3) for x in ctr), "why": why})
    from collections import Counter
    return {"ngons": len(rows), "by_sides": dict(sorted(Counter(rows).items())), "bad": len(bad)}, bad


def density_metrics(me):
    """Per-vertex positions, local edge length (the mean of its edges, m) and flatness: how far the vertex sits
    off the mean of its neighbours along its normal, over that edge length (uneven spacing along the surface
    doesn't count). On a curve meshed with n edges around, flatness is about pi / n, so pi / flatness estimates the
    edges around the curve: ~24 is smooth, ~60+ means edges far shorter than the shape needs (an over-dense
    patch, where another pass removes loops without changing the look)."""
    n = len(me.vertices)
    co = np.empty(n * 3)
    me.vertices.foreach_get("co", co)
    co = co.reshape(-1, 3)
    ev = np.empty(len(me.edges) * 2, np.int64)
    me.edges.foreach_get("vertices", ev)
    ev = ev.reshape(-1, 2)
    ln = np.linalg.norm(co[ev[:, 0]] - co[ev[:, 1]], axis=1)
    deg = np.maximum(np.bincount(ev.ravel(), minlength=n), 1).astype(float)
    mean_len = (np.bincount(ev[:, 0], ln, n) + np.bincount(ev[:, 1], ln, n)) / deg
    nb = np.stack([np.bincount(ev[:, 0], co[ev[:, 1], k], n) + np.bincount(ev[:, 1], co[ev[:, 0], k], n)
                   for k in range(3)], axis=1) / deg[:, None]
    vn = np.empty(n * 3)
    me.vertex_normals.foreach_get("vector", vn)
    flatness = np.abs(((co - nb) * vn.reshape(-1, 3)).sum(1)) / np.maximum(mean_len, 1e-9)
    return co, mean_len, flatness


def uv_islands(me, uv_name=None):
    """An island number per vertex (the island of its first corner) in a UV map (default: the first)."""
    uv = me.uv_layers[uv_name] if uv_name else me.uv_layers[0]
    n = len(me.polygons)
    parent = list(range(n))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i
    corner_uv = {}
    for p in me.polygons:
        for li in p.loop_indices:
            v = me.loops[li].vertex_index
            key = (v, round(uv.data[li].uv[0], 5), round(uv.data[li].uv[1], 5))
            if key in corner_uv:
                a, b = find(corner_uv[key]), find(p.index)
                if a != b:
                    parent[a] = b
            else:
                corner_uv[key] = p.index
    island = np.full(len(me.vertices), -1)
    for p in me.polygons:
        r = find(p.index)
        for v in p.vertices:
            if island[v] < 0:
                island[v] = r
    return island
