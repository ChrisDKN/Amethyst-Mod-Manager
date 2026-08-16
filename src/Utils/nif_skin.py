"""Pose skinned meshes against a skeleton, so a whole actor assembles.

A body is not one mesh. Skyrim builds an actor from separate nifs - head,
body, hands, feet, then whatever armour covers them - and each one's vertices
sit in ITS OWN bones' space, not a shared one. Rendering them raw piles them
on top of each other; placing each by its dominant bone (what Utils.nif_reader
does with no skeleton to hand) is right for a lone FaceGen head but collapses
a body, because feet, hands and torso each normalise into a different bone.

The skeleton is the missing piece. ``skeleton.nif`` holds every bone's world
transform, so a vertex reaches actor space by:

    v_actor = sum over bones( weight_i * BoneWorld_i * Bind_i * v )

Bind_i is the shape's own skin->bone transform (NiSkinData). Rigid shapes -
every FaceGen head part, bound to one bone - need no weights and are placed
whole, which is both cheaper and exactly what the engine does.

This is bind-pose posing, not animation: no controllers are read, so an actor
stands in the skeleton's rest pose.
"""
from __future__ import annotations

import math
from pathlib import Path

from Utils import nif_reader
from Utils.nif_reader import _Cur, _NODE_TYPES, _block_offsets, _world_transform

__all__ = ["read_skeleton", "skeleton_paths", "pose_model",
           "morph_weight_model", "IDENTITY"]

IDENTITY = ((0.0, 0.0, 0.0), (1, 0, 0, 0, 1, 0, 0, 0, 1), 1.0)

# Where an actor's skeleton lives, by the folder its meshes sit under.
_SKELETON_DIR = "meshes/actors/{race}/character assets/"
_DEFAULT_RACE = "character"


def skeleton_paths(race: str = _DEFAULT_RACE, female: bool = False
                   ) -> list[str]:
    """Candidate skeleton paths for an actor, best first."""
    base = _SKELETON_DIR.format(race=race or _DEFAULT_RACE)
    names = ["skeleton_female.nif", "skeleton.nif"] if female else \
            ["skeleton.nif"]
    out = [base + n for n in names]
    if (race or _DEFAULT_RACE) != _DEFAULT_RACE:
        out += skeleton_paths(_DEFAULT_RACE, female)
    return out


def read_skeleton(source) -> dict:
    """``{bone name: (translation, rotation, scale)}`` in actor space.

    *source* is a path or the raw bytes of a skeleton nif.
    """
    data = Path(source).read_bytes() if isinstance(source, (str, Path)) \
        else bytes(source)
    header = nif_reader.read_nif_header(data)
    offs = _block_offsets(header)
    count = min(len(offs), header.num_blocks)

    local: dict[int, dict] = {}
    parent: dict[int, int] = {}
    for i in range(count):
        if header.type_of(i) not in _NODE_TYPES:
            continue
        size = header.block_sizes[i]
        if size <= 0 or offs[i] + size > len(data):
            continue
        try:
            cur = _Cur(data[offs[i]:offs[i] + size])
            av = nif_reader._read_avobject(cur, header)
            local[i] = av
            for child in cur.refs():
                if child >= 0:
                    parent[child] = i
        except Exception:                                # noqa: BLE001
            continue

    out: dict[str, tuple] = {}
    for i, av in local.items():
        name = av.get("name") or ""
        if not name or name in out:
            continue
        try:
            out[name] = _world_transform(i, local, parent)
        except Exception:                                # noqa: BLE001
            continue
    return out


def morph_weight_model(high, low, weight: float) -> int:
    """Morph a Bethesda ``_1.nif`` model toward its ``_0.nif`` sibling.

    Armour Addons name only the high-weight endpoint; the game finds the low
    sibling by convention and interpolates it using NPC_ NAM7. FaceGen heads
    are already baked at that weight, so omitting this step changes the neck
    circumference and creates a real geometric seam.
    """
    if low is None:
        return 0
    weight = min(1.0, max(0.0, float(weight)))
    by_name: dict[str, list] = {}
    for shape in low.shapes:
        by_name.setdefault(shape.name, []).append(shape)
    changed = 0
    for index, shape in enumerate(high.shapes):
        candidates = by_name.get(shape.name, [])
        other = next((s for s in candidates
                      if len(s.vertices) == len(shape.vertices)), None)
        if other is None and index < len(low.shapes):
            candidate = low.shapes[index]
            if len(candidate.vertices) == len(shape.vertices):
                other = candidate
        if other is None or not shape.vertices:
            continue

        def lerp_vectors(a, b, normalise=False):
            if len(a) != len(b):
                return a
            out = []
            for high_v, low_v in zip(a, b):
                value = tuple(high_v[k] * weight
                              + low_v[k] * (1.0 - weight)
                              for k in range(3))
                if normalise:
                    mag = math.sqrt(sum(v * v for v in value))
                    if mag > 1e-12:
                        value = tuple(v / mag for v in value)
                out.append(value)
            return out

        shape.vertices = lerp_vectors(shape.vertices, other.vertices)
        shape.normals = lerp_vectors(shape.normals, other.normals, True)
        shape.tangents = lerp_vectors(shape.tangents, other.tangents, True)
        changed += 1
    return changed


def pose_model(model, skeleton: dict, attach: str = "") -> int:
    """Place every skinned shape of *model* in the skeleton's actor space.

    Returns how many shapes were posed. Shapes whose bones the skeleton does
    not name are left as the reader placed them, so a partial skeleton
    degrades one shape rather than scattering the rest.

    *attach* names a skeleton node for UNSKINNED shapes - a shield or weapon
    carries no bones at all and the engine hangs it off a node instead, so
    without this it stays wherever the mesh's own root put it, usually on the
    floor at the actor's feet.
    """
    if not skeleton:
        return 0
    hook = skeleton.get(attach) if attach else None
    posed = 0
    for shape in model.shapes:
        if not shape.bones or not shape.binds:
            if hook is not None:
                shape.translation, shape.rotation, shape.scale = _compose(
                    hook, (shape.translation, shape.rotation, shape.scale))
                posed += 1
            continue
        if _pose_shape(shape, skeleton):
            posed += 1
    return posed


def _pose_shape(shape, skeleton: dict) -> bool:
    resolved = [skeleton.get(name) for name in shape.bones]
    if not any(r is not None for r in resolved):
        return False
    resolved = _fill_missing_bones(shape, resolved)

    if not shape.skin_weights:
        # Rigid: one bone carries the whole shape, so compose the transforms
        # once instead of touching every vertex.
        idx = _dominant(shape, resolved)
        if idx is None:
            return False
        bone = resolved[idx]
        bind = shape.binds[idx]
        shape.translation, shape.rotation, shape.scale = _compose(bone, bind)
        return True

    out = []
    binds = shape.binds
    n_bones = min(len(binds), len(resolved))
    for i, vertex in enumerate(shape.vertices):
        if i >= len(shape.skin_weights):
            out.append(vertex)
            continue
        indices, weights = shape.skin_weights[i]
        acc = [0.0, 0.0, 0.0]
        total = 0.0
        for slot in range(len(indices)):
            weight = weights[slot]
            bone_i = indices[slot]
            if weight <= 0.0 or bone_i >= n_bones:
                continue
            bone = resolved[bone_i]
            if bone is None:
                continue
            moved = _apply(bone, _apply(binds[bone_i], vertex))
            acc[0] += weight * moved[0]
            acc[1] += weight * moved[1]
            acc[2] += weight * moved[2]
            total += weight
        if total <= 0.0:
            out.append(vertex)
        elif abs(total - 1.0) > 1e-3:
            # Weights that miss bones the skeleton lacks would shrink the
            # vertex toward the origin; renormalise onto what did resolve.
            out.append((acc[0] / total, acc[1] / total, acc[2] / total))
        else:
            out.append((acc[0], acc[1], acc[2]))
    shape.vertices = out
    shape.translation, shape.rotation, shape.scale = IDENTITY
    return True


def _fill_missing_bones(shape, resolved):
    """Stand in for bones the skeleton does not name, using their own bind.

    Physics hair (KS Hairdos and friends) rigs its strands to HDT bones that
    live in the WIG, not in the actor's skeleton: Carlotta's hair names 16
    such bones out of 17. Dropping them and renormalising onto the one bone
    that did resolve - the head - drags every strand up to the skull and the
    hair renders as a stretched smear down the body.

    The stand-in reproduces the shape's OWN skin-to-world map, which every
    RESOLVED bone agrees on for a rigidly-authored piece:

        skin_to_world = BoneWorld_j . Bind_j        (any resolved bone j)
        stand_in_i    = skin_to_world . Bind_i^-1

    so posing resolves to ``skin_to_world . v`` and the vertex lands where the
    shape was authored. Taking the resolved bone's WORLD transform alone -
    without its own bind - is what an earlier version did, and it is wrong the
    moment the shape is not authored in that bone's space: Serana's SMP robe
    anchors on NPC Spine2 (world z~91) while being authored in ACTOR space, so
    every cloak segment was lifted 91 units and the robe stretched to z=191
    with the head buried inside it. Including Bind_j makes that map identity,
    which is exactly what an actor-space mesh needs.
    """
    if all(r is not None for r in resolved):
        return resolved
    binds = shape.binds
    skin_to_world = next(
        (_compose(bone, binds[i])
         for i, bone in enumerate(resolved)
         if bone is not None and i < len(binds)), None)
    if skin_to_world is None:
        return resolved
    out = list(resolved)
    for i, bone in enumerate(out):
        if bone is not None or i >= len(binds):
            continue
        out[i] = _compose(skin_to_world, _invert(binds[i]) or IDENTITY)
    return out


def _invert(transform):
    """The inverse of a bind, or None if it is degenerate."""
    t, r, s = transform
    if not s:
        return None
    inv_s = 1.0 / s
    rt = (r[0], r[3], r[6], r[1], r[4], r[7], r[2], r[5], r[8])
    it = tuple(-inv_s * (rt[i * 3] * t[0] + rt[i * 3 + 1] * t[1]
                         + rt[i * 3 + 2] * t[2]) for i in range(3))
    return it, rt, inv_s


def _dominant(shape, resolved):
    """The resolved bone a rigid shape hangs from: the first that exists."""
    for i, bone in enumerate(resolved):
        if bone is not None and i < len(shape.binds):
            return i
    return None


def _apply(transform, v):
    t, r, s = transform
    x, y, z = v[0], v[1], v[2]
    return (t[0] + s * (r[0] * x + r[1] * y + r[2] * z),
            t[1] + s * (r[3] * x + r[4] * y + r[5] * z),
            t[2] + s * (r[6] * x + r[7] * y + r[8] * z))


def _compose(outer, inner):
    """The single transform equivalent to applying *inner* then *outer*."""
    ot, orr, os_ = outer
    it, ir, is_ = inner
    rot = nif_reader._mat_mul(orr, ir)
    scale = os_ * is_
    trans = _apply(outer, it)
    return trans, rot, scale
