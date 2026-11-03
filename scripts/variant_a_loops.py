"""Variant A: dissolve every other edge loop of the quad grid, in both directions.

Exec in Blender with the retopo_lib namespace in `ns` and the Source in `src`. Kept vertices are original
vertices and kept corners are original corners, so positions, shape keys, weights, UVs, colours and
normals are exact; only the faces get bigger.

Rules:
- Chain edges (open edges, T-junctions, UV seams, creases) are never dissolved.
- Parity: opposite edges of a quad alternate, edges continuing straight through a regular grid vertex
  match. A union-find with parity keeps the first consistent assignment; an edge is only dissolved if no
  quad next to it would lose its other side too (so two neighbouring lines never both go).
- Afterwards, interior vertices left with 2 edges are dissolved, and chain vertices left with only 2
  chain edges of one kind are dissolved where the chain is nearly straight (TURN_LIMIT).
- n-gons left over are triangulated (beauty), so Unity doesn't triangulate concave ones badly.
"""
import math

import bmesh

TURN_LIMIT = 25.0  # degrees; chain vertices bending more than this stay (pattern corners)


def loop_dissolve_spec(src, ns):
    bm = bmesh.new()
    bm.from_mesh(src.me)
    bm.verts.ensure_lookup_table()
    bm.edges.ensure_lookup_table()
    bm.faces.ensure_lookup_table()
    ov = bm.verts.layers.int.new("ov")
    ol = bm.loops.layers.int.new("ol")
    oe = bm.edges.layers.int.new("oe")
    for v in bm.verts:
        v[ov] = v.index
    for e in bm.edges:
        e[oe] = e.index
    for f in bm.faces:
        for k, l in enumerate(f.loops):
            l[ol] = int(src.loop_start[f.index] + k)
    cls = src.edge_class
    bmesh.ops.delete(bm, geom=[v for v in bm.verts if src.comp[v[ov]] != src.main], context="VERTS")
    bm.verts.ensure_lookup_table()
    bm.edges.ensure_lookup_table()
    bm.faces.ensure_lookup_table()
    chain = lambda e: cls[e[oe]] != 0

    # ---- parity union-find over edges
    parent = list(range(len(bm.edges)))
    par = [0] * len(bm.edges)

    def find(x):
        p = 0
        path = []
        while parent[x] != x:
            path.append(x)
            p ^= par[x]
            x = parent[x]
        # path compression
        acc = p
        for y in path:
            old = par[y]
            parent[y] = x
            par[y] = acc
            acc ^= old
        return x, p

    conflicts = 0

    def union(a, b, rel):
        nonlocal conflicts
        ra, pa = find(a)
        rb, pb = find(b)
        if ra == rb:
            if pa ^ pb != rel:
                conflicts += 1
            return
        parent[ra] = rb
        par[ra] = pa ^ pb ^ rel

    def regular(v):
        return len(v.link_edges) == 4 and len(v.link_faces) == 4 and all(len(f.verts) == 4 for f in v.link_faces)

    # BFS over faces so constraints are added in a spatially coherent order
    seen = set()
    for seed in bm.faces:
        if seed.index in seen:
            continue
        queue = [seed]
        seen.add(seed.index)
        while queue:
            f = queue.pop(0)
            if len(f.verts) == 4:
                es = [l.edge for l in f.loops]
                union(es[0].index, es[2].index, 1)
                union(es[1].index, es[3].index, 1)
            for l in f.loops:
                v = l.vert
                if regular(v):
                    # edges around v in cyclic order
                    ring = []
                    lp = l
                    for _ in range(4):
                        ring.append(lp.edge)
                        lp = lp.link_loop_prev.link_loop_radial_next
                    if len({e.index for e in ring}) == 4:
                        union(ring[0].index, ring[2].index, 0)
                        union(ring[1].index, ring[3].index, 0)
                for g in l.edge.link_faces:
                    if g.index not in seen:
                        seen.add(g.index)
                        queue.append(g)

    parity = [find(e.index)[1] for e in bm.edges]

    def removable(e):
        if chain(e) or len(e.link_faces) != 2 or parity[e.index] != 1:
            return False
        for f in e.link_faces:
            if len(f.verts) != 4:
                continue
            opp = next(l.link_loop_next.link_loop_next.edge for l in f.loops if l.edge == e)
            if parity[opp.index] == 1 and not chain(opp):
                return False
        return True

    rem = [e for e in bm.edges if removable(e)]
    n_removed = len(rem)
    bmesh.ops.dissolve_edges(bm, edges=rem, use_verts=False, use_face_split=False)

    # ---- vertex cleanup
    bmesh.ops.delete(bm, geom=[v for v in bm.verts if not v.link_edges], context="VERTS")
    interior2 = [v for v in bm.verts if len(v.link_edges) == 2 and not any(chain(e) for e in v.link_edges)]
    bmesh.ops.dissolve_verts(bm, verts=interior2, use_face_split=False, use_boundary_tear=False)

    def straight_chain_vert(v):
        if len(v.link_edges) != 2:
            return False
        a, b = v.link_edges
        if not (chain(a) and chain(b)) or cls[a[oe]] != cls[b[oe]] or src.sharp[a[oe]] != src.sharp[b[oe]]:
            return False
        d1 = (a.other_vert(v).co - v.co).normalized()
        d2 = (v.co - b.other_vert(v).co).normalized()
        return math.degrees(d1.angle(d2, 0.0)) < TURN_LIMIT

    # every other straight chain vertex in a row, so a long run isn't collapsed to one edge
    cands = {v.index for v in bm.verts if straight_chain_vert(v)}
    picked = []
    blocked = set()
    for v in bm.verts:
        if v.index in cands and v.index not in blocked:
            picked.append(v)
            for e in v.link_edges:
                blocked.add(e.other_vert(v).index)
    bmesh.ops.dissolve_verts(bm, verts=picked, use_face_split=False, use_boundary_tear=False)

    ngons = [f for f in bm.faces if len(f.verts) > 4]
    bmesh.ops.triangulate(bm, faces=ngons, quad_method="BEAUTY", ngon_method="BEAUTY")

    # ---- spec
    spec = ns["Spec"]()
    vid = {}
    for v in bm.verts:
        vid[v.index] = spec.add_vertex([(v[ov], 1.0)])
    for f in bm.faces:
        spec.add_face([vid[v.index] for v in f.verts], [[(l[ol], 1.0)] for l in f.loops], fixed=True)
    for e in bm.edges:
        if src.sharp[e[oe]]:
            spec.sharp.add((vid[e.verts[0].index], vid[e.verts[1].index]))
    info = {"edges_dissolved": n_removed, "parity_conflicts": conflicts, "chain_verts_dissolved": len(picked),
            "ngons_triangulated": len(ngons)}
    bm.free()
    return spec, info


spec_a, info_a = loop_dissolve_spec(src, ns)
