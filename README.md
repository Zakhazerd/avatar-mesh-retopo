# avatar-mesh-retopo

A Claude skill for lowering the triangle count of VRChat/avatar meshes in Blender without breaking shape keys, weights, UVs, vertex colours or normals.

Claude works through a Blender MCP connection:
- it asks which versions to build, up to six (loop levels LOD1/LOD2/LOD3, each with or without shape-key protection), and whether to build them in parallel with agents;
- for garments it can also rebuild every piece as an even quad grid (experimental on bodies, where the loop levels do better);
- it checks for over-dense areas and asks whether to do more there;
- it proves the loop levels exact against the source, and checks the quad rebuild's interpolated data;
- it leaves you on a side-by-side comparison in Blender.

`SKILL.md` is the workflow. `scripts/` holds the tools it uses.

## Install
1. **Copy the skill:** put this folder where Claude Code finds skills, either `~/.claude/skills/avatar-mesh-retopo/` (all projects) or `<project>/.claude/skills/avatar-mesh-retopo/` (one project).
2. **Set the scripts' folder:** replace `HERE = r"<set me: ...>"` at the top of `scripts/build_region.py`, `density_report.py`, `build_variants.py`, `build_quad_region.py`, `verify_quad.py` and `setup_compare.py` with the absolute path of `scripts/`.
3. **Fill in the config:** with the avatar open in Blender, ask Claude to run `scripts/inspect_avatar.py`, then fill in `scripts/avatar_config.py` together. Use a copy per avatar.

## Needs
- **Blender with an MCP add-on** that runs Python in the live session (tested on Blender 5.2 with the official Blender Lab MCP), and `blender.exe` callable from the shell for background renders.
- **Claude Opus 5.5 or more capable.** The skill tells weaker models to warn you first.
- **Python with Pillow,** for the comparison sheets.

## Not in here
No avatar files, meshes, textures or renders. Avatar files never leave your machine. The .gitignore keeps them out of this repository.
