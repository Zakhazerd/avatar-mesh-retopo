"""Rebuilds the garment retopo variants in the live Blender (exec it through the MCP or the Text Editor). The
garment is avatar_config.py's GARMENT.

    exec(open(path, encoding="utf-8").read(), {"__name__": "build_variants", "TAGS": "ABCD"})

Variants (all in the "<garment> Retopo" collection, full duplicates of the garment, which is only read):
  A  every-other-loop dissolve (exact original vertices and corners)
  B  collapse decimate + fold-safe redistribution, data re-sampled from the original
  C  pattern-space remesh at 16 mm, with faces under new layer intersections kept original
  D  the same at 20 mm
Buttons are rebuilt with 8 segments in every variant. Never saves the .blend.
"""
import os

import bpy

HERE = r"<set me: the folder holding these scripts>"
CFG = {}
exec(open(os.path.join(HERE, "avatar_config.py"), encoding="utf-8").read(), CFG)
SOURCE = CFG["GARMENT"]
SETTINGS = {"B": {"ratio": 0.43}, "C": {"h": 0.016}, "D": {"h": 0.020}}
TAGS = globals().get("TAGS", "ABCD")


def run(path, g):
    exec(open(os.path.join(HERE, path), encoding="utf-8").read(), g)
    return g


ns = {"__name__": "retopo_lib"}
run("retopo_lib.py", ns)
ns["COLLECTION"] = f"{SOURCE} Retopo"
src = ns["Source"](bpy.data.objects[SOURCE])
report = {}
try:
    src.flatten(ns["get_collection"]())
    src.build_locators()
    for tag in TAGS:
        if tag == "A":
            g = run("variant_a_loops.py", {"ns": ns, "src": src})
            spec = g["spec_a"]
            spec.extend(ns["button_spec"](src))
            o = ns["write_variant"](src, tag, spec)
        elif tag == "B":
            g = run("variant_b_decimate.py", {"ns": ns, "src": src, **SETTINGS["B"]})
            spec = g["spec_b"]
            spec.extend(ns["button_spec"](src))
            o = ns["write_variant"](src, tag, spec)
        else:
            extra = set()
            for _ in range(6):  # freeze original faces under new intersections until there are none
                g = run("variant_c_pattern.py", {"ns": ns, "src": src, "extra_frozen": extra, **SETTINGS[tag]})
                spec = g["spec_c"]
                spec.extend(ns["button_spec"](src))
                o = ns["write_variant"](src, tag, spec)
                faces, new = ns["new_intersection_faces"](src, o, spec)
                if not new:
                    break
                for f in list(faces):
                    for v in src.bm.faces[f].verts:
                        extra |= {x.index for x in v.link_faces}
        report[o.name] = ns["tri_count"](o)
finally:
    src.free()
bpy.context.view_layer.layer_collection.children[ns["COLLECTION"]].hide_viewport = True
print("RETOPO_BUILT", report)
