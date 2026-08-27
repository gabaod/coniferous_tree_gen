"""
2_generate_foliage_shader.py
=============================================================
STEP 2 of 3 — Material, Bake, and Snapshot-to-Billboard-Card
=============================================================

Run this INSIDE Blender, AFTER running 1_generate_branch_foliage.py
(or after opening branch_library.blend from step 1).

WHAT THIS DOES (redesigned to match the actual intended pipeline)
-------------------------------------------------------------
Each detailed 3D branch generated in step 1 is a lot of geometry
(recursive stems + leaflets). That detail is only there to make a
convincing SNAPSHOT — the real building block used to assemble the
final tree is a cheap flat card, exactly like your reference image 1:
a single rectangular quad with an alpha-cutout texture of the branch
baked onto it.

So this script:

  1. Builds a procedural material (color variation via the vertex
     colors from step 1) on each detailed branch.
  2. Renders each branch, individually, from directly above (top-down
     orthographic camera, transparent background) to a PNG — this is
     the "snapshot" that becomes the card's Albedo+Alpha.
  3. Bakes a real Normal map for each card by projecting ("Selected to
     Active") the detailed source geometry's surface detail down onto
     the flat card's UV — this is what gives a flat billboard card
     believable fake depth under Unity's lighting, instead of looking
     perfectly flat.
  4. Builds a brand-new, simple flat quad sized to that branch's
     footprint, applies the baked Albedo+Alpha and Normal map, and
     makes it two-sided (duplicated, offset, flipped-normal backface
     — safe for Unity's single-sided Standard shader, added only
     AFTER baking so the two overlapping UV islands don't confuse the
     bake).
  5. Also builds a tileable bark material for trunks: a procedural
     bark look baked to bark_albedo.png / bark_normal.png (script 3
     applies these to the trunk with a repeating cylindrical UV).
  6. Puts the branch cards into a "BillboardCards" collection. THIS is
     what step 3 assembles trees out of — the original detailed
     meshes stay in "BranchLibrary" but get hidden, since they're not
     used for assembly or export; keep them around only if you want
     to re-bake later at a different angle or resolution.

Unity 2017 has no Shader Graph and can't read Blender's node
materials directly, which is why step 2's job is to convert
everything to flat PNGs + a simple quad before export.

USAGE
-----
Edit CONFIG below, then run. Output: baked_textures/*.png (per-branch
Albedo+Alpha and Normal maps, plus bark_albedo.png/bark_normal.png),
and a "BillboardCards" collection of ready-to-assemble flat cards.
"""

import bpy
import bmesh
import os
import random
import math
from mathutils import Vector, Matrix

# =====================================================================
# CONFIG
# =====================================================================

CONFIG = {
    "source_collection": "BranchLibrary",
    "output_dir": "//baked_textures/",
    "card_resolution": 512,      # PNG size per branch snapshot
    "bake_samples": 32,          # Cycles samples for the material-look render
    "seed": None,

    # --- Shader look parameters (applied before snapshotting) ---
    # NOTE: the snapshot uses a flat, self-illuminated (Emission) shader,
    # not a lit Principled shader — this makes the bake correct as an
    # Albedo map (no baked-in lighting/shadows) AND makes it work
    # reliably even if your scene has no lights in it. Alpha comes from
    # real geometry coverage (the actual gaps between stems and
    # leaflets), not a synthetic noise mask, so the cutout always
    # matches the branch's real silhouette.
    "color_variation_strength": 0.25,

    # --- Normal map baking (branch cards) ---
    "bake_normal_maps": True,
    "normal_bake_samples": 32,

    # --- Bark material (trunk) ---
    "bake_bark_material": True,
    "bark_resolution": 1024,
    "bark_tile_u_repeats": 2.0,     # times around the trunk's circumference
    "bark_tile_v_per_meter": 0.6,   # repeats per meter of trunk height
                                     # (used here only to size the reference
                                     # bake plane sensibly; actual tiling is
                                     # applied to the trunk's UV in script 3)

    "hide_source_branches": True,   # hide the heavy source geometry after baking
                                      # (in place — kept in BranchLibrary so this
                                      # script can be safely re-run without first
                                      # re-running step 1)
}

BILLBOARD_COLLECTION = "BillboardCards"


# =====================================================================
# Helpers
# =====================================================================

def ensure_dir(path):
    abs_path = bpy.path.abspath(path)
    os.makedirs(abs_path, exist_ok=True)
    return abs_path


def get_or_create_collection(name):
    if name in bpy.data.collections:
        return bpy.data.collections[name]
    col = bpy.data.collections.new(name)
    bpy.context.scene.collection.children.link(col)
    return col


def set_material_transparency(mat):
    """Set viewport alpha-blend mode in a way that's safe across Blender
    versions — Blender 4.2's EEVEE Next removed `shadow_method` entirely
    and narrowed `blend_method`'s valid options, so try the old values
    and fall back gracefully if they don't exist on this build."""
    for value in ('HASHED', 'CLIP', 'BLEND'):
        try:
            mat.blend_method = value
            break
        except TypeError:
            continue
    if hasattr(mat, "shadow_method"):
        for value in ('HASHED', 'CLIP', 'NONE'):
            try:
                mat.shadow_method = value
                break
            except TypeError:
                continue
    if hasattr(mat, "use_transparent_shadow"):
        mat.use_transparent_shadow = True


def build_branch_material(name, rng, cfg):
    """Flat, self-illuminated (Emission) look applied to the detailed
    source geometry purely so it snapshots as a clean, correctly-lit
    Albedo texture — this material is never exported, and deliberately
    does NOT depend on scene lights (Emission renders the same whether
    your file has zero lights or ten)."""
    mat = bpy.data.materials.new(name)
    mat.use_nodes = True
    set_material_transparency(mat)
    nt = mat.node_tree
    nodes = nt.nodes
    links = nt.links
    nodes.clear()

    out = nodes.new("ShaderNodeOutputMaterial")
    out.location = (700, 0)

    emission = nodes.new("ShaderNodeEmission")
    emission.location = (450, 0)

    vcol = nodes.new("ShaderNodeVertexColor")
    vcol.layer_name = "Col"
    vcol.location = (0, 150)

    noise = nodes.new("ShaderNodeTexNoise")
    noise.location = (0, -50)
    noise.inputs["Scale"].default_value = rng.uniform(8, 20)

    hsv = nodes.new("ShaderNodeHueSaturation")
    hsv.location = (200, 150)
    hsv.inputs["Hue"].default_value = 0.5 + rng.uniform(-0.03, 0.03)
    hsv.inputs["Saturation"].default_value = 1.0 + rng.uniform(-0.15, 0.15)
    hsv.inputs["Value"].default_value = 1.0

    mix_color = nodes.new("ShaderNodeMixRGB")
    mix_color.location = (350, 150)
    mix_color.blend_type = 'MULTIPLY'
    mix_color.inputs["Fac"].default_value = cfg["color_variation_strength"]

    links.new(vcol.outputs["Color"], hsv.inputs["Color"])
    links.new(hsv.outputs["Color"], mix_color.inputs["Color1"])
    links.new(noise.outputs["Fac"], mix_color.inputs["Color2"])
    links.new(mix_color.outputs["Color"], emission.inputs["Color"])
    links.new(emission.outputs["Emission"], out.inputs["Surface"])

    return mat


def snapshot_branch(obj, index, out_dir, resolution, samples):
    """Render this single branch from directly above (top-down ortho,
    matching the branch's natural flat X/Y spread from step 1) onto a
    transparent PNG. Returns (filepath, footprint_min, footprint_max)."""
    scene = bpy.context.scene

    bbox_corners = [obj.matrix_world @ Vector(c) for c in obj.bound_box]
    min_v = Vector((min(c.x for c in bbox_corners), min(c.y for c in bbox_corners), min(c.z for c in bbox_corners)))
    max_v = Vector((max(c.x for c in bbox_corners), max(c.y for c in bbox_corners), max(c.z for c in bbox_corners)))
    width = max(max_v.x - min_v.x, 0.01)
    depth = max(max_v.y - min_v.y, 0.01)
    center = (min_v + max_v) / 2

    cam_data = bpy.data.cameras.new(f"SnapCam_{index}")
    cam_data.type = 'ORTHO'
    cam_data.ortho_scale = max(width, depth) * 1.05
    cam_obj = bpy.data.objects.new(f"SnapCam_{index}", cam_data)
    bpy.context.scene.collection.objects.link(cam_obj)
    cam_obj.location = (center.x, center.y, max_v.z + 2.0)
    cam_obj.rotation_euler = (0, 0, 0)  # looking straight down -Z

    # temporarily isolate this object for a clean render
    prev_visibility = {}
    for o in bpy.context.scene.objects:
        if o.type in ('MESH',) and o != obj:
            prev_visibility[o.name] = o.hide_render
            o.hide_render = True

    scene.camera = cam_obj
    scene.render.engine = 'CYCLES'
    scene.cycles.samples = samples
    scene.render.film_transparent = True
    scene.render.resolution_x = resolution
    scene.render.resolution_y = int(resolution * (depth / width)) if width > depth else resolution
    if width <= depth:
        scene.render.resolution_x = int(resolution * (width / depth))
        scene.render.resolution_y = resolution
    scene.render.resolution_x = max(scene.render.resolution_x, 8)
    scene.render.resolution_y = max(scene.render.resolution_y, 8)
    scene.render.image_settings.file_format = 'PNG'
    scene.render.image_settings.color_mode = 'RGBA'

    out_path = os.path.join(out_dir, f"branch_{index:02d}_card.png")
    scene.render.filepath = out_path
    bpy.ops.render.render(write_still=True)

    for name, hide_val in prev_visibility.items():
        bpy.data.objects[name].hide_render = hide_val
    bpy.data.objects.remove(cam_obj, do_unlink=True)
    bpy.data.cameras.remove(cam_data)

    return out_path, min_v, max_v


def build_card_object(index, img_path, min_v, max_v, source_obj):
    """A single flat quad in the XY plane (matching the branch's natural
    orientation from step 1: length along local X, spread along Y).

    IMPORTANT: the quad is anchored at the branch's actual attachment
    point (local origin, i.e. where the stem started growing in step 1)
    rather than centered on its bounding box. If it were centered,
    roughly half of every branch's length would extend backward into
    the trunk once placed in step 3 instead of all of it reaching
    outward — which is what was making assembled trees look thin.

    NOTE: this builds a SINGLE-SIDED quad on purpose — the two-sided
    backface is added later, AFTER normal-map baking (see
    add_offset_backface). Adding it here would give the object two
    overlapping faces sharing the same UV space, which makes a
    Selected-to-Active normal bake ambiguous (undefined which face's
    result "wins" per texel).
    """
    min_x, max_x = min_v.x, max_v.x
    min_y, max_y = min_v.y, max_v.y
    z = (min_v.z + max_v.z) / 2
    width = max(max_x - min_x, 0.01)
    depth = max(max_y - min_y, 0.01)

    bm = bmesh.new()
    # raw (uncentered) coordinates — local (0,0) stays at the branch's
    # real attachment point, not the bounding-box center
    v0 = bm.verts.new((min_x, min_y, 0))
    v1 = bm.verts.new((max_x, min_y, 0))
    v2 = bm.verts.new((max_x, max_y, 0))
    v3 = bm.verts.new((min_x, max_y, 0))
    bm.verts.ensure_lookup_table()
    f = bm.faces.new((v0, v1, v2, v3))
    f.smooth = True

    uv_layer = bm.loops.layers.uv.new("UVMap")
    uv_coords = {v0: (0, 0), v1: (1, 0), v2: (1, 1), v3: (0, 1)}
    for loop in f.loops:
        loop[uv_layer].uv = uv_coords[loop.vert]

    mesh = bpy.data.meshes.new(f"BillboardCard_{index:02d}Mesh")
    bm.normal_update()
    bm.to_mesh(mesh)
    bm.free()

    card_obj = bpy.data.objects.new(f"BillboardCard_{index:02d}", mesh)
    card_obj.location = (0, 0, z)

    mat = bpy.data.materials.new(f"BillboardCardMat_{index:02d}")
    mat.use_nodes = True
    set_material_transparency(mat)
    nt = mat.node_tree
    bsdf = nt.nodes.get("Principled BSDF")
    bsdf.inputs["Roughness"].default_value = 0.7
    img = bpy.data.images.load(img_path)
    tex_node = nt.nodes.new("ShaderNodeTexImage")
    tex_node.image = img
    nt.links.new(tex_node.outputs["Color"], bsdf.inputs["Base Color"])
    nt.links.new(tex_node.outputs["Alpha"], bsdf.inputs["Alpha"])
    card_obj.data.materials.append(mat)

    # carry over sizing metadata used by step 3's placement logic
    card_obj["branch_length"] = source_obj.get("branch_length", width)
    card_obj["branch_width"] = source_obj.get("branch_width", depth)
    card_obj["is_branch_card"] = True
    card_obj["card_texture"] = img_path

    return card_obj


def bake_normal_map(source_obj, card_obj, index, out_dir, resolution, samples, min_v, max_v):
    """'Selected to Active' bake: projects the detailed source branch's
    surface detail down onto the flat card's UV, so the card fakes real
    depth under Unity's lighting instead of looking perfectly flat.
    Must run BEFORE the card gets its two-sided backface (see module
    docstring) since overlapping UV islands make the bake ambiguous."""
    scene = bpy.context.scene
    scene.render.engine = 'CYCLES'
    scene.cycles.samples = samples

    mat = card_obj.data.materials[0]
    nt = mat.node_tree
    img = bpy.data.images.new(f"branch_{index:02d}_card_normal", width=resolution, height=resolution)
    img.colorspace_settings.name = 'Non-Color'
    img_node = nt.nodes.new("ShaderNodeTexImage")
    img_node.image = img
    nt.nodes.active = img_node

    bpy.ops.object.select_all(action='DESELECT')
    source_obj.select_set(True)
    card_obj.select_set(True)
    bpy.context.view_layer.objects.active = card_obj

    depth_range = max(max_v.z - min_v.z, 0.01)
    ray_distance = depth_range * 2.0 + 0.05
    cage_extrusion = depth_range * 1.5 + 0.02

    bpy.ops.object.bake(
        type='NORMAL',
        use_selected_to_active=True,
        cage_extrusion=cage_extrusion,
        max_ray_distance=ray_distance,
        normal_space='TANGENT',
    )

    filepath = os.path.join(out_dir, f"branch_{index:02d}_card_normal.png")
    img.filepath_raw = filepath
    img.file_format = 'PNG'
    img.save()

    normal_map_node = nt.nodes.new("ShaderNodeNormalMap")
    nt.links.new(img_node.outputs["Color"], normal_map_node.inputs["Color"])
    bsdf = nt.nodes.get("Principled BSDF")
    nt.links.new(normal_map_node.outputs["Normal"], bsdf.inputs["Normal"])

    return filepath


def add_offset_backface(mesh, offset=0.0008):
    """Adds a slightly-offset, flipped-normal duplicate of every face so
    the card reads correctly from both sides under Unity's single-sided
    Standard shader. Operates directly on mesh data (no bpy.ops/context
    dependency) so it's safe to call right after baking. The offset
    avoids z-fighting (coincident faces flicker in both Blender's
    viewport and in Unity)."""
    bm = bmesh.new()
    bm.from_mesh(mesh)
    bm.faces.ensure_lookup_table()
    uv_layer = bm.loops.layers.uv.verify()

    for f in list(bm.faces):
        verts = list(f.verts)
        orig_uvs = [loop[uv_layer].uv.copy() for loop in f.loops]
        new_verts = [bm.verts.new(v.co - Vector((0, 0, offset))) for v in verts]
        bm.verts.ensure_lookup_table()
        new_face = bm.faces.new(list(reversed(new_verts)))
        new_face.smooth = True
        for loop, uv in zip(new_face.loops, list(reversed(orig_uvs))):
            loop[uv_layer].uv = uv

    bm.normal_update()
    bm.to_mesh(mesh)
    bm.free()


def build_bark_material_for_bake(rng):
    """Procedural bark look: vertical noise-based striations for color,
    fine noise fed through a Bump node for surface relief. Used only to
    bake bark_albedo.png / bark_normal.png onto a flat reference plane —
    never exported itself (Unity 2017 can't read it directly)."""
    mat = bpy.data.materials.new("BarkSourceMat")
    mat.use_nodes = True
    nt = mat.node_tree
    nodes = nt.nodes
    links = nt.links
    nodes.clear()

    out = nodes.new("ShaderNodeOutputMaterial")
    out.location = (900, 0)

    tex_coord = nodes.new("ShaderNodeTexCoord")
    tex_coord.location = (-600, 0)
    mapping = nodes.new("ShaderNodeMapping")
    mapping.location = (-400, 0)
    mapping.inputs["Scale"].default_value = (3.0, 18.0, 1.0)  # stretched for vertical furrows
    links.new(tex_coord.outputs["Generated"], mapping.inputs["Vector"])

    color_noise = nodes.new("ShaderNodeTexNoise")
    color_noise.location = (-200, 150)
    color_noise.inputs["Scale"].default_value = rng.uniform(6, 12)
    links.new(mapping.outputs["Vector"], color_noise.inputs["Vector"])

    bump_noise = nodes.new("ShaderNodeTexNoise")
    bump_noise.location = (-200, -150)
    bump_noise.inputs["Scale"].default_value = rng.uniform(30, 50)
    links.new(mapping.outputs["Vector"], bump_noise.inputs["Vector"])

    ramp = nodes.new("ShaderNodeValToRGB")
    ramp.location = (50, 150)
    ramp.color_ramp.elements[0].color = (0.045, 0.03, 0.02, 1.0)
    ramp.color_ramp.elements[1].color = (0.16, 0.11, 0.075, 1.0)
    links.new(color_noise.outputs["Fac"], ramp.inputs["Fac"])

    bsdf = nodes.new("ShaderNodeBsdfPrincipled")
    bsdf.location = (400, 0)
    bsdf.inputs["Roughness"].default_value = 0.95
    links.new(ramp.outputs["Color"], bsdf.inputs["Base Color"])

    bump = nodes.new("ShaderNodeBump")
    bump.location = (200, -150)
    bump.inputs["Strength"].default_value = 0.6
    links.new(bump_noise.outputs["Fac"], bump.inputs["Height"])
    links.new(bump.outputs["Normal"], bsdf.inputs["Normal"])

    links.new(bsdf.outputs["BSDF"], out.inputs["Surface"])

    return mat, nt, ramp, out, bsdf


def bake_bark_textures(cfg, rng, out_dir):
    """Builds a temporary flat reference plane, bakes an Albedo (flat/
    unlit, same reasoning as the branch cards — no baked-in lighting)
    and a Normal map from the procedural bark shader, saves both PNGs,
    then deletes the reference plane. Script 3 applies these to actual
    trunk meshes with a repeating cylindrical UV."""
    resolution = cfg["bark_resolution"]

    bm = bmesh.new()
    v0 = bm.verts.new((0, 0, 0))
    v1 = bm.verts.new((1, 0, 0))
    v2 = bm.verts.new((1, 1, 0))
    v3 = bm.verts.new((0, 1, 0))
    bm.verts.ensure_lookup_table()
    f = bm.faces.new((v0, v1, v2, v3))
    uv_layer = bm.loops.layers.uv.new("UVMap")
    uv_coords = {v0: (0, 0), v1: (1, 0), v2: (1, 1), v3: (0, 1)}
    for loop in f.loops:
        loop[uv_layer].uv = uv_coords[loop.vert]
    mesh = bpy.data.meshes.new("BarkBakePlaneMesh")
    bm.to_mesh(mesh)
    bm.free()

    plane_obj = bpy.data.objects.new("BarkBakePlane", mesh)
    bpy.context.scene.collection.objects.link(plane_obj)

    mat, nt, ramp, out_node, bsdf = build_bark_material_for_bake(rng)
    plane_obj.data.materials.append(mat)

    scene = bpy.context.scene
    scene.render.engine = 'CYCLES'
    scene.cycles.samples = cfg["bake_samples"]

    bpy.ops.object.select_all(action='DESELECT')
    plane_obj.select_set(True)
    bpy.context.view_layer.objects.active = plane_obj

    # --- Albedo: temporarily reroute to a flat Emission output, exactly
    # like the branch cards, so lighting/shadows aren't baked in ---
    albedo_img = bpy.data.images.new("bark_albedo", width=resolution, height=resolution)
    albedo_node = nt.nodes.new("ShaderNodeTexImage")
    albedo_node.image = albedo_img
    nt.nodes.active = albedo_node

    emission = nt.nodes.new("ShaderNodeEmission")
    nt.links.new(ramp.outputs["Color"], emission.inputs["Color"])
    old_link_socket = out_node.inputs["Surface"].links[0].from_socket
    nt.links.new(emission.outputs["Emission"], out_node.inputs["Surface"])

    bpy.ops.object.bake(type='EMIT')
    albedo_path = os.path.join(out_dir, "bark_albedo.png")
    albedo_img.filepath_raw = albedo_path
    albedo_img.file_format = 'PNG'
    albedo_img.save()

    nt.links.new(old_link_socket, out_node.inputs["Surface"])  # restore bsdf hookup
    nt.nodes.remove(emission)

    # --- Normal: self-bake the Bump node's effect onto this object's
    # own UV (no Selected-to-Active needed — it's baking its own
    # procedural surface, not projecting from another mesh) ---
    normal_img = bpy.data.images.new("bark_normal", width=resolution, height=resolution)
    normal_img.colorspace_settings.name = 'Non-Color'
    normal_node = nt.nodes.new("ShaderNodeTexImage")
    normal_node.image = normal_img
    nt.nodes.active = normal_node

    bpy.ops.object.bake(type='NORMAL')
    normal_path = os.path.join(out_dir, "bark_normal.png")
    normal_img.filepath_raw = normal_path
    normal_img.file_format = 'PNG'
    normal_img.save()

    bpy.data.objects.remove(plane_obj, do_unlink=True)
    bpy.data.meshes.remove(mesh)
    bpy.data.materials.remove(mat)

    return albedo_path, normal_path


# =====================================================================
# Main
# =====================================================================

def main():
    cfg = CONFIG
    rng = random.Random(cfg["seed"])

    source_col = bpy.data.collections.get(cfg["source_collection"])
    if not source_col or not source_col.objects:
        print(f"[shader_gen] No objects found in '{cfg['source_collection']}'. Run step 1 first.")
        return

    out_dir = ensure_dir(cfg["output_dir"])
    billboard_col = get_or_create_collection(BILLBOARD_COLLECTION)
    for obj in list(billboard_col.objects):
        mesh = obj.data
        mat = obj.active_material
        bpy.data.objects.remove(obj, do_unlink=True)
        if mesh and mesh.users == 0:
            bpy.data.meshes.remove(mesh)
        if mat and mat.users == 0:
            bpy.data.materials.remove(mat)
    bpy.data.orphans_purge(do_local_ids=True, do_recursive=True)

    source_objs = [o for o in source_col.objects if o.get("is_branch_card")]

    for i, src_obj in enumerate(source_objs):
        src_obj.hide_viewport = False   # ensure visible for its own render pass
        src_obj.hide_render = False
        mat = build_branch_material(f"SourceLookMat_{i:02d}", rng, cfg)
        src_obj.data.materials.clear()
        src_obj.data.materials.append(mat)

        img_path, min_v, max_v = snapshot_branch(
            src_obj, i, out_dir, cfg["card_resolution"], cfg["bake_samples"]
        )
        card_obj = build_card_object(i, img_path, min_v, max_v, src_obj)
        billboard_col.objects.link(card_obj)  # must be in the view layer before it can be baked to

        if cfg["bake_normal_maps"]:
            # must happen before the backface is added (see build_card_object
            # / bake_normal_map docstrings — overlapping UVs break the bake)
            bake_normal_map(
                src_obj, card_obj, i, out_dir, cfg["card_resolution"],
                cfg["normal_bake_samples"], min_v, max_v
            )

        add_offset_backface(card_obj.data)

        print(f"[shader_gen] Snapshotted {src_obj.name} -> {card_obj.name} ({img_path})")

    if cfg["hide_source_branches"]:
        for obj in source_objs:
            obj.hide_viewport = True
            obj.hide_render = True

    if cfg["bake_bark_material"]:
        bark_albedo_path, bark_normal_path = bake_bark_textures(cfg, rng, out_dir)
        print(f"[shader_gen] Baked bark textures: {bark_albedo_path}, {bark_normal_path}")

    print(f"[shader_gen] Done. {len(source_objs)} billboard cards created in "
          f"'{BILLBOARD_COLLECTION}'. Textures in {out_dir}")
    print("[shader_gen] NEXT: run step 3 — it assembles trees from "
          f"'{BILLBOARD_COLLECTION}', not the original heavy geometry.")


if __name__ == "__main__":
    main()
