"""
3_assemble_tree_and_export.py
=============================================================
STEP 3 of 3 — Tree Assembly & Export
=============================================================

Run this INSIDE Blender, AFTER running steps 1 and 2 (billboard
branch cards must already exist in a "BillboardCards" collection).

WHAT THIS DOES
---------------
  1. Generates a randomized trunk (tapered, gently leaning mesh) with
     a simple bark material.
  2. Scatters BILLBOARD CARD instances (from step 2 — cheap flat
     quads, not the heavy source geometry) around the trunk following
     a "species profile" that controls how branch length/density/
     droop change with height, so the same card library can produce a
     narrow juniper, a conical spruce, or a tiered pine, etc.
  3. Joins everything into ONE mesh and exports it as a single FBX.

That's it — no separate LOD levels, no whole-tree billboard. The
assembled tree (built from billboard branch cards) IS the final
deliverable, matching your reference image 2.

USAGE
-----
Edit CONFIG (pick a SPECIES_PROFILE, set NUM_TREES, output folder),
then run. For each tree it creates "Tree_##" and exports
"Tree_##.fbx" into OUTPUT_DIR.
"""

import bpy
import bmesh
import random
import math
import os
from mathutils import Vector, Matrix

# =====================================================================
# CONFIG
# =====================================================================

def make_whorl_density(n_whorls, start_t, whorl_width, base_density, height_falloff=0.35):
    """Builds a tiered/whorled density function: bands of branches with
    real bare-trunk gaps between them, starting only after start_t (so
    the lower trunk is clear, like a real pine that self-prunes its
    lowest branches) — instead of one continuous cone like spruce."""
    def f(t):
        if t < start_t:
            return 0
        cycle_t = (t - start_t) / max(1.0 - start_t, 1e-6)
        phase = (cycle_t * n_whorls) % 1.0
        if phase < whorl_width:
            return base_density * (1.0 - height_falloff * t)
        return 0
    return f


def make_rounded_top_length_curve(plateau, round_start_t, tip_min):
    """Long, roughly-constant branch length through most of the crown
    (plateau), only tapering in the last stretch for a rounded/flat top
    — instead of continuously narrowing to a point like spruce's cone."""
    def f(t):
        if t < round_start_t:
            return plateau
        frac = (t - round_start_t) / max(1.0 - round_start_t, 1e-6)
        return plateau * max(1.0 - frac, 0.0) ** 0.5 + tip_min
    return f


SPECIES_PROFILES = {
    # profile functions describe, from base (t=0) to tip (t=1) of the
    # trunk, how branch length / density / droop scale — this is what
    # differentiates each species' silhouette
    "juniper_dense": {
        "height": (2.5, 4.5),
        "trunk_base_radius": (0.05, 0.09),
        "length_curve": lambda t: 0.7 * (1.0 - t) ** 0.7 + 0.12,
        "length_multiplier": 0.5,      # ~4x narrower than spruce's 2.0
        "density_curve": lambda t: 34 * (1.0 - 0.1 * t),   # denser than spruce
        "droop_curve": lambda t: 0.25 + 0.35 * t,
        "spiral_angle_deg": 137.5,
        "lean": (0.0, 0.05),
        "bark_tint": (1.0, 1.0, 1.0),
    },
    "spruce_conical": {
        "height": (5.0, 9.0),
        "trunk_base_radius": (0.10, 0.18),
        "length_curve": lambda t: 1.0 * (1.0 - t) ** 1.3 + 0.16,
        "length_multiplier": 2.0,      # branches reach twice as far out
        "density_curve": lambda t: 28 * (1.0 - 0.15 * t),
        "droop_curve": lambda t: 0.35 + 0.4 * t,
        "spiral_angle_deg": 137.5,
        "lean": (0.0, 0.05),
        "bark_tint": (1.0, 1.0, 1.0),
    },
    "pine_open": {
        "height": (6.0, 9.5),
        "trunk_base_radius": (0.10, 0.16),
        "length_curve": make_rounded_top_length_curve(plateau=0.85, round_start_t=0.72, tip_min=0.12),
        "length_multiplier": 1.15,
        "density_curve": make_whorl_density(n_whorls=6, start_t=0.18, whorl_width=0.22, base_density=26),
        "droop_curve": lambda t: 0.12 + 0.25 * t,
        "spiral_angle_deg": 137.5,
        "lean": (0.02, 0.12),
        "bark_tint": (1.0, 1.0, 1.0),
    },
    "pine_red": {
        "height": (7.0, 11.0),
        "trunk_base_radius": (0.12, 0.19),
        "length_curve": make_rounded_top_length_curve(plateau=1.0, round_start_t=0.68, tip_min=0.14),
        "length_multiplier": 1.3,      # more open, longer-reaching branches
        "density_curve": make_whorl_density(n_whorls=5, start_t=0.22, whorl_width=0.18, base_density=22),
        "droop_curve": lambda t: 0.15 + 0.2 * t,
        "spiral_angle_deg": 137.5,
        "lean": (0.03, 0.14),
        "bark_tint": (1.3, 0.82, 0.68),   # reddish-brown bark
    },
}

CONFIG = {
    "seed": None,
    "num_trees": 5,
    "species": "spruce_conical",     # key into SPECIES_PROFILES
    "card_collection": "BillboardCards",   # cards from step 2
    "output_dir": "//exported_trees/",

    "trunk_segments": 10,
    "trunk_sides": 8,
    "branch_scale_variation": (0.9, 1.5),

    # Triangle budget: instead of a fixed number of scatter passes (which
    # gave wildly different totals per species — shorter/narrower species
    # like juniper simply have fewer height-slots available), keep adding
    # scatter passes until the tree reaches target_triangle_budget, and
    # trim if it overshoots max_triangle_budget. This makes total branch
    # count roughly equal across species regardless of height/profile,
    # which is also what makes a narrow species look properly DENSE
    # rather than just sparse-and-narrow.
    "target_triangle_budget": 2500,
    "max_triangle_budget": 3000,
    "tris_per_card": 4,     # 2 tris front + 2 tris back (see build_card_object
                             # + add_offset_backface in script 2) — update this
                             # if you change how many faces a card has
    "max_density_passes_safety": 30,   # hard stop so a bad config can't infinite-loop

    "variant_spacing_m": 6.0,     # purely cosmetic: how far apart to lay out
                                   # multiple tree variants in the viewport after
                                   # export, so they don't sit stacked on each
                                   # other at the origin
    "guarantee_top_coverage_t": 0.9,   # above this height fraction, always place a
                                        # branch (no random skip) so the trunk tip
                                        # doesn't stick out bare above the foliage

    # Branch placement pitch: kept gentle now, since the down-then-up
    # "bowl" shape is baked directly into the branch geometry itself
    # (script 1's initial_dip_deg/tip_arch_deg). This just controls the
    # OVERALL attachment lean per species/height — how much a branch's
    # base points down before its own curve takes over.
    "placement_pitch_scale": 0.5,

    # Taper: makes lower branches proportionally longer than the species
    # length_curve alone would, without touching the (already-short,
    # already-correct) branch lengths near the top — fixes a canopy that
    # looks too uniform-width (and therefore like the tree "should be
    # shorter") by fanning the base out further.
    "taper_strength": 0.6,   # 0 = no extra taper, 1.0 = base branches up to
                              # 2x longer than the species curve alone gives

    # Uniform final scale, applied to the whole assembled tree (trunk +
    # branches together) and baked into the mesh before export — not
    # just a transform, so it survives FBX import into Unity at the
    # size you actually see in Blender.
    "final_scale_multiplier": 2.0,

    # --- Bark material (trunk) ---
    "bark_texture_dir": "//baked_textures/",   # must match script 2's output_dir
    "bark_uv_repeat_u": 2.0,      # times around the trunk's circumference
    "bark_uv_repeat_v_per_meter": 0.6,   # repeats per meter of trunk height
}


TREES_COLLECTION = "GeneratedTrees"


def get_or_create_collection(name):
    if name in bpy.data.collections:
        return bpy.data.collections[name]
    col = bpy.data.collections.new(name)
    bpy.context.scene.collection.children.link(col)
    return col


def clear_trees_collection(col):
    """Wipes any trees from a previous run (including previous species)
    so re-running this script replaces them instead of piling up
    duplicates alongside the old ones."""
    for obj in list(col.objects):
        mesh = obj.data if obj.type == 'MESH' else None
        mat = obj.active_material if obj.type == 'MESH' else None
        bpy.data.objects.remove(obj, do_unlink=True)
        if mesh and mesh.users == 0:
            bpy.data.meshes.remove(mesh)
        if mat and mat.users == 0:
            bpy.data.materials.remove(mat)
    bpy.data.orphans_purge(do_local_ids=True, do_recursive=True)


# =====================================================================
# Trunk generation
# =====================================================================

def build_trunk(profile, rng):
    height = rng.uniform(*profile["height"])
    base_radius = rng.uniform(*profile["trunk_base_radius"])
    lean_amount = rng.uniform(*profile["lean"])
    lean_dir = rng.uniform(0, math.tau)

    bm = bmesh.new()
    segs = CONFIG["trunk_segments"]
    sides = CONFIG["trunk_sides"]
    u_repeat = CONFIG["bark_uv_repeat_u"]
    v_per_meter = CONFIG["bark_uv_repeat_v_per_meter"]

    rings = []
    for i in range(segs + 1):
        t = i / segs
        z = height * t
        radius = base_radius * (1.0 - t) ** 1.5 + 0.01
        lean_x = math.sin(lean_dir) * lean_amount * height * (t ** 2)
        lean_y = math.cos(lean_dir) * lean_amount * height * (t ** 2)
        wobble_x = rng.uniform(-0.01, 0.01) * height * t
        wobble_y = rng.uniform(-0.01, 0.01) * height * t

        ring = []
        for s in range(sides):
            ang = (s / sides) * math.tau
            x = lean_x + wobble_x + math.cos(ang) * radius
            y = lean_y + wobble_y + math.sin(ang) * radius
            ring.append(bm.verts.new((x, y, z)))
        rings.append(ring)

    bm.verts.ensure_lookup_table()
    uv_layer = bm.loops.layers.uv.new("UVMap")

    for i in range(segs):
        t0, t1 = i / segs, (i + 1) / segs
        v0v, v1v = height * t0 * v_per_meter, height * t1 * v_per_meter
        for s in range(sides):
            s2 = (s + 1) % sides
            u0 = (s / sides) * u_repeat
            u1 = u_repeat if s2 == 0 else (s2 / sides) * u_repeat  # avoid wrap-around seam snap
            va, vb = rings[i][s], rings[i][s2]
            vc, vd = rings[i + 1][s2], rings[i + 1][s]
            f = bm.faces.new((va, vb, vc, vd))
            uv_map = {va: (u0, v0v), vb: (u1, v0v), vc: (u1, v1v), vd: (u0, v1v)}
            for loop in f.loops:
                loop[uv_layer].uv = uv_map[loop.vert]

    cap_face = bm.faces.new(rings[-1])
    top_v = height * v_per_meter
    for idx, loop in enumerate(cap_face.loops):
        loop[uv_layer].uv = ((idx / len(rings[-1])) * u_repeat, top_v)

    mesh = bpy.data.meshes.new("TrunkMesh")
    bm.normal_update()
    bm.to_mesh(mesh)
    bm.free()

    trunk_obj = bpy.data.objects.new("Trunk", mesh)
    get_or_create_collection(TREES_COLLECTION).objects.link(trunk_obj)
    trunk_obj.data.materials.append(
        get_or_build_bark_material(CONFIG["species"], profile.get("bark_tint", (1.0, 1.0, 1.0)))
    )

    return trunk_obj, height, base_radius, lean_amount, lean_dir


def get_or_build_bark_material(species_key, tint=(1.0, 1.0, 1.0)):
    """Loads the bark_albedo.png / bark_normal.png baked by script 2 and
    wires them into a Standard-shader-friendly material, one per species
    so a species-specific tint (e.g. red pine's reddish bark) doesn't
    affect other trees sharing the same baked textures. Falls back to a
    flat brown color with a console warning if step 2 hasn't been run
    with bake_bark_material=True yet."""
    mat_name = f"BarkMaterial_{species_key}"
    bark_mat = bpy.data.materials.get(mat_name)
    if bark_mat:
        return bark_mat

    bark_mat = bpy.data.materials.new(mat_name)
    bark_mat.use_nodes = True
    nt = bark_mat.node_tree
    bsdf = nt.nodes.get("Principled BSDF")
    bsdf.inputs["Roughness"].default_value = 0.9

    bark_dir = bpy.path.abspath(CONFIG["bark_texture_dir"])
    albedo_path = os.path.join(bark_dir, "bark_albedo.png")
    normal_path = os.path.join(bark_dir, "bark_normal.png")
    needs_tint = tint != (1.0, 1.0, 1.0)

    if os.path.exists(albedo_path):
        albedo_img = bpy.data.images.load(albedo_path, check_existing=True)
        albedo_node = nt.nodes.new("ShaderNodeTexImage")
        albedo_node.image = albedo_img
        albedo_node.extension = 'REPEAT'   # UVs go beyond 0..1 on purpose, for tiling

        if needs_tint:
            tint_node = nt.nodes.new("ShaderNodeMixRGB")
            tint_node.blend_type = 'MULTIPLY'
            tint_node.inputs["Fac"].default_value = 1.0
            tint_node.inputs["Color2"].default_value = (*tint, 1.0)
            nt.links.new(albedo_node.outputs["Color"], tint_node.inputs["Color1"])
            nt.links.new(tint_node.outputs["Color"], bsdf.inputs["Base Color"])
        else:
            nt.links.new(albedo_node.outputs["Color"], bsdf.inputs["Base Color"])
    else:
        fallback = (0.10 * tint[0], 0.06 * tint[1], 0.04 * tint[2], 1.0)
        bsdf.inputs["Base Color"].default_value = fallback
        print(f"[assemble] WARNING: {albedo_path} not found — run step 2 with "
              f"bake_bark_material=True first. Using a flat fallback color for now.")

    if os.path.exists(normal_path):
        normal_img = bpy.data.images.load(normal_path, check_existing=True)
        normal_img.colorspace_settings.name = 'Non-Color'
        normal_tex_node = nt.nodes.new("ShaderNodeTexImage")
        normal_tex_node.image = normal_img
        normal_tex_node.extension = 'REPEAT'
        normal_map_node = nt.nodes.new("ShaderNodeNormalMap")
        nt.links.new(normal_tex_node.outputs["Color"], normal_map_node.inputs["Color"])
        nt.links.new(normal_map_node.outputs["Normal"], bsdf.inputs["Normal"])

    return bark_mat


# =====================================================================
# Branch card scattering
# =====================================================================

def scatter_branches(height, profile, card_pool, rng, tree_index, base_radius,
                      lean_amount, lean_dir, pass_index=0):
    placed = []
    spiral_angle = math.radians(profile["spiral_angle_deg"])
    # offset the starting angle per pass so multiple density passes
    # interleave around the trunk instead of retracing the same spiral
    current_angle = rng.uniform(0, math.tau) + pass_index * math.radians(63.7)

    n_slots = int(height * rng.uniform(14, 20))

    for i in range(n_slots):
        t = i / max(n_slots - 1, 1)
        density = profile["density_curve"](t)
        if density <= 0 and t < CONFIG["guarantee_top_coverage_t"]:
            continue
        if t < CONFIG["guarantee_top_coverage_t"] and rng.uniform(0, 30) > density:
            continue

        z = height * t
        # extra taper: boosts length near the base without touching the
        # (already correct) short lengths near the top
        taper_boost = 1.0 + CONFIG["taper_strength"] * (1.0 - t)
        length_factor = (max(profile["length_curve"](t), 0.05) * taper_boost
                          * profile.get("length_multiplier", 1.0))
        droop = profile["droop_curve"](t)

        current_angle += spiral_angle + rng.uniform(-0.15, 0.15)
        # anchor right at the trunk's actual surface at this height —
        # matching build_trunk's own lean/curve formula exactly, so
        # branches stay aligned with the trunk even where it curves,
        # instead of assuming a perfectly straight vertical trunk
        trunk_radius_here = base_radius * (1.0 - t) ** 1.5 + 0.01
        lean_x = math.sin(lean_dir) * lean_amount * height * (t ** 2)
        lean_y = math.cos(lean_dir) * lean_amount * height * (t ** 2)
        local_x = math.cos(current_angle) * trunk_radius_here
        local_y = math.sin(current_angle) * trunk_radius_here
        x = lean_x + local_x
        y = lean_y + local_y

        card_template = rng.choice(card_pool)
        inst = card_template.copy()
        inst.data = card_template.data  # linked duplicate — shares mesh + material
        inst.name = f"Tree{tree_index:02d}_P{pass_index}_Card_{i:03d}"
        get_or_create_collection(TREES_COLLECTION).objects.link(inst)

        scale = rng.uniform(*CONFIG["branch_scale_variation"]) * (0.4 + 0.6 * length_factor)

        # orientation uses the LOCAL radial direction (ignoring lean) —
        # lean only shifts the trunk's centerline sideways, it doesn't
        # change which way branches face around the circumference
        outward = math.atan2(local_y, local_x)
        rot = Matrix.Rotation(outward, 4, 'Z')
        # gentle overall attachment lean — the down-then-up bowl shape
        # itself is already baked into the branch geometry (script 1),
        # this just nudges the whole branch's base angle per species/height
        pitch = droop * rng.uniform(0.7, 1.1) * CONFIG["placement_pitch_scale"]
        droop_rot = Matrix.Rotation(pitch, 4, 'Y')
        roll_rot = Matrix.Rotation(rng.uniform(-0.2, 0.2), 4, 'X')
        inst.matrix_world = (Matrix.Translation((x, y, z)) @ rot @ droop_rot @ roll_rot)
        inst.scale = (scale, scale, scale)

        placed.append(inst)

    return placed


def join_tree_parts(trunk_obj, card_objs, name):
    bpy.ops.object.select_all(action='DESELECT')
    for o in card_objs:
        o.select_set(True)
    trunk_obj.select_set(True)
    bpy.context.view_layer.objects.active = trunk_obj
    bpy.ops.object.join()
    trunk_obj.name = name
    return trunk_obj


def count_triangles(obj):
    mesh = obj.data
    mesh.calc_loop_triangles()
    return len(mesh.loop_triangles)


def remove_object_and_orphan_data(obj):
    mesh = obj.data
    bpy.data.objects.remove(obj, do_unlink=True)
    if mesh and mesh.users == 0:
        bpy.data.meshes.remove(mesh)


# =====================================================================
# Export
# =====================================================================

def export_fbx(obj, filepath):
    bpy.ops.object.select_all(action='DESELECT')
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj
    bpy.ops.export_scene.fbx(
        filepath=filepath,
        use_selection=True,
        global_scale=1.0,
        apply_unit_scale=True,
        bake_space_transform=True,
        object_types={'MESH'},
        mesh_smooth_type='FACE',
        path_mode='COPY',
        embed_textures=True,
        axis_forward='-Z',
        axis_up='Y',
    )


# =====================================================================
# Main
# =====================================================================

def main():
    cfg = CONFIG
    rng = random.Random(cfg["seed"])
    profile = SPECIES_PROFILES[cfg["species"]]

    card_col = bpy.data.collections.get(cfg["card_collection"])
    if not card_col or not card_col.objects:
        print(f"[assemble] No billboard cards found in '{cfg['card_collection']}'. "
              f"Run steps 1 and 2 first.")
        return
    card_pool = list(card_col.objects)

    out_dir = bpy.path.abspath(cfg["output_dir"])
    os.makedirs(out_dir, exist_ok=True)

    trees_col = get_or_create_collection(TREES_COLLECTION)
    clear_trees_collection(trees_col)

    for t_idx in range(cfg["num_trees"]):
        trunk_obj, height, base_radius, lean_amount, lean_dir = build_trunk(profile, rng)
        trunk_tris = count_triangles(trunk_obj)

        tris_per_card = cfg["tris_per_card"]
        min_cards = max(1, int((cfg["target_triangle_budget"] - trunk_tris) / tris_per_card))
        max_cards = max(1, int((cfg["max_triangle_budget"] - trunk_tris) / tris_per_card))

        card_instances = []
        pass_i = 0
        while len(card_instances) < min_cards and pass_i < cfg["max_density_passes_safety"]:
            card_instances += scatter_branches(
                height, profile, card_pool, rng, t_idx, base_radius,
                lean_amount, lean_dir, pass_index=pass_i
            )
            pass_i += 1

        if len(card_instances) > max_cards:
            rng.shuffle(card_instances)
            excess = card_instances[max_cards:]
            card_instances = card_instances[:max_cards]
            for obj in excess:
                remove_object_and_orphan_data(obj)

        tree_name = f"Tree_{t_idx:02d}"
        tree_obj = join_tree_parts(trunk_obj, card_instances, tree_name)

        # bake the final uniform scale into the mesh itself (not just a
        # transform) so it exports correctly and survives FBX import at
        # the size you actually see in Blender
        mult = cfg["final_scale_multiplier"]
        tree_obj.scale = (mult, mult, mult)
        bpy.context.view_layer.objects.active = tree_obj
        bpy.ops.object.select_all(action='DESELECT')
        tree_obj.select_set(True)
        bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)

        final_tris = count_triangles(tree_obj)

        fbx_path = os.path.join(out_dir, f"{tree_name}.fbx")
        export_fbx(tree_obj, fbx_path)

        # purely cosmetic: spread variants apart in the viewport AFTER
        # exporting, so the exported FBX still sits cleanly at the
        # origin (what you want for a Unity prefab) but multiple
        # variants don't visually overlap while you're working in Blender
        tree_obj.location.x += t_idx * cfg["variant_spacing_m"]

        print(f"[assemble] Built {tree_name}: height={height:.2f}m x{mult} scale, "
              f"{len(card_instances)} branch cards ({pass_i} passes), "
              f"{final_tris} triangles (target {cfg['target_triangle_budget']}-{cfg['max_triangle_budget']}) "
              f"-> {fbx_path}")

    print(f"[assemble] Done. {cfg['num_trees']} trees exported to {out_dir}")


if __name__ == "__main__":
    main()
