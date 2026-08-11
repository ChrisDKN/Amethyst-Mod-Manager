"""
nif_preview.py
Panel-scoped 3D preview for .nif meshes (QOpenGLWidget, no new deps).

Parses off-thread via Utils.nif_reader, bakes world transforms into vertices,
and resolves textures through Utils.asset_resolver (what the game would load)
with archive/loose fallbacks. Starfield geometry is fetched from external
.mesh files. Meshes are Z-up; dragging turns the asset about +Z like a
turntable - the camera and lights stay put, so highlights sweep across the
surface as it spins.
"""

from __future__ import annotations

import array
import math
from itertools import chain
from pathlib import Path
from shiboken6 import VoidPtr

from PySide6.QtCore import QEvent, Qt, QTimer, Signal
from PySide6.QtGui import (
    QActionGroup, QColor, QMatrix4x4, QSurfaceFormat, QVector3D,
)
from PySide6.QtWidgets import (
    QComboBox, QLabel, QMenu, QSlider, QToolButton, QVBoxLayout, QWidget,
)

# The QtOpenGL* modules need libQt6OpenGL/libQt6OpenGLWidgets, which not every
# host has. Import must not be fatal: gl_status() decides whether any of this
# is usable, and the preview falls back to _NoGLViewport when it is not - the
# class bodies below are only ever *executed* when gl_status() says yes.
try:
    from PySide6.QtOpenGL import (
        QOpenGLBuffer, QOpenGLShader, QOpenGLShaderProgram, QOpenGLTexture,
        QOpenGLVersionFunctionsFactory, QOpenGLVersionProfile,
        QOpenGLVertexArrayObject,
    )
    from PySide6.QtOpenGLWidgets import QOpenGLWidget
except Exception:                                        # noqa: BLE001
    QOpenGLBuffer = QOpenGLShader = QOpenGLShaderProgram = None
    QOpenGLTexture = QOpenGLVersionFunctionsFactory = None
    QOpenGLVersionProfile = QOpenGLVertexArrayObject = None
    QOpenGLWidget = QWidget

from Utils.asset_resolver import DirCache as _DirCache
from gui_qt.eliding_label import ElidingLabel
from gui_qt.flow_layout import FlowLayout, enable_height_for_width
from gui_qt.gl_support import gl_status
from gui_qt.safe_emit import safe_emit
from gui_qt.theme_qt import active_palette, _c
from gui_qt.worker import LatestWorker

PREVIEW_EXTS = {".nif"}

# Cap decoded textures: a 4K RGBA QImage is 67 MB, and a dozen of those
# exhausts memory on a handheld.
TEXTURE_MAX_DIM = 1024

_IDENTITY_ROT = (1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0)

# Indexed asset subtrees. materials/ (FO4 texture paths live there) and
# geometries/ (Starfield meshes) are required, not optional.
ASSET_PREFIXES = ("textures/", "materials/", "geometries/")

# Backdrops; light default because many game textures are near-black.
BACKGROUNDS = {
    "light": "#d4d7db",
    "grey": "#8b8f94",
    "dark": "#2b2d30",
    "black": "#0b0b0c",
}
BACKGROUND_ORDER = ["light", "grey", "dark", "black"]

# Brightness is a gamma lift: 1.0 neutral, higher raises shadows. Stored as an
# int percent so it round-trips through the ini and the slider unchanged.
BRIGHTNESS_MIN, BRIGHTNESS_MAX, BRIGHTNESS_DEFAULT = 60, 260, 100

# The fixed camera's angles; also where the light rig is anchored.
_HOME_YAW = math.radians(-60.0)
_HOME_PITCH = math.radians(22.0)

# Wireframe: off, lines over the solid render, or lines only.
WIRE_OFF, WIRE_OVERLAY, WIRE_ONLY = "off", "overlay", "only"

# Texture slots that mean the same thing in Bethesda texture sets, FO4 .bgsm
# and Starfield .mat. Later slots differ per source, so they are not offered.
TEXTURE_SLOTS = (("diffuse", 0), ("normal", 1))

# PySide6 exposes no GL constant module; these are the standard values.
_GL_TRIANGLES = 0x0004
_GL_DEPTH_BUFFER_BIT = 0x0100
_GL_FRONT_AND_BACK = 0x0408
_GL_DEPTH_TEST = 0x0B71
_GL_UNSIGNED_INT = 0x1405
_GL_FLOAT = 0x1406
_GL_LINE = 0x1B01
_GL_FILL = 0x1B02
_GL_BACK = 0x0405
_GL_CULL_FACE = 0x0B44
_GL_POLYGON_OFFSET_LINE = 0x2A02
_GL_COLOR_BUFFER_BIT = 0x4000
_GL_BLEND = 0x0BE2
_GL_SRC_ALPHA = 0x0302
_GL_ONE_MINUS_SRC_ALPHA = 0x0303

# PySide6 binds glDrawElements' `indices` as a real pointer, so an integer 0 is
# rejected; with an element buffer bound it must be a null VoidPtr offset.
_NULL_OFFSET = VoidPtr(0)

_VERT_SRC = """#version 330 core
layout(location = 0) in vec3 aPos;
layout(location = 1) in vec3 aNormal;
layout(location = 2) in vec2 aUV;
layout(location = 3) in vec3 aTangent;
layout(location = 4) in vec4 aColor;
uniform mat4 uMVP;
out vec3 vNormal;
out vec2 vUV;
out vec3 vTangent;
out vec3 vWorld;
out vec4 vColor;
void main() {
    vNormal = aNormal;
    vTangent = aTangent;
    vColor = aColor;
    vWorld = aPos;          // positions are already baked to world space
    // NO V flip: QOpenGLTexture(QImage) already mirrors on upload; flipping
    // again samples the wrong atlas island.
    vUV = aUV;
    gl_Position = uMVP * vec4(aPos, 1.0);
}
"""

_FRAG_SRC = """#version 330 core
in vec3 vNormal;
in vec2 vUV;
in vec3 vTangent;
in vec3 vWorld;
in vec4 vColor;
// Only 1 where the mesh HAS colours and SLSF2_Vertex_Colors is set - plenty
// of meshes carry a stale colour array the engine ignores.
uniform float uHasVColor;
// Runtime colour multiply (FaceGen hair). White for everything else.
uniform vec3 uTint;
// TruePBR ambient occlusion - blue channel of the _rmaos map. We do not
// implement PBR shading, but AO is a plain multiply and without it recessed
// detail (roof shingles, beam joints) washes out.
uniform sampler2D uAoTex;
uniform float uHasAo;
// 1 when the diffuse DDS declared an sRGB format (PBR packs do; legacy
// Skyrim textures never declare one). Those meshes get a colour-managed
// path - decode to linear, light, tonemap, re-encode - which is why their
// mid-tones stop washing out. Legacy meshes keep the BodySlide behaviour.
uniform float uSrgbAlbedo;
// uHasTex is a float: PySide6 setUniformValue silently misses int uniforms.
uniform sampler2D uTex;
uniform sampler2D uNormTex;
uniform float uHasTex;
uniform float uHasNorm;      // 0 none, 1 tangent-space, 2 model-space (_msn)
uniform float uSpecular;
uniform vec3 uBaseColor;
uniform vec3 uEye;
uniform float uGamma;
uniform float uFlat;
// Cut-out geometry (fur, hair, foliage): < 0 disables the test entirely.
uniform float uAlphaThreshold;
// 1 on the blended pass, so the diffuse map's alpha reaches the framebuffer.
uniform float uBlend;
out vec4 FragColor;

// BodySlide's rig (GLSurface::InitLighting): three directional lights plus a
// camera-locked frontal one, each adding its own share of ambient. BodySlide
// states the directions in ITS world - Y-up, +Z toward the default camera -
// so they are camera coordinates in all but name. Python rotates them onto
// the HOME camera basis (uLd*), then leaves them fixed in the world: the
// drag reads as the asset turning under still lights, not the viewer flying
// around it. Dropping them into our Z-up model world unrotated put the two
// front lights overhead and the backlight underneath.
const float AMBIENT = 0.2;
uniform vec3 uLd0;
uniform vec3 uLd1;
uniform vec3 uLd2;
uniform vec3 uCamRight;
uniform vec3 uCamUp;

// Environment map (Skyrim shader type 1). BodySlide samples a cubemap with
// the reflection vector; ours samples one face through a sphere-map lookup -
// chrome env maps are soft gradients, so the difference is not visible.
uniform sampler2D uEnvTex;
uniform sampler2D uMaskTex;
uniform float uEnvScale;
uniform float uHasMask;

// Per-mesh material, from the NIF's BSLightingShaderProperty.
uniform vec3 uSpecColor;
uniform float uSpecStrength;
uniform float uShininess;

// Uncharted-2 filmic curve, applied exactly as BodySlide applies it.
vec3 tonemap(vec3 x) {
    const float A = 0.15, B = 0.50, C = 0.10, D = 0.20, E = 0.02, F = 0.30;
    return ((x * (A * x + C * B) + D * E) / (x * (A * x + B) + D * F)) - E / F;
}

void addLight(vec3 dir, float diffuse, vec3 n, vec3 v, float gloss,
              inout vec3 lit, inout vec3 spec) {
    float ndl = max(dot(n, dir), 0.0);
    float ndh = max(dot(n, normalize(dir + v)), 0.0);
    lit += AMBIENT + ndl * diffuse;
    spec += clamp(uSpecColor * uSpecStrength * gloss
                  * pow(ndh, uShininess), 0.0, 1.0) * diffuse;
}

void main() {
    // Wireframe overlay pass: unlit, ungraded, so lines stay legible.
    if (uFlat > 0.5) { FragColor = vec4(uBaseColor, 1.0); return; }

    // Cut out before any shading work: discarded fragments write no depth,
    // so alpha-TESTED meshes need no sorting.
    vec4 texel = uHasTex > 0.5 ? texture(uTex, vUV) : vec4(uBaseColor, 1.0);
    // BodySlide multiplies the vertex colour into BOTH albedo and alpha, and
    // the engine alpha-tests the combined value.
    if (uHasVColor > 0.5) texel *= vColor;
    if (uAlphaThreshold >= 0.0 && texel.a <= uAlphaThreshold) discard;

    vec3 n = normalize(vNormal);
    float gloss = 0.0;
    if (uHasNorm > 1.5) {
        // Model-space map: the texel IS the normal, no tangent basis. Red is
        // inverted (as in BodySlide's shader), and gloss comes from the
        // dedicated specular map packed into alpha, never the map's own alpha.
        vec4 nm = texture(uNormTex, vUV);
        n = normalize(nm.rgb * 2.0 - 1.0);
        n.r = -n.r;
        gloss = nm.a;
    } else if (uHasNorm > 0.5) {
        vec4 nm = texture(uNormTex, vUV);
        vec3 t = vTangent - n * dot(n, vTangent);   // Gram-Schmidt
        if (length(t) > 1e-4) {
            t = normalize(t);
            mat3 tbn = mat3(t, cross(n, t), n);
            n = normalize(tbn * (nm.rgb * 2.0 - 1.0));
        }
        // Skyrim keeps its gloss/spec mask in the normal map's ALPHA.
        gloss = nm.a;
    }
    gloss *= uSpecular;

    vec3 v = normalize(uEye - vWorld);
    vec3 albedo = texel.rgb;
    if (uSrgbAlbedo > 0.5) albedo = pow(albedo, vec3(2.2));
    albedo *= uTint;
    if (uHasAo > 0.5) albedo *= texture(uAoTex, vUV).b;

    if (uEnvScale > 0.0) {
        vec3 rfl = reflect(-v, n);
        vec2 suv = vec2(dot(rfl, uCamRight), -dot(rfl, uCamUp)) * 0.5 + 0.5;
        // No env mask -> fall back to the spec factor, as BodySlide does.
        float m = uHasMask > 0.5 ? texture(uMaskTex, vUV).r : gloss;
        albedo += texture(uEnvTex, suv).rgb * uEnvScale * m;
    }

    vec3 lit = vec3(0.0);
    vec3 spec = vec3(0.0);
    addLight(v, 0.20, n, v, gloss, lit, spec);
    addLight(uLd0, 0.60, n, v, gloss, lit, spec);
    addLight(uLd1, 0.60, n, v, gloss, lit, spec);
    addLight(uLd2, 0.85, n, v, gloss, lit, spec);

    vec3 col = tonemap(albedo * lit + spec) / tonemap(vec3(1.0));
    if (uSrgbAlbedo > 0.5) col = pow(max(col, 0.0), vec3(1.0 / 2.2));
    // Gamma lift, not a multiply: raises shadows (near-black leather/metal)
    // without blowing highlights to white.
    FragColor = vec4(pow(clamp(col, 0.0, 1.0), vec3(1.0 / uGamma)),
                     mix(1.0, texel.a, uBlend));
}
"""


class _Mesh:
    """One shape's CPU-side buffers, built off-thread and uploaded on demand."""

    __slots__ = ("name", "verts", "indices", "image", "has_image", "tri_count",
                 "normal_image", "model_space_normals", "spec",
                 "env_image", "mask_image", "env_scale",
                 "alpha_threshold", "alpha_blend", "center", "has_colors",
                 "tint", "ao_image", "ao_tex", "srgb_albedo",
                 "vao", "vbo", "ibo", "texture", "normal_tex",
                 "env_tex", "mask_tex")

    def __init__(self, name, verts, indices, image, tri_count,
                 normal_image=None, model_space_normals=False, spec=None,
                 env_image=None, mask_image=None, env_scale=0.0,
                 alpha_threshold=-1.0, alpha_blend=False, center=(0.0, 0.0, 0.0),
                 has_colors=False, tint=(1.0, 1.0, 1.0), ao_image=None,
                 srgb_albedo=False):
        self.name = name
        self.verts = verts
        self.indices = indices
        self.image = image
        # Kept because `image` is dropped once the texture is on the GPU.
        self.has_image = image is not None
        self.normal_image = normal_image
        self.model_space_normals = model_space_normals
        # (r, g, b, strength, shininess) for the shader's specular term.
        self.spec = spec or (1.0, 1.0, 1.0, 1.0, 80.0)
        self.env_image = env_image
        self.mask_image = mask_image
        self.env_scale = env_scale
        # < 0 = no alpha test. Blended meshes draw last, back to front.
        self.alpha_threshold = alpha_threshold
        self.alpha_blend = alpha_blend
        self.center = center
        # Widens the vertex from 11 floats to 15; only meshes that use it pay.
        self.has_colors = has_colors
        # Runtime colour multiply (FaceGen hair); white otherwise.
        self.tint = tint
        self.ao_image = ao_image
        self.ao_tex = None
        # The DDS declared an sRGB format, so its values need
        # linearising before lighting. Legacy files never say.
        self.srgb_albedo = srgb_albedo
        self.tri_count = tri_count
        self.vao = self.vbo = self.ibo = None
        self.texture = self.normal_tex = None
        self.env_tex = self.mask_tex = None


def _face_normals(verts: list, tris: list) -> list:
    """Derive per-vertex normals when a mesh ships without them."""
    acc = [[0.0, 0.0, 0.0] for _ in verts]
    n = len(verts)
    for a, b, c in tris:
        if a >= n or b >= n or c >= n:
            continue
        ax, ay, az = verts[a]
        bx, by, bz = verts[b]
        cx, cy, cz = verts[c]
        ux, uy, uz = bx - ax, by - ay, bz - az
        vx, vy, vz = cx - ax, cy - ay, cz - az
        nx, ny, nz = uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx
        for i in (a, b, c):
            t = acc[i]
            t[0] += nx
            t[1] += ny
            t[2] += nz
    out = []
    for x, y, z in acc:
        m = math.sqrt(x * x + y * y + z * z)
        out.append((x / m, y / m, z / m) if m > 1e-12 else (0.0, 0.0, 1.0))
    return out


def _fit_texture(img):
    """Cap a decoded texture at TEXTURE_MAX_DIM (memory, not cosmetics)."""
    if img is None or img.isNull():
        return img
    big = max(img.width(), img.height())
    if big <= TEXTURE_MAX_DIM:
        return img
    return img.scaled(
        img.width() * TEXTURE_MAX_DIM // big,
        img.height() * TEXTURE_MAX_DIM // big,
        Qt.KeepAspectRatio, Qt.SmoothTransformation)


def _model_space_normal(nrm_blob, spec_blob, log=None):
    """Decode an _msn normal map, packing a separate spec map into its alpha.

    A model-space map's alpha is NOT a gloss mask (it is usually solid 255).
    Skyrim keeps skin gloss in a dedicated specular map (texture slot 7) and
    uses its RED channel, per BodySlide's own shader. Packing that into alpha
    lets the shader read gloss from one sampler for both map types; with no
    spec map the alpha is zeroed, so skin renders matte rather than glossy.
    """
    try:
        import io
        from PIL import Image as PilImage
        from PySide6.QtGui import QImage
        from Utils.dds_compat import sanitise_dds, skip_dds_mips
        nrm_blob = skip_dds_mips(nrm_blob, TEXTURE_MAX_DIM)
        with PilImage.open(io.BytesIO(sanitise_dds(nrm_blob))) as im:
            big = max(im.width, im.height)
            if big > TEXTURE_MAX_DIM:
                im = im.reduce(max(1, big // TEXTURE_MAX_DIM))
            rgb = im.convert("RGB")
        if spec_blob:
            spec_blob = skip_dds_mips(spec_blob, TEXTURE_MAX_DIM)
            with PilImage.open(io.BytesIO(sanitise_dds(spec_blob))) as sp:
                gloss = sp.convert("RGB").split()[0]        # red channel
                if gloss.size != rgb.size:
                    gloss = gloss.resize(rgb.size)
        else:
            gloss = PilImage.new("L", rgb.size, 0)
        rgb.putalpha(gloss)
        raw = rgb.tobytes("raw", "RGBA")
        return QImage(raw, rgb.width, rgb.height,
                      QImage.Format_RGBA8888).copy()
    except Exception as exc:                             # noqa: BLE001
        _log(log, f"      ! model-space normal decode failed: {exc!r}")
        return None


def _make_gl_texture(img):
    """Upload *img* explicitly rather than via QOpenGLTexture(QImage).

    The convenience constructor PREMULTIPLIES by alpha, which silently scales
    RGB down. That ruins any texture whose alpha is meaningful - Skyrim keeps
    its gloss mask in the normal map's alpha, so a normal map came back ~5x too
    dark and the surface barely responded to lighting.
    """
    if img is None or img.isNull():
        return None
    from PySide6.QtGui import QImage
    src = img.convertToFormat(QImage.Format_RGBA8888)
    tex = QOpenGLTexture(QOpenGLTexture.Target2D)
    tex.setSize(src.width(), src.height())
    tex.setFormat(QOpenGLTexture.RGBA8_UNorm)
    tex.setMipLevels(tex.maximumMipLevels())
    tex.allocateStorage()
    tex.setData(QOpenGLTexture.RGBA, QOpenGLTexture.UInt8, src.constBits())
    tex.generateMipMaps()
    tex.setMinificationFilter(QOpenGLTexture.LinearMipMapLinear)
    tex.setMagnificationFilter(QOpenGLTexture.Linear)
    tex.setWrapMode(QOpenGLTexture.Repeat)
    return tex


def _qimage_from_bytes(data: bytes, log=None):
    """Decode texture bytes pulled from an archive (DDS goes via Pillow)."""
    from PySide6.QtGui import QImage
    from Utils.dds_compat import sanitise_dds, skip_dds_mips
    # A DDS ships its own mip chain: decode the first level that fits the
    # cap rather than a 4K top mip (~400ms of BC7) we would only shrink.
    data = skip_dds_mips(data, TEXTURE_MAX_DIM)
    img = QImage()
    if img.loadFromData(data) and not img.isNull():
        return img
    try:
        import io
        from PIL import Image as PilImage
        with PilImage.open(io.BytesIO(sanitise_dds(data))) as im:
            # Reduce BEFORE convert so the full-size RGBA never hits the heap.
            big = max(im.width, im.height)
            if big > TEXTURE_MAX_DIM:
                im = im.reduce(max(1, big // TEXTURE_MAX_DIM))
            im = im.convert("RGBA")
            raw = im.tobytes("raw", "RGBA")
            return QImage(raw, im.width, im.height,
                          QImage.Format_RGBA8888).copy()
    except Exception as exc:                             # noqa: BLE001
        _log(log, f"      ! image decode failed ({_fmt_bytes(len(data))}): {exc!r}")
        return None


def _make_texture_loader(texture_roots: list[Path], archives=None, resolver=None,
                         override=None, slot: int = 0, log=None, cancel=None):
    """Return ``shape -> QImage|None``; resolver first, then roots/archives.

    FO4/Starfield shapes name a material file whose textures override the
    mesh's own (usually empty) texture set. *override* (``rel -> bytes|None``)
    is consulted before everything else; requested paths are recorded on
    ``load.requested``.
    """
    cache = _DirCache()
    seen: dict[str, object] = {}
    # Which diffuse maps DECLARE an sRGB DXGI format (PBR packs do).
    srgb_albedo: dict[str, bool] = {}
    materials: dict[str, object] = {}
    requested: list[str] = []
    requested_seen: set[str] = set()
    missing: list[str] = []

    def note_request(rel: str) -> None:
        key = rel.replace("\\", "/").lower().strip().lstrip("/")
        if key.startswith("data/"):
            key = key[5:]
        if not key.startswith(("textures/", "materials/")):
            if key.endswith((".bgsm", ".bgem", ".mat")):
                key = "materials/" + key
            elif key.endswith((".dds", ".tga", ".png", ".bmp", ".jpg",
                               ".jpeg")):
                key = "textures/" + key
            else:
                return
        if key not in requested_seen:
            requested_seen.add(key)
            requested.append(key)

    def fetch(rel: str):
        """Raw bytes for a data-relative path; retries with textures/ prefix."""
        if not rel or (cancel is not None and cancel()):
            return None
        blob = _fetch_exact(rel)
        if blob is not None:
            return blob
        low = rel.replace("\\", "/").lower()
        if not low.startswith(("textures/", "materials/", "data/")):
            blob = _fetch_exact("textures/" + rel)
            if blob is not None:
                return blob
        # Recorded so the build summary can list exactly what went unfound -
        # the usual reason a mesh previews as untextured clay.
        if rel not in missing:
            missing.append(rel)
            _log(log, f"      MISS {rel.replace(chr(92), '/')} (searched: "
                      f"{'override, ' if override else ''}"
                      f"{'resolver' if resolver else f'{len(texture_roots)} loose root(s)'}"
                      f"{', archives' if archives is not None else ''})")
        return None

    def _fetch_exact(rel: str):
        # Texture-source switching must include the material and every map it
        # caused us to fetch, not just the diffuse map.  External Starfield
        # geometry deliberately stays out of this texture-only picker.
        note_request(rel)
        if override is not None:
            blob = override(rel)
            if blob:
                _log(log, f"      hit  {rel.replace(chr(92), '/')} <- source override "
                          f"({_fmt_bytes(len(blob))})")
                return blob
        if resolver is not None:
            blob = resolver.read(rel)
            if blob:
                _log(log, f"      hit  {rel.replace(chr(92), '/')} <- resolver "
                          f"({_fmt_bytes(len(blob))})")
                return blob
        else:
            # Loose files win over archives, matching what the engine loads.
            for root in texture_roots:
                found = cache.resolve(root, rel)
                if found is not None:
                    try:
                        data = found.read_bytes()
                    except OSError as exc:
                        _log(log, f"      ! {rel.replace(chr(92), '/')} found at {found} but "
                                  f"unreadable: {exc}")
                        return None
                    _log(log, f"      hit  {rel.replace(chr(92), '/')} <- {found} "
                              f"({_fmt_bytes(len(data))})")
                    return data
        if archives is not None:
            # Only source for a mesh previewed out of an uninstalled mod's archive.
            blob = archives.read(rel)
            if blob:
                _log(log, f"      hit  {rel.replace(chr(92), '/')} <- archive "
                          f"({_fmt_bytes(len(blob))})")
            return blob
        return None

    def material_slot(rel: str) -> str:
        key = rel.lower()
        if key not in materials:
            from Utils.bgsm_reader import read_material
            blob = fetch(rel)
            materials[key] = read_material(blob) if blob else None
        mat = materials[key]
        if mat is None:
            return ""
        if slot == 0:
            return mat.diffuse          # skips leading empty slots
        return mat.paths[slot] if slot < len(mat.paths) else ""

    def shape_slot(shape) -> str:
        if slot == 0:
            return shape.diffuse
        return shape.textures[slot] if slot < len(shape.textures) else ""

    def normal_map(shape):
        """(QImage|None, model_space) for the shape's normal map (slot 1)."""
        rel = ""
        if shape.material:
            mat = materials.get(shape.material.lower())
            if mat is None:
                material_slot(shape.material)      # populates the cache
                mat = materials.get(shape.material.lower())
            if mat is not None and len(mat.paths) > 1:
                rel = mat.paths[1]
        if not rel and len(shape.textures) > 1:
            rel = shape.textures[1]
        if not rel:
            return None, False
        # Bethesda body maps are MODEL space (_msn); they must not be run
        # through a tangent basis.
        model_space = rel.replace("\\", "/").lower().endswith("msn.dds")
        key = "N:" + rel.lower()
        if key in seen:
            return seen[key], model_space
        blob = fetch(rel)
        if blob and model_space:
            # Skyrim slot 7 is the dedicated specular map for skin.
            spec_rel = shape.textures[7] if len(shape.textures) > 7 else ""
            img = _model_space_normal(blob, fetch(spec_rel) if spec_rel else None, log)
        else:
            img = _qimage_from_bytes(blob, log) if blob else None
        if img is not None and img.isNull():
            img = None
        if blob and img is None:
            _log(log, f"      ! normal map {rel.replace(chr(92), '/')} found but not decodable")
        elif img is not None:
            _log(log, f"      normal {img.width()}x{img.height()}"
                      f"{' model-space (_msn)' if model_space else ' tangent-space'}")
        img = _fit_texture(img)
        seen[key] = img
        return img, model_space

    def env_maps(shape):
        """(env QImage|None, mask QImage|None) for an env-mapped shape.

        Slot 4 is a cubemap; Pillow decodes its first face, which is enough
        for the sphere-map approximation the shader uses. Slot 5 masks it.
        """
        if shape.env_map_scale <= 0.0 or len(shape.textures) <= 4:
            return None, None
        out = []
        for idx in (4, 5):
            rel = shape.textures[idx] if idx < len(shape.textures) else ""
            if not rel:
                out.append(None)
                continue
            key = "E:" + rel.lower()
            if key not in seen:
                blob = fetch(rel)
                img = _qimage_from_bytes(blob, log) if blob else None
                if img is not None and img.isNull():
                    img = None
                seen[key] = _fit_texture(img)
            out.append(seen[key])
        return out[0], out[1]

    def ao_map(shape):
        """TruePBR ambient occlusion: the BLUE channel of slot 5's _rmaos.

        Roughness/metallic/subsurface in the other channels need real PBR
        shading, but AO is a plain multiply and it is what keeps recessed
        detail (roof shingles, beam joints) from washing out.
        """
        if not shape.pbr or len(shape.textures) <= 5:
            return None
        rel = shape.textures[5]
        if not rel:
            return None
        key = "AO:" + rel.lower()
        if key not in seen:
            blob = fetch(rel)
            img = _qimage_from_bytes(blob, log) if blob else None
            if img is not None and img.isNull():
                img = None
            seen[key] = _fit_texture(img)
        return seen[key]

    def diffuse_key(shape) -> str:
        """The cache key for a shape's diffuse - what fetch() ends up asking."""
        rel = material_slot(shape.material) if shape.material else ""
        if not rel:
            rel = shape_slot(shape)
        if not rel:
            return ""
        key = rel.replace("\\", "/").lower()
        if not key.startswith(("textures/", "materials/", "data/")):
            key = "textures/" + key
        return key

    def load(shape):
        rel = material_slot(shape.material) if shape.material else ""
        if not rel:
            rel = shape_slot(shape)
        if not rel:
            return None
        key = diffuse_key(shape)
        if key in seen:
            return seen[key]
        blob = fetch(rel)
        image = _qimage_from_bytes(blob, log) if blob else None
        if image is not None and image.isNull():
            image = None
        if blob and image is None:
            _log(log, f"      ! {rel.replace(chr(92), '/')} was found but could NOT be decoded "
                      f"({_fmt_bytes(len(blob))}) - unsupported DDS format?")
        pre = (image.width(), image.height()) if image is not None else None
        image = _fit_texture(image)
        if blob:
            from Utils.dds_compat import is_srgb_dds
            srgb_albedo[key] = is_srgb_dds(blob)
            if image is not None:
                shrunk = ("" if pre == (image.width(), image.height())
                          else f" (downscaled from {pre[0]}x{pre[1]})")
                _log(log, f"      diffuse {image.width()}x{image.height()}"
                          f"{shrunk}"
                          f"{' sRGB' if srgb_albedo[key] else ''}")
        seen[key] = image
        return image

    load.fetch = fetch
    load.requested = requested
    load.normal_map = normal_map
    load.env_maps = env_maps
    load.ao_map = ao_map
    load.is_srgb = lambda shape: srgb_albedo.get(diffuse_key(shape), False)
    load.missing = missing
    return load


def _load_external_geometry(model, fetch):
    """Fill in Starfield shapes: geometry lives in geometries/<path>.mesh."""
    from Utils.sf_mesh_reader import read_sf_mesh
    cache: dict[str, object] = {}
    for shape in model.shapes:
        if shape.vertices or not shape.mesh_path:
            continue
        rel = "geometries/" + shape.mesh_path.replace("\\", "/") + ".mesh"
        key = rel.lower()
        if key not in cache:
            blob = fetch(rel)
            cache[key] = read_sf_mesh(blob) if blob else None
        mesh = cache[key]
        if mesh is None:
            continue
        shape.vertices = mesh.vertices
        shape.uvs = mesh.uvs
        shape.triangles = mesh.triangles


def _build_meshes(model, load_texture, cancel=None):
    """Bake world transforms and interleave into GL-ready buffers."""
    meshes: list[_Mesh] = []
    lo = [float("inf")] * 3
    hi = [float("-inf")] * 3

    for shape in model.shapes:
        if cancel is not None and cancel():
            return [], None
        verts = shape.vertices
        tris = shape.triangles
        if not verts or not tris:
            continue
        normals = shape.normals
        if len(normals) != len(verts):
            normals = _face_normals(verts, tris)
        uvs = shape.uvs
        if len(uvs) != len(verts):
            uvs = [(0.0, 0.0)] * len(verts)
        # No tangents (Skyrim LE data blocks, Starfield, unskinned bodies) just
        # means no normal mapping for that shape; the shader falls back.
        tangents = shape.tangents
        if len(tangents) != len(verts):
            tangents = [(0.0, 0.0, 0.0)] * len(verts)
        # The engine ignores the colour array unless SLSF2_Vertex_Colors is
        # set, and 405 shapes in one real load order carry a stale one.
        use_colors = shape.vertex_colors and len(shape.colors) == len(verts)
        colors = shape.colors if use_colors else None

        tx, ty, tz = shape.translation
        r = shape.rotation
        s = shape.scale
        r0, r1, r2, r3, r4, r5, r6, r7, r8 = r
        # Transform in whole-list passes; the identity case (most statics)
        # reuses the parsed lists untouched.
        if r == _IDENTITY_ROT and s == 1.0:
            if (tx, ty, tz) == (0.0, 0.0, 0.0):
                wverts = verts
            else:
                wverts = [(x + tx, y + ty, z + tz) for x, y, z in verts]
            wnorms = normals
            wtans = tangents
        else:
            wverts = [(tx + s * (r0 * x + r1 * y + r2 * z),
                       ty + s * (r3 * x + r4 * y + r5 * z),
                       tz + s * (r6 * x + r7 * y + r8 * z))
                      for x, y, z in verts]
            wnorms = [(r0 * x + r1 * y + r2 * z,
                       r3 * x + r4 * y + r5 * z,
                       r6 * x + r7 * y + r8 * z)
                      for x, y, z in normals]
            wtans = [(r0 * x + r1 * y + r2 * z,
                      r3 * x + r4 * y + r5 * z,
                      r6 * x + r7 * y + r8 * z)
                     for x, y, z in tangents]

        # Interleave pos/normal/uv/tangent(/colour) without a per-vertex
        # Python loop: chain flattens the zipped tuples at C speed.
        groups = (zip(wverts, wnorms, uvs, wtans, colors) if colors is not None
                  else zip(wverts, wnorms, uvs, wtans))
        flat = array.array("f", chain.from_iterable(
            chain.from_iterable(groups)))

        # Bounds from strided slices of the final buffer (C speed); the
        # per-shape centroid orders the blended pass.
        fpv = 15 if colors is not None else 11
        xs, ys, zs = flat[0::fpv], flat[1::fpv], flat[2::fpv]
        mlo = (min(xs), min(ys), min(zs))
        mhi = (max(xs), max(ys), max(zs))
        for k in range(3):
            if mlo[k] < lo[k]:
                lo[k] = mlo[k]
            if mhi[k] > hi[k]:
                hi[k] = mhi[k]

        nv = len(verts)
        idx = array.array("I", chain.from_iterable(tris))
        if idx and max(idx) >= nv:
            # Rare corrupt file: drop only the out-of-range triangles.
            idx = array.array("I")
            for a, b, c in tris:
                if a < nv and b < nv and c < nv:
                    idx.extend((a, b, c))
        if not idx:
            continue

        image = load_texture(shape)
        nrm_img, model_space = (load_texture.normal_map(shape)
                                if hasattr(load_texture, "normal_map")
                                else (None, False))
        # BodySlide drives its highlight from these material fields; a mesh
        # with SLSF1_Specular off renders matte (strength 0).
        strength = shape.spec_strength if shape.spec_enabled else 0.0
        # TruePBR repurposes the whole block: glossiness reads 0, the normal
        # map's alpha is not a gloss mask, and slot 5 is _rmaos rather than an
        # env mask. We do not implement PBR, so shade those diffuse-only
        # instead of feeding junk into the Blinn-Phong lobe.
        if shape.pbr or shape.glossiness < 1.0:
            strength = 0.0
        spec = (*shape.spec_color, strength, max(1.0, shape.glossiness))
        env_img, mask_img = (load_texture.env_maps(shape)
                             if hasattr(load_texture, "env_maps") and not shape.pbr
                             else (None, None))
        # NiAlphaProperty thresholds are 0-255; GL compares against 0-1.
        # Only useful with a texture - an untextured shape has alpha 1.
        thr = (shape.alpha_threshold / 255.0
               if shape.alpha_test and image is not None else -1.0)
        centre = tuple((mlo[k] + mhi[k]) * 0.5 for k in range(3))
        meshes.append(_Mesh(shape.name, flat, idx, image, len(idx) // 3,
                            nrm_img, model_space, spec,
                            env_img, mask_img,
                            shape.env_map_scale if env_img else 0.0,
                            thr, shape.alpha_blend and image is not None,
                            centre, colors is not None, shape.tint,
                            load_texture.ao_map(shape)
                            if hasattr(load_texture, 'ao_map') else None,
                            bool(load_texture.is_srgb(shape))
                            if hasattr(load_texture, 'is_srgb') else False))

    if not meshes:
        return [], None
    bounds = (tuple(lo), tuple(hi))
    return meshes, bounds


def _neutralise_meshes(meshes) -> None:
    """Sever GL wrappers whose context is gone: QOpenGLTexture's destructor
    dereferences its creation context (areSharing), so letting GC run it after
    the context died segfaults. invalidate() leaks the tiny C++ shell instead;
    the GPU memory goes with the context's share group."""
    import shiboken6
    for m in meshes:
        for obj in (m.vao, m.vbo, m.ibo, m.texture, m.normal_tex,
                    m.env_tex, m.mask_tex, m.ao_tex):
            if obj is not None:
                try:
                    shiboken6.invalidate(obj)
                except Exception:                        # noqa: BLE001
                    pass
        m.vao = m.vbo = m.ibo = m.texture = m.normal_tex = None
        m.env_tex = m.mask_tex = m.ao_tex = None


def _log(fn, message: str) -> None:
    """Emit one diagnostic line, tagged so the viewer's chatter is filterable.

    Logging must never break a preview, and this is called from the parse
    worker as well as the GUI thread.
    """
    if fn is None:
        return
    try:
        fn(f"NIF: {message}")
    except Exception:                                    # noqa: BLE001
        pass


def _fmt_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024.0
    return f"{n:.1f}GB"


def _model_cache_key(source, texture_roots, archive_roots, resolver, archives,
                     mesh_rel, plugin_dirs):
    """Identity of every input that can change the parsed/augmented model.

    Texture choice and texture slot are deliberately absent: they only affect
    decoded images and are the common reason for rebuilding the preview.
    """
    if isinstance(source, (bytes, bytearray)):
        source_key = ("memory", id(source), len(source))
    else:
        path = Path(source)
        try:
            stat = path.stat()
            source_key = ("path", str(path), stat.st_mtime_ns, stat.st_size)
        except OSError:
            source_key = ("path", str(path), None, None)

    def path_key(paths):
        return tuple(str(Path(p)) for p in (paths or ()))

    effective_plugins = plugin_dirs or texture_roots
    return (source_key, mesh_rel.replace("\\", "/").lower(),
            path_key(texture_roots), path_key(archive_roots),
            path_key(effective_plugins), id(resolver), id(archives))


def _log_model(log, model) -> None:
    """Report the parsed NIF: header, shapes and anything skipped."""
    h = model.header
    _log(log, f"  header: version 0x{h.version:08X} bs_version {h.bs_version}"
              f" · {h.num_blocks} blocks · {len(h.block_types)} block types"
              f" · {len(model.shapes)} shape(s)")
    if model.skipped:
        worst = sorted(model.skipped.items(), key=lambda kv: -kv[1])
        _log(log, "  block types not read (normal for skin/collision/"
                  "animation data): "
                  + ", ".join(f"{k} x{v}" for k, v in worst[:8]))
    if not model.shapes:
        _log(log, "  ! no shapes - nothing to draw. Either the file has no"
                  " renderable geometry or its block layout is unsupported.")
        # Files older than 20.2.0.5 are walked block by block against nif.xml;
        # say where that stopped rather than just "unsupported". Reporting must
        # never be what breaks the viewer, so tolerate a model without it.
        walk_error = getattr(model, "walk_error", "")
        if walk_error:
            _log(log, f"  ! block walk failed: {walk_error}")
    for s in model.shapes:
        flags = []
        if s.pbr:
            flags.append("PBR")
        if s.vertex_colors:
            flags.append("vcolor" if s.colors else "vcolor-flag-no-data")
        elif s.colors:
            flags.append("vcolor-data-ignored")
        if s.alpha_test:
            flags.append(f"alpha-test>{s.alpha_threshold}")
        if s.alpha_blend:
            flags.append("alpha-blend")
        if s.env_map_scale:
            flags.append(f"envmap x{s.env_map_scale:.2f}")
        if s.material:
            flags.append(f"material={s.material}")
        if s.mesh_path:
            flags.append(f"geom={s.mesh_path}")
        _log(log, f"    shape {s.name!r} [{s.block_type}] "
                  f"{len(s.vertices)}v/{len(s.triangles)}t"
                  f" · gloss {s.glossiness:.0f} spec {s.spec_strength:.2f}"
                  f"{' ' + ' '.join(flags) if flags else ''}")
        if s.vertices and not s.triangles:
            _log(log, f"      ! {s.name!r} has vertices but no triangles")
        if not s.vertices and not s.mesh_path:
            _log(log, f"      ! {s.name!r} has no geometry "
                      f"(declared {s.num_vertices} vertices)")
        for i, t in enumerate(s.textures):
            if t:
                _log(log, f"      slot {i}: {t.replace(chr(92), '/')}")


def _log_build(log, meshes, bounds, loader) -> None:
    """Report what actually reached the GPU-bound buffers."""
    if not meshes:
        _log(log, "  ! nothing drawable was built")
        return
    tris = sum(m.tri_count for m in meshes)
    textured = sum(1 for m in meshes if m.has_image)
    normals = sum(1 for m in meshes if m.normal_image is not None)
    vcols = sum(1 for m in meshes if m.has_colors)
    aos = sum(1 for m in meshes if m.ao_image is not None)
    srgb = sum(1 for m in meshes if m.srgb_albedo)
    envs = sum(1 for m in meshes if m.env_image is not None)
    _log(log, f"  totals: {tris} triangles · {textured}/{len(meshes)} textured"
              f" · {normals} normal-mapped · {vcols} vertex-coloured"
              f" · {aos} with AO · {srgb} sRGB-decoded · {envs} env-mapped")
    if bounds:
        lo, hi = bounds
        size = tuple(round(hi[i] - lo[i], 1) for i in range(3))
        _log(log, f"  bounds: {size[0]} x {size[1]} x {size[2]} units")
    missed = getattr(loader, "missing", None)
    if missed:
        _log(log, f"  ! {len(missed)} texture(s) NOT found - these are why a"
                  f" mesh renders as untextured clay:")
        for rel in list(missed)[:20]:
            _log(log, f"      missing: {rel.replace(chr(92), '/')}")
        if len(missed) > 20:
            _log(log, f"      … and {len(missed) - 20} more")


def _neutralise_view(view) -> None:
    """Last-resort orphan cleanup when a context dies (Python attrs only -
    the widget's C++ half may already be mid-destruction)."""
    _neutralise_meshes(list(view._meshes) + list(view._pending or ()))
    view._meshes = []
    view._pending = None


class _Viewport(QOpenGLWidget):
    """The GL canvas: turntable rotate/pan/zoom over the parsed shapes."""

    loaded = Signal(object, object, int, object)  # meshes, bounds, gen, tex paths
    failed = Signal(str, int)

    def __init__(self, parent=None, log_fn=None):
        super().__init__(parent)
        # Host's log sink. The parse/build runs off-thread, so this must stay
        # thread-safe - app.py's _append_log marshals to the GUI thread.
        self.log_fn = log_fn
        fmt = QSurfaceFormat()
        fmt.setVersion(3, 3)
        fmt.setProfile(QSurfaceFormat.CoreProfile)
        fmt.setDepthBufferSize(24)
        fmt.setSamples(4)
        self.setFormat(fmt)

        self._program: QOpenGLShaderProgram | None = None
        self._core = None
        self._u_mvp = self._u_hastex = self._u_base = self._u_gamma = -1
        self._u_flat = self._u_hasnorm = self._u_spec = self._u_eye = -1
        self._u_tex = self._u_normtex = -1
        self._u_speccol = self._u_specstr = self._u_shine = -1
        self._u_ld = (-1, -1, -1)
        self._u_envtex = self._u_masktex = self._u_envscale = -1
        self._u_hasmask = self._u_camright = self._u_camup = -1
        self._u_athresh = self._u_blend = self._u_hasvcol = -1
        self._u_tint = self._u_aotex = self._u_hasao = self._u_srgb = -1
        self._meshes: list[_Mesh] = []
        self._pending: list[_Mesh] | None = None
        self._uploaded = False
        self._gl_error = ""
        self._generation = 0
        self._load_jobs = LatestWorker("nif-preview-load")
        # Parsing, plugin overrides, hair tint and Starfield .mesh expansion
        # are invariant when only the texture source/slot changes.
        self._cached_model_key = None
        self._cached_model = None
        self._reload_args = None
        self._needs_reload = False
        self._keep_view = False
        # Built on the first resize; see resizeEvent for why paints pause.
        self._resize_hold = None

        self._yaw = _HOME_YAW
        self._pitch = _HOME_PITCH
        self._distance = 100.0
        self._center = QVector3D(0, 0, 0)    # rotation pivot: the mesh centre
        # Pan lives in view-plane coordinates, not world space: rotation then
        # always spins the asset about its own centre instead of arcing a
        # panned view across the screen.
        self._pan = [0.0, 0.0]
        self._home = (self._yaw, self._pitch, self._distance, QVector3D(0, 0, 0))
        self._last_pos = None
        self._last_buttons = Qt.NoButton
        self.wireframe = WIRE_OFF
        self.cull_backfaces = False
        self.textured = True
        self.texture_slot = 0
        self.detail = True          # normal maps + specular
        self.invert_mouse = True
        self._bg = QColor(BACKGROUNDS["light"])
        self._base = (0.40, 0.39, 0.37)
        self._gamma = 1.0

        self.setMinimumSize(1, 1)
        self.setFocusPolicy(Qt.StrongFocus)
        self.loaded.connect(self._on_loaded)

    # -- loading ------------------------------------------------------------
    def load(self, source, texture_roots: list[Path], archive_roots=None,
             resolver=None, archives=None, tex_override=None,
             mesh_rel: str = "", plugin_dirs=None,
             keep_view: bool = False):
        """Parse and build *source* (path or raw bytes) off-thread, then swap.

        *archives* lets a mesh read from inside a BSA find its own textures.
        *mesh_rel* (the mesh's data-relative path) enables plugin TXST
        overrides, scanned from *plugin_dirs* (default: the texture roots).
        """
        self._generation += 1
        gen = self._generation
        # keep_view: same mesh, new textures - don't snap the camera back.
        self._keep_view = bool(keep_view)
        # Kept so the mesh can be rebuilt after a context loss (tab detach).
        self._reload_args = (source, texture_roots, archive_roots,
                             resolver, archives, tex_override,
                             mesh_rel, plugin_dirs)
        self._discard_pending()
        log = self.log_fn

        src_desc = (f"{_fmt_bytes(len(source))} of data"
                    if isinstance(source, (bytes, bytearray))
                    else str(source))
        _log(log, f"--- load #{gen}: {src_desc}")
        _log(log, f"  texture roots ({len(texture_roots or ())}): "
                  f"{', '.join(str(r) for r in (texture_roots or ())) or 'none'}")
        _log(log, f"  archive roots: "
                  f"{len(archive_roots or ()) if archive_roots else 0}"
                  f" · resolver: {'yes' if resolver else 'no'}"
                  f" · archive index: {'yes' if archives else 'no'}"
                  f" · texture slot: {self.texture_slot}"
                  f" · override: {'yes' if tex_override else 'no'}")
        if mesh_rel:
            _log(log, f"  data-relative path: {mesh_rel}")

        def work():
            import time
            from Utils.nif_reader import read_nif
            t_start = time.monotonic()
            try:
                model_key = _model_cache_key(
                    source, texture_roots, archive_roots, resolver, archives,
                    mesh_rel, plugin_dirs)
                extra = archives
                if extra is None and archive_roots:
                    from Utils.archive_lookup import ArchiveLookup, find_archives
                    found = find_archives(archive_roots)
                    _log(log, f"  scanned {len(archive_roots)} archive root(s):"
                              f" {len(found)} archive(s) indexed")
                    extra = ArchiveLookup(found, keep_prefix=ASSET_PREFIXES)
                loader = _make_texture_loader(texture_roots, extra, resolver,
                                              tex_override, self.texture_slot,
                                              log,
                                              cancel=lambda: gen != self._generation)
                if (model_key == self._cached_model_key
                        and self._cached_model is not None):
                    model = self._cached_model
                    _log(log, "  reused parsed model and plugin/geometry lookups")
                else:
                    t0 = time.monotonic()
                    model = read_nif(source)
                    _log(log, f"  parsed in "
                              f"{(time.monotonic() - t0) * 1000:.0f}ms")
                    if gen != self._generation:
                        return
                    _log_model(log, model)
                    if mesh_rel:
                        # The game may swap the baked texture set via plugin
                        # records; without this such meshes preview as white clay.
                        from Utils.txst_lookup import apply_alt_textures
                        dirs = plugin_dirs or texture_roots
                        _log(log, "  plugin scan dirs: "
                                  + (", ".join(str(d) for d in dirs) or "none"))
                        try:
                            t0 = time.monotonic()
                            n = apply_alt_textures(
                                model, mesh_rel, dirs,
                                cancel=lambda: gen != self._generation)
                            _log(log, f"  plugin texture-set overrides: {n} shape(s)"
                                      f" ({(time.monotonic() - t0) * 1000:.0f}ms)")
                        except Exception as exc:         # noqa: BLE001
                            _log(log, f"  ! texture-set override scan failed: {exc!r}")
                        # Skyrim ships hair textures greyscale and tints them from
                        # the NPC record, so FaceGen hair is white without this.
                        try:
                            from Utils.facegen_tint import apply_hair_tint
                            t0 = time.monotonic()
                            n = apply_hair_tint(model, mesh_rel, dirs)
                            if n:
                                tint = next((s.tint for s in model.shapes
                                             if s.tint != (1.0, 1.0, 1.0)), None)
                                rgb = (tuple(round(c * 255) for c in tint)
                                       if tint else "?")
                                _log(log, f"  hair tint {rgb} applied to {n} shape(s)"
                                          f" ({(time.monotonic() - t0) * 1000:.0f}ms)")
                        except Exception as exc:         # noqa: BLE001
                            _log(log, f"  ! hair tint lookup failed: {exc!r}")
                    if gen != self._generation:
                        return
                    # Starfield keeps geometry in external .mesh files.
                    external = [s for s in model.shapes if s.mesh_path]
                    if external:
                        _log(log, f"  {len(external)} shape(s) use external "
                                  f".mesh geometry (Starfield)")
                        _load_external_geometry(model, loader.fetch)
                        filled = sum(1 for s in external if s.vertices)
                        _log(log, f"  external geometry resolved for "
                                  f"{filled}/{len(external)}")
                    if gen != self._generation:
                        return
                    self._cached_model_key = model_key
                    self._cached_model = model
                t0 = time.monotonic()
                meshes, bounds = _build_meshes(
                    model, loader, cancel=lambda: gen != self._generation)
                if gen != self._generation:
                    return
                _log(log, f"  built {len(meshes)} drawable mesh(es) in "
                          f"{(time.monotonic() - t0) * 1000:.0f}ms")
            except Exception as e:                       # noqa: BLE001
                import traceback
                _log(log, f"  !! load failed: {e!r}")
                for line in traceback.format_exc().strip().splitlines()[-4:]:
                    _log(log, f"     {line.strip()}")
                safe_emit(self.failed, str(e), gen)
                return
            _log_build(log, meshes, bounds, loader)
            _log(log, f"  load #{gen} done in "
                      f"{(time.monotonic() - t_start) * 1000:.0f}ms")
            safe_emit(self.loaded, meshes, bounds, gen,
                      list(dict.fromkeys(loader.requested)))

        self._load_jobs.submit(work)

    def reload(self, keep_view: bool = True):
        """Re-run the last load (e.g. after switching texture map)."""
        if self._reload_args is not None:
            self.load(*self._reload_args, keep_view=keep_view)

    def _discard_pending(self):
        self._pending = None

    def cancel_load(self):
        """Invalidate queued/in-flight CPU work without clearing the viewport."""
        self._generation += 1
        self._reload_args = None
        self._load_jobs.discard_pending()
        self._pending = None

    def clear(self):
        """Cancel queued work and clear the displayed CPU/GPU mesh safely."""
        self.cancel_load()
        self._cached_model_key = None
        self._cached_model = None
        if self.context() is not None:
            try:
                self.makeCurrent()
                self._release_gpu()
                self.doneCurrent()
            except RuntimeError:
                _neutralise_meshes(self._meshes)
                self._meshes = []
        else:
            _neutralise_meshes(self._meshes)
            self._meshes = []
        self._uploaded = False
        self.update()

    def _on_loaded(self, meshes, bounds, gen, _tex_paths=None):
        if gen != self._generation:
            return                                   # a newer file won the race
        # GL deletes need the context current or the driver keeps the objects.
        if self.context() is not None:
            self.makeCurrent()
            self._release_gpu()
            self.doneCurrent()
        else:
            _neutralise_meshes(self._meshes)
            self._meshes = []
        self._pending = meshes
        self._uploaded = False
        if bounds is not None and not self._keep_view:
            self._frame(bounds)
        self._keep_view = False
        self.update()

    def _frame(self, bounds):
        (lx, ly, lz), (hx, hy, hz) = bounds
        cx, cy, cz = (lx + hx) / 2, (ly + hy) / 2, (lz + hz) / 2
        radius = max(hx - lx, hy - ly, hz - lz, 1e-3) * 0.5
        self._center = QVector3D(cx, cy, cz)
        self._pan = [0.0, 0.0]
        self._distance = radius * 3.0
        self._yaw = _HOME_YAW
        self._pitch = _HOME_PITCH
        self._home = (self._yaw, self._pitch, self._distance,
                      QVector3D(cx, cy, cz))

    # -- GL -----------------------------------------------------------------
    def initializeGL(self):
        log = self.log_fn
        ctx0 = self.context()
        try:
            f0 = ctx0.functions()
            fmt = ctx0.format()
            _log(log, "GL context: "
                      f"{f0.glGetString(0x1F01)} {f0.glGetString(0x1F00)}"
                      f" · GL {f0.glGetString(0x1F02)}"
                      f" · GLSL {f0.glGetString(0x8B8C)}")
            _log(log, f"  surface: {fmt.majorVersion()}.{fmt.minorVersion()}"
                      f" {'core' if fmt.profile() == QSurfaceFormat.CoreProfile else 'compat'}"
                      f" · depth {fmt.depthBufferSize()}"
                      f" · samples {fmt.samples()}"
                      f" · {'sw' if ctx0.isOpenGLES() else 'hw'}")
        except Exception as exc:                         # noqa: BLE001
            _log(log, f"GL context: could not be queried ({exc!r})")

        prog = QOpenGLShaderProgram(self)
        ok = prog.addShaderFromSourceCode(QOpenGLShader.Vertex, _VERT_SRC)
        if not ok:
            _log(log, f"! vertex shader failed: {prog.log()}")
        frag_ok = prog.addShaderFromSourceCode(QOpenGLShader.Fragment, _FRAG_SRC)
        if not frag_ok:
            _log(log, f"! fragment shader failed: {prog.log()}")
        ok = frag_ok and ok
        linked = prog.link()
        if not linked:
            _log(log, f"! shader link failed: {prog.log()}")
        ok = linked and ok
        if not ok:
            self._gl_error = prog.log() or "shader compilation failed"
            _log(log, "!! the viewport cannot draw - shaders did not build")
            self._program = None
            return
        _log(log, "  shaders compiled and linked")
        self._program = prog
        self._u_mvp = prog.uniformLocation("uMVP")
        self._u_hastex = prog.uniformLocation("uHasTex")
        self._u_base = prog.uniformLocation("uBaseColor")
        self._u_gamma = prog.uniformLocation("uGamma")
        self._u_flat = prog.uniformLocation("uFlat")
        self._u_hasnorm = prog.uniformLocation("uHasNorm")
        self._u_spec = prog.uniformLocation("uSpecular")
        self._u_eye = prog.uniformLocation("uEye")
        self._u_tex = prog.uniformLocation("uTex")
        self._u_normtex = prog.uniformLocation("uNormTex")
        self._u_speccol = prog.uniformLocation("uSpecColor")
        self._u_specstr = prog.uniformLocation("uSpecStrength")
        self._u_shine = prog.uniformLocation("uShininess")
        self._u_ld = tuple(prog.uniformLocation(f"uLd{i}") for i in range(3))
        self._u_envtex = prog.uniformLocation("uEnvTex")
        self._u_masktex = prog.uniformLocation("uMaskTex")
        self._u_envscale = prog.uniformLocation("uEnvScale")
        self._u_hasmask = prog.uniformLocation("uHasMask")
        self._u_camright = prog.uniformLocation("uCamRight")
        self._u_camup = prog.uniformLocation("uCamUp")
        self._u_athresh = prog.uniformLocation("uAlphaThreshold")
        self._u_blend = prog.uniformLocation("uBlend")
        self._u_hasvcol = prog.uniformLocation("uHasVColor")
        self._u_tint = prog.uniformLocation("uTint")
        self._u_aotex = prog.uniformLocation("uAoTex")
        self._u_hasao = prog.uniformLocation("uHasAo")
        self._u_srgb = prog.uniformLocation("uSrgbAlbedo")
        # A -1 means the name is missing or the compiler dropped it as unused.
        # Setting one is a silent no-op, which has cost real debugging time.
        unresolved = [n for n, loc in (
            ("uMVP", self._u_mvp), ("uHasTex", self._u_hastex),
            ("uBaseColor", self._u_base), ("uGamma", self._u_gamma),
            ("uFlat", self._u_flat), ("uHasNorm", self._u_hasnorm),
            ("uSpecular", self._u_spec), ("uEye", self._u_eye),
            ("uTex", self._u_tex), ("uNormTex", self._u_normtex),
            ("uSpecColor", self._u_speccol), ("uSpecStrength", self._u_specstr),
            ("uShininess", self._u_shine), ("uEnvTex", self._u_envtex),
            ("uMaskTex", self._u_masktex), ("uEnvScale", self._u_envscale),
            ("uHasMask", self._u_hasmask), ("uCamRight", self._u_camright),
            ("uCamUp", self._u_camup), ("uAlphaThreshold", self._u_athresh),
            ("uBlend", self._u_blend), ("uHasVColor", self._u_hasvcol),
            ("uTint", self._u_tint), ("uAoTex", self._u_aotex),
            ("uHasAo", self._u_hasao), ("uSrgbAlbedo", self._u_srgb),
        ) if loc < 0]
        if unresolved:
            _log(log, "  ! uniforms not resolved (writes to these do nothing): "
                      + ", ".join(unresolved))
        ctx = self.context()
        ctx.functions().glEnable(_GL_DEPTH_TEST)
        # glPolygonMode (wireframe) needs the 3.3 core functions object.
        profile = QOpenGLVersionProfile()
        profile.setVersion(3, 3)
        profile.setProfile(QSurfaceFormat.CoreProfile)
        try:
            self._core = QOpenGLVersionFunctionsFactory.get(profile, ctx)
        except Exception as exc:                         # noqa: BLE001
            self._core = None
            _log(log, f"  ! GL 3.3 core functions unavailable ({exc!r}) - "
                      f"wireframe modes will fall back to solid")
        # Reparenting (tab detach/re-pin) destroys the context and everything
        # uploaded to it. Free while it is still current, then rebuild from
        # the kept load args when the new context initialises.
        ctx.aboutToBeDestroyed.connect(self._on_context_lost)
        # Safety net for hosts that delete this widget as a CHILD: their
        # DeferredDelete never reaches us and the bound connection above is
        # dropped mid-destruction - but a receiver-less lambda still fires,
        # and it touches only Python-side state.
        ctx.aboutToBeDestroyed.connect(lambda v=self: _neutralise_view(v))
        if self._needs_reload:
            self._needs_reload = False
            if self._reload_args is not None:
                # A context-loss rebuild is a recovery: keep the camera.
                self.load(*self._reload_args, keep_view=True)

    def _on_context_lost(self):
        from PySide6.QtGui import QOpenGLContext
        _log(self.log_fn, "GL context destroyed (tab detach/re-pin?) - "
                          "freeing buffers, mesh will be rebuilt")
        try:
            self.makeCurrent()
        except Exception as exc:                         # noqa: BLE001
            _log(self.log_fn, f"  makeCurrent during teardown failed: {exc!r}")
        if QOpenGLContext.currentContext() is not None:
            self._release_gpu()
            self.doneCurrent()
        else:
            # No current context to free under - and once the creation context
            # is gone even a later destroy() crashes (it derefs the stored
            # context in areSharing; seen as a GC-time SIGSEGV). Sever the
            # wrappers instead; the share group reclaims the GPU side.
            _neutralise_meshes(self._meshes)
            self._meshes = []
        self._pending = None
        self._uploaded = False
        self._needs_reload = True

    def release_gl(self):
        """Free GL objects while the context lives (called on DeferredDelete;
        aboutToBeDestroyed fires too late - Qt has dropped our connections)."""
        if self.context() is None:
            return
        try:
            self.makeCurrent()
        except RuntimeError:
            return
        self._release_gpu()
        self._pending = None
        self._uploaded = False
        self.doneCurrent()

    def event(self, e):
        if e.type() == QEvent.DeferredDelete:
            self.release_gl()
        return super().event(e)

    def _release_gpu(self):
        for m in self._meshes:
            for obj in (m.vao, m.vbo, m.ibo, m.texture, m.normal_tex,
                        m.env_tex, m.mask_tex, m.ao_tex):
                if obj is not None:
                    try:
                        obj.destroy()
                    except RuntimeError:
                        pass
            m.vao = m.vbo = m.ibo = m.texture = m.normal_tex = None
            m.env_tex = m.mask_tex = m.ao_tex = None
        self._meshes = []

    def _upload(self):
        prog = self._program
        vram = tex_count = 0
        for m in self._pending or []:
            m.vao = QOpenGLVertexArrayObject()
            m.vao.create()
            m.vao.bind()

            m.vbo = QOpenGLBuffer(QOpenGLBuffer.VertexBuffer)
            m.vbo.create()
            m.vbo.bind()
            data = m.verts.tobytes()
            m.vbo.allocate(data, len(data))

            # Colours widen the vertex; the enable state is captured by this
            # mesh's VAO, so meshes without them never read attribute 4.
            stride = (15 if m.has_colors else 11) * 4
            prog.enableAttributeArray(0)
            prog.setAttributeBuffer(0, _GL_FLOAT, 0, 3, stride)
            prog.enableAttributeArray(1)
            prog.setAttributeBuffer(1, _GL_FLOAT, 3 * 4, 3, stride)
            prog.enableAttributeArray(2)
            prog.setAttributeBuffer(2, _GL_FLOAT, 6 * 4, 2, stride)
            prog.enableAttributeArray(3)
            prog.setAttributeBuffer(3, _GL_FLOAT, 8 * 4, 3, stride)
            if m.has_colors:
                prog.enableAttributeArray(4)
                prog.setAttributeBuffer(4, _GL_FLOAT, 11 * 4, 4, stride)

            m.ibo = QOpenGLBuffer(QOpenGLBuffer.IndexBuffer)
            m.ibo.create()
            m.ibo.bind()
            idata = m.indices.tobytes()
            m.ibo.allocate(idata, len(idata))

            m.vao.release()
            m.vbo.release()
            m.ibo.release()

            vram += len(data) + len(idata)
            for attr, img in (("texture", m.image),
                              ("normal_tex", m.normal_image),
                              ("env_tex", m.env_image),
                              ("mask_tex", m.mask_image),
                              ("ao_tex", m.ao_image)):
                tex = _make_gl_texture(img)
                if tex is not None:
                    setattr(m, attr, tex)
                    tex_count += 1
                    vram += img.width() * img.height() * 4
                elif img is not None:
                    _log(self.log_fn, f"  ! {m.name!r}: {attr} failed to "
                                      f"upload to the GPU")
            m.normal_image = m.env_image = m.mask_image = None
            m.ao_image = None
            # Free both CPU copies now the GPU owns the data.
            m.verts = array.array("f")
            m.image = None
        n = len(self._pending or [])
        self._meshes = self._pending or []
        self._pending = None
        self._uploaded = True
        if n:
            _log(self.log_fn, f"  uploaded {n} mesh(es) and {tex_count} "
                              f"texture(s) to the GPU (~{_fmt_bytes(vram)})")

    def paintGL(self):
        f = self.context().functions()
        col = self._bg
        f.glClearColor(col.redF(), col.greenF(), col.blueF(), 1.0)
        f.glClear(_GL_COLOR_BUFFER_BIT | _GL_DEPTH_BUFFER_BIT)
        if self._program is None:
            return
        if not self._uploaded and self._pending is not None:
            self._upload()
        if not self._meshes:
            return

        f.glEnable(_GL_DEPTH_TEST)
        if self.cull_backfaces:
            f.glEnable(_GL_CULL_FACE)
            f.glCullFace(_GL_BACK)
        else:
            f.glDisable(_GL_CULL_FACE)

        # glPolygonMode is core-profile only; without it, fall back to solid.
        wire = self.wireframe if self._core is not None else WIRE_OFF
        prog = self._program
        prog.bind()
        prog.setUniformValue(self._u_mvp, self._mvp())
        # glUniform* directly: setUniformValue silently drops plain scalars.
        f.glUniform1f(self._u_gamma, self._gamma)
        # Sampler units, set per frame: assigning them once at link time did
        # not stick, leaving uNormTex on unit 0 (i.e. sampling the diffuse).
        f.glUniform1i(self._u_tex, 0)
        f.glUniform1i(self._u_normtex, 1)
        eye = self._eye()
        f.glUniform3f(self._u_eye, eye.x(), eye.y(), eye.z())
        for loc, d in zip(self._u_ld, self._light_dirs()):
            f.glUniform3f(loc, d.x(), d.y(), d.z())
        right, up, _fwd = self._camera_basis()
        f.glUniform3f(self._u_camright, right.x(), right.y(), right.z())
        f.glUniform3f(self._u_camup, up.x(), up.y(), up.z())
        f.glUniform1i(self._u_envtex, 2)
        f.glUniform1i(self._u_masktex, 3)
        f.glUniform1i(self._u_aotex, 4)
        # BodySlide's default specularStrength.
        f.glUniform1f(self._u_spec, 1.0 if self.detail else 0.0)

        if wire != WIRE_ONLY:
            f.glUniform1f(self._u_flat, 0.0)
            f.glUniform3f(self._u_base, *self._base)
            # Alpha-TESTED meshes stay in the opaque pass: a discarded
            # fragment writes no depth, so they need no ordering. Only truly
            # BLENDED ones must come last, back to front, without depth
            # writes - per mesh, so surfaces inside one shape can still
            # order wrong (BodySlide has the same limit).
            blended = [m for m in self._meshes
                       if m.alpha_blend and self.textured
                       and m.texture is not None]
            if blended:
                opaque = [m for m in self._meshes if m not in blended]
                self._draw_meshes(f, solid=True, meshes=opaque)
                eye = self._eye()
                blended.sort(
                    key=lambda m: -((m.center[0] - eye.x()) ** 2
                                    + (m.center[1] - eye.y()) ** 2
                                    + (m.center[2] - eye.z()) ** 2))
                f.glEnable(_GL_BLEND)
                f.glBlendFunc(_GL_SRC_ALPHA, _GL_ONE_MINUS_SRC_ALPHA)
                f.glDepthMask(False)
                self._draw_meshes(f, solid=True, meshes=blended)
                f.glDepthMask(True)
                f.glDisable(_GL_BLEND)
            else:
                self._draw_meshes(f, solid=True)

        if wire != WIRE_OFF:
            self._core.glPolygonMode(_GL_FRONT_AND_BACK, _GL_LINE)
            if wire == WIRE_OVERLAY:
                # Nudge the lines toward the viewer so they do not z-fight the
                # surface they sit on.
                f.glEnable(_GL_POLYGON_OFFSET_LINE)
                f.glPolygonOffset(-1.0, -1.0)
            f.glUniform1f(self._u_flat, 1.0)
            f.glUniform3f(self._u_base, *self._wire_color())
            self._draw_meshes(f, solid=False)
            if wire == WIRE_OVERLAY:
                f.glDisable(_GL_POLYGON_OFFSET_LINE)
            self._core.glPolygonMode(_GL_FRONT_AND_BACK, _GL_FILL)

        prog.release()

    def _wire_color(self):
        """Line colour that reads against the current backdrop."""
        lum = (0.299 * self._bg.redF() + 0.587 * self._bg.greenF()
               + 0.114 * self._bg.blueF())
        return (0.10, 0.10, 0.12) if lum > 0.5 else (0.85, 0.88, 0.92)

    def _draw_meshes(self, f, solid: bool, meshes=None):
        for m in self._meshes if meshes is None else meshes:
            m.vao.bind()
            m.ibo.bind()
            use_tex = solid and self.textured and m.texture is not None
            if use_tex:
                m.texture.bind(0)
            f.glUniform1f(self._u_hastex, 1.0 if use_tex else 0.0)
            # The cut-out needs the texture's alpha, so it only applies where
            # the diffuse map is actually bound.
            f.glUniform1f(self._u_athresh,
                          m.alpha_threshold if use_tex else -1.0)
            f.glUniform1f(self._u_blend,
                          1.0 if (use_tex and m.alpha_blend) else 0.0)
            f.glUniform1f(self._u_hasvcol,
                          1.0 if (solid and m.has_colors) else 0.0)
            f.glUniform3f(self._u_tint, *(m.tint if solid else (1.0, 1.0, 1.0)))
            # AO rides with the diffuse, not the "shine" toggle: it is part of
            # the base colour, not a lighting effect.
            use_ao = (solid and self.textured and m.ao_tex is not None
                      and self.texture_slot == 0)
            if use_ao:
                m.ao_tex.bind(4)
            f.glUniform1f(self._u_hasao, 1.0 if use_ao else 0.0)
            f.glUniform1f(self._u_srgb,
                          1.0 if (use_tex and m.srgb_albedo) else 0.0)
            sr, sg, sb, sstr, shine = m.spec
            f.glUniform3f(self._u_speccol, sr, sg, sb)
            f.glUniform1f(self._u_specstr, sstr)
            f.glUniform1f(self._u_shine, shine)
            # The env sheen is part of "shine": off with the detail toggle.
            use_env = (solid and self.detail and m.env_tex is not None
                       and self.texture_slot == 0)
            if use_env:
                m.env_tex.bind(2)
                if m.mask_tex is not None:
                    m.mask_tex.bind(3)
                f.glUniform1f(self._u_hasmask,
                              1.0 if m.mask_tex is not None else 0.0)
            f.glUniform1f(self._u_envscale, m.env_scale if use_env else 0.0)
            # Normal maps only make sense on the lit pass, and only when the
            # base colour is the diffuse map (slot 1 shown raw is the map).
            use_norm = (solid and self.detail and m.normal_tex is not None
                        and self.texture_slot == 0)
            if use_norm:
                m.normal_tex.bind(1)
                f.glUniform1f(self._u_hasnorm,
                              2.0 if m.model_space_normals else 1.0)
            else:
                f.glUniform1f(self._u_hasnorm, 0.0)
            f.glDrawElements(_GL_TRIANGLES, len(m.indices),
                             _GL_UNSIGNED_INT, _NULL_OFFSET)
            if use_ao:
                m.ao_tex.release(4)
            if use_env:
                m.env_tex.release(2)
                if m.mask_tex is not None:
                    m.mask_tex.release(3)
            if use_norm:
                m.normal_tex.release(1)
            if use_tex:
                m.texture.release(0)
            m.ibo.release()
            m.vao.release()

    def resizeGL(self, w, h):
        self.context().functions().glViewport(0, 0, max(1, w), max(1, h))

    def resizeEvent(self, e):
        """Stop painting until a resize drag settles.

        On GLX/DRI3 a buffer swap that lands mid-resize can block forever in
        xcb_wait_for_special_event - dragging a splitter across the viewport
        froze the whole app. No repaint during the drag means no swap in
        flight, so the hang has no window to happen in. This replaces the old
        app-wide QT_XCB_GL_INTEGRATION=xcb_egl, which fixed the freeze by
        moving every user onto the EGL path and blacked out the entire UI
        where that path does not render (GH#350).
        """
        super().resizeEvent(e)
        if self._resize_hold is None:
            self._resize_hold = QTimer(self)
            self._resize_hold.setSingleShot(True)
            self._resize_hold.setInterval(120)
            self._resize_hold.timeout.connect(self._end_resize_hold)
        self.setUpdatesEnabled(False)
        self._resize_hold.start()

    def _end_resize_hold(self):
        self.setUpdatesEnabled(True)
        self.update()

    # BodySlide's directional lights, in camera coordinates (x right, y up,
    # z toward the viewer): two front keys and one backlight.
    _RIG = (
        (-0.90, 0.10, 1.00),
        (0.70, 0.10, 1.00),
        (0.30, 0.20, -1.00),
    )

    @staticmethod
    def _basis_for(yaw: float, pitch: float):
        """(right, up, forward) unit vectors of a camera at these angles."""
        fwd = QVector3D(-math.cos(pitch) * math.cos(yaw),
                        -math.cos(pitch) * math.sin(yaw),
                        -math.sin(pitch))
        right = QVector3D.crossProduct(fwd, QVector3D(0, 0, 1))
        if right.lengthSquared() < 1e-6:            # looking straight down/up
            right = QVector3D(math.cos(yaw + math.pi / 2),
                              math.sin(yaw + math.pi / 2), 0)
        right = right.normalized()
        return right, QVector3D.crossProduct(right, fwd), fwd

    def _camera_basis(self):
        """(right, up, forward) unit vectors of the current camera."""
        return self._basis_for(self._yaw, self._pitch)

    def _light_dirs(self):
        """The rig on the home basis - fixed in the world, not the viewer."""
        # Anchoring the lights while yaw/pitch change is what turns the orbit
        # into a turntable: the same relative motion now reads as the asset
        # rotating under still lights, with highlights sweeping across it.
        right, up, fwd = self._basis_for(_HOME_YAW, _HOME_PITCH)
        return [(right * x + up * y - fwd * z).normalized()
                for x, y, z in self._RIG]

    def _pan_axes(self):
        """(right, up) drag axes of the view plane at the current angles."""
        right = QVector3D(math.sin(self._yaw), -math.cos(self._yaw), 0.0)
        up = QVector3D(
            -math.sin(self._pitch) * math.cos(self._yaw),
            -math.sin(self._pitch) * math.sin(self._yaw),
            math.cos(self._pitch),
        )
        return right, up

    def _look_target(self) -> QVector3D:
        """The look-at point: the mesh centre pushed by the pan offset."""
        right, up = self._pan_axes()
        return self._center + right * self._pan[0] + up * self._pan[1]

    def _eye(self) -> QVector3D:
        d = max(self._distance, 1e-3)
        t = self._look_target()
        return QVector3D(
            t.x() + d * math.cos(self._pitch) * math.cos(self._yaw),
            t.y() + d * math.cos(self._pitch) * math.sin(self._yaw),
            t.z() + d * math.sin(self._pitch),
        )

    def _mvp(self) -> QMatrix4x4:
        w = max(1, self.width())
        h = max(1, self.height())
        d = max(self._distance, 1e-3)
        eye = self._eye()
        proj = QMatrix4x4()
        proj.perspective(45.0, w / h, max(d * 0.001, 1e-3), d * 50.0)
        view = QMatrix4x4()
        view.lookAt(eye, self._look_target(), QVector3D(0, 0, 1))
        return proj * view

    # -- interaction --------------------------------------------------------
    def mousePressEvent(self, e):
        self._last_pos = e.position()
        self._last_buttons = e.buttons()

    def mouseMoveEvent(self, e):
        if self._last_pos is None:
            return
        delta = e.position() - self._last_pos
        self._last_pos = e.position()
        # Rotate and pan are deliberately mirrored; the toggle flips both.
        rot_sign = -1.0 if self.invert_mouse else 1.0
        pan_sign = -rot_sign
        if e.buttons() & Qt.LeftButton:
            self._yaw += rot_sign * delta.x() * 0.01
            self._pitch = max(-1.5533, min(
                1.5533, self._pitch - rot_sign * delta.y() * 0.01))
        elif e.buttons() & (Qt.RightButton | Qt.MiddleButton):
            # Pan across the view plane, scaled so the drag tracks the cursor.
            scale = self._distance * 0.0022 * pan_sign
            self._pan[0] += delta.x() * scale
            self._pan[1] += delta.y() * scale
        else:
            return
        self.update()

    def mouseReleaseEvent(self, e):
        self._last_pos = None

    def wheelEvent(self, e):
        steps = e.angleDelta().y() / 120.0
        if steps:
            self._distance = max(1e-3, self._distance * (0.85 ** steps))
            self.update()

    def set_brightness(self, percent: int):
        """Set the gamma lift from an int percent (100 = neutral)."""
        pct = max(BRIGHTNESS_MIN, min(BRIGHTNESS_MAX, int(percent)))
        self._gamma = pct / 100.0
        self.update()

    def set_background(self, key: str):
        """Swap the backdrop preset; clay colour flips to keep the silhouette."""
        self._bg = QColor(BACKGROUNDS.get(key, BACKGROUNDS["light"]))
        lum = (0.299 * self._bg.redF() + 0.587 * self._bg.greenF()
               + 0.114 * self._bg.blueF())
        self._base = ((0.40, 0.39, 0.37) if lum > 0.5
                      else (0.72, 0.71, 0.68))
        self.update()

    def mouseDoubleClickEvent(self, e):
        self._yaw, self._pitch, self._distance, center = self._home
        self._center = QVector3D(center)
        self._pan = [0.0, 0.0]
        self.update()


class _NoGLViewport(QWidget):
    """Stand-in canvas for machines where Qt's GL path is unusable.

    Creating a real QOpenGLWidget there does not just fail to draw the mesh -
    it turns the whole window black (GH#350), so we never build one. This
    keeps the surrounding preview UI intact and explains itself instead.
    """

    loaded = Signal(object, object, int, object)
    failed = Signal(str, int)

    def __init__(self, reason: str = "", parent=None):
        super().__init__(parent)
        self._reason = reason
        pal = active_palette()
        self.setStyleSheet(f"background:{BACKGROUNDS['dark']};")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 24, 24, 24)
        text = self.tr("3D preview is unavailable on this system.")
        if reason:
            text += "\n\n" + reason
        label = QLabel(text)
        label.setWordWrap(True)
        label.setAlignment(Qt.AlignCenter)
        label.setStyleSheet(f"color:{_c(pal, 'TEXT_DIM')};")
        lay.addWidget(label)

        # The attributes NifPreview's toggles write straight through.
        self.invert_mouse = True
        self.cull_backfaces = False
        self.textured = True
        self.detail = True
        self.wireframe = WIRE_OFF
        self.texture_slot = 0
        self._generation = 0

    def load(self, *_a, **_kw):
        # Report through the normal channel so the header stops at a reason
        # instead of sitting on "Loading…" forever.
        self._generation += 1
        safe_emit(self.failed, self.tr("no OpenGL"), self._generation)

    def reload(self, *_a, **_kw):
        self.load()

    def clear(self):
        self._generation += 1

    def cancel_load(self):
        self._generation += 1

    def set_brightness(self, *_a):
        pass

    def set_background(self, *_a):
        pass

    def release_gl(self, *_a):
        pass


class NifPreview(QWidget):
    """A panel-scoped .nif viewer: header + stats, view toggles, GL viewport."""

    # Texture paths the last-loaded mesh asked for (drives the source picker).
    textures_seen = Signal(object)
    # A texture source was picked: its opaque data, None = as the game loads.
    texture_source_changed = Signal(object)

    def __init__(self, path: "Path | None", display_name: str = "",
                 texture_roots: list[Path] | None = None,
                 archive_roots: list[Path] | None = None,
                 resolver=None, parent=None, log_fn=None):
        # path None = caller feeds bytes via set_nif_data() (archive member).
        super().__init__(parent)
        self.log_fn = log_fn
        self.setObjectName("NifPreview")
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)

        pal = active_palette()
        bar = QWidget()
        bar.setStyleSheet(f"background:{_c(pal, 'BG_HEADER')};")
        bar_col = QVBoxLayout(bar)
        bar_col.setContentsMargins(10, 6, 10, 6)
        bar_col.setSpacing(4)

        # Two rows: identity above, controls below. Both FlowLayouts, so a
        # narrow pane wraps instead of forcing a minimum width on the splitter.
        title_host = QWidget()
        title_row = FlowLayout(title_host, spacing=12)
        title_row.setContentsMargins(0, 0, 0, 0)
        enable_height_for_width(title_host)

        # ElidingLabel tooltips the full title; the drag hint is on the viewport.
        self._header = ElidingLabel(display_name or path.name)
        self._header.setStyleSheet(
            f"color:{_c(pal, 'TEXT_MAIN')}; font-weight:600;")
        title_row.addWidget(self._header)

        self._stats = QLabel("")
        self._stats.setStyleSheet(f"color:{_c(pal, 'TEXT_DIM')};")
        title_row.addWidget(self._stats)
        bar_col.addWidget(title_host)

        ctl_host = QWidget()
        ctl_row = FlowLayout(ctl_host, spacing=12)
        ctl_row.setContentsMargins(0, 0, 0, 0)
        enable_height_for_width(ctl_host)

        # Everything discrete lives in one menu; only the continuous control
        # (brightness) and the contextual texture-source picker stay on the bar.
        self._view_btn = QToolButton()
        self._view_btn.setText(self.tr("View"))
        self._view_btn.setPopupMode(QToolButton.InstantPopup)
        self._view_btn.setStyleSheet(f"color:{_c(pal, 'TEXT_DIM')};")
        self._menu = QMenu(self._view_btn)
        self._view_btn.setMenu(self._menu)
        ctl_row.addWidget(self._view_btn)

        self._act_tex = self._menu.addAction(self.tr("Textures"))
        self._act_tex.setCheckable(True)
        self._act_tex.setChecked(True)
        self._act_tex.triggered.connect(self._on_textured)

        self._act_detail = self._menu.addAction(self.tr("Normal maps + shine"))
        self._act_detail.setCheckable(True)
        self._act_detail.setChecked(True)
        self._act_detail.setToolTip(self.tr(
            "Apply the mesh's normal map and its gloss mask"))
        self._act_detail.triggered.connect(self._on_detail)

        self._act_cull = self._menu.addAction(self.tr("Cull backfaces"))
        self._act_cull.setCheckable(True)
        self._act_cull.setToolTip(self.tr(
            "Hide inward-facing triangles - reveals inside-out normals"))
        self._act_cull.triggered.connect(self._on_cull)

        wire_menu = self._menu.addMenu(self.tr("Wireframe"))
        self._wire_group = QActionGroup(self)
        for key, label in ((WIRE_OFF, self.tr("Off")),
                           (WIRE_OVERLAY, self.tr("Overlay")),
                           (WIRE_ONLY, self.tr("Lines only"))):
            act = wire_menu.addAction(label)
            act.setCheckable(True)
            act.setData(key)
            act.setChecked(key == WIRE_OFF)
            self._wire_group.addAction(act)
        self._wire_group.triggered.connect(self._on_wireframe)

        slot_menu = self._menu.addMenu(self.tr("Texture map"))
        self._slot_group = QActionGroup(self)
        for key, index in TEXTURE_SLOTS:
            act = slot_menu.addAction(
                self.tr("Diffuse") if key == "diffuse" else self.tr("Normal"))
            act.setCheckable(True)
            act.setData(index)
            act.setChecked(index == 0)
            self._slot_group.addAction(act)
        self._slot_group.triggered.connect(self._on_texture_slot)

        bg_menu = self._menu.addMenu(self.tr("Background"))
        self._bg_group = QActionGroup(self)
        for key, label in (("light", self.tr("Light")), ("grey", self.tr("Grey")),
                           ("dark", self.tr("Dark")), ("black", self.tr("Black"))):
            act = bg_menu.addAction(label)
            act.setCheckable(True)
            act.setData(key)
            self._bg_group.addAction(act)
        self._bg_group.triggered.connect(self._on_background)

        self._menu.addSeparator()
        self._act_invert = self._menu.addAction(self.tr("Invert mouse"))
        self._act_invert.setCheckable(True)
        self._act_invert.setToolTip(self.tr(
            "Reverse the drag direction for rotating and panning"))
        self._act_invert.triggered.connect(self._on_invert_mouse)

        self._bright = QSlider(Qt.Horizontal)
        self._bright.setRange(BRIGHTNESS_MIN, BRIGHTNESS_MAX)
        self._bright.setFixedWidth(90)
        self._bright.setToolTip(self.tr(
            "Brightness - lifts dark textures without blowing out highlights; "
            "double-click to reset"))
        self._bright.installEventFilter(self)
        ctl_row.addWidget(QLabel("\u2600"))
        ctl_row.addWidget(self._bright)

        self._tex_box = QComboBox()
        self._tex_box.setToolTip(self.tr(
            "Preview this mesh with another mod's copy of its textures"))
        self._tex_box.hide()          # shown once a host offers alternatives
        self._tex_box.activated.connect(self._on_texture_source)
        ctl_row.addWidget(self._tex_box)
        bar_col.addWidget(ctl_host)

        v.addWidget(bar)

        gl_ok, gl_why = gl_status()
        _log(log_fn, f"viewer opening · OpenGL {'available' if gl_ok else 'UNAVAILABLE'}"
                     + (f" ({gl_why})" if not gl_ok else ""))
        if gl_ok:
            self._view = _Viewport(log_fn=log_fn)
            self._view.setToolTip(self.tr(
                "Drag to rotate · right-drag to pan · scroll to zoom · "
                "double-click to reframe"))
        else:
            self._view = _NoGLViewport(gl_why)
        self._view.loaded.connect(self._on_loaded)
        self._view.failed.connect(self._on_failed)
        v.addWidget(self._view, 1)

        # Restore prefs. QAction.triggered and QSlider.sliderReleased are
        # user-only, so setting state here can never rewrite the config.
        try:
            from Utils.ui_config import load_nif_invert_mouse
            inverted = load_nif_invert_mouse()
        except Exception:
            inverted = True
        self._view.invert_mouse = bool(inverted)
        self._act_invert.setChecked(bool(inverted))

        try:
            from Utils.ui_config import load_nif_cull_backfaces
            cull = load_nif_cull_backfaces()
        except Exception:
            cull = False
        self._view.cull_backfaces = bool(cull)
        self._act_cull.setChecked(bool(cull))

        try:
            from Utils.ui_config import load_nif_brightness
            bright = load_nif_brightness()
        except Exception:
            bright = BRIGHTNESS_DEFAULT
        self._view.set_brightness(bright)
        self._bright.setValue(max(BRIGHTNESS_MIN, min(BRIGHTNESS_MAX, bright)))
        self._bright.valueChanged.connect(self._on_brightness)
        # Save on release only: user-only, and avoids a write per drag pixel.
        self._bright.sliderReleased.connect(self._save_brightness)

        try:
            from Utils.ui_config import load_nif_background
            saved = load_nif_background()
        except Exception:
            saved = "light"
        if saved not in BACKGROUNDS:
            saved = "light"
        self._view.set_background(saved)
        for act in self._bg_group.actions():
            act.setChecked(act.data() == saved)

        if path is not None:
            self.set_nif(path, display_name, texture_roots, archive_roots,
                         resolver)
        else:
            self._header.setText(display_name)

    def set_nif(self, path: Path, display_name: str = "",
                texture_roots: list[Path] | None = None,
                archive_roots: list[Path] | None = None, resolver=None,
                tex_override=None, keep_view: bool = False):
        """Swap the previewed mesh in place; *keep_view* skips re-framing."""
        self._header.setText(display_name or path.name)
        self._stats.setText(self.tr("Loading…"))
        roots = (default_texture_roots(path)
                 if texture_roots is None else texture_roots)
        archive_dirs = (default_archive_roots(path)
                        if archive_roots is None else archive_roots)
        self._view.load(path,
                        roots,
                        archive_dirs,
                        resolver, None, tex_override,
                        mesh_rel=_mesh_rel_of(path), keep_view=keep_view)

    def clear(self, status: str = ""):
        """Clear a stale model after a definitive read/parse failure."""
        self._view.clear()
        self._stats.setText(status)

    def cancel_load(self):
        """Drop stale CPU work while retaining the last completed mesh."""
        self._view.cancel_load()

    def set_texture_sources(self, items, current=None):
        """Fill the texture-source picker: [(label, data), …]; empty hides it."""
        self._tex_box.blockSignals(True)
        self._tex_box.clear()
        for label, data in items or ():
            self._tex_box.addItem(label, data)
        idx = self._tex_box.findData(current) if items else -1
        self._tex_box.setCurrentIndex(max(0, idx))
        self._tex_box.blockSignals(False)
        self._tex_box.setVisible(bool(items))

    def reload_textures(self):
        """Re-resolve textures, keeping the camera where it is."""
        self._stats.setText(self.tr("Loading…"))
        self._view.reload(keep_view=True)

    def texture_source(self):
        """Data of the picked source, or None for 'as the game loads'."""
        return self._tex_box.currentData() if self._tex_box.isVisible() else None

    def _on_texture_source(self, _index):
        _log(self.log_fn,
             f"option: texture source = {self._tex_box.currentText()!r}")
        self.texture_source_changed.emit(self._tex_box.currentData())

    def set_title(self, display_name: str, status: str = ""):
        """Retitle without loading - a browser showing what it is about to read."""
        self._header.setText(display_name)
        self._stats.setText(status)

    def set_nif_data(self, data: bytes, display_name: str,
                     resolver=None, archives=None, tex_override=None,
                     keep_view: bool = False, mesh_rel: str = "",
                     plugin_dirs=None):
        """Preview in-memory bytes (a BSA/BA2 member); *keep_view* skips
        re-framing."""
        self._header.setText(display_name)
        self._stats.setText(self.tr("Loading…"))
        self._view.load(data, [], None, resolver, archives, tex_override,
                        mesh_rel=mesh_rel, plugin_dirs=plugin_dirs,
                        keep_view=keep_view)

    def _on_loaded(self, meshes, bounds, gen, tex_paths=None):
        if gen != self._view._generation:
            return          # a stale load must not retitle or repopulate the picker
        # Emitted even for a geometry-less mesh: the host's texture picker is
        # driven by which paths were REQUESTED, not by what resolved.
        safe_emit(self.textures_seen, list(tex_paths or ()))
        if not meshes:
            _log(self.log_fn, "displayed: no drawable geometry")
            self._stats.setText(self.tr("no drawable geometry"))
            return
        tris = sum(m.tri_count for m in meshes)
        textured = sum(1 for m in meshes if m.has_image)
        parts = [self.tr("{0} shapes").format(len(meshes)),
                 self.tr("{0} tris").format(f"{tris:,}")]
        if textured < len(meshes):
            parts.append(self.tr("{0}/{1} textured").format(textured, len(meshes)))
        self._stats.setText(" · ".join(parts))

    def _on_failed(self, message, gen):
        if gen != self._view._generation:
            return
        _log(self.log_fn, f"preview failed: {message}")
        status = self.tr("failed: {0}").format(message)
        self.clear(status)

    def _on_textured(self, on):
        _log(self.log_fn, f"option: textures {'on' if on else 'off'}")
        self._view.textured = bool(on)
        self._view.update()

    def _on_wireframe(self, act):
        _log(self.log_fn, f"option: wireframe = {act.data()}")
        self._view.wireframe = act.data()
        self._view.update()

    def _on_detail(self, on):
        _log(self.log_fn,
             f"option: normal maps + shine {'on' if on else 'off'}")
        self._view.detail = bool(on)
        self._view.update()

    def _on_cull(self, on):
        _log(self.log_fn, f"option: cull backfaces {'on' if on else 'off'}")
        self._view.cull_backfaces = bool(on)
        self._view.update()
        try:
            from Utils.ui_config import save_nif_cull_backfaces
            save_nif_cull_backfaces(bool(on))
        except Exception as exc:
            _log(self.log_fn, f"! could not save cull setting: {exc!r}")

    def _on_texture_slot(self, act):
        """Re-resolve textures for the chosen map; geometry is untouched."""
        _log(self.log_fn, f"option: showing texture slot {act.data()}"
                          f" (0 = diffuse, 1 = normal) - re-resolving")
        self._view.texture_slot = int(act.data())
        self.reload_textures()

    def _on_invert_mouse(self, on):
        _log(self.log_fn, f"option: invert mouse {'on' if on else 'off'}")
        self._view.invert_mouse = bool(on)
        try:
            from Utils.ui_config import save_nif_invert_mouse
            save_nif_invert_mouse(bool(on))
        except Exception as exc:
            _log(self.log_fn, f"! could not save invert setting: {exc!r}")

    def eventFilter(self, obj, e):
        # Double-click the brightness slider to snap back to neutral.
        if obj is self._bright and e.type() == QEvent.MouseButtonDblClick:
            self._bright.setValue(BRIGHTNESS_DEFAULT)
            self._view.set_brightness(BRIGHTNESS_DEFAULT)
            self._save_brightness()
            return True
        return super().eventFilter(obj, e)

    def _on_brightness(self, value):
        self._view.set_brightness(value)

    def _save_brightness(self):
        _log(self.log_fn, f"option: brightness {self._bright.value()}%")
        try:
            from Utils.ui_config import save_nif_brightness
            save_nif_brightness(int(self._bright.value()))
        except Exception as exc:
            _log(self.log_fn, f"! could not save brightness: {exc!r}")

    def _on_background(self, act):
        key = act.data() or "light"
        _log(self.log_fn, f"option: background = {key}")
        self._view.set_background(key)
        try:
            from Utils.ui_config import save_nif_background
            save_nif_background(key)
        except Exception as exc:
            _log(self.log_fn, f"! could not save background: {exc!r}")

    def event(self, e):
        # Scoped tabs close via deleteLater(); closeEvent never fires.
        if e.type() == QEvent.DeferredDelete:
            ctl = getattr(self, "_tex_source_ctl", None)
            if ctl is not None:
                ctl.cancel()
            self._view.cancel_load()
            self._view.release_gl()
        return super().event(e)


def _mesh_rel_of(path) -> str:
    """The mesh's data-relative path ('meshes/...'), or '' if underivable."""
    parts = [p.lower() for p in Path(path).parts]
    if "meshes" not in parts:
        return ""
    idx = len(parts) - 1 - parts[::-1].index("meshes")
    return "/".join(Path(path).parts[idx:])


def default_texture_roots(nif_path: Path) -> list[Path]:
    """Folders that could hold this mesh's loose textures (mod root first)."""
    roots: list[Path] = []
    p = nif_path.resolve().parent
    for parent in [p, *p.parents]:
        if (parent / "textures").is_dir() or (parent / "Textures").is_dir():
            roots.append(parent)
        if parent.name.lower() == "meshes":
            up = parent.parent
            if up not in roots:
                roots.append(up)
        if len(roots) >= 4:
            break
    return roots


def default_archive_roots(nif_path: Path) -> list[Path]:
    """Folders whose archives may hold the textures: own mod, then siblings."""
    p = nif_path.resolve().parent
    own = None
    mods_root = None
    for parent in [p, *p.parents]:
        if parent.parent.name.lower() == "mods":
            own = parent
            mods_root = parent.parent
            break
    if own is None:
        return []
    roots = [own]
    try:
        for entry in sorted(mods_root.iterdir()):
            if entry.is_dir() and entry != own:
                roots.append(entry)
    except OSError:
        pass
    return roots
