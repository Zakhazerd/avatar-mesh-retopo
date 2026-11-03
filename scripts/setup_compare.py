"""Sets up the live Blender to compare an object with its retopo versions, and takes it down again. Object names
default to avatar_config.py's (or the config named by CONFIG in the exec namespace).

    ns = {"__name__": "setup_compare"}; exec(open(path, encoding="utf-8").read(), ns)
    ns["setup"]("ND", [("Body", "Original"), ("Body Retopo Neck Down L1", "L1"), ...])  # source first
    ns["add_rig"]("ND", "L1")   # a posable armature copy for that lineup copy
    ns["show"]("ND")            # when finished: the user's view on the side-by-side lineup
    ns["remove"]("ND")          # takes it all down

- "Compare <name> <label> (<tris>)" view layers, one per entry: just that version and the config's garments, in
  place and skinned, so posing the real rig compares deformation (other meshes in the file are hidden there). Their states are copied from the main view layer, and
  they're not used for rendering, so F12 is unchanged.
- "Compare <name> Lineup" (shows only the lineup): static copies (no modifiers, shape keys kept) side by side along the avatar's right,
  wireframe on, labelled with their triangle counts. The source's copy is a full copy; versions' copies share the
  version's mesh (no memory; shape-key values are then shared with it). add_rig() gives one a posable armature.
- The main view layer shows only the source; the versions and the lineup are hidden there and in every other
  view layer (new collections and objects would otherwise show up in older comparisons).
Only reads the source. Never saves the .blend.
"""
import math
import os

import bpy
from mathutils import Matrix, Vector

HERE = r"<set me: the folder holding these scripts>"
CFG = {}
exec(open(globals().get("CONFIG") or os.path.join(HERE, "avatar_config.py"), encoding="utf-8").read(), CFG)


def main_layer(sc):
    return sc.view_layers[CFG.get("MAIN_VIEW_LAYER") or sc.view_layers[0].name]


def names(name, source=None):
    source = source or CFG["BODY"]
    return {"source": source, "prefix": f"Compare {name} ", "lineup": f"{source} {name} Lineup"}


def tris(o):
    return sum(p.loop_total - 2 for p in o.data.polygons)


def layer_collections(lc):
    yield lc
    for c in lc.children:
        yield from layer_collections(c)


def find_lc(vl, name):
    return next(lc for lc in layer_collections(vl.layer_collection) if lc.name == name)


def remove(name, source=None):
    n = names(name, source)
    sc = bpy.context.scene
    for win in bpy.context.window_manager.windows:
        if win.view_layer.name.startswith(n["prefix"]):
            win.view_layer = main_layer(sc)
    for vl in [v for v in sc.view_layers if v.name.startswith(n["prefix"])]:
        sc.view_layers.remove(vl)
    col = bpy.data.collections.get(n["lineup"])
    if col:
        src_data = bpy.data.objects[n["source"]].data
        for o in list(col.objects):
            data = o.data
            bpy.data.objects.remove(o)
            if data.users == 0 and data != src_data:  # meshes, label curves, posable armature copies
                kinds = {bpy.types.Mesh: bpy.data.meshes, bpy.types.Armature: bpy.data.armatures}
                kinds.get(type(data), bpy.data.curves).remove(data)
        bpy.data.collections.remove(col)


def _axes():
    front = Vector(CFG["FRONT"]).normalized()
    up = Vector((0, 0, 1))
    return up.cross(front), up  # the avatar's left, up


def build_lineup(sc, n, entries, link_variants=True):
    col = bpy.data.collections.new(n["lineup"])
    sc.collection.children.link(col)
    src = bpy.data.objects[n["source"]]
    src_world = src.matrix_world.copy()
    pts = [src_world @ v.co for v in src.data.vertices]
    zs = [p.z for p in pts]
    left, up = _axes()
    width = max(p.dot(left) for p in pts) - min(p.dot(left) for p in pts)
    step = 1.15 * width  # side by side along the avatar's right, an arm span apart
    front = Vector(CFG["FRONT"]).normalized()
    yaw = math.atan2(front.y, front.x) - math.atan2(-1.0, 0.0)
    label_rot = (Matrix.Rotation(yaw, 4, "Z") @ Matrix.Rotation(math.pi / 2, 4, "X")).to_euler()  # readable from the front
    for i, (name, label) in enumerate(entries):
        o = bpy.data.objects[name]
        c = o.copy()
        if name == n["source"] or not link_variants:
            c.data = o.data.copy()  # never share the source's mesh
            c.data.name = f"{n['lineup']} {label}"
        assert c.data != src.data
        c.name = f"{n['lineup']} {label}"
        for m in list(c.modifiers):
            c.modifiers.remove(m)
        c.parent = None
        offset = -left * step * (i + 1)
        c.matrix_world = Matrix.Translation(offset) @ src_world
        c.show_wire = True
        col.objects.link(c)
        cu = bpy.data.curves.new(f"{n['lineup']} label {label}", "FONT")
        cu.body = f"{label}\n{tris(o):,} tris"
        cu.size = 0.035 * (max(zs) - min(zs))
        cu.align_x = "CENTER"
        t = bpy.data.objects.new(f"{n['lineup']} label {label}", cu)
        t.location = src_world.translation + offset + up * (max(zs) - src_world.translation.z + 0.1 * (max(zs) - min(zs)))
        t.rotation_euler = label_rot
        col.objects.link(t)
    return col


def setup(name, entries, source=None, variants=None, link_variants=True):
    """entries: [(object, label)], the source first. variants: the versions' collection (default "<source> Retopo")."""
    n = names(name, source)
    variants = variants or f"{n['source']} Retopo"
    remove(name, source)
    sc = bpy.context.scene
    main = main_layer(sc)
    build_lineup(sc, n, entries, link_variants)

    # other view layers (main, other comparisons): this lineup excluded, these versions hidden
    for vl in sc.view_layers:
        find_lc(vl, n["lineup"]).exclude = True
        if vl.name != main.name:
            for obj, _ in entries:
                if obj != n["source"] and obj in vl.objects:
                    bpy.data.objects[obj].hide_set(True, view_layer=vl)
    # main view layer: back to the source only
    bpy.data.objects[n["source"]].hide_set(False, view_layer=main)
    find_lc(main, variants).hide_viewport = True

    lc_state = {lc.name: (lc.exclude, lc.hide_viewport) for lc in layer_collections(main.layer_collection)}
    obj_hidden = {o.name: o.hide_get(view_layer=main) for o in sc.objects}
    variant_objs = [o.name for o in bpy.data.collections[variants].objects]
    lineup_objs = {o.name for o in bpy.data.collections[n["lineup"]].objects}
    made = []
    for obj, label in entries + [(None, "Lineup")]:
        title = n["prefix"] + (f"{label} ({tris(bpy.data.objects[obj]):,})" if obj else label)
        vl = sc.view_layers.new(title)
        vl.use = False  # not rendered
        for lc in layer_collections(vl.layer_collection):
            if lc.name in lc_state:
                lc.exclude, lc.hide_viewport = lc_state[lc.name]
        for o in sc.objects:
            if o.name in obj_hidden and o.name in vl.objects:
                o.hide_set(obj_hidden[o.name], view_layer=vl)
        if obj is None:  # the lineup layer shows only the lineup (the file may hold other avatars too)
            find_lc(vl, n["lineup"]).exclude = False
            for o in vl.objects:
                o.hide_set(o.name not in lineup_objs, view_layer=vl)
        else:  # just this version and the config's garments (other avatars in the file stay out of the way)
            find_lc(vl, variants).hide_viewport = False
            keep = {obj} | set(CFG["GARMENTS"])
            for o in vl.objects:
                if o.type == "MESH" or o.name in variant_objs:
                    o.hide_set(o.name not in keep, view_layer=vl)
        made.append(vl.name)
    return made


def show(name, source=None):
    """Leaves the user looking at the comparison: every window on the lineup view layer, each 3D view turned to the
    avatar's front and framed on all the lineup copies. Selection and the viewport's perspective/ortho are kept.
    Outside Object Mode (posing a rig, editing a mesh) it only switches the view layer."""
    n = names(name, source)
    if bpy.context.mode != "OBJECT":
        for win in bpy.context.window_manager.windows:
            win.view_layer = bpy.context.scene.view_layers[n["prefix"] + "Lineup"]
        return n["prefix"] + "Lineup"
    vl = bpy.context.scene.view_layers[n["prefix"] + "Lineup"]
    copies = [o for o in bpy.data.collections[n["lineup"]].objects if o.type in ("MESH", "FONT")]
    front = Vector(CFG["FRONT"]).normalized()
    yaw = math.atan2(front.y, front.x) - math.atan2(-1.0, 0.0)
    facing = (Matrix.Rotation(yaw, 4, "Z") @ Matrix.Rotation(math.pi / 2, 4, "X")).to_quaternion()
    for win in bpy.context.window_manager.windows:
        win.view_layer = vl
        selected = [o for o in vl.objects if o.select_get(view_layer=vl)]
        active = vl.objects.active
        for o in vl.objects:
            o.select_set(o in copies, view_layer=vl)
        for area in win.screen.areas:
            if area.type != "VIEW_3D":
                continue
            area.spaces.active.region_3d.view_rotation = facing
            region = next(r for r in area.regions if r.type == "WINDOW")
            with bpy.context.temp_override(window=win, area=area, region=region, view_layer=vl):
                bpy.ops.view3d.view_selected()
        for o in vl.objects:
            o.select_set(o in selected, view_layer=vl)
        vl.objects.active = active
    return vl.name


def add_rig(name, label, source=None):
    """Gives a lineup copy its own posable copy of the source's armature, at the same offset: posing it moves only
    that copy. Self-targeting bone constraints (twist bones) follow the copy. remove() deletes it with the lineup."""
    n = names(name, source)
    src = bpy.data.objects[n["source"]]
    copy = bpy.data.objects[f"{n['lineup']} {label}"]
    mod = next(m for m in src.modifiers if m.type == "ARMATURE")
    arm = mod.object
    rig = arm.copy()
    rig.data = arm.data.copy()
    rig.name = rig.data.name = f"{n['lineup']} Armature {label}"
    rig.animation_data_clear()
    bpy.data.collections[n["lineup"]].objects.link(rig)
    rig.parent = None
    rig.matrix_world = copy.matrix_world @ src.matrix_world.inverted() @ arm.matrix_world
    for pb in rig.pose.bones:
        for c in pb.constraints:
            if getattr(c, "target", None) == arm:
                c.target = rig
    m = copy.modifiers.new("Armature", "ARMATURE")
    m.object = rig
    for k in ("use_vertex_groups", "use_bone_envelopes", "use_deform_preserve_volume", "use_multi_modifier"):
        setattr(m, k, getattr(mod, k))
    world = copy.matrix_world.copy()
    copy.parent = rig
    copy.matrix_parent_inverse = rig.matrix_world.inverted()
    copy.matrix_world = world
    return rig.name
