"""Renders comparison views of an object and its variants in a background Blender, from a scratch copy of the
.blend. Object names, bones, textures and the facing direction come from avatar_config.py (next to this file).

    blender.exe -b <scratch copy.blend> --python render_views.py -- <job.json>

job.json: {"out": folder, "objects": [names], "views": [VIEWS keys], "modes": [wire|features|solid|density|tex|normal|uvgrid|own],
           "pose": a poses.POSES name, "res": 1600, "context": true (tex/normal/own: also show the body and the
           garments that aren't being rendered), "context_objects": [names] (shown instead of those),
           "frame_from": object whose (posed) vertices frame the views (default: the config's BODY),
           "keys": {shape key: value}, "tag": extra file name part, "config": another avatar's config file,
           "extra_views": {name: [rotation, direction, ["point", [x, y, z], size in m]]} (views for a mesh without
           a rig, centred on a world point)}
Without an armature only the rest pose and views that don't need bones ("point" ones, front/back/side) work.
Writes <out>/<mode>_<view>_<pose+tag>_<object>.png. Never saves the .blend.
"""
import json
import math
import os
import sys

import bmesh
import bpy
import numpy as np
from mathutils import Euler, Matrix, Vector

job = json.loads(open(sys.argv[sys.argv.index("--") + 1], encoding="utf-8").read())
HERE = os.path.dirname(os.path.abspath(__file__))
CFG = {}
exec(open(job.get("config") or os.path.join(HERE, "avatar_config.py"), encoding="utf-8").read(), CFG)
scene = bpy.context.scene
vl = bpy.context.view_layer
OBJECTS = job["objects"]
FRAME = bpy.data.objects[job.get("frame_from", CFG["BODY"])]
CONTEXT = job.get("context_objects") or [n for n in [CFG["BODY"]] + CFG["GARMENTS"]
                                         if n not in OBJECTS and n != FRAME.name]
os.makedirs(job["out"], exist_ok=True)


def _show_collections(lc):  # the live file keeps the retopo collections hidden; show them so they get evaluated
    lc.hide_viewport = False
    for c in lc.children:
        _show_collections(c)


_show_collections(bpy.context.view_layer.layer_collection)
for o in scene.objects:  # render what the FBX gets: Multires at level 0 (the export drops it)
    for m in o.modifiers:
        if m.type == "MULTIRES":
            m.levels = m.render_levels = 0
# ---------- pose and shape keys ----------
_rigged = FRAME if any(m.type == "ARMATURE" for m in FRAME.modifiers) else bpy.data.objects[CFG["BODY"]]
arm = next((m.object for m in _rigged.modifiers if m.type == "ARMATURE" and m.object), None)
POSE_NS = {"CFG": CFG}
exec(open(os.path.join(HERE, "poses.py"), encoding="utf-8").read(), POSE_NS)
if arm is not None:
    POSE_NS["set_pose"](arm, vl, job.get("pose", "rest"))
elif job.get("pose", "rest") != "rest":
    raise ValueError("no armature: only the rest pose can be rendered")
for name in OBJECTS:  # optional shape key values, the same on every compared object
    ks = bpy.data.objects[name].data.shape_keys
    for k, val in job.get("keys", {}).items():
        if ks and k in ks.key_blocks:
            ks.key_blocks[k].value = val
vl.update()


# ---------- scene ----------
for o in scene.objects:
    o.hide_render = True
cam_data = bpy.data.cameras.new("RetopoCam")
cam_data.type = "ORTHO"
cam = bpy.data.objects.new("RetopoCam", cam_data)
scene.collection.objects.link(cam)
scene.camera = cam
scene.render.film_transparent = False
scene.render.image_settings.file_format = "PNG"
scene.render.image_settings.color_mode = "RGB"
scene.display_settings.display_device = "sRGB"

# Views for an avatar facing -Y (turned to the config's FRONT): camera rotation (degrees), direction from the target
# to the camera, and the framing:
#   None                       all of FRAME's vertices
#   ("verts", [roles], pad)    FRAME's vertices with more than half their bone weight on these roles' bones and the
#                              bones under them (posed), padded; seen from above, everything higher is clipped away
#   ("at", [roles], size, (du, dv))  centred on the mean of these bones' (posed) heads, offset by (du, dv) along the
#                              image's right/up; size and offsets are fractions of FRAME's height
FRONT_V, SIDE_V, BACK_V = ((90, 0, 0), (0, -1, 0)), ((90, 0, 90), (1, 0, 0)), ((90, 0, 180), (0, 1, 0))
TOP_V, BOTTOM_V = ((0, 0, 0), (0, 0, 1)), ((180, 0, 0), (0, 0, -1))
VIEWS = {
    "front": FRONT_V + (None,), "back": BACK_V + (None,), "side": SIDE_V + (None,),
    # garment views
    "chest": FRONT_V + (("at", ["chest"], 0.36, (0, 0.01)),),
    "chest_side": SIDE_V + (("at", ["chest"], 0.36, (0, 0.01)),),
    "back_close": BACK_V + (("at", ["chest"], 0.36, (0, 0)),),
    "sleeve": FRONT_V + (("verts", ["upper_arm_l"], 1.1),),
    "chest_close": FRONT_V + (("at", ["chest"], 0.18, (0.06, 0.005)),),
    "elbow": FRONT_V + (("at", ["lower_arm_l"], 0.18, (0, 0)),),
    # body views (the left side where one side is shown)
    "torso_front": FRONT_V + (("at", ["hips", "neck"], 0.36, (0, -0.06)),),
    "torso_back": BACK_V + (("at", ["hips", "neck"], 0.36, (0, -0.08)),),
    "torso_side": SIDE_V + (("at", ["hips", "neck"], 0.36, (-0.025, -0.06)),),
    "breast_side": SIDE_V + (("at", ["chest"], 0.15, (-0.04, 0.03)),),
    "hips_back": BACK_V + (("at", ["hips"], 0.25, (0, -0.08)),),
    "hips_side": SIDE_V + (("at", ["hips"], 0.25, (0.02, -0.08)),),
    "arm_front": FRONT_V + (("verts", ["upper_arm_l"], 1.1),),
    "arm_back": BACK_V + (("verts", ["upper_arm_l"], 1.1),),
    "hand_top": TOP_V + (("verts", ["hand_l"], 1.25),),
    "hand_bottom": BOTTOM_V + (("verts", ["hand_l"], 1.25),),
    "hand_front": FRONT_V + (("verts", ["hand_l"], 1.25),),
    "fist_front": FRONT_V + (("verts", ["hand_l"], 1.3),),
    "leg_front": FRONT_V + (("verts", ["upper_leg_l"], 1.1),),
    "leg_back": BACK_V + (("verts", ["upper_leg_l"], 1.1),),
    "leg_side": SIDE_V + (("verts", ["upper_leg_l"], 1.1),),
    "thigh_front": FRONT_V + (("at", ["upper_leg_l", "lower_leg_l"], 0.21, (0, 0)),),
    "knee_side": SIDE_V + (("at", ["lower_leg_l"], 0.21, (0, 0)),),
    "foot_side": SIDE_V + (("verts", ["foot_l"], 1.25),),
    "foot_front": FRONT_V + (("verts", ["foot_l"], 1.25),),
    "foot_top": TOP_V + (("verts", ["foot_l"], 1.25),),
    "foot_bottom": BOTTOM_V + (("verts", ["foot_l"], 1.25),),
    "legbend_side": SIDE_V + (("verts", ["upper_leg_l"], 1.15),),
    "knee_bend": SIDE_V + (("at", ["lower_leg_l"], 0.21, (0, 0)),),
}
for _name, (_rot, _d, _frame) in job.get("extra_views", {}).items():  # JSON lists -> the tuples above
    VIEWS[_name] = (tuple(_rot), tuple(_d), None if _frame is None else (_frame[0], tuple(_frame[1]), _frame[2]))
_f = Vector(CFG["FRONT"])
YAW =Matrix.Rotation(math.atan2(_f.y, _f.x) - math.atan2(-1.0, 0.0), 3, "Z")  # -Y-facing views -> the avatar's front


def frame_vertices():
    """FRAME's posed vertices in world space, and its rest height."""
    dg = bpy.context.evaluated_depsgraph_get()
    ev = FRAME.evaluated_get(dg)
    me = ev.to_mesh()
    co = np.empty(len(me.vertices) * 3)
    me.vertices.foreach_get("co", co)
    ev.to_mesh_clear()
    mw = np.array(FRAME.matrix_world)
    rest = np.empty(len(FRAME.data.vertices) * 3)
    FRAME.data.vertices.foreach_get("co", rest)
    rest = rest.reshape(-1, 3) @ mw[:3, :3].T + mw[:3, 3]
    if len(co) != len(rest):  # a modifier changed the topology: frame on the rest shape
        return rest, float(np.ptp(rest[:, 2]))
    return co.reshape(-1, 3) @ mw[:3, :3].T + mw[:3, 3], float(np.ptp(rest[:, 2]))


EV_CO, HEIGHT = frame_vertices()
RLIB = {"__name__": "retopo_lib"}
exec(open(os.path.join(HERE, "retopo_lib.py"), encoding="utf-8").read(), RLIB)


def density_colours(o):
    """A point colour attribute with o's local density against FRAME's median (scratch copy only: never saved)."""
    _, frame_edge, _ = RLIB["density_metrics"](FRAME.data)
    _, edge, _ = RLIB["density_metrics"](o.data)
    ratio = np.log2((np.median(frame_edge) / np.maximum(edge, 1e-9)) ** 2)  # 0 at the median, +2 at 4x
    t = np.clip(ratio / 2, -1, 1)
    white = np.ones(3)
    rgb = np.where(t[:, None] >= 0, white * (1 - t[:, None]) + np.array([0.9, 0.08, 0.05]) * t[:, None],
                   white * (1 + t[:, None]) + np.array([0.15, 0.35, 0.95]) * -t[:, None])
    a = o.data.color_attributes.new("retopo_density", "FLOAT_COLOR", "POINT")
    a.data.foreach_set("color", np.hstack([rgb, np.ones((len(rgb), 1))]).ravel())
    o.data.color_attributes.active_color = a
    o.data.color_attributes.render_color_index = list(o.data.color_attributes).index(a)


def role_mask(roles):
    """FRAME's vertices with more than half their bone weight on these roles' bones and the bones under them."""
    if arm is None:
        raise ValueError(f"views framed by {roles} need an armature: use a 'point' view")
    names = set()
    for role in roles:
        b = arm.data.bones[CFG["BONES"][role]]
        names |= {b.name} | {c.name for c in b.children_recursive}
    gi = {g.index for g in FRAME.vertex_groups if g.name in names}
    bone = {g.index for g in FRAME.vertex_groups if g.name in arm.data.bones}
    w, t = np.zeros(len(EV_CO)), np.zeros(len(EV_CO))
    for v in FRAME.data.vertices:
        for g in v.groups:
            if g.group in bone:
                t[v.index] += g.weight
                if g.group in gi:
                    w[v.index] += g.weight
    return w > 0.5 * np.maximum(t, 1e-9)


def place_camera(view):
    rot, d, frame = VIEWS[view]
    m = YAW @ Euler([math.radians(a) for a in rot]).to_matrix()
    cam.rotation_euler = m.to_euler()
    d = YAW @ Vector(d)
    right, up = np.array(m.col[0]), np.array(m.col[1])
    top = None
    if frame is None or frame[0] == "verts":
        pts = EV_CO if frame is None else EV_CO[role_mask(frame[1])]
        pad = 1.06 if frame is None else frame[2]
        u, v = pts @ right, pts @ up
        depth = float((pts @ np.array(d)).mean())
        c = Vector(right * (u.min() + u.max()) / 2 + up * (v.min() + v.max()) / 2) + d * depth
        w, h = (u.max() - u.min()) * pad, (v.max() - v.min()) * pad
        if frame is not None:
            top = float(pts[:, 2].max())
            w, h = max(w, 0.35 * h), max(h, 0.35 * w)  # a limb is long and thin: keep some room around it
    elif frame[0] == "point":  # a world point and a size in metres
        c = Vector(frame[1])
        w = h = frame[2]
    else:
        if arm is None:
            raise ValueError(f"view {view!r} frames by bones, and there's no armature: use a 'point' view")
        _, roles, size, (du, dv) = frame
        heads = [arm.matrix_world @ arm.pose.bones[CFG["BONES"][r]].head for r in roles]
        c = sum(heads, Vector()) / len(heads) + (Vector(right) * du + Vector(up) * dv) * HEIGHT
        w = h = size * HEIGHT
    cam_data.ortho_scale = max(w, h)
    scene.render.resolution_x = res if w >= h else max(16, int(res * w / h))
    scene.render.resolution_y = res if h > w else max(16, int(res * h / w))
    scene.render.resolution_percentage = 100
    cam.location = c + d * 5
    cam_data.clip_end = 20
    # seen from above (a hand, a foot), everything higher than the framed part is clipped away
    cam_data.clip_start = cam.location.z - (top + 0.02) if (d.z > 0.5 and top is not None) else 0.1
    return cam_data.ortho_scale / max(scene.render.resolution_x, scene.render.resolution_y)


res = job.get("res", 1600)


# ---------- materials ----------
def image(path):
    for im in bpy.data.images:
        if im.filepath and os.path.normcase(bpy.path.abspath(im.filepath)) == os.path.normcase(path):
            return im
    return bpy.data.images.load(path)


def check_material(kind, uv_name):
    """tex: the config's CHECK_TEXTURES colour + normal map. normal: white + the normal map only. uvgrid: a UV grid.
    All on the UV map uv_name."""
    tex = CFG.get("CHECK_TEXTURES") or {}
    if kind in ("tex", "normal") and not (tex.get("normal") and (kind == "normal" or tex.get("color"))):
        raise ValueError(f"mode {kind!r} needs CHECK_TEXTURES in avatar_config.py")
    m = bpy.data.materials.new(f"RetopoCheck_{kind}")
    m.use_nodes = True
    nt = m.node_tree
    bsdf = nt.nodes["Principled BSDF"]
    uvn = nt.nodes.new("ShaderNodeUVMap")
    uvn.uv_map = uv_name
    if kind in ("tex", "normal"):
        n_img = nt.nodes.new("ShaderNodeTexImage")
        n_img.image = image(tex["normal"])
        n_img.image.colorspace_settings.name = "Non-Color"
        nm = nt.nodes.new("ShaderNodeNormalMap")
        nm.uv_map = uv_name
        nt.links.new(uvn.outputs["UV"], n_img.inputs["Vector"])
        nt.links.new(n_img.outputs["Color"], nm.inputs["Color"])
        nt.links.new(nm.outputs["Normal"], bsdf.inputs["Normal"])
        bsdf.inputs["Roughness"].default_value = 0.8
    if kind == "tex":
        c_img = nt.nodes.new("ShaderNodeTexImage")
        c_img.image = image(tex["color"])
        nt.links.new(uvn.outputs["UV"], c_img.inputs["Vector"])
        nt.links.new(c_img.outputs["Color"], bsdf.inputs["Base Color"])
    elif kind == "normal":
        bsdf.inputs["Base Color"].default_value = (0.8, 0.8, 0.8, 1)
    elif kind == "uvgrid":
        g = bpy.data.images.new("RetopoUVGrid", 1024, 1024)
        g.generated_type = "COLOR_GRID"
        c_img = nt.nodes.new("ShaderNodeTexImage")
        c_img.image = g
        nt.links.new(uvn.outputs["UV"], c_img.inputs["Vector"])
        nt.links.new(c_img.outputs["Color"], bsdf.inputs["Base Color"])
        bsdf.inputs["Roughness"].default_value = 0.9
        nt.nodes.active = c_img
    return m


def with_material(o, mat):
    o.material_slots[0].link = "OBJECT"
    o.material_slots[0].material = mat


# ---------- feature lines (seams blue, creases red, T-junctions green, boundaries black) ----------
def feature_curves(o):
    me = o.data
    bm = bmesh.new()
    bm.from_mesh(me)
    uv = bm.loops.layers.uv.active or bm.loops.layers.uv[0]
    sharp = me.attributes.get("sharp_edge")
    sh = [False] * len(me.edges)
    if sharp:
        sharp.data.foreach_get("value", sh)

    def seam(e):
        f1, f2 = e.link_faces
        for v in e.verts:
            a = next(l for l in f1.loops if l.vert == v)[uv].uv
            b = next(l for l in f2.loops if l.vert == v)[uv].uv
            if (a - b).length > 1e-5:
                return True
        return False

    cats = {"boundary": [], "nonmanifold": [], "seam": [], "crease": []}
    for e in bm.edges:
        if e.is_boundary:
            cats["boundary"].append(e)
        elif len(e.link_faces) > 2:
            cats["nonmanifold"].append(e)
        elif seam(e):
            cats["seam"].append(e)
        elif sh[e.index]:
            cats["crease"].append(e)
    colours = {"boundary": (0.05, 0.05, 0.05, 1), "nonmanifold": (0.0, 0.8, 0.1, 1), "seam": (0.1, 0.3, 1.0, 1),
               "crease": (1.0, 0.1, 0.05, 1)}
    made = []
    dg = bpy.context.evaluated_depsgraph_get()
    ev = o.evaluated_get(dg).to_mesh()
    for k, es in cats.items():
        cu = bpy.data.curves.new(f"feat_{k}", "CURVE")
        cu.dimensions = "3D"
        cu.bevel_depth = 0.0012
        for e in es:
            sp = cu.splines.new("POLY")
            sp.points.add(1)
            for i, v in enumerate(e.verts):
                p = o.matrix_world @ ev.vertices[v.index].co
                sp.points[i].co = (p.x, p.y, p.z, 1)
        ob = bpy.data.objects.new(f"feat_{k}", cu)
        ob.color = colours[k]
        scene.collection.objects.link(ob)
        made.append(ob)
    o.evaluated_get(dg).to_mesh_clear()
    bm.free()
    return made


# ---------- render ----------
def render(path):
    scene.render.filepath = path
    bpy.ops.render.render(write_still=True)


def workbench(color_type):
    scene.render.engine = "BLENDER_WORKBENCH"
    scene.view_settings.view_transform = "Standard"
    sh = scene.display.shading
    sh.light = "STUDIO"
    sh.color_type = color_type
    sh.show_cavity = False
    sh.show_specular_highlight = True
    scene.display.render_aa = "16"


def eevee():
    for vt in ("AgX", "AgX - SDR", "Standard"):  # the names depend on the display device
        try:
            scene.view_settings.view_transform = vt
            break
        except TypeError:
            continue
    for name in ("BLENDER_EEVEE", "BLENDER_EEVEE_NEXT"):
        try:
            scene.render.engine = name
            break
        except TypeError:
            continue
    try:
        scene.eevee.taa_render_samples = 32
    except Exception:
        pass


pose = job.get("pose", "rest")
done = []
for mode in job["modes"]:
    for name in OBJECTS:
        o = bpy.data.objects[name]
        extras = []
        if mode == "wire":
            workbench("OBJECT")
            o.color = (0.82, 0.82, 0.82, 1)
            w = o.copy()
            scene.collection.objects.link(w)
            wm = w.modifiers.new("RetopoWire", "WIREFRAME")
            wm.use_replace = True
            wm.use_even_offset = False
            wm.offset = 0.0
            w.color = (0.05, 0.05, 0.08, 1)
            extras.append(w)
        elif mode == "features":
            workbench("OBJECT")
            o.color = (0.85, 0.85, 0.85, 1)
            extras += feature_curves(o)
        elif mode == "solid":  # plain shaded, for meshes without their textures
            workbench("OBJECT")
            o.color = (0.86, 0.8, 0.76, 1)
        elif mode == "density":  # heat map: blue half the frame object's median density, white at it, red 4x+
            workbench("VERTEX")
            density_colours(o)
        elif mode == "uvgrid":
            workbench("TEXTURE")
            with_material(o, check_material("uvgrid", o.data.uv_layers.active.name))
        elif mode == "own":  # the object's own materials
            eevee()
        else:  # tex / normal
            eevee()
            with_material(o, check_material(mode, o.data.uv_layers.active.name))
        shown = [o] + extras
        if mode in ("tex", "normal", "own") and job.get("context", True):
            shown += [bpy.data.objects[n] for n in CONTEXT if n in bpy.data.objects]
        for s in shown:
            s.hide_render = False
            s.hide_viewport = False
        if CFG.get("SUN") and scene.objects.get(CFG["SUN"]):
            scene.objects[CFG["SUN"]].hide_render = False
        for view in job["views"]:
            px = place_camera(view)
            if mode == "wire":
                wm.thickness = px * 0.9
            path = os.path.join(job["out"], f"{mode}_{view}_{pose}{job.get('tag', '')}_{name}.png")
            render(path)
            done.append(path)
        for s in shown:
            s.hide_render = True
        for e in extras:
            bpy.data.objects.remove(e)
        if mode not in ("wire", "features", "own"):
            o.material_slots[0].link = "DATA"
print("RETOPO_RENDERED", json.dumps(done))
