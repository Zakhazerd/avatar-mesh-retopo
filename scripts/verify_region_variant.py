"""verify(var_name, oi, region_mask, source) in live Blender: checks a region-reduced duplicate against its source.
oi: variant vertex -> Body vertex. Read-only."""
import bpy
import numpy as np


def _co(me):
    a = np.empty(len(me.vertices) * 3)
    me.vertices.foreach_get("co", a)
    return a.reshape(-1, 3)


def _attr(me, a):
    n = {"POINT": len(me.vertices), "EDGE": len(me.edges), "FACE": len(me.polygons), "CORNER": len(me.loops)}[a.domain]
    w = {"FLOAT2": 2, "FLOAT_VECTOR": 3, "FLOAT_COLOR": 4, "BYTE_COLOR": 4, "QUATERNION": 4, "INT32_2D": 2, "INT16_2D": 2}.get(a.data_type, 1)
    prop = {"FLOAT_VECTOR": "vector", "FLOAT2": "vector", "FLOAT_COLOR": "color", "BYTE_COLOR": "color", "QUATERNION": "value"}.get(a.data_type, "value")
    dt = bool if a.data_type == "BOOLEAN" else np.float64
    arr = np.empty(n * w, dt)
    a.data.foreach_get(prop, arr)
    return arr.reshape(n, w)


def verify(var_name, oi, region, source):
    b = bpy.data.objects[source].data
    v = bpy.data.objects[var_name].data
    out = {}
    out["positions"] = bool(np.array_equal(_co(v), _co(b)[oi]))
    kb_b, kb_v = b.shape_keys.key_blocks, v.shape_keys.key_blocks
    same_keys = [k.name for k in kb_b] == [k.name for k in kb_v]
    bad_keys = []
    nb, nv = len(b.vertices), len(v.vertices)
    for kb, kv in zip(kb_b, kb_v):
        a, c = np.empty(nb * 3, np.float32), np.empty(nv * 3, np.float32)
        kb.data.foreach_get("co", a)
        kv.data.foreach_get("co", c)
        if not np.array_equal(c.reshape(-1, 3), a.reshape(-1, 3)[oi]) or kb.value != kv.value or \
                kb.relative_key.name != kv.relative_key.name or (kb.slider_min, kb.slider_max) != (kv.slider_min, kv.slider_max):
            bad_keys.append(kb.name)
    out["keys"] = {"count": len(kb_v), "names_same": same_keys, "differ": bad_keys}
    wb = [sorted((g.group, g.weight) for g in x.groups) for x in b.vertices]
    out["weights"] = all(sorted((g.group, g.weight) for g in x.groups) == wb[oi[i]] for i, x in enumerate(v.vertices))
    # attributes
    attrs = {}
    corner_b_vert = np.empty(len(b.loops), np.int64)
    b.loops.foreach_get("vertex_index", corner_b_vert)
    corner_v_vert = np.empty(len(v.loops), np.int64)
    v.loops.foreach_get("vertex_index", corner_v_vert)
    for a in b.attributes:
        if a.name.startswith(".") and a.domain != "POINT" or a.name in ("position",):
            continue
        av = v.attributes.get(a.name)
        if av is None or av.domain != a.domain or av.data_type != a.data_type:
            attrs[a.name] = "missing"
            continue
        if a.domain == "POINT":
            attrs[a.name] = bool(np.array_equal(_attr(v, av), _attr(b, a)[oi]))
        elif a.domain == "CORNER":
            xb, xv = _attr(b, a), _attr(v, av)
            have = {}
            for i, vert in enumerate(corner_b_vert):
                have.setdefault(int(vert), set()).add(tuple(xb[i]))
            miss = sum(1 for i, vert in enumerate(corner_v_vert) if tuple(xv[i]) not in have[int(oi[vert])])
            attrs[a.name] = "ok" if miss == 0 else f"{miss} corners with new values"
    out["attributes"] = attrs
    # faces outside the region: every Body face touching a vertex outside it must still exist, same corners
    def cycles(me, vmap=None):
        d = {}
        for p in me.polygons:
            vs = [int(vmap[x]) if vmap is not None else x for x in p.vertices]
            k = vs.index(min(vs))
            d[tuple(vs[k:] + vs[:k])] = p.index
        return d
    cb, cv = cycles(b), cycles(v, oi)
    outside = [c for c in cb if not all(region[x] for x in c)]
    out["outside_faces"] = {"count": len(outside), "missing": sum(1 for c in outside if c not in cv)}
    # custom normals: outside faces keep theirs, region corners are reset (match auto)
    nb_ = np.empty(len(b.loops) * 3); b.corner_normals.foreach_get("vector", nb_); nb_ = nb_.reshape(-1, 3)
    nv_ = np.empty(len(v.loops) * 3); v.corner_normals.foreach_get("vector", nv_); nv_ = nv_.reshape(-1, 3)
    worst = 0.0
    for c in outside:
        pb, pv = b.polygons[cb[c]], v.polygons[cv[c]] if c in cv else None
        if pv is None:
            continue
        lb = {b.loops[li].vertex_index: li for li in pb.loop_indices}
        for li in pv.loop_indices:
            bl = lb[int(oi[v.loops[li].vertex_index])]
            worst = max(worst, float(np.degrees(np.arccos(np.clip(np.dot(nv_[li], nb_[bl]), -1, 1)))))
    out["outside_normals_max_deg"] = round(worst, 4)
    # n-gons and counts
    sides = np.array([p.loop_total for p in v.polygons])
    out["tris"] = int((sides - 2).sum())
    out["faces_by_sides"] = {"3": int((sides == 3).sum()), "4": int((sides == 4).sum()), "5": int((sides == 5).sum()), "6+": int((sides >= 6).sum())}
    out["groups_same"] = [g.name for g in bpy.data.objects[source].vertex_groups] == [g.name for g in bpy.data.objects[var_name].vertex_groups]
    out["materials_same"] = [s.material for s in bpy.data.objects[source].material_slots] == [s.material for s in bpy.data.objects[var_name].material_slots]
    return out
