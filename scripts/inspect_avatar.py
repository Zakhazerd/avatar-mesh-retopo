"""Lists the candidates for avatar_config.py from the live Blender (read-only). Run it through the MCP:

    ns = {"__name__": "inspect_avatar"}; exec(open(path, encoding="utf-8").read(), ns); result = ns["report"]

The bone roles and the facing direction are guesses from names and the feet: check them with the user, and look at
the colour channels and vertex groups before calling one a mask.
"""
import os
import re
from collections import Counter

import bpy
import numpy as np

ROLES = {  # role: (words that must appear, one of them; words that rule a bone out)
    "hips": (["hips", "hip", "pelvis"], ["thigh", "leg"]),
    "spine": (["spine"], ["upper", "2", "3"]),
    "chest": (["upperchest", "chest", "spine2"], ["breast"]),
    "neck": (["neck"], []),
    "head": (["head"], ["top", "end"]),
    "upper_arm": (["upperarm", "arm"], ["fore", "lower", "twist", "shoulder", "clavicle"]),
    "lower_arm": (["lowerarm", "forearm", "elbow"], ["twist"]),
    "hand": (["hand", "wrist"], ["twist", "index", "middle", "ring", "little", "pinky", "thumb"]),
    "upper_leg": (["upperleg", "upleg", "thigh", "leg"], ["lower", "knee", "twist", "support"]),
    "lower_leg": (["lowerleg", "knee", "calf", "shin"], ["twist", "support"]),
    "foot": (["foot", "ankle"], ["toe", "ik"]),
}
SKIP = ["twist", "helper", "ik", "pole", "end", "tip", "support", "adj", "root"]


def _norm(name):
    return re.sub(r"[^a-z0-9]", "", name.lower())


def _side(name):
    n = name.lower()
    if re.search(r"(^|[^a-z])(l|left)([^a-z]|$)", n) or "left" in n or re.search(r"[._ ]l$", n):
        return "l"
    if re.search(r"(^|[^a-z])(r|right)([^a-z]|$)", n) or "right" in n or re.search(r"[._ ]r$", n):
        return "r"
    return None


def guess_bones(arm):
    bones = [b for b in arm.data.bones if b.use_deform]
    out = {}
    for role, (want, ban) in ROLES.items():
        for side in ([None] if role in ("hips", "spine", "chest", "neck", "head") else ["l", "r"]):
            cands = []
            for b in bones:
                n = _norm(b.name)
                if not any(w in n for w in want) or any(x in n for x in ban + SKIP):
                    continue
                if side is not None and _side(b.name) != side:
                    continue
                if side is None and _side(b.name) is not None:
                    continue
                rank = min(i for i, w in enumerate(want) if w in n)  # earlier words in the list are better matches
                cands.append((rank, len(b.name), b.name))
            key = role if side is None else f"{role}_{side}"
            out[key] = [c[2] for c in sorted(cands)[:3]]
    hand = (out.get("hand_l") or [None])[0]
    fingers = []
    if hand:
        for f in ("index", "middle", "ring", "little", "pinky"):
            chain = []
            b = next((c for c in arm.data.bones[hand].children_recursive if f in _norm(c.name)), None)
            while b is not None and f in _norm(b.name):
                chain.append(b.name)
                b = next((c for c in b.children if f in _norm(c.name)), None)
            if chain:
                fingers.append(chain[:3])
    out["fingers_l"] = fingers
    return out


def guess_front(arm, foot):
    """The feet point forward: the foot bone's direction in XY, snapped to an axis."""
    if not foot:
        return None
    b = arm.data.bones[foot]
    d = arm.matrix_world.to_3x3() @ (b.tail_local - b.head_local)
    if abs(d.x) > abs(d.y):
        return (1 if d.x > 0 else -1, 0, 0)
    return (0, 1 if d.y > 0 else -1, 0)


def colour_layers(me):
    out = []
    for a in me.color_attributes:
        n = len(me.loops) if a.domain == "CORNER" else len(me.vertices)
        c = np.empty(n * 4)
        a.data.foreach_get("color", c)
        c = c.reshape(-1, 4)
        chans = {}
        for i, ch in enumerate("RGBA"):
            x = c[:, i]
            binary = float(np.mean((x < 0.02) | (x > 0.98)))
            chans[ch] = {"share_over_half": round(float(np.mean(x > 0.5)), 4), "binary_share": round(binary, 3)}
        out.append({"name": a.name, "domain": a.domain, "type": a.data_type, "channels": chans})
    return out


def key_prefixes(me):
    if not me.shape_keys:
        return {}
    c = Counter(re.split(r"[_ .]", k.name)[0] + "_" for k in me.shape_keys.key_blocks[1:])
    return dict(c.most_common(12))


def key_health(me):
    """Shape keys that look broken: moving most of the mesh, or by more than a fifth of its size (a Symmetrize run
    with keys, a unit or vertex-order mismatch on import). Check them with the user before guarding keys."""
    if not me.shape_keys:
        return []
    n = len(me.vertices)
    co = np.empty(n * 3)
    me.vertices.foreach_get("co", co)
    co = co.reshape(-1, 3)
    size = float(np.ptp(co, axis=0).max())
    out = []
    for k in me.shape_keys.key_blocks[1:]:
        a, r = np.empty(n * 3), np.empty(n * 3)
        k.data.foreach_get("co", a)
        k.relative_key.data.foreach_get("co", r)
        d = np.linalg.norm((a - r).reshape(-1, 3), axis=1)
        share, top = float(np.mean(d > 0.0005)), float(d.max())
        if share > 0.4 or top > 0.2 * size:
            out.append({"key": k.name, "moves_share": round(share, 3), "max_m": round(top, 3)})
    return out


def missing_textures(o):
    out = []
    for s in o.material_slots:
        m = s.material
        if not (m and m.use_nodes):
            continue
        for n in m.node_tree.nodes:
            if n.type == "TEX_IMAGE" and n.image and not n.image.packed_file:
                p = bpy.path.abspath(n.image.filepath) if n.image.filepath else ""
                if not (p and os.path.exists(p)):
                    out.append({"material": m.name, "image": n.image.name})
    return out


meshes = []
for o in bpy.context.scene.objects:
    if o.type != "MESH" or " Retopo " in o.name or o.name.endswith(" Retopo") or "Lineup" in o.name:
        continue  # this workflow's own duplicates
    mod = next((m for m in o.modifiers if m.type == "ARMATURE" and m.object), None)
    me = o.data
    meshes.append({"name": o.name, "armature": mod.object.name if mod else None, "tris": sum(p.loop_total - 2 for p in me.polygons),
                   "verts": len(me.vertices), "shape_keys": len(me.shape_keys.key_blocks) if me.shape_keys else 0,
                   "vertex_groups": len(o.vertex_groups), "uv_maps": [u.name for u in me.uv_layers],
                   "materials": [s.material.name if s.material else None for s in o.material_slots],
                   "multires": any(m.type == "MULTIRES" for m in o.modifiers), "custom_normals": me.has_custom_normals,
                   "visible": o.visible_get(view_layer=bpy.context.scene.view_layers[0])})  # in the main layer
meshes.sort(key=lambda d: -d["tris"])
report = {"meshes": meshes}
if meshes:
    # the body: visible, then rigged, then many keys and many tris
    body = max(meshes, key=lambda d: (d["visible"], d["armature"] is not None, max(d["shape_keys"], 1) * d["tris"]))
    o = bpy.data.objects[body["name"]]
    report["body_guess"] = body["name"]
    report["colour_layers"] = colour_layers(o.data)
    report["shape_key_prefixes"] = key_prefixes(o.data)
    report["suspicious_shape_keys"] = key_health(o.data)
    report["missing_textures"] = missing_textures(o)
    if body["armature"]:
        arm = bpy.data.objects[body["armature"]]
        bones = guess_bones(arm)
        report["garment_guesses"] = [d["name"] for d in meshes if d["name"] != body["name"]
                                     and d["armature"] == arm.name and d["visible"]]  # ask which ones cover the body
        report["bones_guess"] = bones
        report["front_guess"] = guess_front(arm, (bones.get("foot_l") or [None])[0])
        names = {b.name for b in arm.data.bones}
        nonbone = [g.name for g in o.vertex_groups if g.name not in names]
        report["non_bone_groups"] = {"count": len(nonbone), "sample": nonbone[:25]}
    else:
        report["no_rig"] = ("no armature: pick the region by material or the whole mesh (REGION_MODE), frame close-ups "
                            "with 'point' views, and only the rest pose can be checked")
    report["note"] = ("check every guess with the user; a colour channel that is mostly binary and covers a small "
                      "share is a likely feature mask (KEEP_MASKS)")
