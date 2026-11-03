"""Everything avatar-specific, in one place: every script here reads this file. Fill it in for the avatar in front
of you before running anything; inspect_avatar.py (run in the live Blender) lists the candidates: meshes, the
armature and its bones, colour layers, non-bone vertex groups, shape-key name prefixes, suspicious shape keys and
missing textures. Confirm the values with the user. For a second avatar, copy this file (avatar_config_<name>.py)
and pass it as CONFIG to the live scripts and as "config" in the background jobs.
"""

BODY = "<set me: the skinned body mesh>"  # the source for body regions and the reference for metrics
GARMENT = None  # a garment for the garment methods (build_variants.py), e.g. "Shirt"
GARMENTS = []  # everything worn over the body, e.g. ["Stockings", "Bra"]: the clothing guard, the poke-through
# metric, and what's shown over the body in textured renders

# The body region to reduce (build_region.py), by REGION_MODE:
#   "below"     at most half their bone weight on REGION_ROOT_BONE and the bones under it (the neck bone gives
#               "neck down"); only vertex groups that are bones count
#   "under"     more than half on them (an upper-leg bone gives one leg)
#   "materials" the faces using REGION_MATERIALS (for a mesh without a rig)
#   "all"       the whole mesh (a headless body, a garment)
REGION_NAME = "Neck Down"  # part of the duplicates' names: "<BODY> Retopo <REGION_NAME> <level>"
REGION_MODE = "below"
REGION_ROOT_BONE = "<set me: e.g. the neck bone>"
REGION_EXTRA_BONES = []  # more bones counted with the root's (helper bones outside its hierarchy)
REGION_MATERIALS = []

# Small features left at full detail (a millimetre guard can't protect them; see SKILL.md 4.5):
# ("color", layer, channel 0-3, threshold), ("group", vertex group, threshold) or ("material", name).
# Ask the user what marks them.
KEEP_MASKS = []  # e.g. [("color", "Col", 2, 0.5)] for a blue-channel nipple mask

HIDE_KEY_PREFIXES = ()  # shape keys that collapse the body under clothes, which the key guard skips, e.g. ("Shrink_",)
COLOR_BORDER_LAYERS = None  # colour layers whose jumps protect edges (nail masks); None = every colour layer

# The rig. FRONT is the way the avatar faces with Z up (Blender's front view sees a -Y-facing avatar); poses and
# camera views are turned to match. The poses assume a T- or A-pose with the hands' palms facing down. Without an
# armature, leave BONES empty: only the rest pose is checked and close-ups are "point" views (render_views.py).
FRONT = (0, -1, 0)
BONES = {  # humanoid role -> this rig's bone name (poses and camera framing); check every one
    "hips": "Hips", "spine": "Spine", "chest": "Chest", "neck": "Neck", "head": "Head",
    "upper_arm_l": "UpperArm_L", "lower_arm_l": "LowerArm_L", "hand_l": "Hand_L",
    "upper_arm_r": "UpperArm_R", "lower_arm_r": "LowerArm_R",
    "upper_leg_l": "UpperLeg_L", "lower_leg_l": "LowerLeg_L", "foot_l": "Foot_L",
    "fingers_l": [],  # chains base to tip, thumb left out, e.g. [["IndexProximal_L", "IndexIntermediate_L", ...], ...]
}

# Renders. CHECK_TEXTURES feed the "tex" / "normal" modes (a garment's colour and normal map); None skips them.
CHECK_TEXTURES = {"color": None, "normal": None}
SUN = None  # a sun light to keep on in Eevee renders
MAIN_VIEW_LAYER = None  # the view layer setup_compare.py treats as the user's own; None = the scene's first
