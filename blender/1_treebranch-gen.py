"""
1_generate_branch_foliage.py
=============================================================
STEP 1 of 3 — Procedural Conifer Branch / Frond Generator
=============================================================

Run this INSIDE Blender (Scripting tab, or `blender -b -P this_file.py`).
Blender 2.9x / 3.x / 4.x API (bpy). Tested logic against the standard
bpy/bmesh API — no external dependencies.

WHAT THIS DOES
---------------
Generates flat, randomized "branch cards" — the same kind of asset as
your reference image 1 (a single frond with a fractal, branching
leaf silhouette). Instead of a texture doing all the work, the MESH
itself follows a recursively-branched skeleton (stem -> sub-stems ->
leaflets), so no two branches are alike and the outline reads as a
real conifer frond even before any material is applied.

Each generated branch is:
  - A ribbon-strip skeleton (stem + child stems) built with bmesh
  - Covered in small leaflet quads/tris along each stem
  - Given vertex colors (a green->yellow->brown gradient + per-branch
    tint) that script 2 will bake into the albedo texture
  - Given proper planar UVs so script 2 can bake textures onto it
  - Tagged with custom properties describing its own random params,
    so script 3 can pick appropriately-sized/aged branches when it
    assembles a full tree

All generated branches are collected into a "BranchLibrary" collection
and also appended into a re-usable .blend library file so you don't
have to regenerate branches every time you build a new tree.

USAGE
-----
Edit the CONFIG block below, then run the script. It will:
  1. Clear (or reuse) a "BranchLibrary" collection in the current file
  2. Generate NUM_VARIANTS branch objects
  3. Save them to OUTPUT_BLEND as a library .blend

Then open 3_assemble_tree_and_export.py and point it at OUTPUT_BLEND.
"""

import bpy
import bmesh
import random
import math
from mathutils import Vector, Matrix, Euler

# =====================================================================
# CONFIG — tweak these to change the "species" of branch you generate
# =====================================================================

CONFIG = {
    "seed": None,                # None = random every run, or set an int for repeatability
    "num_variants": 12,          # how many unique branch cards to generate this pass
    "output_blend": "//branch_library.blend",  # relative to current .blend; change as needed

    # --- Recursive branching shape ---
    "max_depth": 3,              # how many levels of sub-branching (2=simple, 4=very fractal)
    "depth_length_decay": (0.55, 0.75),   # each child stem is this fraction of parent length
    "depth_width_decay": (0.5, 0.7),      # each child stem is this fraction of parent width
    "branch_angle_deg": (25, 55),         # angle child stems split off from parent
    "branches_per_stem": (3, 5),          # how many children spawn per stem segment
    "stem_segments": (4, 7),              # subdivisions along a stem (for bend/droop)

    # --- Root stem "bowl"/U-shape (down near the trunk, arching back up
    # by the tip) — explicit angle-by-height control, not incremental
    # rotation, so the direction is guaranteed rather than inferred ---
    "initial_dip_deg": (15, 35),   # how far below horizontal the root stem
                                    # points right at its base (near the trunk)
    "tip_arch_deg": (30, 60),      # how far above horizontal it points by its tip
    # Sub-branches (children of the root) get a smaller, more relaxed
    # residual curl instead of the full bowl shape, since they already
    # inherit the root's curve from whatever point they branch off of:
    "child_curl_strength": (0.05, 0.15),
    "curl_direction": -1,                 # sign of the child-stem residual curl only
    "twist_jitter_deg": 12,               # random azimuthal jitter so it's not perfectly flat/planar

    # --- Overall branch size ---
    "root_length": (0.35, 0.65),   # meters, length of the main stem
    "root_width": (0.015, 0.03),   # meters, width of the main stem ribbon

    # --- Leaflets (the small flat "leaf" quads covering the stems) ---
    "leaflet_density": (0.9, 1.4),        # leaflets per unit stem length (relative)
    "leaflet_size": (0.025, 0.06),        # meters
    "leaflet_angle_deg": (35, 70),        # angle leaflets fan out from the stem
    "leaflet_pairs": True,                 # spawn leaflets in opposite pairs (like real conifer sprays)

    # --- Color (baked to vertex colors, later baked to texture by script 2) ---
    # Dark, saturated greens — deliberately avoiding any yellow tint so
    # the tree doesn't read as nitrogen-deficient/yellowing.
    "color_tip": (0.12, 0.32, 0.13),      # young growth (still a clear green, not yellow-green)
    "color_mid": (0.06, 0.22, 0.09),      # mature (deep green)
    "color_base": (0.16, 0.12, 0.08),     # woody base (brown)
}

COLLECTION_NAME = "BranchLibrary"


# =====================================================================
# Helpers
# =====================================================================

def rand_range(rng, pair):
    return rng.uniform(pair[0], pair[1])


def get_or_create_collection(name):
    if name in bpy.data.collections:
        col = bpy.data.collections[name]
    else:
        col = bpy.data.collections.new(name)
        bpy.context.scene.collection.children.link(col)
    return col


def clear_collection(col):
    for obj in list(col.objects):
        mesh = obj.data if obj.type == 'MESH' else None
        bpy.data.objects.remove(obj, do_unlink=True)
        if mesh and mesh.users == 0:
            bpy.data.meshes.remove(mesh)


def full_pipeline_reset():
    """Regenerating branches invalidates anything downstream (billboard
    cards baked from the old geometry, assembled trees built from those
    cards), so wipe the whole pipeline's collections here rather than
    requiring manual cleanup before every re-run. Collections are fully
    removed (not just emptied) so they don't linger as grayed-out empty
    entries in the outliner — only "BranchLibrary" gets recreated,
    immediately after, by main()."""
    for name in ("BranchLibrary", "BillboardCards", "GeneratedTrees"):
        col = bpy.data.collections.get(name)
        if not col:
            continue
        clear_collection(col)
        if col.name in bpy.context.scene.collection.children:
            bpy.context.scene.collection.children.unlink(col)
        bpy.data.collections.remove(col)

    for _ in range(2):  # a couple passes catches chained dependencies
        bpy.data.orphans_purge(do_local_ids=True, do_recursive=True)


def ensure_vertex_color_layer(bm, name="Col"):
    if name not in bm.loops.layers.color:
        return bm.loops.layers.color.new(name)
    return bm.loops.layers.color[name]


def lerp3(a, b, t):
    return (a[0] + (b[0] - a[0]) * t,
            a[1] + (b[1] - a[1]) * t,
            a[2] + (b[2] - a[2]) * t)


# =====================================================================
# Core recursive stem builder
# =====================================================================

class StemSegment:
    """A single generation of stem: a poly-line with width, that may
    spawn child stems and carries leaflets along its length."""

    def __init__(self, origin, direction, length, width, depth, rng, cfg):
        self.origin = origin
        self.direction = direction.normalized()
        self.length = length
        self.width = width
        self.depth = depth
        self.rng = rng
        self.cfg = cfg
        self.points = []   # world-space points along the stem centerline
        self.widths = []   # width at each point
        self._build_centerline()

    def _build_centerline(self):
        cfg = self.cfg
        rng = self.rng
        segs = rng.randint(*cfg["stem_segments"])

        pos = Vector(self.origin)
        base_dir = Vector(self.direction).normalized()
        is_root = (self.depth == 0)

        if is_root:
            # Explicit, verifiable down-then-up "bowl" profile: pitch
            # angle is computed directly from height fraction rather
            # than accumulated via incremental rotation, so the
            # direction (down at the base, up by the tip) is guaranteed
            # instead of depending on a cross-product's sign.
            dip = math.radians(rand_range(rng, cfg["initial_dip_deg"]))
            arch = math.radians(rand_range(rng, cfg["tip_arch_deg"]))
            forward_xy = Vector((base_dir.x, base_dir.y, 0))
            if forward_xy.length < 1e-5:
                forward_xy = Vector((1, 0, 0))
            forward_xy.normalize()
            # horizontal axis perpendicular to the growth direction —
            # rotating around it tilts the direction purely up/down
            side_axis = forward_xy.cross(Vector((0, 0, 1)))
            if side_axis.length < 1e-5:
                side_axis = Vector((0, 1, 0))
            side_axis.normalize()
            dir_vec = base_dir
        else:
            # Sub-branches: smaller, naturalistic residual curl (not a
            # forced bowl shape — they already inherit the root's curve
            # from wherever along the root they branch off).
            curl = rand_range(rng, cfg["child_curl_strength"])
            dir_vec = base_dir
            droop_axis = dir_vec.cross(Vector((0, 0, 1)))
            if droop_axis.length < 1e-5:
                droop_axis = Vector((1, 0, 0))
            droop_axis.normalize()

        for i in range(segs + 1):
            t = i / segs
            w = self.width * (1.0 - 0.85 * t)  # taper toward tip
            self.points.append(pos.copy())
            self.widths.append(max(w, 0.0015))

            if i == segs:
                break

            step_len = self.length / segs

            if is_root:
                # side_axis works out to roughly -Y for a +X-forward stem,
                # which flips the usual +Y rotation convention — verified
                # sign empirically: NEGATIVE theta = down, POSITIVE = up
                theta = -dip + (arch + dip) * ((i + 1) / segs)
                rot = Matrix.Rotation(theta, 4, side_axis)
                dir_vec = (rot @ forward_xy).normalized()
            else:
                rot = Matrix.Rotation(cfg["curl_direction"] * curl * step_len * 4.0, 4, droop_axis)
                dir_vec = (rot @ dir_vec).normalized()

            jitter = Matrix.Rotation(math.radians(rng.uniform(-cfg["twist_jitter_deg"],
                                                                cfg["twist_jitter_deg"])),
                                      4, Vector((0, 0, 1)))
            dir_vec = (jitter @ dir_vec).normalized()

            pos = pos + dir_vec * step_len

        self.end_pos = self.points[-1]
        self.end_dir = dir_vec


def generate_children(stem, rng, cfg, depth):
    """Spawn child StemSegments branching off `stem`."""
    children = []
    if depth >= cfg["max_depth"]:
        return children

    n_children = rng.randint(*cfg["branches_per_stem"])
    # spawn points spread along the back 70% of the stem (not right at base)
    for i in range(n_children):
        t = rng.uniform(0.35, 0.95)
        idx = min(int(t * (len(stem.points) - 1)), len(stem.points) - 2)
        origin = stem.points[idx]
        base_dir = (stem.points[idx + 1] - stem.points[idx]).normalized()

        angle = math.radians(rand_range(rng, cfg["branch_angle_deg"]))
        side = 1 if (i % 2 == 0) else -1
        up = Vector((0, 0, 1))
        side_axis = base_dir.cross(up)
        if side_axis.length < 1e-5:
            side_axis = Vector((1, 0, 0))
        side_axis.normalize()

        rot = Matrix.Rotation(angle * side, 4, side_axis)
        twist = Matrix.Rotation(math.radians(rng.uniform(-cfg["twist_jitter_deg"],
                                                           cfg["twist_jitter_deg"])), 4, base_dir)
        child_dir = (twist @ rot @ base_dir).normalized()

        length_decay = rand_range(rng, cfg["depth_length_decay"])
        width_decay = rand_range(rng, cfg["depth_width_decay"])
        child_length = stem.length * length_decay
        child_width = stem.width * width_decay

        child = StemSegment(origin, child_dir, child_length, child_width, depth + 1, rng, cfg)
        children.append(child)

    return children


def collect_all_stems(root_stem, rng, cfg):
    """Breadth-first collect the full recursive stem tree into a flat list."""
    all_stems = [root_stem]
    frontier = [root_stem]
    depth = 1
    while frontier:
        next_frontier = []
        for stem in frontier:
            kids = generate_children(stem, rng, cfg, stem.depth)
            for k in kids:
                all_stems.append(k)
                next_frontier.append(k)
        frontier = next_frontier
        depth += 1
        if depth > cfg["max_depth"] + 1:
            break
    return all_stems


# =====================================================================
# Mesh building (bmesh) — stems as ribbons, leaflets as small quads
# =====================================================================

def add_ribbon(bm, stem, col_layer, uv_layer, cfg, uv_bounds):
    """Add a flat ribbon strip mesh for one stem, colored by depth/age."""
    verts_left = []
    verts_right = []

    # side vector: perpendicular to stem direction, mostly in local XY
    for i, p in enumerate(stem.points):
        if i < len(stem.points) - 1:
            fwd = (stem.points[i + 1] - p).normalized()
        else:
            fwd = (p - stem.points[i - 1]).normalized()
        side = fwd.cross(Vector((0, 0, 1)))
        if side.length < 1e-5:
            side = Vector((1, 0, 0))
        side.normalize()
        w = stem.widths[i]
        verts_left.append(bm.verts.new(p + side * w * 0.5))
        verts_right.append(bm.verts.new(p - side * w * 0.5))

    bm.verts.ensure_lookup_table()

    color_t_by_depth = min(stem.depth / max(cfg["max_depth"], 1), 1.0)
    for i in range(len(stem.points) - 1):
        f = bm.faces.new((verts_left[i], verts_right[i], verts_right[i + 1], verts_left[i + 1]))
        f.smooth = True
        # color: base->mid->tip depending on depth AND position along stem
        tip_t = i / max(len(stem.points) - 2, 1)
        overall_t = min(color_t_by_depth + 0.4 * tip_t, 1.0)
        if overall_t < 0.5:
            c = lerp3(cfg["color_base"], cfg["color_mid"], overall_t * 2.0)
        else:
            c = lerp3(cfg["color_mid"], cfg["color_tip"], (overall_t - 0.5) * 2.0)
        for loop in f.loops:
            loop[col_layer] = (c[0], c[1], c[2], 1.0)
            # simple planar UV projection (XZ plane) normalized to uv_bounds
            co = loop.vert.co
            u = (co.x - uv_bounds[0]) / uv_bounds[2]
            v = (co.z - uv_bounds[1]) / uv_bounds[3]
            loop[uv_layer].uv = (u, v)


def add_leaflet(bm, origin, direction, up_hint, size, col_layer, uv_layer, color, uv_bounds):
    """A tiny flat quad representing a leaflet cluster fanning off a stem point."""
    fwd = direction.normalized()
    side = fwd.cross(up_hint)
    if side.length < 1e-5:
        side = Vector((1, 0, 0))
    side.normalize()

    tip = origin + fwd * size
    v0 = bm.verts.new(origin - side * size * 0.18)
    v1 = bm.verts.new(origin + side * size * 0.18)
    v2 = bm.verts.new(tip + side * size * 0.06)
    v3 = bm.verts.new(tip - side * size * 0.06)
    bm.verts.ensure_lookup_table()
    f = bm.faces.new((v0, v1, v2, v3))
    f.smooth = True
    for loop in f.loops:
        loop[col_layer] = (color[0], color[1], color[2], 1.0)
        co = loop.vert.co
        u = (co.x - uv_bounds[0]) / uv_bounds[2]
        v = (co.z - uv_bounds[1]) / uv_bounds[3]
        loop[uv_layer].uv = (u, v)


def build_branch_mesh(cfg, rng):
    root_length = rand_range(rng, cfg["root_length"])
    root_width = rand_range(rng, cfg["root_width"])
    root = StemSegment(Vector((0, 0, 0)), Vector((1, 0, 0.05)), root_length, root_width, 0, rng, cfg)
    all_stems = collect_all_stems(root, rng, cfg)

    # compute bounds for UV normalization (planar, XZ)
    xs = [p.x for s in all_stems for p in s.points]
    zs = [p.z for s in all_stems for p in s.points]
    pad = 0.05
    min_x, max_x = min(xs) - pad, max(xs) + pad
    min_z, max_z = min(zs) - pad, max(zs) + pad
    uv_bounds = (min_x, min_z, max(max_x - min_x, 1e-4), max(max_z - min_z, 1e-4))

    bm = bmesh.new()
    col_layer = ensure_vertex_color_layer(bm, "Col")
    uv_layer = bm.loops.layers.uv.new("UVMap")

    for stem in all_stems:
        add_ribbon(bm, stem, col_layer, uv_layer, cfg, uv_bounds)

        # scatter leaflets along this stem
        density = rand_range(rng, cfg["leaflet_density"])
        n_leaflets = max(2, int(len(stem.points) * density * 2))
        for i in range(n_leaflets):
            t = rng.uniform(0.1, 0.98)
            idx = min(int(t * (len(stem.points) - 1)), len(stem.points) - 2)
            p = stem.points[idx].lerp(stem.points[idx + 1], t * len(stem.points) - idx)
            fwd = (stem.points[idx + 1] - stem.points[idx]).normalized()
            up = Vector((0, 0, 1))
            side = fwd.cross(up)
            if side.length < 1e-5:
                side = Vector((1, 0, 0))
            side.normalize()

            angle = math.radians(rand_range(rng, cfg["leaflet_angle_deg"]))
            pairs = [1, -1] if cfg["leaflet_pairs"] else [rng.choice([1, -1])]
            for sign in pairs:
                rot = Matrix.Rotation(angle * sign, 4, up)
                leaf_dir = (rot @ fwd).normalized()
                size = rand_range(rng, cfg["leaflet_size"]) * (1.0 - 0.5 * (stem.depth / max(cfg["max_depth"], 1)))
                overall_t = min(stem.depth / max(cfg["max_depth"], 1) + 0.5 * t, 1.0)
                if overall_t < 0.5:
                    c = lerp3(cfg["color_base"], cfg["color_mid"], overall_t * 2.0)
                else:
                    c = lerp3(cfg["color_mid"], cfg["color_tip"], (overall_t - 0.5) * 2.0)
                add_leaflet(bm, p, leaf_dir, up, size, col_layer, uv_layer, c, uv_bounds)

    mesh = bpy.data.meshes.new("BranchCardMesh")
    bm.normal_update()
    bm.to_mesh(mesh)
    bm.free()
    mesh.update()

    return mesh, root_length, root_width


def make_two_sided(obj, offset=0.0008):
    """Duplicate all faces with flipped normals so the card reads correctly
    from both sides under Unity's single-sided Standard shader. The
    duplicate is nudged slightly along its normal first — without this,
    the two coincident faces z-fight (flickery, washed-out look) in both
    Blender's viewport and in Unity."""
    bpy.context.view_layer.objects.active = obj
    bpy.ops.object.mode_set(mode='EDIT')
    bpy.ops.mesh.select_all(action='SELECT')
    bpy.ops.mesh.duplicate()
    bpy.ops.transform.shrink_fatten(value=-offset)
    bpy.ops.mesh.flip_normals()
    bpy.ops.object.mode_set(mode='OBJECT')


# =====================================================================
# Main
# =====================================================================

def main():
    cfg = CONFIG
    rng = random.Random(cfg["seed"])

    full_pipeline_reset()
    col = get_or_create_collection(COLLECTION_NAME)

    for i in range(cfg["num_variants"]):
        variant_seed = rng.randint(0, 10_000_000)
        variant_rng = random.Random(variant_seed)

        mesh, root_length, root_width = build_branch_mesh(cfg, variant_rng)
        obj = bpy.data.objects.new(f"Branch_{i:02d}", mesh)
        col.objects.link(obj)

        # store generation metadata so script 3 can pick branches by size
        obj["branch_seed"] = variant_seed
        obj["branch_length"] = root_length
        obj["branch_width"] = root_width
        obj["is_branch_card"] = True

        make_two_sided(obj)

    # save as a reusable library file
    try:
        bpy.ops.wm.save_as_mainfile(filepath=bpy.path.abspath(cfg["output_blend"]), copy=True)
        print(f"[branch_gen] Saved {cfg['num_variants']} branches to {cfg['output_blend']}")
    except Exception as e:
        print(f"[branch_gen] Could not auto-save library blend ({e}). "
              f"Save the current .blend manually to build your library.")

    print(f"[branch_gen] Done. Generated {cfg['num_variants']} branch variants "
          f"in collection '{COLLECTION_NAME}'.")


if __name__ == "__main__":
    main()
