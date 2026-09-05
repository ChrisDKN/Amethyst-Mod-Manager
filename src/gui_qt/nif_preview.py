"""
nif_preview.py
Panel-scoped 3D preview for .nif meshes (QOpenGLWidget, no new deps).

Parses off-thread via Utils.assets.nif, bakes world transforms into vertices,
and resolves textures through Utils.assets.resolver (what the game would load)
with archive/loose fallbacks. Starfield geometry is fetched from external
.mesh files. Meshes are Z-up; the default turntable camera can be switched to
an unrestricted trackball while the lights remain fixed around the asset.
"""

from __future__ import annotations

import array
import math
import os
import threading
from collections import OrderedDict
from itertools import chain
from pathlib import Path
from shiboken6 import VoidPtr

from PySide6.QtCore import QEvent, QRect, Qt, QTimer, Signal
from PySide6.QtGui import (
    QActionGroup, QColor, QImage, QMatrix4x4, QPainter, QSurfaceFormat,
    QVector3D,
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

from Utils.assets.resolver import DirCache as _DirCache
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

# NPCs repeatedly share body, eye, mouth and armour maps.  Keep their capped
# CPU images across preview loads, but put a hard ceiling on it: 160 MiB holds
# roughly forty 1024x1024 RGBA maps and cannot grow with a long browsing
# session.  GPU textures remain scene-owned and are released normally.
TEXTURE_CACHE_BYTES = 160 * 1024 * 1024

_CACHE_MISS = object()


def _image_cost(value) -> int:
    """Approximate the QImage storage retained by a cache value."""
    if isinstance(value, (tuple, list)):
        return sum(_image_cost(item) for item in value)
    size = getattr(value, "sizeInBytes", None)
    if size is None:
        return 0
    try:
        return max(0, int(size()))
    except (RuntimeError, TypeError, ValueError):
        return 0


class _DecodedTextureCache:
    """Thread-safe, byte-bounded LRU of capped CPU-side texture images."""

    def __init__(self, max_bytes: int = TEXTURE_CACHE_BYTES):
        self.max_bytes = max(0, int(max_bytes))
        self._items = OrderedDict()
        self._bytes = 0
        self._lock = threading.Lock()

    def get(self, key):
        with self._lock:
            try:
                value, cost = self._items.pop(key)
            except KeyError:
                return _CACHE_MISS
            self._items[key] = (value, cost)
            return value

    def put(self, key, value) -> None:
        cost = _image_cost(value)
        if value is None or cost <= 0 or cost > self.max_bytes:
            return
        with self._lock:
            old = self._items.pop(key, None)
            if old is not None:
                self._bytes -= old[1]
            self._items[key] = (value, cost)
            self._bytes += cost
            while self._bytes > self.max_bytes and self._items:
                _old_key, (_old_value, old_cost) = \
                    self._items.popitem(last=False)
                self._bytes -= old_cost

    def clear(self) -> None:
        with self._lock:
            self._items.clear()
            self._bytes = 0

    @property
    def usage(self) -> tuple[int, int]:
        with self._lock:
            return len(self._items), self._bytes


_IDENTITY_ROT = (1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0)

# Indexed asset subtrees. materials/ (FO4 texture paths live there) and
# geometries/ (Starfield meshes) are required, not optional.
ASSET_PREFIXES = ("textures/", "materials/", "geometries/")

# Backdrops; light default because many game textures are near-black.
# "green" is the broadcast chroma-key green, for keying the model out of a
# screenshot in an image editor - it is deliberately a colour no skin, hair or
# armour texture lands on.
BACKGROUNDS = {
    "light": "#d4d7db",
    "grey": "#8b8f94",
    "dark": "#2b2d30",
    "black": "#0b0b0c",
    "green": "#00b140",
}
BACKGROUND_ORDER = ["light", "grey", "dark", "black", "green"]
# Export-only pseudo-backdrop: not a colour, so it is deliberately NOT in
# BACKGROUNDS - the viewport never paints it and it never reaches the ini.
BACKGROUND_TRANSPARENT = "transparent"

# Brightness is a gamma lift: 1.0 neutral, higher raises shadows. Stored as an
# int percent so it round-trips through the ini and the slider unchanged.
BRIGHTNESS_MIN, BRIGHTNESS_MAX, BRIGHTNESS_DEFAULT = 60, 260, 100

# Portrait framing, matched to NPC Plugin Chooser 2's own portrait renderer so
# an exported mugshot sits beside its packs without jumping scale. Its
# defaults (Models/Settings.cs): CamYaw 90, CamPitch 2, HeadTopOffset 0.0,
# HeadBottomOffset -0.05, output 750x750.
#
# The framed subject is the WHOLE head - hair included - not the face shape
# alone. Framing on the face crops tall hairstyles, which is exactly what the
# reference shot does not do.
#
# NPC2 renders at a 25-degree vertical FOV; this viewport's projection is a
# fixed 45 (see _Viewport.paintGL). Distance is therefore solved from OUR 45,
# not NPC2's 25 - using the wrong one puts the head at roughly half size. The
# resulting COMPOSITION matches; only the lens differs, so a wider FOV shows
# slightly more perspective foreshortening.
_VIEWPORT_FOV = 45.0
_PORTRAIT_YAW = 90.0
_PORTRAIT_PITCH = 2.0
# Fractions of head height added above the crown and below the chin; the
# negative bottom extends the crop DOWN into the neck and shoulders, which is
# what gives the composition its head-and-shoulders look.
#
# SOLVED from a real NPC2 mugshot rather than copied from its settings: its
# own offsets (0.0 / -0.05) are relative to a head measured on a FULL BODY
# render, where the torso already fills the lower frame. We render the head
# alone, so the same numbers would crop at the chin. Measured off the
# reference (750px): crown 1.5% down, neck at 70%, i.e. the head is 68.5% of
# frame height with 30% below it. That needs a visible height of 1.46x the
# head, distributed to leave only a sliver of headroom.
_PORTRAIT_TOP_OFFSET = 0.022
_PORTRAIT_BOTTOM_OFFSET = -0.437
_PORTRAIT_FILL = 1.0
# Ceiling on the inset as a fraction of the pane's smaller side, so a narrow
# pane gets a smaller portrait instead of one covering the model.
_PORTRAIT_MAX_PANE = 0.45

# NPC image export: four narrow turntable views and one wider face panel.  The
# ratios deliberately mirror a traditional character reference sheet while
# deriving the actual resolution from the live framebuffer (never upscaling a
# small viewport into a blurry nominal size).
_SHEET_BODY_ASPECT = 0.42
_SHEET_FACE_ASPECT = 0.86
_SHEET_MAX_HEIGHT = 1200
# Panels are rendered offscreen, so the export is NOT limited by the preview
# pane's size on screen. 2048 gives a ~5000px-wide sheet - poster-sized for a
# reference image, and still one frame per angle.
_SHEET_EXPORT_HEIGHT = 2048

_HOME_YAW = math.radians(-60.0)
_HOME_PITCH = math.radians(22.0)
_TURNTABLE_PITCH_LIMIT = 1.5533

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
_GL_ONE = 0x0001

# PySide6 binds glDrawElements' `indices` as a real pointer, so an integer 0 is
# rejected; with an element buffer bound it must be a null VoidPtr offset.
_NULL_OFFSET = VoidPtr(0)

_VERT_SRC = """#version 330 core
layout(location = 0) in vec3 aPos;
layout(location = 1) in vec3 aNormal;
layout(location = 2) in vec2 aUV;
// xyz is Bethesda's tangent (the texture-V direction); w preserves the
// stored bitangent handedness for mirrored UV islands.
layout(location = 3) in vec4 aTangent;
layout(location = 4) in vec4 aColor;
uniform mat4 uMVP;
out vec3 vNormal;
out vec2 vUV;
out vec3 vTangent;
out float vBitangentSign;
out vec3 vWorld;
out vec4 vColor;
void main() {
    vNormal = aNormal;
    vTangent = aTangent.xyz;
    vBitangentSign = aTangent.w;
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
in float vBitangentSign;
in vec3 vWorld;
in vec4 vColor;
// Only 1 where the mesh HAS colours and SLSF2_Vertex_Colors is set - plenty
// of meshes carry a stale colour array the engine ignores.
uniform float uHasVColor;
// Runtime colour multiply (FaceGen hair). White for everything else.
uniform vec3 uTint;
// Community Shaders TruePBR packed material map: roughness, metallic, AO and
// specular in RGBA.  It is deliberately kept linear (unlike the albedo).
uniform sampler2D uRmaosTex;
uniform float uPbr;
uniform float uHasRmaos;
// x = NIF Specular Level, y = NIF Roughness Scale. PGPatcher repurposes the
// legacy glossiness and specular-strength fields for these values.
uniform vec2 uPbrParams;
// 1 when the diffuse DDS declared an sRGB format (PBR packs do; legacy
// Skyrim textures never declare one). Those meshes get a colour-managed
// path - decode to linear, light, tonemap, re-encode - which is why their
// mid-tones stop washing out. Legacy meshes keep the BodySlide behaviour.
uniform float uSrgbAlbedo;
// uHasTex is a float: PySide6 setUniformValue silently misses int uniforms.
uniform sampler2D uTex;
uniform sampler2D uNormTex;
uniform float uHasTex;
uniform float uHasNorm;      // 0 none, 1 tangent-space, 2 model-space (NIF flag)
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

// Compact Cook-Torrance/GGX path for TruePBR. The real Community Shaders
// renderer also has an HDR sky probe; the preview uses a neutral procedural
// studio environment so metals still have something to reflect.
const float PI = 3.14159265359;

float distributionGGX(float ndh, float roughness) {
    float a = roughness * roughness;
    float a2 = a * a;
    float d = ndh * ndh * (a2 - 1.0) + 1.0;
    return a2 / max(PI * d * d, 0.0001);
}

float geometrySchlickGGX(float nd, float roughness) {
    float r = roughness + 1.0;
    float k = r * r / 8.0;
    return nd / max(nd * (1.0 - k) + k, 0.0001);
}

vec3 fresnelSchlick(float cosTheta, vec3 f0) {
    return f0 + (1.0 - f0) * pow(1.0 - cosTheta, 5.0);
}

vec3 fresnelSchlickRoughness(float cosTheta, vec3 f0, float roughness) {
    return f0 + (max(vec3(1.0 - roughness), f0) - f0)
                * pow(1.0 - cosTheta, 5.0);
}

void addPbrLight(vec3 dir, float strength, vec3 n, vec3 v, vec3 albedo,
                 float roughness, float metallic, vec3 f0, inout vec3 outCol) {
    vec3 h = normalize(v + dir);
    float ndl = max(dot(n, dir), 0.0);
    float ndv = max(dot(n, v), 0.001);
    float ndh = max(dot(n, h), 0.0);
    float vdh = max(dot(v, h), 0.0);
    vec3 f = fresnelSchlick(vdh, f0);
    float d = distributionGGX(ndh, roughness);
    float g = geometrySchlickGGX(ndv, roughness)
              * geometrySchlickGGX(ndl, roughness);
    vec3 specular = d * g * f / max(4.0 * ndv * ndl, 0.001);
    vec3 diffuse = (1.0 - f) * (1.0 - metallic) * albedo / PI;
    outCol += (diffuse + specular) * strength * ndl;
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
        vec3 tangentNormal = nm.rgb * 2.0 - 1.0;
        // FO4 drops the third component from many tangent-space normal maps
        // (including FaceGen's misleadingly named _msn maps).  Pillow quite
        // correctly decodes the absent blue channel as zero; treating that
        // encoded zero as -1 made every pore and crease point sideways.  The
        // engine reconstructs the positive hemisphere from the stored RG.
        if (nm.b < (0.5 / 255.0)) {
            tangentNormal.z = sqrt(max(0.0,
                1.0 - dot(tangentNormal.xy, tangentNormal.xy)));
        }
        vec3 t = vTangent - n * dot(n, vTangent);   // Gram-Schmidt
        if (length(t) > 1e-4) {
            t = normalize(t);
            // NIF/BodySlide convention: bitangent is texture U (normal-map
            // X), tangent is texture V (normal-map Y). The sign is essential
            // where an atlas mirrors an island.
            vec3 b = vBitangentSign * cross(n, t);
            mat3 tbn = mat3(b, t, n);
            n = normalize(tbn * tangentNormal);
        }
        // Skyrim keeps its gloss/spec mask in the normal map's ALPHA.
        gloss = nm.a;
    }
    gloss *= uSpecular;

    vec3 v = normalize(uEye - vWorld);
    vec3 albedo = texel.rgb;
    if (uSrgbAlbedo > 0.5) albedo = pow(albedo, vec3(2.2));
    albedo *= uTint;

    // TruePBR cannot be approximated by feeding its albedo to the legacy
    // diffuse shader: metals put their reflectance colour in that map and
    // therefore turn into bright chalk. Consume all four RMAOS channels and
    // use a metallic/roughness BRDF instead.
    if (uPbr > 0.5) {
        vec4 rmaos = uHasRmaos > 0.5
                     ? texture(uRmaosTex, vUV) : vec4(0.5, 0.0, 1.0, 1.0);
        float roughness = clamp(rmaos.r * max(uPbrParams.y, 0.01), 0.045, 1.0);
        float metallic = clamp(rmaos.g, 0.0, 1.0);
        float ao = clamp(rmaos.b, 0.0, 1.0);
        float dielectric = clamp(uPbrParams.x * rmaos.a, 0.0, 1.0);
        vec3 f0 = mix(vec3(dielectric), albedo, metallic);

        vec3 pbr = vec3(0.0);
        addPbrLight(v,    0.35, n, v, albedo, roughness, metallic, f0, pbr);
        addPbrLight(uLd0, 1.10, n, v, albedo, roughness, metallic, f0, pbr);
        addPbrLight(uLd1, 1.10, n, v, albedo, roughness, metallic, f0, pbr);
        addPbrLight(uLd2, 1.45, n, v, albedo, roughness, metallic, f0, pbr);

        float ndv = max(dot(n, v), 0.0);
        vec3 rfl = reflect(-v, n);
        float skyMix = clamp(rfl.z * 0.5 + 0.5, 0.0, 1.0);
        vec3 studio = mix(vec3(0.055, 0.045, 0.040),
                          vec3(0.62, 0.68, 0.76), skyMix);
        vec3 fEnv = fresnelSchlickRoughness(ndv, f0, roughness);
        vec3 diffuseEnv = albedo * (1.0 - metallic) * 0.22;
        vec3 specularEnv = studio * fEnv * mix(0.85, 0.35, roughness);
        pbr += (diffuseEnv + specularEnv) * ao;

        vec3 col = tonemap(pbr) / tonemap(vec3(1.0));
        // PBR lighting is always linear even when the source albedo itself
        // was authored/stored linear, so the framebuffer always needs an
        // sRGB transfer here.
        col = pow(max(col, 0.0), vec3(1.0 / 2.2));
        FragColor = vec4(pow(clamp(col, 0.0, 1.0), vec3(1.0 / uGamma)),
                         mix(1.0, texel.a, uBlend));
        return;
    }

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


class _Geometry:
    __slots__ = ("verts", "indices", "lo", "hi", "has_colors")

    def __init__(self, verts, indices, lo, hi, has_colors):
        self.verts = verts
        self.indices = indices
        self.lo = lo
        self.hi = hi
        self.has_colors = has_colors


class _Mesh:
    """One shape's CPU-side buffers, built off-thread and uploaded on demand."""

    __slots__ = ("name", "verts", "indices", "image", "has_image", "tri_count",
                 "normal_image", "model_space_normals", "spec",
                 "env_image", "mask_image", "env_scale",
                 "alpha_threshold", "alpha_blend", "center", "has_colors",
                 "tint", "rmaos_image", "rmaos_tex", "pbr", "pbr_params",
                 "srgb_albedo", "texture_clamp_mode",
                 "double_sided", "depth_test", "depth_write",
                 "vao", "vbo", "ibo", "texture", "normal_tex",
                 "env_tex", "mask_tex", "geometry")

    def __init__(self, name, verts, indices, image, tri_count,
                 normal_image=None, model_space_normals=False, spec=None,
                 env_image=None, mask_image=None, env_scale=0.0,
                 alpha_threshold=-1.0, alpha_blend=False, center=(0.0, 0.0, 0.0),
                 has_colors=False, tint=(1.0, 1.0, 1.0), rmaos_image=None,
                 pbr=False, pbr_params=(0.04, 1.0), srgb_albedo=False,
                 texture_clamp_mode=3, double_sided=False,
                 depth_test=True, depth_write=True, geometry=None):
        self.name = name
        self.verts = verts
        self.indices = indices
        self.geometry = geometry
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
        # Widens the vertex from 12 floats to 16; only meshes that use it pay.
        self.has_colors = has_colors
        # Runtime colour multiply (FaceGen hair); white otherwise.
        self.tint = tint
        self.rmaos_image = rmaos_image
        self.rmaos_tex = None
        self.pbr = pbr
        self.pbr_params = pbr_params
        # The DDS declared an sRGB format, so its values need
        # linearising before lighting. Legacy files never say.
        self.srgb_albedo = srgb_albedo
        self.texture_clamp_mode = texture_clamp_mode
        self.double_sided = double_sided
        self.depth_test = depth_test
        self.depth_write = depth_write
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


def _multiply_tint_map(base, tint):
    """Composite a FaceGen tint map over a head's base skin texture.

    Bethesda's tint-mask convention: the map is multiplied at DOUBLE strength.
    Its flat field carries the NPC's texture-lighting colour (rather than a
    fixed grey), while painted regions add brows, eyeshadow, lipstick and
    freckles. Done here rather than in the shader so the existing
    single-diffuse path is untouched.

    Keep the multiply in a wide intermediate.  Doubling the 8-bit tint image
    first clips every value above 0.5 to white; a typical skin field is about
    0.87, so that mistake removes the head's lighting-tint boost while the
    body still receives it in the shader and creates a very visible neck seam.
    """
    from PIL import Image as PilImage, ImageMath
    from PySide6.QtGui import QImage

    scaled = tint.scaled(base.width(), base.height(),
                         Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
    base_rgba = base.convertToFormat(QImage.Format_RGBA8888)
    tint_rgba = scaled.convertToFormat(QImage.Format_RGBA8888)
    base_image = PilImage.frombytes(
        "RGBA", (base_rgba.width(), base_rgba.height()),
        bytes(base_rgba.constBits()))
    tint_image = PilImage.frombytes(
        "RGBA", (tint_rgba.width(), tint_rgba.height()),
        bytes(tint_rgba.constBits()))
    base_channels = base_image.split()
    tint_channels = tint_image.split()
    channels = [
        ImageMath.lambda_eval(
            lambda args: args["base"] * args["tint"] * 2 / 255,
            base=base_channel, tint=tint_channel).convert("L")
        for base_channel, tint_channel
        in zip(base_channels[:3], tint_channels[:3])
    ]
    result = PilImage.merge("RGB", tuple(channels)).convert("RGBA")
    result.putalpha(base_channels[3])
    raw = result.tobytes("raw", "RGBA")
    return QImage(raw, result.width, result.height,
                  QImage.Format_RGBA8888).copy()


def _remap_palette(base, palette, index: float):
    """Apply a FO4 greyscale-to-palette row while preserving diffuse alpha.

    Hair CLFM records provide the vertical coordinate and the diffuse map's
    greyscale value provides the horizontal coordinate. Pillow's channel LUTs
    keep this a C-speed operation even for a 1K hair map.
    """
    from PIL import Image as PilImage
    from PySide6.QtGui import QImage

    if (base is None or base.isNull() or palette is None or palette.isNull()
            or palette.width() < 1 or palette.height() < 1):
        return base
    rgba = base.convertToFormat(QImage.Format_RGBA8888)
    image = PilImage.frombytes(
        "RGBA", (rgba.width(), rgba.height()), bytes(rgba.constBits()))

    # Vanilla's ramp is only 64 pixels wide. Expand it to one entry for each
    # possible diffuse byte using the same interpolation a GPU sampler uses.
    ramp = palette.scaled(
        256, palette.height(), Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
    row = round(min(1.0, max(0.0, index)) * (ramp.height() - 1))
    colours = [ramp.pixelColor(x, row) for x in range(256)]
    source = image.getchannel("R")
    channels = [source.point([getattr(c, name)() for c in colours])
                for name in ("red", "green", "blue")]
    result = PilImage.merge("RGB", tuple(channels)).convert("RGBA")
    result.putalpha(image.getchannel("A"))
    raw = result.tobytes("raw", "RGBA")
    return QImage(raw, result.width, result.height,
                  QImage.Format_RGBA8888).copy()


def _legacy_dds_pil(data: bytes, PilImage):
    """Fast Pillow image for an ordinary uncompressed RGB(A) DDS.

    Pillow's DDS plugin expands these legacy files through a very slow pixel
    path (a 2K 24-bit skin specular map takes about 6.5 seconds on the target
    handheld).  Their payload is already packed RGB/BGR rows, so Pillow's C
    raw decoder can expose it directly in a few milliseconds.  Compressed,
    DX10 and unusual bit-mask layouts fall back to the normal DDS plugin.
    """
    import struct

    if len(data) < 128 or not data.startswith(b"DDS "):
        return None
    try:
        height, width, pitch, depth = struct.unpack_from("<4I", data, 12)
        pf_flags, fourcc, bits, rmask, gmask, bmask, amask = \
            struct.unpack_from("<I4s5I", data, 80)
    except struct.error:
        return None
    if (not (pf_flags & 0x40) or (pf_flags & 0x4) or fourcc.strip(b"\0")
            or depth > 1 or not 0 < width <= 32768 or not 0 < height <= 32768):
        return None

    if bits == 24:
        mode = "RGB"
        if (rmask, gmask, bmask, amask) == (0xFF0000, 0xFF00, 0xFF, 0):
            raw_mode = "BGR"
        elif (rmask, gmask, bmask, amask) == (0xFF, 0xFF00, 0xFF0000, 0):
            raw_mode = "RGB"
        else:
            return None
    elif bits == 32:
        bgr = (rmask, gmask, bmask) == (0xFF0000, 0xFF00, 0xFF)
        rgb = (rmask, gmask, bmask) == (0xFF, 0xFF00, 0xFF0000)
        if not (bgr or rgb) or amask not in (0, 0xFF000000):
            return None
        mode = "RGBA" if amask else "RGB"
        raw_mode = (("BGRA" if bgr else "RGBA") if amask
                    else ("BGRX" if bgr else "RGBX"))
    else:
        return None

    row_bytes = ((width * bits + 31) // 32) * 4
    pitch = max(pitch, row_bytes)
    end = 128 + pitch * height
    if end > len(data):
        return None
    try:
        return PilImage.frombytes(mode, (width, height), data[128:end],
                                  "raw", raw_mode, pitch, 1)
    except (TypeError, ValueError):
        return None


def _model_space_normal(nrm_blob, spec_blob, log=None):
    """Decode a normal map, packing a separate spec map into its alpha.

    A model-space map's alpha is NOT a gloss mask (it is usually solid 255).
    Skyrim keeps skin gloss in a dedicated specular map (texture slot 7) and
    FO4 FaceGen does the same for its tangent-space map. Both use the specular
    map's RED channel. Packing that into alpha lets the shader read gloss from
    one sampler for both map types; with no spec map the alpha is zeroed, so a
    model-space skin map renders matte rather than using its opaque alpha.
    """
    try:
        import io
        from PIL import Image as PilImage
        from PySide6.QtGui import QImage
        from Utils.assets.dds import sanitise_dds, skip_dds_mips
        nrm_blob = skip_dds_mips(nrm_blob, TEXTURE_MAX_DIM)
        with PilImage.open(io.BytesIO(sanitise_dds(nrm_blob))) as im:
            big = max(im.width, im.height)
            if big > TEXTURE_MAX_DIM:
                im = im.reduce(max(1, big // TEXTURE_MAX_DIM))
            rgb = im.convert("RGB")
        if spec_blob:
            spec_blob = skip_dds_mips(spec_blob, TEXTURE_MAX_DIM)
            fast = _legacy_dds_pil(spec_blob, PilImage)
            if fast is not None:
                gloss = fast.getchannel("R")                # red channel
            else:
                with PilImage.open(io.BytesIO(sanitise_dds(spec_blob))) as sp:
                    gloss = sp.convert("RGB").split()[0]    # red channel
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


def _make_gl_texture(img, clamp_mode=3):
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
    # TexClampMode stores U/S in bit 1 and V/T in bit 0.
    wrap_s = (QOpenGLTexture.Repeat if clamp_mode & 2
              else QOpenGLTexture.ClampToEdge)
    wrap_t = (QOpenGLTexture.Repeat if clamp_mode & 1
              else QOpenGLTexture.ClampToEdge)
    tex.setWrapMode(QOpenGLTexture.DirectionS, wrap_s)
    tex.setWrapMode(QOpenGLTexture.DirectionT, wrap_t)
    return tex


def _qimage_from_bytes(data: bytes, log=None):
    """Decode texture bytes pulled from an archive (DDS goes via Pillow)."""
    from PySide6.QtGui import QImage
    from Utils.assets.dds import sanitise_dds, skip_dds_mips
    # A DDS ships its own mip chain: decode the first level that fits the
    # cap rather than a 4K top mip (~400ms of BC7) we would only shrink.
    data = skip_dds_mips(data, TEXTURE_MAX_DIM)
    # FaceTint and several specular maps are ordinary uncompressed DDS files.
    # Pillow's DDS plugin expands those byte-by-byte in Python (an 8-9 second
    # stall for Serana's 2K tint map), although their payload is already packed
    # RGB(A).  The same direct decoder used by model-space spec maps turns that
    # into a few milliseconds and preserves the exact channel layout.
    if data.startswith(b"DDS "):
        try:
            from PIL import Image as PilImage
            fast = _legacy_dds_pil(data, PilImage)
            if fast is not None:
                big = max(fast.width, fast.height)
                if big > TEXTURE_MAX_DIM:
                    fast = fast.reduce(max(1, big // TEXTURE_MAX_DIM))
                fast = fast.convert("RGBA")
                raw = fast.tobytes("raw", "RGBA")
                return QImage(raw, fast.width, fast.height,
                              QImage.Format_RGBA8888).copy()
        except Exception:                               # noqa: BLE001
            # Fall through to Qt/Pillow's general decoders for unusual files.
            pass
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
                         override=None, slot: int = 0, log=None, cancel=None,
                         decoded_cache: _DecodedTextureCache | None = None):
    """Return ``shape -> QImage|None``; resolver first, then roots/archives.

    FO4/Starfield shapes name a material file whose textures override the
    mesh's own (usually empty) texture set. *override* (``rel -> bytes|None``)
    is consulted before everything else; requested paths are recorded on
    ``load.requested``.
    """
    cache = _DirCache()
    seen: dict[object, object] = {}
    # Which diffuse maps DECLARE an sRGB DXGI format (PBR packs do).
    srgb_albedo: dict[str, bool] = {}
    materials: dict[str, object] = {}
    requested: list[str] = []
    requested_seen: set[str] = set()
    missing: list[str] = []
    resolver_hits: set[str] = set()
    archive_hits: set[str] = set()
    cache_stats = {"hits": 0, "misses": 0}

    def asset_key(rel: str) -> str:
        key = rel.replace("\\", "/").lower().strip().lstrip("/")
        if key.startswith("data/"):
            key = key[5:]
        if not key.startswith(("textures/", "materials/", "geometries/")):
            if key.endswith((".bgsm", ".bgem", ".mat")):
                key = "materials/" + key
            elif key.endswith((".dds", ".tga", ".png", ".bmp", ".jpg",
                               ".jpeg")):
                key = "textures/" + key
        return key

    def source_stamp(path) -> tuple:
        try:
            stat = os.stat(path)
            return str(path), stat.st_mtime_ns, stat.st_size
        except OSError:
            return str(path), 0, -1

    # A selected texture-source override is deliberately excluded: its
    # callback may return different bytes for the same path on every reload.
    # Resolver hits share one namespace across NPCs. A selected mod's sibling
    # archive fallback gets a second, archive-stamped namespace so its private
    # maps are reusable without leaking into another replacer's preview.
    shared_namespace = None
    fallback_namespace = None
    if decoded_cache is not None and override is None:
        if resolver is not None:
            shared_namespace = ("resolver", resolver)
            archive_paths = getattr(archives, "_archives", ()) if archives else ()
            if archives is not None:
                fallback_namespace = (
                    "resolver-fallback", resolver,
                    tuple(source_stamp(Path(p)) for p in archive_paths),
                    id(archives) if not archive_paths else 0,
                )
        else:
            archive_paths = getattr(archives, "_archives", ()) if archives else ()
            shared_namespace = (
                "roots", tuple(source_stamp(Path(p)) for p in texture_roots),
                "archives", tuple(source_stamp(Path(p)) for p in archive_paths),
                id(archives) if archives is not None and not archive_paths else 0,
            )

    def shared_key(namespace, kind: str, *rels: str):
        return (namespace, kind,
                tuple(asset_key(rel) for rel in rels if rel))

    def shared_get(kind: str, *rels: str):
        if shared_namespace is None:
            return _CACHE_MISS
        for rel in rels:
            if rel:
                note_request(rel)
        for namespace in (shared_namespace, fallback_namespace):
            if namespace is None:
                continue
            value = decoded_cache.get(shared_key(namespace, kind, *rels))
            if value is not _CACHE_MISS:
                cache_stats["hits"] += 1
                names = ", ".join(asset_key(rel).rsplit("/", 1)[-1]
                                  for rel in rels if rel)
                _log(log, f"      cache {names}")
                return value
        cache_stats["misses"] += 1
        return _CACHE_MISS

    def shared_put(kind: str, rels, value) -> None:
        if shared_namespace is None or value is None:
            return
        paths = tuple(rel for rel in rels if rel)
        namespace = shared_namespace
        if resolver is not None:
            keys = tuple(asset_key(rel) for rel in paths)
            if all(key in resolver_hits for key in keys):
                namespace = shared_namespace
            elif (fallback_namespace is not None
                  and all(key in archive_hits for key in keys)):
                namespace = fallback_namespace
            else:
                # A composite using maps from different sources is uncommon;
                # rebuilding it is safer than giving it an ambiguous identity.
                return
        decoded_cache.put(shared_key(namespace, kind, *paths), value)

    def note_request(rel: str) -> None:
        key = asset_key(rel)
        if not key.startswith(("textures/", "materials/")):
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
                resolver_hits.add(asset_key(rel))
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
                archive_hits.add(asset_key(rel))
                _log(log, f"      hit  {rel.replace(chr(92), '/')} <- archive "
                          f"({_fmt_bytes(len(blob))})")
            return blob
        return None

    def _fetch_selected_exact(rel: str):
        """Read an NPC-copy-specific asset before the profile winner.

        FaceTint is baked as a pair with FaceGeom. When the user selects a
        losing/vanilla head for comparison, applying the winning replacer's
        FaceTint changes that head's skin colour and makeup. Ordinary mesh
        textures still use _fetch_exact's game-load precedence; only callers
        explicitly asking for the selected copy use this route.
        """
        note_request(rel)
        if override is not None:
            blob = override(rel)
            if blob:
                _log(log, f"      hit  {rel.replace(chr(92), '/')} <- source override "
                          f"({_fmt_bytes(len(blob))})")
                return blob
        for root in texture_roots:
            found = cache.resolve(root, rel)
            if found is None:
                continue
            try:
                blob = found.read_bytes()
            except OSError:
                continue
            _log(log, f"      hit  {rel.replace(chr(92), '/')} <- selected copy "
                      f"({_fmt_bytes(len(blob))})")
            return blob
        if archives is not None:
            blob = archives.read(rel)
            if blob:
                _log(log, f"      hit  {rel.replace(chr(92), '/')} <- selected archive "
                          f"({_fmt_bytes(len(blob))})")
                return blob
        if resolver is not None:
            blob = resolver.read(rel)
            if blob:
                _log(log, f"      hit  {rel.replace(chr(92), '/')} <- resolver "
                          f"({_fmt_bytes(len(blob))})")
            return blob
        return None

    def material_slot(rel: str) -> str:
        key = rel.lower()
        if key not in materials:
            from Utils.assets.materials import read_material
            blob = fetch(rel)
            materials[key] = read_material(blob) if blob else None
        mat = materials[key]
        if mat is None:
            return ""
        if slot == 0:
            return mat.diffuse          # skips leading empty slots
        return mat.paths[slot] if slot < len(mat.paths) else ""

    def material_state(shape):
        """Load and return a shape's external BGSM/BGEM/MAT, if any."""
        if not shape.material:
            return None
        key = shape.material.lower()
        if key not in materials:
            material_slot(shape.material)
        return materials.get(key)

    def shape_slot(shape) -> str:
        if slot == 0:
            return shape.diffuse
        return shape.textures[slot] if slot < len(shape.textures) else ""

    def palette_slot(shape) -> str:
        """FO4 greyscale colour LUT (texture slot 3), if the shader has one."""
        if shape.material:
            mat = materials.get(shape.material.lower())
            if mat is None:
                material_slot(shape.material)
                mat = materials.get(shape.material.lower())
            if mat is not None and len(mat.paths) > 3:
                return mat.paths[3]
        return shape.textures[3] if len(shape.textures) > 3 else ""

    def decoded_image(rel: str, *, exact: bool = False):
        """Decode one ordinary image, sharing successful capped results."""
        # `exact` is the selected FaceGeom copy's optional FaceTint. Its source
        # identity differs from the profile winner, so it must not reuse the
        # resolver-wide cache (that made vanilla Nazeem inherit a replacer's
        # much lighter tint map after viewing the replacer first).
        cached = _CACHE_MISS if exact else shared_get("image", rel)
        if cached is not _CACHE_MISS:
            return cached
        blob = _fetch_selected_exact(rel) if exact else fetch(rel)
        img = _qimage_from_bytes(blob, log) if blob else None
        if img is not None and img.isNull():
            img = None
        img = _fit_texture(img)
        if img is not None and not exact:
            shared_put("image", (rel,), img)
        return img

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
        # The shader flag, not the suffix, selects the coordinate space.  FO4
        # FaceGen confusingly calls its per-NPC tangent-space map ``_msn``;
        # classifying it like Skyrim's body maps produces severe facial
        # creases and entirely wrong lighting.
        model_space = bool(getattr(shape, "model_space_normals", False))
        # Slot 7 is the dedicated specular map for skin in both Skyrim and
        # FO4 FaceGen. It is part of the key because two skins may share a
        # normal map but use different gloss maps.
        spec_rel = shape.textures[7] if len(shape.textures) > 7 else ""
        key = ("N:", rel.lower(), spec_rel.lower())
        if key in seen:
            return seen[key], model_space
        found = False
        if model_space or spec_rel:
            cached = shared_get("model-normal", rel, spec_rel)
            if cached is not _CACHE_MISS:
                img = cached
                found = True
            else:
                blob = fetch(rel)
                spec_blob = fetch(spec_rel) if spec_rel else None
                found = bool(blob)
                img = (_model_space_normal(blob, spec_blob, log)
                       if blob else None)
                if img is not None:
                    img = _fit_texture(img)
                    shared_put("model-normal", (rel, spec_rel), img)
        else:
            cached = shared_get("image", rel)
            if cached is not _CACHE_MISS:
                img = cached
                found = True
            else:
                blob = fetch(rel)
                found = bool(blob)
                img = _qimage_from_bytes(blob, log) if blob else None
                if img is not None and img.isNull():
                    img = None
                img = _fit_texture(img)
                if img is not None:
                    shared_put("image", (rel,), img)
        if img is not None and img.isNull():
            img = None
        if found and img is None:
            _log(log, f"      ! normal map {rel.replace(chr(92), '/')} found but not decodable")
        elif img is not None:
            _log(log, f"      normal {img.width()}x{img.height()}"
                      f"{' model-space' if model_space else ' tangent-space'}")
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
                seen[key] = decoded_image(rel)
            out.append(seen[key])
        return out[0], out[1]

    def rmaos_map(shape):
        """TruePBR roughness/metallic/AO/specular map from texture slot 5."""
        if not shape.pbr or len(shape.textures) <= 5:
            return None
        rel = shape.textures[5]
        if not rel:
            return None
        key = "RMAOS:" + rel.lower()
        if key not in seen:
            seen[key] = decoded_image(rel)
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
        # The overlay belongs in the key: the head shares its base texture with
        # every other NPC using that skin, but the tint map is per-NPC.
        key = diffuse_key(shape)
        overlay = getattr(shape, "tint_overlay", "") if slot == 0 else ""
        palette_index = getattr(shape, "palette_index", None)
        palette_rel = (palette_slot(shape)
                       if slot == 0 and palette_index is not None
                       and getattr(shape, "greyscale_to_palette", False)
                       else "")
        cache_key = ((key, overlay, palette_rel, round(palette_index, 7))
                     if palette_rel else ((key, overlay) if overlay else key))
        if cache_key in seen:
            return seen[cache_key]
        cached = shared_get("diffuse", rel)
        blob = None
        pre = None
        if cached is not _CACHE_MISS:
            image, is_srgb = cached
        else:
            blob = fetch(rel)
            image = _qimage_from_bytes(blob, log) if blob else None
            if image is not None and image.isNull():
                image = None
            if blob and image is None:
                _log(log, f"      ! {rel.replace(chr(92), '/')} was found but could NOT be "
                          f"decoded ({_fmt_bytes(len(blob))}) - unsupported DDS format?")
            pre = (image.width(), image.height()) if image is not None else None
            image = _fit_texture(image)
            from Utils.assets.dds import is_srgb_dds
            is_srgb = is_srgb_dds(blob) if blob else False
            if image is not None:
                shared_put("diffuse", (rel,), (image, is_srgb))
        if image is not None and overlay:
            # _fetch_exact, not fetch: plenty of NPCs ship no tint map and its
            # absence is normal, so it must not be reported as a missing
            # texture the way a mesh's own maps are.
            tint_img = decoded_image(overlay, exact=True)
            if tint_img is not None and not tint_img.isNull():
                image = _multiply_tint_map(image, tint_img)
                _log(log, f"      face tint {overlay.rsplit('/', 1)[-1]} "
                          f"({tint_img.width()}x{tint_img.height()}) multiplied "
                          f"over {rel.replace(chr(92), '/').rsplit('/', 1)[-1]}")
        if image is not None and palette_rel:
            palette = decoded_image(palette_rel)
            if palette is not None and not palette.isNull():
                image = _remap_palette(image, palette, palette_index)
                _log(log, f"      hair palette {palette_rel.rsplit('/', 1)[-1]} "
                          f"at row {palette_index:.4f}")
        srgb_albedo[key] = is_srgb
        if image is not None:
            shrunk = ("" if pre is None or pre == (image.width(), image.height())
                      else f" (downscaled from {pre[0]}x{pre[1]})")
            _log(log, f"      diffuse {image.width()}x{image.height()}"
                      f"{shrunk}"
                      f"{' sRGB' if srgb_albedo[key] else ''}")
        seen[cache_key] = image
        return image

    load.fetch = fetch
    load.fetch_selected = _fetch_selected_exact
    load.requested = requested
    load.normal_map = normal_map
    load.material_state = material_state
    load.env_maps = env_maps
    load.rmaos_map = rmaos_map
    load.is_srgb = lambda shape: srgb_albedo.get(diffuse_key(shape), False)
    load.missing = missing
    load.cache_stats = cache_stats
    load.decoded_cache = decoded_cache
    return load


def _load_external_geometry(model, fetch):
    """Fill in Starfield shapes: geometry lives in geometries/<path>.mesh."""
    from Utils.assets.starfield import read_sf_mesh
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


def _build_geometry(shape, uv_scale, uv_offset):
    verts, tris = shape.vertices, shape.triangles
    normals = shape.normals
    if len(normals) != len(verts):
        normals = _face_normals(verts, tris)
        # The parsed/assembled model is retained for texture-source and
        # texture-slot reloads. Keep this geometry-only result with it so
        # a 100k-triangle body does not regenerate identical normals on
        # every reload.
        shape.normals = normals
    uvs = shape.uvs
    if len(uvs) != len(verts):
        uvs = [(0.0, 0.0)] * len(verts)
    if uv_scale != (1.0, 1.0) or uv_offset != (0.0, 0.0):
        su, sv = uv_scale
        ou, ov = uv_offset
        uvs = [(u * su + ou, v * sv + ov) for u, v in uvs]
    # No tangents (Skyrim LE data blocks, Starfield, unskinned bodies) just
    # means no normal mapping for that shape; the shader falls back.
    tangents = shape.tangents
    if len(tangents) != len(verts):
        tangents = [(0.0, 0.0, 0.0)] * len(verts)
    signs = getattr(shape, "bitangent_signs", ())
    if len(signs) != len(verts):
        signs = [1.0] * len(verts)
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

    # Compact tangent frame: xyz plus the one bit needed to reconstruct
    # the stored bitangent direction in the shader.
    wtans = [(x, y, z, sign)
             for (x, y, z), sign in zip(wtans, signs)]

    # Interleave pos/normal/uv/tangent(/colour) without a per-vertex
    # Python loop: chain flattens the zipped tuples at C speed.
    groups = (zip(wverts, wnorms, uvs, wtans, colors) if colors is not None
              else zip(wverts, wnorms, uvs, wtans))
    flat = array.array("f", chain.from_iterable(
        chain.from_iterable(groups)))

    # Bounds from strided slices of the final buffer (C speed); the
    # per-shape centroid orders the blended pass.
    fpv = 16 if colors is not None else 12
    xs, ys, zs = flat[0::fpv], flat[1::fpv], flat[2::fpv]
    mlo = (min(xs), min(ys), min(zs))
    mhi = (max(xs), max(ys), max(zs))
    nv = len(verts)
    idx = array.array("I", chain.from_iterable(tris))
    if idx and max(idx) >= nv:
        # Rare corrupt file: drop only the out-of-range triangles.
        idx = array.array("I")
        for a, b, c in tris:
            if a < nv and b < nv and c < nv:
                idx.extend((a, b, c))
    if not idx:
        return None

    return _Geometry(flat, idx, mlo, mhi, colors is not None)


def _build_meshes(model, load_texture, cancel=None, geometry_cache=None):
    """Build materials and reuse geometry cached for this prepared model."""
    meshes: list[_Mesh] = []
    lo = [float("inf")] * 3
    hi = [float("-inf")] * 3
    # The FaceGen head's own extent, kept apart from the assembled actor's so
    # a portrait can frame the face even with a whole body on screen.
    head_lo = [float("inf")] * 3
    head_hi = [float("-inf")] * 3

    for shape_index, shape in enumerate(model.shapes):
        if cancel is not None and cancel():
            return [], None, None
        if getattr(shape, "hidden", False):
            continue
        external_material = (load_texture.material_state(shape)
                             if hasattr(load_texture, "material_state")
                             else None)
        verts = shape.vertices
        tris = shape.triangles
        if not verts or not tris:
            continue
        uv_scale = tuple(external_material.uv_scale
                         if external_material is not None
                         else getattr(shape, "uv_scale", (1.0, 1.0)))
        uv_offset = tuple(external_material.uv_offset
                          if external_material is not None
                          else getattr(shape, "uv_offset", (0.0, 0.0)))
        key = (uv_scale, uv_offset)
        cached = (geometry_cache.get(shape_index)
                  if geometry_cache is not None else None)
        if cached is not None and cached[0] == key:
            geometry = cached[1]
        else:
            geometry = _build_geometry(shape, uv_scale, uv_offset)
            if geometry_cache is not None:
                geometry_cache[shape_index] = (key, geometry)
        if geometry is None:
            continue
        flat, idx = geometry.verts, geometry.indices
        mlo, mhi = geometry.lo, geometry.hi
        for k in range(3):
            lo[k] = min(lo[k], mlo[k])
            hi[k] = max(hi[k], mhi[k])
            if getattr(shape, "is_head", False):
                head_lo[k] = min(head_lo[k], mlo[k])
                head_hi[k] = max(head_hi[k], mhi[k])

        image = load_texture(shape)
        nrm_img, model_space = (load_texture.normal_map(shape)
                                if hasattr(load_texture, "normal_map")
                                else (None, False))
        # BodySlide drives its highlight from these material fields; a mesh
        # with SLSF1_Specular off renders matte (strength 0).
        strength = shape.spec_strength if shape.spec_enabled else 0.0
        # TruePBR repurposes the whole block: its values are handed separately
        # to the PBR path below, never fed into the legacy Blinn-Phong lobe.
        if shape.pbr or shape.glossiness < 1.0:
            strength = 0.0
        spec = (*shape.spec_color, strength, max(1.0, shape.glossiness))
        env_img, mask_img = (load_texture.env_maps(shape)
                             if hasattr(load_texture, "env_maps") and not shape.pbr
                             else (None, None))
        # NiAlphaProperty thresholds are 0-255; GL compares against 0-1.
        # Only useful with a texture - an untextured shape has alpha 1.
        material_alpha_test = bool(external_material is not None
                                   and external_material.alpha_test)
        alpha_test = shape.alpha_test or material_alpha_test
        alpha_threshold = (external_material.alpha_threshold
                           if material_alpha_test else shape.alpha_threshold)
        alpha_blend = (shape.alpha_blend
                       or bool(external_material is not None
                               and external_material.alpha_blend))
        thr = (alpha_threshold / 255.0
               if alpha_test and image is not None else -1.0)
        clamp_mode = (external_material.texture_clamp_mode
                      if external_material is not None
                      else getattr(shape, "texture_clamp_mode", 3))
        double_sided = (external_material.double_sided
                        if external_material is not None
                        else getattr(shape, "double_sided", False))
        depth_test = (external_material.depth_test
                      if external_material is not None
                      else getattr(shape, "depth_test", True))
        depth_write = (external_material.depth_write
                       if external_material is not None
                       else getattr(shape, "depth_write", True))
        centre = tuple((mlo[k] + mhi[k]) * 0.5 for k in range(3))
        meshes.append(_Mesh(shape.name, flat, idx, image, len(idx) // 3,
                            nrm_img, model_space, spec,
                            env_img, mask_img,
                            shape.env_map_scale if env_img else 0.0,
                            thr, alpha_blend and image is not None,
                            centre, geometry.has_colors, shape.tint,
                            load_texture.rmaos_map(shape)
                            if hasattr(load_texture, 'rmaos_map') else None,
                            shape.pbr,
                            (shape.glossiness, shape.spec_strength),
                            bool(load_texture.is_srgb(shape))
                            if hasattr(load_texture, 'is_srgb') else False,
                            clamp_mode, double_sided, depth_test, depth_write,
                            geometry=geometry))

    if not meshes:
        return [], None, None
    bounds = (tuple(lo), tuple(hi))
    head = ((tuple(head_lo), tuple(head_hi))
            if head_lo[0] != float("inf") else None)
    return meshes, bounds, head


def _render_passes(meshes, textured: bool):
    """Split meshes into opaque, late-solid and alpha-blended passes.

    Hair commonly alpha-tests its cards but disables depth WRITES. Drawing it
    with ordinary opaque geometry lets a subsequently drawn collar overwrite
    strands that are actually closer to the camera. The engine queues those
    no-write cutouts after depth-writing surfaces: they still depth-TEST
    against the jacket, so front strands pass and rear strands remain hidden.
    Fully blended layers stay last and are sorted separately by the caller.
    """
    opaque = []
    late_solid = []
    blended = []
    for mesh in meshes:
        if (mesh.alpha_blend and textured
                and getattr(mesh, "texture", None) is not None):
            blended.append(mesh)
        elif not mesh.depth_write or not mesh.depth_test:
            late_solid.append(mesh)
        else:
            opaque.append(mesh)
    return opaque, late_solid, blended


def _depth_write_for_draw(mesh, solid: bool, use_texture: bool) -> bool:
    """Return the depth-mask state needed for this viewport draw.

    FO4's outer hair cards are alpha-tested (therefore opaque wherever they
    survive the cutout) but commonly carry ``SLSF2_ZBuffer_Write`` disabled.
    Keeping that flag verbatim leaves only the head in the depth buffer.  The
    separately blended, scalp-hugging hairline then passes its depth test and
    is composited over the outer cards, exposing a head-shaped silhouette.

    The late-solid pass has already put clothing and skin in the depth buffer,
    so letting textured cutouts establish their own depth here preserves the
    jacket ordering and makes subsequent translucent layers respect the outer
    hair surface.  True alpha blends and non-textured diagnostic modes retain
    the source material's depth-write state.
    """
    return bool(mesh.depth_write or (
        solid and use_texture and getattr(mesh, "depth_test", True)
        and not mesh.alpha_blend
        and mesh.alpha_threshold >= 0.0))


def _release_mesh_buffers(meshes) -> None:
    for m in meshes:
        for obj in (m.vao, m.vbo, m.ibo):
            if obj is not None:
                try:
                    obj.destroy()
                except RuntimeError:
                    pass
        m.vao = m.vbo = m.ibo = None
        m.texture = m.normal_tex = m.env_tex = m.mask_tex = m.rmaos_tex = None


def _neutralise_meshes(meshes, textures=()) -> None:
    """Sever GL wrappers whose context is gone: QOpenGLTexture's destructor
    dereferences its creation context (areSharing), so letting GC run it after
    the context died segfaults. invalidate() leaks the tiny C++ shell instead;
    the GPU memory goes with the context's share group."""
    import shiboken6
    seen = set()
    for obj in textures:
        if obj is not None and id(obj) not in seen:
            seen.add(id(obj))
            try:
                shiboken6.invalidate(obj)
            except Exception:                            # noqa: BLE001
                pass
    for m in meshes:
        for obj in (m.vao, m.vbo, m.ibo, m.texture, m.normal_tex,
                    m.env_tex, m.mask_tex, m.rmaos_tex):
            if obj is not None and id(obj) not in seen:
                seen.add(id(obj))
                try:
                    shiboken6.invalidate(obj)
                except Exception:                        # noqa: BLE001
                    pass
        m.vao = m.vbo = m.ibo = m.texture = m.normal_tex = None
        m.env_tex = m.mask_tex = m.rmaos_tex = None


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


def _add_parts(model, parts, plugin_dirs, log, cancel, bones=None,
               skin_tint=None, hair_mesh_rel: str = "") -> None:
    """Append an actor's other meshes - body, hands, armour - to *model*.

    Each part is parsed, texture-overridden and POSED on its own: overrides are
    keyed on the mesh path, and an unskinned piece (a shield) needs the
    skeleton node its own slot names, which only makes sense per part.
    """
    from Utils.assets.nif import read_nif
    from Utils.assets.skinning import morph_weight_model, pose_model
    from Utils.assets.texture_sets import apply_alt_textures
    hair_ctx = None
    if hair_mesh_rel:
        # A FO4 hat can replace the hidden scalp hair with a wig built into
        # the armour NIF. It still uses the NPC's CLFM palette row, so retain
        # the FACE's plugin context while loading the outfit's own textures.
        from Utils.npc.facegen import FormsContext
        hair_ctx = FormsContext(plugin_dirs)
    for entry in parts:
        data, rel = entry[0], entry[1]
        attach = entry[2] if len(entry) > 2 else ""
        low_data = entry[3] if len(entry) > 3 else None
        weight = entry[4] if len(entry) > 4 else 1.0
        skin_textures = entry[5] if len(entry) > 5 else ()
        # Each part's OWN mod, so its plugin's TXST overrides are seen; the
        # head's plugins know nothing about the armour's dead texture paths.
        part_dirs = (entry[6] if len(entry) > 6 and entry[6] else plugin_dirs)
        if cancel():
            return
        try:
            part = read_nif(data)
        except Exception as exc:                         # noqa: BLE001
            _log(log, f"  ! body part {rel} could not be parsed: {exc!r}")
            continue
        morphed = 0
        if low_data is not None and weight < 0.999:
            try:
                morphed = morph_weight_model(part, read_nif(low_data), weight)
            except Exception as exc:                     # noqa: BLE001
                _log(log, f"  ! body weight morph failed for {rel}: {exc!r}")
        if rel and part_dirs:
            try:
                n = apply_alt_textures(part, rel, part_dirs, cancel=cancel)
                if n:
                    _log(log, f"      {rel.rsplit('/', 1)[-1]}: {n} shape(s) "
                              f"retextured from its mod's plugin")
            except Exception:                            # noqa: BLE001
                pass
        texture_hits = 0
        if skin_textures:
            for shape in part.shapes:
                # ARMA NAM0/NAM1 is a direct runtime texture set for skin
                # shader shapes, independent of the mesh's alternate textures.
                if getattr(shape, "lighting_shader_type", 0) == 5:
                    shape.textures = list(skin_textures)
                    texture_hits += 1
        hair_tinted = 0
        if (hair_ctx is not None
                and getattr(getattr(part, "header", None), "bs_version", 0)
                == 130):
            try:
                from Utils.npc.facegen import apply_hair_tint
                hair_tinted = apply_hair_tint(
                    part, hair_mesh_rel, plugin_dirs, hair_ctx)
            except Exception:                            # noqa: BLE001
                hair_tinted = 0
        # Cloth/HDT nifs often carry skinned collision volumes alongside the
        # visible garment. They are BSTriShapes with geometry but no shader at
        # all (CapeProxy, PantsCol, Stabilizer, ...). Skyrim feeds those to the
        # physics system; treating them as ordinary untextured geometry puts
        # opaque clay shells over the actor.
        part.shapes, helpers = _actor_visible_shapes(part.shapes)
        posed = pose_model(part, bones, attach) if bones else 0
        tinted = _tint_skin_shapes(part.shapes, skin_tint)
        _log(log, f"  + {rel.rsplit('/', 1)[-1]}: {len(part.shapes)} shape(s)"
                  f", posed {posed}"
                  + (f", weight {weight * 100:.0f} ({morphed} morphed)"
                     if morphed else "")
                  + (f", skin texture-set {texture_hits}"
                     if texture_hits else "")
                  + (f", skin-tinted {tinted}" if tinted else "")
                  + (f", hair-palette {hair_tinted}"
                     if hair_tinted else "")
                  + (f", hid {len(helpers)} physics helper(s)"
                     if helpers else "")
                  + (f", attached to {attach}" if attach else ""))
        model.shapes.extend(part.shapes)


def _replace_head_parts(model, parts, plugin_dirs, log, cancel) -> None:
    """Replace baked scalp hair with FO4 HDPT models selected by the ESP.

    FO4 runtime hair is commonly BSSkin-authored in actor/skeleton space,
    whereas a baked FaceGeom head is centred on the head itself. ``read_nif``
    applies the dominant bone bind that brings the part into that face-local
    space. Detach its skin metadata here so the later whole-actor pose does
    not undo the normalisation and put the hair a head-height above the NPC.
    """
    from Utils.npc.facegen import remove_hair
    from Utils.assets.nif import read_nif
    from Utils.assets.texture_sets import apply_alt_textures

    removed = remove_hair(model)
    added = 0
    for data, rel, textures, part_dirs in parts:
        if cancel():
            return
        try:
            part = read_nif(data)
        except Exception as exc:                         # noqa: BLE001
            _log(log, f"  ! head part {rel} could not be parsed: {exc!r}")
            continue
        dirs = part_dirs or plugin_dirs
        if rel and dirs:
            try:
                apply_alt_textures(part, rel, dirs, cancel=cancel)
            except Exception:                            # noqa: BLE001
                pass
        if textures:
            for shape in part.shapes:
                shape.textures = list(textures)
        part.shapes, helpers = _actor_visible_shapes(part.shapes)
        detached = _detach_head_part_skinning(part.shapes)
        model.shapes.extend(part.shapes)
        added += len(part.shapes)
        _log(log, f"  + runtime head part {rel.rsplit('/', 1)[-1]}: "
                  f"{len(part.shapes)} shape(s)"
                  + (f", fixed {detached} to face space" if detached else "")
                  + (f", hid {len(helpers)} helper(s)" if helpers else ""))
    _log(log, f"  ESP hairstyle: replaced {removed} baked shape(s) with "
              f"{added} runtime shape(s)")


def _detach_head_part_skinning(shapes) -> int:
    """Keep a parsed runtime head part in its bind-normalised face space."""
    detached = 0
    for shape in shapes:
        if not (shape.bones or shape.binds or shape.skin_weights):
            continue
        shape.bones = []
        shape.binds = []
        shape.skin_weights = []
        detached += 1
    return detached


def _apply_eye_textures(model, textures) -> int:
    """Apply an FO4 eye HDPT's TNAM set to the baked eyeball geometry."""
    if not textures:
        return 0
    changed = 0
    for shape in model.shapes:
        name = (shape.name or "").lower()
        if ("eye" in name
                and not any(part in name for part in ("wet", "ao", "lash"))):
            shape.textures = list(textures)
            changed += 1
    return changed


def _tint_skin_shapes(shapes, skin_tint) -> int:
    """Apply NPC texture lighting to shader-subtype-5 skin shapes."""
    if skin_tint is None:
        return 0
    tinted = 0
    for shape in shapes:
        # The marker catches exposed skin inside outfit meshes without
        # recolouring the clothing around it. On FO4 the factor may exceed 1.
        if getattr(shape, "lighting_shader_type", 0) == 5:
            shape.tint = skin_tint
            tinted += 1
    return tinted


def _tint_face_shapes(shapes, face_skin_tint) -> int:
    """Correct a baked FO4 face from its baseline QNAM to the winning one."""
    if face_skin_tint is None:
        return 0
    tinted = 0
    for shape in shapes:
        # FO4's FaceCustomization front head uses shader subtype 4. Generic
        # rear-head/body skin is subtype 5 and receives the absolute QNAM via
        # _tint_skin_shapes instead.
        if getattr(shape, "lighting_shader_type", 0) != 4:
            continue
        current = getattr(shape, "tint", (1.0, 1.0, 1.0))
        shape.tint = tuple(current[i] * face_skin_tint[i] for i in range(3))
        tinted += 1
    return tinted


def _actor_visible_shapes(shapes):
    """Separate game-rendered actor shapes from shaderless physics helpers.

    A named texture may still be missing and must remain visible as clay so
    the diagnostic can report it. Only a shape with no shader, material or
    texture reference is excluded.
    """
    visible = []
    helpers = []
    for shape in shapes:
        textures = getattr(shape, "textures", ()) or ()
        if (getattr(shape, "shader_type", "")
                or getattr(shape, "material", "")
                or any(textures)):
            visible.append(shape)
        else:
            helpers.append(shape)
    return visible, helpers


def _hide_facegen_runtime_shapes(model, mesh_rel: str) -> list[str]:
    """Drop FO4 FaceGen geometry that is enabled only by an engine event.

    Fallout 4 bakes ``MaleNeckGore``/``FemaleNeckGore`` into otherwise normal
    NPC FaceGeom files.  The game reveals that surface after dismemberment;
    drawing every NIF shape unconditionally leaves pieces of the internal
    gore shell protruding through the scalp and temples on an intact preview.

    Match the explicit shape role rather than its meat texture: that keeps
    ordinary scars, mouth parts and the rear-head cap visible, and also works
    for replacers that supply their own gore material.
    """
    rel = (mesh_rel or "").replace("\\", "/").lower()
    if (getattr(getattr(model, "header", None), "bs_version", 0) != 130
            or "/facegendata/facegeom/" not in f"/{rel}"):
        return []
    hidden = [shape.name for shape in model.shapes
              if (shape.name or "").lower().endswith("neckgore")]
    if hidden:
        model.shapes[:] = [shape for shape in model.shapes
                           if not (shape.name or "").lower().endswith(
                               "neckgore")]
    return hidden


def _pose_actor(model, bones, log) -> None:
    """Place the head's own shapes in the skeleton's space."""
    from Utils.assets.skinning import pose_model
    posed = pose_model(model, bones)
    _log(log, f"  posed {posed}/{len(model.shapes)} head shape(s) "
              f"against {len(bones)} bones")


def _read_bones(skeleton_data, log):
    from Utils.assets.skinning import read_skeleton
    try:
        bones = read_skeleton(skeleton_data)
    except Exception as exc:                             # noqa: BLE001
        _log(log, f"  ! skeleton could not be read: {exc!r}")
        return {}
    if not bones:
        _log(log, "  ! skeleton named no bones - parts left unposed")
    return bones


def _model_cache_key(source, texture_roots, archive_roots, resolver, archives,
                     mesh_rel, plugin_dirs, parts=None, skeleton=None,
                     skin_tint=None, hide_hair=False, head_parts=None,
                     eye_textures=None, face_morph=None,
                     face_skin_tint=None):
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
    # Parts and skeleton change the SHAPES of the cached model, so a head-only
    # parse must never be reused for a whole actor (or the reverse).
    parts_key = tuple((e[1], e[4] if len(e) > 4 else 1.0,
                       bool(e[3]) if len(e) > 3 else False,
                       tuple(e[5]) if len(e) > 5 else ())
                      for e in (parts or ()))
    head_key = tuple((entry[1], tuple(entry[2]))
                     for entry in (head_parts or ()))
    morph_key = ()
    if face_morph:
        tri, weights, rel = face_morph
        morph_key = (rel, id(tri), len(tri),
                     tuple(sorted((name, round(value, 7))
                                  for name, value in weights.items())))
    return (source_key, mesh_rel.replace("\\", "/").lower(),
            path_key(texture_roots), path_key(archive_roots),
            path_key(effective_plugins), id(resolver), id(archives),
            parts_key, bool(skeleton), skin_tint, bool(hide_hair), head_key,
            tuple(eye_textures or ()), morph_key, face_skin_tint)


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
        if s.vertex_colors:
            flags.append("vcolor" if s.colors else "vcolor-flag-no-data")
        elif s.colors:
            flags.append("vcolor-data-ignored")
        if s.alpha_test:
            flags.append(f"alpha-test>{s.alpha_threshold}")
        if s.alpha_blend:
            flags.append("alpha-blend")
        if not getattr(s, "depth_test", True):
            flags.append("no-depth-test")
        if not getattr(s, "depth_write", True):
            flags.append("no-depth-write")
        if getattr(s, "double_sided", False):
            flags.append("two-sided")
        if s.env_map_scale:
            flags.append(f"envmap x{s.env_map_scale:.2f}")
        if s.tint != (1.0, 1.0, 1.0):
            flags.append("baked-tint=" + ",".join(
                str(round(c * 255)) for c in s.tint))
        if s.material:
            flags.append(f"material={s.material}")
        if s.mesh_path:
            flags.append(f"geom={s.mesh_path}")
        material = (f"PBR spec-level {s.glossiness:.3f} "
                    f"roughness x{s.spec_strength:.2f}"
                    if s.pbr else
                    f"gloss {s.glossiness:.0f} spec {s.spec_strength:.2f}")
        _log(log, f"    shape {s.name!r} [{s.block_type}] "
                  f"{len(s.vertices)}v/{len(s.triangles)}t"
                  f" · {material}"
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
    pbrs = sum(1 for m in meshes if m.pbr)
    rmaos = sum(1 for m in meshes if m.rmaos_image is not None)
    srgb = sum(1 for m in meshes if m.srgb_albedo)
    envs = sum(1 for m in meshes if m.env_image is not None)
    _log(log, f"  totals: {tris} triangles · {textured}/{len(meshes)} textured"
              f" · {normals} normal-mapped · {vcols} vertex-coloured"
              f" · {pbrs} PBR ({rmaos} RMAOS)"
              f" · {srgb} sRGB-decoded · {envs} env-mapped")
    cache_stats = getattr(loader, "cache_stats", None)
    decoded_cache = getattr(loader, "decoded_cache", None)
    if cache_stats and decoded_cache is not None:
        items, size = decoded_cache.usage
        _log(log, f"  decoded texture cache: {cache_stats['hits']} hit(s), "
                  f"{cache_stats['misses']} miss(es) · {items} item(s), "
                  f"{_fmt_bytes(size)} retained")
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
    _neutralise_meshes(list(view._meshes) + list(view._pending or ()),
                       view._gpu_textures.values())
    view._gpu_textures.clear()
    view._meshes = []
    view._pending = None


def _sheet_panel(image: QImage, width: int, height: int) -> QImage:
    """Centre-crop *image* to a panel without stretching it."""
    target_aspect = width / max(1, height)
    source_aspect = image.width() / max(1, image.height())
    if source_aspect > target_aspect:
        crop_h = image.height()
        crop_w = max(1, round(crop_h * target_aspect))
        crop_x = (image.width() - crop_w) // 2
        rect = QRect(crop_x, 0, crop_w, crop_h)
    else:
        crop_w = image.width()
        crop_h = max(1, round(crop_w / target_aspect))
        crop_y = (image.height() - crop_h) // 2
        rect = QRect(0, crop_y, crop_w, crop_h)
    return image.copy(rect).scaled(width, height, Qt.IgnoreAspectRatio,
                                   Qt.SmoothTransformation)


def _compose_turntable_sheet(frames: list[QImage], portrait: QImage,
                             transparent: bool = False,
                             fill: str | None = None,
                             max_height: int = _SHEET_MAX_HEIGHT):
    """Lay four actor frames and a close-up into one reference-sheet image."""
    if len(frames) != 4 or portrait is None or portrait.isNull():
        return None
    images = [*frames, portrait]
    if any(image is None or image.isNull() for image in images):
        return None

    # Restrict height to what every source can supply at its panel ratio. This
    # normally resolves to the framebuffer height, but also behaves sensibly
    # when the viewer has been squeezed unusually narrow in the splitter.
    usable = [image.height() for image in images]
    usable.extend(int(image.width() / _SHEET_BODY_ASPECT)
                  for image in frames)
    usable.append(int(portrait.width() / _SHEET_FACE_ASPECT))
    height = min(max_height, *usable)
    if height <= 0:
        return None
    body_w = max(1, round(height * _SHEET_BODY_ASPECT))
    face_w = max(1, round(height * _SHEET_FACE_ASPECT))
    # Premultiplied for the transparent sheet: that is what the GL grab hands
    # back, and both the smooth scale in _sheet_panel and the compositing
    # below are only correct on premultiplied data. Qt un-premultiplies again
    # when it writes the PNG.
    if transparent:
        sheet = QImage(body_w * 4 + face_w, height,
                       QImage.Format_ARGB32_Premultiplied)
        sheet.fill(Qt.transparent)
    else:
        sheet = QImage(body_w * 4 + face_w, height, QImage.Format_RGB32)
        sheet.fill(QColor(fill or BACKGROUNDS["dark"]))
    painter = QPainter(sheet)
    try:
        x = 0
        for frame in frames:
            panel = _sheet_panel(frame, body_w, height)
            painter.drawImage(x, 0, panel)
            x += body_w
        painter.drawImage(x, 0, _sheet_panel(portrait, face_w, height))
    finally:
        painter.end()
    return sheet


class _Viewport(QOpenGLWidget):
    """The GL canvas: turntable/free-camera orbit over the parsed shapes."""

    # meshes, bounds, gen, tex paths, head bounds
    loaded = Signal(object, object, int, object, object)
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
        # An alpha channel in the default framebuffer: without one Qt hands
        # back an opaque RGB32 from grabFramebuffer() and a transparent export
        # is impossible. On screen the clear below keeps alpha at 1, so the
        # canvas still paints solid.
        fmt.setAlphaBufferSize(8)
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
        self._u_tint = self._u_rmaostex = self._u_pbr = self._u_hasrmaos = -1
        self._u_pbrparams = self._u_srgb = -1
        self._meshes: list[_Mesh] = []
        self._gpu_textures = {}
        self._pending: list[_Mesh] | None = None
        self._uploaded = False
        self._gl_error = ""
        self._generation = 0
        self._load_jobs = LatestWorker("nif-preview-load")
        self._decoded_textures = _DecodedTextureCache()
        # Parsing, plugin overrides, hair tint and Starfield .mesh expansion
        # are invariant when only the texture source/slot changes.
        self._cached_model_key = None
        self._cached_model = None
        self._cached_geometry = {}
        self._reload_args = None
        self._needs_reload = False
        self._keep_view = False
        # Built on the first resize; see resizeEvent for why paints pause.
        self._resize_hold = None

        self._right, self._up, self._forward = self._basis_for(
            _HOME_YAW, _HOME_PITCH)
        self._distance = 100.0
        # (yaw, pitch, look-at height as a fraction of the model) or None for
        # the mesh-browser 3/4 default. Set by a host that shows ACTORS.
        self.home_view = None
        self._source_key = None
        self._framed_source = None
        # Set only while rendering into an export FBO, so _mvp uses that
        # size's aspect instead of the widget's.
        self._render_size = None
        self._bounds = None
        self._head_bounds = None
        self._center = QVector3D(0, 0, 0)    # rotation pivot: the mesh centre
        # Pan lives in view-plane coordinates, not world space: rotation then
        # always spins the asset about its own centre instead of arcing a
        # panned view across the screen.
        self._pan = [0.0, 0.0]
        self._home = (self._copy_camera_basis(), self._distance,
                      QVector3D(0, 0, 0))
        self._last_pos = None
        self._last_buttons = Qt.NoButton
        self.free_camera = False
        self.wireframe = WIRE_OFF
        self.cull_backfaces = False
        self.textured = True
        self.texture_slot = 0
        self.detail = True          # normal maps + specular
        self.invert_mouse = True
        self._bg = QColor(BACKGROUNDS["light"])
        self._base = (0.40, 0.39, 0.37)
        self._gamma = 1.0
        # Set only for the duration of an export grab; never while painting to
        # the screen, where a see-through canvas would show the window behind.
        self._clear_transparent = False

        self.setMinimumSize(1, 1)
        self.setFocusPolicy(Qt.StrongFocus)
        self.loaded.connect(self._on_loaded)

    # -- loading ------------------------------------------------------------
    def load(self, source, texture_roots: list[Path], archive_roots=None,
             resolver=None, archives=None, tex_override=None,
             mesh_rel: str = "", plugin_dirs=None, parts=None, skeleton=None,
             skin_tint=None, hide_hair: bool = False,
             head_parts=None, eye_textures=None, face_morph=None,
             face_skin_tint=None,
             keep_view: bool = False):
        """Parse and build *source* (path or raw bytes) off-thread, then swap.

        *archives* lets a mesh read from inside a BSA find its own textures.
        *mesh_rel* (the mesh's data-relative path) enables plugin TXST
        overrides, scanned from *plugin_dirs* (default: the texture roots).

        *parts* are extra meshes to show in the same scene - an actor's body,
        hands and feet alongside its head. They may include the ``_0.nif``
        endpoint and NPC weight used to morph a ``_1.nif`` part. *skeleton* is
        a skeleton nif's bytes; without it each mesh sits in its own bones'
        space and the pieces pile up on each other rather than assembling.
        """
        self._generation += 1
        gen = self._generation
        # keep_view: same mesh, new textures - don't snap the camera back.
        self._keep_view = bool(keep_view)
        if mesh_rel:
            self._source_key = ("asset", mesh_rel.replace("\\", "/").lower())
        elif isinstance(source, (bytes, bytearray)):
            self._source_key = ("memory", id(source))
        else:
            self._source_key = ("path", str(Path(source)))
        # Kept so the mesh can be rebuilt after a context loss (tab detach).
        self._reload_args = (source, texture_roots, archive_roots,
                             resolver, archives, tex_override,
                             mesh_rel, plugin_dirs, parts, skeleton, skin_tint,
                             hide_hair, head_parts, eye_textures, face_morph,
                             face_skin_tint)
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
                  f" · archive index: {'yes' if archives is not None else 'no'}"
                  f" · texture slot: {self.texture_slot}"
                  f" · override: {'yes' if tex_override else 'no'}")
        if mesh_rel:
            _log(log, f"  data-relative path: {mesh_rel}")

        def work():
            import time
            from Utils.assets.nif import read_nif
            t_start = time.monotonic()
            try:
                model_key = _model_cache_key(
                    source, texture_roots, archive_roots, resolver, archives,
                    mesh_rel, plugin_dirs, parts, skeleton, skin_tint,
                    hide_hair, head_parts, eye_textures, face_morph,
                    face_skin_tint)
                extra = archives
                if extra is None and archive_roots:
                    from Utils.archives.lookup import ArchiveLookup, find_archives
                    found = find_archives(archive_roots)
                    _log(log, f"  scanned {len(archive_roots)} archive root(s):"
                              f" {len(found)} archive(s) indexed")
                    extra = ArchiveLookup(found, keep_prefix=ASSET_PREFIXES)
                loader = _make_texture_loader(texture_roots, extra, resolver,
                                              tex_override, self.texture_slot,
                                              log,
                                              cancel=lambda: gen != self._generation,
                                              decoded_cache=self._decoded_textures)
                if (model_key == self._cached_model_key
                        and self._cached_model is not None):
                    model = self._cached_model
                    geometry_cache = self._cached_geometry
                    _log(log, "  reused parsed model and plugin/geometry lookups")
                else:
                    t0 = time.monotonic()
                    model = read_nif(source)
                    _log(log, f"  parsed in "
                              f"{(time.monotonic() - t0) * 1000:.0f}ms")
                    if gen != self._generation:
                        return
                    hidden_runtime = _hide_facegen_runtime_shapes(
                        model, mesh_rel)
                    if hidden_runtime:
                        _log(log, "  hid engine-controlled FaceGen shape(s): "
                                  + ", ".join(hidden_runtime))
                    _log_model(log, model)
                    if mesh_rel:
                        # The game may swap the baked texture set via plugin
                        # records; without this such meshes preview as white clay.
                        from Utils.assets.texture_sets import apply_alt_textures
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
                        if head_parts:
                            try:
                                _replace_head_parts(
                                    model, head_parts, dirs, log,
                                    lambda: gen != self._generation)
                            except Exception as exc:     # noqa: BLE001
                                _log(log, f"  ! runtime head parts failed: {exc!r}")
                        if eye_textures:
                            changed = _apply_eye_textures(model, eye_textures)
                            _log(log, f"  ESP eye texture-set applied to "
                                      f"{changed} shape(s)")
                        if face_morph:
                            try:
                                from Utils.npc.fo4 import apply_face_morphs
                                tri, weights, tri_rel = face_morph
                                applied, vertices = apply_face_morphs(
                                    model, tri, weights)
                                _log(log, f"  ESP face sliders: {applied}/"
                                          f"{len(weights)} morph(s), "
                                          f"{vertices} vertices via "
                                          f"{tri_rel.rsplit('/', 1)[-1]}")
                            except Exception as exc:     # noqa: BLE001
                                _log(log, f"  ! runtime face morph failed: {exc!r}")
                        # Skyrim ships hair textures greyscale and tints them from
                        # the NPC record, so FaceGen hair is white without this.
                        try:
                            from Utils.npc.facegen import apply_hair_tint
                            t0 = time.monotonic()
                            n = apply_hair_tint(model, mesh_rel, dirs)
                            if n:
                                remap = next((s.palette_index for s in model.shapes
                                              if s.palette_index is not None), None)
                                if remap is not None:
                                    detail = f"palette row {remap:.4f}"
                                else:
                                    tint = next((s.tint for s in model.shapes
                                                 if s.tint != (1.0, 1.0, 1.0)), None)
                                    detail = (str(tuple(round(c * 255) for c in tint))
                                              if tint else "unknown colour")
                                _log(log, f"  hair tint {detail} applied to {n} shape(s)"
                                          f" ({(time.monotonic() - t0) * 1000:.0f}ms)")
                        except Exception as exc:         # noqa: BLE001
                            _log(log, f"  ! hair tint lookup failed: {exc!r}")
                        # The baked mesh names a generic head texture; makeup,
                        # brows and skin tone live in the per-NPC FaceTint map
                        # the engine multiplies over it.
                        try:
                            from Utils.npc.facegen import apply_face_tint
                            apply_face_tint(model, mesh_rel)
                        except Exception as exc:         # noqa: BLE001
                            _log(log, f"  ! face tint lookup failed: {exc!r}")
                    if hide_hair:
                        # A hood or helmet claims the hair slot, so the engine
                        # hides the hair. The baked head still carries it and
                        # it would grow straight through the hat.
                        try:
                            from Utils.npc.facegen import remove_hair
                            gone = remove_hair(model)
                            _log(log, f"  outfit covers the hair slot: "
                                      f"{gone} hair shape(s) hidden")
                        except Exception as exc:         # noqa: BLE001
                            _log(log, f"  ! hiding hair failed: {exc!r}")
                    face_skin = _tint_face_shapes(
                        model.shapes, face_skin_tint)
                    if face_skin:
                        detail = ", ".join(
                            f"{value:.3f}" for value in face_skin_tint)
                        _log(log, f"  corrected {face_skin} baked face shape(s) "
                                  f"by winning/baseline skin ratio ({detail})")
                    # FO4 bakes the front head into FaceCustomization, but its
                    # rear-head/neck cap remains generic subtype-5 skin.
                    head_skin = _tint_skin_shapes(model.shapes, skin_tint)
                    if head_skin:
                        _log(log, f"  skin-tinted {head_skin} head shape(s)")
                    # Tag the FACE shape - not the hair - before the body is
                    # appended. A portrait framed on head+hair shrinks the face
                    # to fit a tall hairstyle in; framing on the face itself
                    # keeps every NPC the same size and simply crops the hair.
                    if "facegeom" in mesh_rel.replace("\\", "/").lower():
                        try:
                            from Utils.npc.facegen import head_shape
                            _face = head_shape(model)
                        except Exception:                # noqa: BLE001
                            _face = None
                        if _face is not None:
                            _face.is_head = True
                    bones = _read_bones(skeleton, log) if skeleton else {}
                    # The head first, so its shapes are posed before the body
                    # meshes are appended and posed with their own attachments.
                    if bones:
                        _pose_actor(model, bones, log)
                    if parts:
                        _add_parts(model, parts, plugin_dirs or texture_roots,
                                   log, lambda: gen != self._generation, bones,
                                   skin_tint, mesh_rel)
                    if gen != self._generation:
                        return
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
                    geometry_cache = {}
                    self._cached_geometry = geometry_cache
                t0 = time.monotonic()
                meshes, bounds, head_bounds = _build_meshes(
                    model, loader, cancel=lambda: gen != self._generation,
                    geometry_cache=geometry_cache)
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
                      list(dict.fromkeys(loader.requested)), head_bounds)

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
        self._cached_geometry = {}
        self._framed_source = None
        if self.context() is not None:
            try:
                self.makeCurrent()
                self._release_gpu()
                self.doneCurrent()
            except RuntimeError:
                _neutralise_view(self)
        else:
            _neutralise_view(self)
        self._uploaded = False
        self._bounds = None
        self._head_bounds = None
        self.update()

    def _on_loaded(self, meshes, bounds, gen, _tex_paths=None,
                   head_bounds=None):
        if gen != self._generation:
            return                                   # a newer file won the race
        self._pending = meshes
        self._uploaded = False
        self._bounds = bounds
        self._head_bounds = head_bounds
        # Variants share a camera even when their bounds differ. A reload
        # superseding the first load must still frame its new asset.
        if bounds is not None and (not self._keep_view
                                   or self._source_key != self._framed_source):
            self._frame(bounds)
            self._framed_source = self._source_key
            yaw, pitch = self._camera_angles()
            _log(self.log_fn,
                 f"  framed: yaw {math.degrees(yaw):.0f}° "
                 f"pitch {math.degrees(pitch):.0f}° "
                 f"dist {self._distance:.0f} "
                 f"look-at z {self._center.z():.1f}"
                 f"{' (actor home)' if self.home_view else ''}")
        else:
            _log(self.log_fn,
                 f"  camera HELD: {'no bounds' if bounds is None else 'keep_view'}"
                 f" - dist {self._distance:.0f} look-at z {self._center.z():.1f}")
        self._keep_view = False
        self.update()

    def capture_portrait(self, size: int = 308):
        """A front-on still of the head, or None.

        Rendered by re-running the normal paint with the camera moved, then
        moved straight back: one extra frame per NPC rather than a second GL
        context. Skyrim heads face +Y (verified over 119/120 vanilla FaceGen
        meshes: the eyes sit on the +Y side of the head's centre), so a yaw of
        +90 degrees looks the actor in the face.

        Framed head-and-shoulders by _aim_at_face, the same framing the
        exported mugshot uses, so this corner thumbnail previews the file.
        """
        bounds = self._head_bounds
        if bounds is None or not (self._meshes or self._pending):
            return None
        if self.context() is None:
            return None
        saved = (self._copy_camera_basis(), self._distance,
                 QVector3D(self._center), list(self._pan))
        try:
            # One framing for the inset, the single export and the batch, so
            # the corner thumbnail matches the file it will produce.
            self._aim_at_face(bounds)
            image = self.grabFramebuffer()
        except Exception as exc:                         # noqa: BLE001
            _log(self.log_fn, f"  ! portrait capture failed: {exc!r}")
            image = None
        finally:
            basis, self._distance, self._center, self._pan = saved
            self._set_camera_basis(basis)
            self.update()
        if image is None or image.isNull():
            return None
        side = min(image.width(), image.height())
        if side <= 0:
            return None
        square = image.copy((image.width() - side) // 2,
                            (image.height() - side) // 2, side, side)
        # Never upscale past what was actually rendered, and never let the
        # inset swallow a narrow pane - it is a corner reference, not a second
        # viewport. Both caps only bite on a small pane; at any ordinary size
        # the requested size wins.
        pane = min(self.width(), self.height())
        want = min(size, side, int(pane * _PORTRAIT_MAX_PANE) or size)
        if want <= 0:
            return None
        return square.scaled(want, want, Qt.KeepAspectRatio,
                             Qt.SmoothTransformation)

    def capture_turntable_sheet(self, background: str | None = None,
                                height: int = _SHEET_EXPORT_HEIGHT):
        """Return a four-angle actor sheet with a face close-up, or None.

        All five panels are rendered from the already-loaded scene.  Camera
        state AND the on-screen backdrop are restored even if a driver refuses
        one of the framebuffer grabs, so exporting never disturbs the
        interactive view.  *background* is a BACKGROUNDS key to export on,
        ``"transparent"`` for a cut-out of the actor alone, or None to keep
        whatever the viewport is showing.

        Panels are rendered OFFSCREEN at *height*, so the sheet is not capped
        by however large the preview pane happens to be on screen.
        """
        bounds = self._bounds
        head_bounds = self._head_bounds
        if (bounds is None or head_bounds is None
                or not (self._meshes or self._pending)):
            return None
        if self.context() is None:
            return None
        saved = (self._copy_camera_basis(), self._distance,
                 QVector3D(self._center), list(self._pan), self._home)
        # The backdrop drives the clay and wireframe colours too, so swapping
        # it for the grab means saving the whole trio, not just _bg.
        saved_bg = (QColor(self._bg), self._base)
        frames = []
        portrait = None
        transparent = background == BACKGROUND_TRANSPARENT
        self._clear_transparent = transparent
        if not transparent and background in BACKGROUNDS:
            self.set_background(background)
        try:
            # A level, consistently framed turntable. Skyrim actors face +Y,
            # hence +90 is front and -90 is back.
            self._frame(bounds)
            body_size = (max(1, round(height * _SHEET_BODY_ASPECT)), height)
            for degrees in (0.0, 90.0, -90.0, 180.0):
                self._set_camera_angles(math.radians(degrees), 0.0)
                frame = self._grab(body_size)
                if frame is None or frame.isNull():
                    return None
                frames.append(frame)

            self._aim_at_face(head_bounds)
            portrait = self._grab(
                (max(1, round(height * _SHEET_FACE_ASPECT)), height))
        except Exception as exc:                         # noqa: BLE001
            _log(self.log_fn, f"  ! image-sheet capture failed: {exc!r}")
            return None
        finally:
            self._clear_transparent = False
            self._bg, self._base = saved_bg
            basis, self._distance, self._center, self._pan, self._home = saved
            self._set_camera_basis(basis)
            self.update()
        if portrait is None or portrait.isNull():
            return None
        # The gutter colour only shows if panel rounding ever leaves a seam,
        # but it must be the EXPORTED backdrop when it does, not the viewer's.
        fill = BACKGROUNDS.get(background, self._bg.name())
        return _compose_turntable_sheet(frames, portrait, transparent, fill,
                                       max_height=max(height, _SHEET_MAX_HEIGHT))

    def _aim_at_face(self, head_bounds):
        """Point the camera at the head, framed head-and-shoulders.

        *head_bounds* is the face shape's extent. The framed subject is the
        whole head where one is loaded, so hair is included rather than
        cropped, and the bottom edge is pushed into the neck and shoulders -
        NPC Plugin Chooser 2's composition, using its own parameters.
        """
        (fx0, fy0, fz0), (fx1, fy1, fz1) = head_bounds
        # Prefer the full model: on a FaceGen head that is face + hair + brows
        # + eyes, i.e. the whole head. Fall back to the face when a caller has
        # no scene bounds (nothing else is loaded to widen to).
        (lx, ly, lz), (hx, hy, hz) = self._bounds or head_bounds
        # Guard against a whole BODY being on screen: the portrait must stay a
        # portrait, so never frame anything much taller than the head itself.
        if (hz - lz) > (fz1 - fz0) * 2.5:
            lx, ly, lz = fx0, fy0, fz0
            hx, hy, hz = fx1, fy1, fz1

        head_h = max(hz - lz, 1e-3)
        # Offsets are fractions of head height; the negative bottom extends the
        # view DOWN past the chin, which is what puts shoulders in frame.
        top = hz + head_h * _PORTRAIT_TOP_OFFSET
        bottom = lz + head_h * _PORTRAIT_BOTTOM_OFFSET
        self._center = QVector3D((lx + hx) / 2, (ly + hy) / 2,
                                 (top + bottom) / 2)
        self._pan = [0.0, 0.0]
        # The square crop takes the smaller viewport side, so the width has to
        # clear the same extent as the height, not the pane's aspect.
        want = max(top - bottom, hx - lx, 1e-3) * _PORTRAIT_FILL
        half_fov = math.radians(_VIEWPORT_FOV / 2.0)
        self._distance = want / (2.0 * math.tan(half_fov))
        self._set_camera_angles(math.radians(_PORTRAIT_YAW),
                                math.radians(_PORTRAIT_PITCH))

    def capture_face_image(self, background: str | None = None,
                           size: int = _SHEET_EXPORT_HEIGHT):
        """Just the head close-up, at export resolution, or None.

        The same framing the sheet's fifth panel uses, on its own - a portrait
        rather than a reference sheet. Square, because a face has no reason to
        inherit the sheet's tall body aspect.
        """
        head_bounds = self._head_bounds
        if head_bounds is None or not (self._meshes or self._pending):
            return None
        if self.context() is None:
            return None
        saved = (self._copy_camera_basis(), self._distance,
                 QVector3D(self._center), list(self._pan), self._home)
        saved_bg = (QColor(self._bg), self._base)
        transparent = background == BACKGROUND_TRANSPARENT
        self._clear_transparent = transparent
        if not transparent and background in BACKGROUNDS:
            self.set_background(background)
        try:
            self._aim_at_face(head_bounds)
            return self._grab((size, size))
        except Exception as exc:                         # noqa: BLE001
            _log(self.log_fn, f"  ! face capture failed: {exc!r}")
            return None
        finally:
            self._clear_transparent = False
            self._bg, self._base = saved_bg
            basis, self._distance, self._center, self._pan, self._home = saved
            self._set_camera_basis(basis)
            self.update()

    def _render_offscreen(self, width: int, height: int):
        """Render one frame at an arbitrary size, or None.

        `grabFramebuffer()` renders at the WIDGET's size, so an export was
        capped by however large the preview pane happened to be - a few
        hundred pixels. Drawing into a private multisampled FBO instead lets
        the sheet be exported far larger than anything on screen.
        """
        from PySide6.QtOpenGL import (QOpenGLFramebufferObject,
                                      QOpenGLFramebufferObjectFormat)
        width = max(1, int(width))
        height = max(1, int(height))
        self.makeCurrent()
        fbo = None
        try:
            fmt = QOpenGLFramebufferObjectFormat()
            fmt.setAttachment(QOpenGLFramebufferObject.CombinedDepthStencil)
            # Match the widget's own multisampling so exported edges are as
            # smooth as the preview's; drop to 0 if the driver refuses.
            try:
                fmt.setSamples(self.format().samples() or 0)
            except Exception:                            # noqa: BLE001
                pass
            fbo = QOpenGLFramebufferObject(width, height, fmt)
            if not fbo.isValid():
                return None
            self._render_size = (width, height)
            fbo.bind()
            self.context().functions().glViewport(0, 0, width, height)
            self.paintGL()
            fbo.release()
            return fbo.toImage()
        except Exception as exc:                         # noqa: BLE001
            _log(self.log_fn, f"  ! offscreen render failed: {exc!r}")
            return None
        finally:
            self._render_size = None
            if fbo is not None and fbo.isBound():
                fbo.release()
            # Put the viewport back or the next on-screen paint is clipped to
            # the export's dimensions.
            try:
                ratio = self.devicePixelRatioF()
                self.context().functions().glViewport(
                    0, 0, max(1, int(self.width() * ratio)),
                    max(1, int(self.height() * ratio)))
            except Exception:                            # noqa: BLE001
                pass
            self.doneCurrent()

    def _grab(self, size=None):
        """One frame, offscreen at *size* when given, else the on-screen grab."""
        if size is not None:
            image = self._render_offscreen(*size)
            if image is not None and not image.isNull():
                return image
        return self.grabFramebuffer()

    def _frame(self, bounds):
        (lx, ly, lz), (hx, hy, hz) = bounds
        cx, cy, cz = (lx + hx) / 2, (ly + hy) / 2, (lz + hz) / 2
        radius = max(hx - lx, hy - ly, hz - lz, 1e-3) * 0.5
        if self.home_view is not None:
            # An actor viewer wants the person facing the camera, framed like
            # a character sheet; a mesh browser wants the 3/4 view that shows
            # an object's form. Only the host knows which it is.
            yaw, pitch, look = self.home_view
            cz = lz + (hz - lz) * look
        else:
            yaw, pitch = _HOME_YAW, _HOME_PITCH
        self._center = QVector3D(cx, cy, cz)
        self._pan = [0.0, 0.0]
        self._distance = radius * 3.0
        self._set_camera_angles(yaw, pitch)
        self._home = (self._copy_camera_basis(), self._distance,
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
        self._u_rmaostex = prog.uniformLocation("uRmaosTex")
        self._u_pbr = prog.uniformLocation("uPbr")
        self._u_hasrmaos = prog.uniformLocation("uHasRmaos")
        self._u_pbrparams = prog.uniformLocation("uPbrParams")
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
            ("uTint", self._u_tint), ("uRmaosTex", self._u_rmaostex),
            ("uPbr", self._u_pbr), ("uHasRmaos", self._u_hasrmaos),
            ("uPbrParams", self._u_pbrparams),
            ("uSrgbAlbedo", self._u_srgb),
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
            _neutralise_view(self)
        self._pending = None
        self._uploaded = False
        self._needs_reload = True

    def release_gl(self):
        """Free GL objects while the context lives (called on DeferredDelete;
        aboutToBeDestroyed fires too late - Qt has dropped our connections)."""
        if self.context() is None:
            _neutralise_view(self)
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
        _release_mesh_buffers(self._meshes + list(self._pending or ()))
        for tex in self._gpu_textures.values():
            try:
                tex.destroy()
            except RuntimeError:
                pass
        self._gpu_textures.clear()
        self._meshes = []
        self._pending = None

    def _upload_geometry(self, m):
        prog = self._program
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
        stride = (16 if m.has_colors else 12) * 4
        prog.enableAttributeArray(0)
        prog.setAttributeBuffer(0, _GL_FLOAT, 0, 3, stride)
        prog.enableAttributeArray(1)
        prog.setAttributeBuffer(1, _GL_FLOAT, 3 * 4, 3, stride)
        prog.enableAttributeArray(2)
        prog.setAttributeBuffer(2, _GL_FLOAT, 6 * 4, 2, stride)
        prog.enableAttributeArray(3)
        prog.setAttributeBuffer(3, _GL_FLOAT, 8 * 4, 4, stride)
        if m.has_colors:
            prog.enableAttributeArray(4)
            prog.setAttributeBuffer(4, _GL_FLOAT, 12 * 4, 4, stride)

        m.ibo = QOpenGLBuffer(QOpenGLBuffer.IndexBuffer)
        m.ibo.create()
        m.ibo.bind()
        idata = m.indices.tobytes()
        m.ibo.allocate(idata, len(idata))

        m.vao.release()
        m.vbo.release()
        m.ibo.release()

        return len(data) + len(idata)

    def _upload(self):
        previous = {m.geometry: m for m in self._meshes
                    if m.geometry is not None}
        used_textures = set()
        vram = tex_count = reused_meshes = reused_textures = 0
        pending = self._pending or []
        try:
            for m in pending:
                old = previous.pop(m.geometry, None)
                if old is not None and all(
                        obj is not None for obj in (old.vao, old.vbo, old.ibo)):
                    m.vao, m.vbo, m.ibo = old.vao, old.vbo, old.ibo
                    old.vao = old.vbo = old.ibo = None
                    reused_meshes += 1
                else:
                    vram += self._upload_geometry(m)

                for attr, img in (("texture", m.image),
                                  ("normal_tex", m.normal_image),
                                  ("env_tex", m.env_image),
                                  ("mask_tex", m.mask_image),
                                  ("rmaos_tex", m.rmaos_image)):
                    if img is None or img.isNull():
                        continue
                    key = (img.cacheKey(), m.texture_clamp_mode)
                    tex = self._gpu_textures.get(key)
                    if tex is None:
                        tex = _make_gl_texture(img, m.texture_clamp_mode)
                        if tex is not None:
                            self._gpu_textures[key] = tex
                            tex_count += 1
                            vram += img.width() * img.height() * 16 // 3
                    else:
                        reused_textures += 1
                    if tex is not None:
                        setattr(m, attr, tex)
                        used_textures.add(key)
                    else:
                        _log(self.log_fn, f"  ! {m.name!r}: {attr} failed to "
                                          f"upload to the GPU")
                m.normal_image = m.env_image = m.mask_image = None
                m.rmaos_image = m.image = None
                m.verts = array.array("f")
        except Exception as exc:                         # noqa: BLE001
            self._release_gpu()
            self._uploaded = False
            message, gen, failed = str(exc), self._generation, self.failed
            _log(self.log_fn, f"  ! GPU upload failed: {message}")
            QTimer.singleShot(0, lambda: safe_emit(failed, message, gen))
            return

        _release_mesh_buffers(self._meshes)
        for key in self._gpu_textures.keys() - used_textures:
            tex = self._gpu_textures.pop(key)
            try:
                tex.destroy()
            except RuntimeError:
                pass
        self._meshes = pending
        self._pending = None
        self._uploaded = True
        if pending:
            _log(self.log_fn,
                 f"  uploaded {len(pending) - reused_meshes} mesh(es) and "
                 f"{tex_count} texture(s) (~{_fmt_bytes(vram)}); reused "
                 f"{reused_meshes} mesh(es), {reused_textures} texture binding(s)")

    def paintGL(self):
        f = self.context().functions()
        col = self._bg
        if self._clear_transparent:
            # Black, not the backdrop colour: multisampled silhouette edges
            # resolve to a blend of the mesh and whatever the buffer was
            # cleared to, and the grab is read back as PREMULTIPLIED alpha -
            # which is exactly what blending against black produces. Any other
            # clear colour would fringe the cut-out in that colour.
            f.glClearColor(0.0, 0.0, 0.0, 0.0)
        else:
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
        f.glUniform1i(self._u_rmaostex, 4)
        # BodySlide's default specularStrength.
        f.glUniform1f(self._u_spec, 1.0 if self.detail else 0.0)

        if wire != WIRE_ONLY:
            f.glUniform1f(self._u_flat, 0.0)
            f.glUniform3f(self._u_base, *self._base)
            # Depth-writing surfaces establish the jacket/body first. Hair
            # cutouts that deliberately do not write depth follow them: their
            # depth TEST keeps rear strands hidden, while front strands are no
            # longer overwritten by a collar drawn later. True alpha blends
            # remain last and back-to-front.
            opaque, late_solid, blended = _render_passes(
                self._meshes, self.textured)
            if blended:
                self._draw_meshes(f, solid=True, meshes=opaque)
                self._draw_meshes(f, solid=True, meshes=late_solid)
                eye = self._eye()
                blended.sort(
                    key=lambda m: -((m.center[0] - eye.x()) ** 2
                                    + (m.center[1] - eye.y()) ** 2
                                    + (m.center[2] - eye.z()) ** 2))
                f.glEnable(_GL_BLEND)
                # Separate alpha: the plain SRC_ALPHA function would leave
                # a*a + (1-a) in the alpha channel, punching translucent holes
                # through hair and eyelashes now that the buffer HAS an alpha
                # channel. ONE/ONE_MINUS_SRC_ALPHA keeps an opaque backdrop
                # opaque and accumulates true coverage over a cleared one.
                f.glBlendFuncSeparate(_GL_SRC_ALPHA, _GL_ONE_MINUS_SRC_ALPHA,
                                      _GL_ONE, _GL_ONE_MINUS_SRC_ALPHA)
                self._draw_meshes(f, solid=True, meshes=blended)
                f.glDepthMask(True)
                f.glDisable(_GL_BLEND)
            else:
                self._draw_meshes(f, solid=True, meshes=opaque)
                self._draw_meshes(f, solid=True, meshes=late_solid)

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
            if m.depth_test:
                f.glEnable(_GL_DEPTH_TEST)
            else:
                f.glDisable(_GL_DEPTH_TEST)
            if self.cull_backfaces and not m.double_sided:
                f.glEnable(_GL_CULL_FACE)
                f.glCullFace(_GL_BACK)
            else:
                f.glDisable(_GL_CULL_FACE)
            m.vao.bind()
            m.ibo.bind()
            use_tex = solid and self.textured and m.texture is not None
            f.glDepthMask(_depth_write_for_draw(m, solid, use_tex))
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
            use_pbr = (solid and self.detail and use_tex and m.pbr
                       and self.texture_slot == 0)
            use_rmaos = use_pbr and m.rmaos_tex is not None
            if use_rmaos:
                m.rmaos_tex.bind(4)
            f.glUniform1f(self._u_pbr, 1.0 if use_pbr else 0.0)
            f.glUniform1f(self._u_hasrmaos, 1.0 if use_rmaos else 0.0)
            f.glUniform2f(self._u_pbrparams, *m.pbr_params)
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
            if use_rmaos:
                m.rmaos_tex.release(4)
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
        # Leave predictable state for wire overlays and the next frame.
        f.glDepthMask(True)
        f.glEnable(_GL_DEPTH_TEST)

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

    def _set_camera_angles(self, yaw: float, pitch: float):
        self._right, self._up, self._forward = self._basis_for(yaw, pitch)

    def _set_camera_basis(self, basis):
        self._right, self._up, self._forward = (
            QVector3D(axis) for axis in basis)

    def _copy_camera_basis(self):
        return tuple(QVector3D(axis) for axis in self._camera_basis())

    def _camera_basis(self):
        """(right, up, forward) unit vectors of the current camera."""
        return self._right, self._up, self._forward

    def _camera_angles(self):
        """Orbital yaw/pitch for diagnostics; roll is held by the basis."""
        yaw = math.atan2(-self._forward.y(), -self._forward.x())
        pitch = math.asin(max(-1.0, min(1.0, -self._forward.z())))
        return yaw, pitch

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
        return -self._right, self._up

    def _look_target(self) -> QVector3D:
        """The look-at point: the mesh centre pushed by the pan offset."""
        right, up = self._pan_axes()
        return self._center + right * self._pan[0] + up * self._pan[1]

    def _eye(self) -> QVector3D:
        d = max(self._distance, 1e-3)
        t = self._look_target()
        return t - self._forward * d

    def _mvp(self) -> QMatrix4x4:
        # An offscreen grab renders at its own size, not the widget's, and the
        # aspect has to follow or the export is stretched.
        if self._render_size is not None:
            w, h = self._render_size
        else:
            w = max(1, self.width())
            h = max(1, self.height())
        d = max(self._distance, 1e-3)
        eye = self._eye()
        proj = QMatrix4x4()
        proj.perspective(_VIEWPORT_FOV, w / h, max(d * 0.001, 1e-3), d * 50.0)
        view = QMatrix4x4()
        view.lookAt(eye, self._look_target(), self._up)
        return proj * view

    @staticmethod
    def _rotated(vector: QVector3D, axis: QVector3D,
                 angle: float) -> QVector3D:
        c = math.cos(angle)
        s = math.sin(angle)
        return (vector * c + QVector3D.crossProduct(axis, vector) * s
                + axis * QVector3D.dotProduct(axis, vector) * (1.0 - c))

    def _orbit(self, horizontal: float, vertical: float):
        """Rotate the whole camera frame about axes in the current view plane."""
        angle = math.hypot(horizontal, vertical)
        if angle < 1e-12:
            return
        axis = (self._up * horizontal + self._right * vertical) / angle
        self._right = self._rotated(self._right, axis, angle)
        self._up = self._rotated(self._up, axis, angle)
        self._forward = self._rotated(
            self._forward, axis, angle).normalized()
        self._right = QVector3D.crossProduct(
            self._forward, self._up).normalized()
        self._up = QVector3D.crossProduct(
            self._right, self._forward).normalized()

    def set_free_camera(self, enabled: bool):
        enabled = bool(enabled)
        if self.free_camera == enabled:
            return
        self.free_camera = enabled
        if not enabled:
            yaw, pitch = self._camera_angles()
            pitch = max(-_TURNTABLE_PITCH_LIMIT,
                        min(_TURNTABLE_PITCH_LIMIT, pitch))
            self._set_camera_angles(yaw, pitch)
        self.update()

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
            horizontal = rot_sign * delta.x() * 0.01
            vertical = rot_sign * delta.y() * 0.01
            if self.free_camera:
                self._orbit(horizontal, vertical)
            else:
                yaw, pitch = self._camera_angles()
                yaw += horizontal
                pitch = max(-_TURNTABLE_PITCH_LIMIT,
                            min(_TURNTABLE_PITCH_LIMIT, pitch - vertical))
                self._set_camera_angles(yaw, pitch)
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
        basis, self._distance, center = self._home
        self._set_camera_basis(basis)
        self._center = QVector3D(center)
        self._pan = [0.0, 0.0]
        self.update()


class _NoGLViewport(QWidget):
    """Stand-in canvas for machines where Qt's GL path is unusable.

    Creating a real QOpenGLWidget there does not just fail to draw the mesh -
    it turns the whole window black (GH#350), so we never build one. This
    keeps the surrounding preview UI intact and explains itself instead.
    """

    loaded = Signal(object, object, int, object, object)
    failed = Signal(str, int)

    def __init__(self, reason: str = "", parent=None):
        super().__init__(parent)
        self._reason = reason
        self.home_view = None       # hosts set this unconditionally
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
        self.free_camera = False
        self.cull_backfaces = False
        self.textured = True
        self.detail = True
        self.wireframe = WIRE_OFF
        self.texture_slot = 0
        self._generation = 0

    def capture_portrait(self, *_a, **_kw):
        return None

    def capture_turntable_sheet(self, *_a, **_kw):
        return None

    def capture_face_image(self, *_a, **_kw):
        return None

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

    def set_free_camera(self, enabled):
        self.free_camera = bool(enabled)

    def release_gl(self, *_a):
        pass


class NifPreview(QWidget):
    """A panel-scoped .nif viewer: header + stats, view toggles, GL viewport."""

    # Texture paths the last-loaded mesh asked for (drives the source picker).
    textures_seen = Signal(object)
    # A front-on still of the head, once per load (QImage).
    portrait_ready = Signal(object)
    # Whether the currently requested scene has finished and can be captured.
    scene_ready = Signal(bool)
    # A texture source was picked: its opaque data, None = as the game loads.
    texture_source_changed = Signal(object)

    def __init__(self, path: "Path | None", display_name: str = "",
                 texture_roots: list[Path] | None = None,
                 archive_roots: list[Path] | None = None,
                 resolver=None, parent=None, log_fn=None):
        # path None = caller feeds bytes via set_nif_data() (archive member).
        super().__init__(parent)
        self.log_fn = log_fn
        self._capture_ready = False
        # Overwritten from the ini further down; set here so background_key()
        # is answerable from the moment the widget exists.
        self._bg_key = "light"
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
            "Apply normal maps and material shine, including PBR "
            "roughness and metallic maps"))
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
                           ("dark", self.tr("Dark")), ("black", self.tr("Black")),
                           ("green", self.tr("Green screen"))):
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

        self._act_free_camera = self._menu.addAction(self.tr("Free camera"))
        self._act_free_camera.setCheckable(True)
        self._act_free_camera.setToolTip(self.tr(
            "Allow unrestricted rotation around every axis"))
        self._act_free_camera.triggered.connect(self._on_free_camera)

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
            from Utils.ui.config import load_nif_invert_mouse
            inverted = load_nif_invert_mouse()
        except Exception:
            inverted = True
        self._view.invert_mouse = bool(inverted)
        self._act_invert.setChecked(bool(inverted))

        try:
            from Utils.ui.config import load_nif_free_camera
            free_camera = load_nif_free_camera()
        except Exception:
            free_camera = False
        self._view.set_free_camera(bool(free_camera))
        self._act_free_camera.setChecked(bool(free_camera))

        try:
            from Utils.ui.config import load_nif_cull_backfaces
            cull = load_nif_cull_backfaces()
        except Exception:
            cull = False
        self._view.cull_backfaces = bool(cull)
        self._act_cull.setChecked(bool(cull))

        try:
            from Utils.ui.config import load_nif_brightness
            bright = load_nif_brightness()
        except Exception:
            bright = BRIGHTNESS_DEFAULT
        self._view.set_brightness(bright)
        self._bright.setValue(max(BRIGHTNESS_MIN, min(BRIGHTNESS_MAX, bright)))
        self._bright.valueChanged.connect(self._on_brightness)
        # Save on release only: user-only, and avoids a write per drag pixel.
        self._bright.sliderReleased.connect(self._save_brightness)

        try:
            from Utils.ui.config import load_nif_background
            saved = load_nif_background()
        except Exception:
            saved = "light"
        if saved not in BACKGROUNDS:
            saved = "light"
        self._bg_key = saved
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
        self._set_capture_ready(False)
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
        self._set_capture_ready(False)
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
        self._set_capture_ready(False)
        self._stats.setText(self.tr("Loading…"))
        self._view.reload(keep_view=True)

    def texture_source(self):
        """Data of the picked source, or None for 'as the game loads'."""
        return self._tex_box.currentData() if self._tex_box.isVisible() else None

    def _on_texture_source(self, _index):
        _log(self.log_fn,
             f"option: texture source = {self._tex_box.currentText()!r}")
        self.texture_source_changed.emit(self._tex_box.currentData())

    def set_home_view(self, yaw: float, pitch: float, look: float = 0.5):
        """Where the camera rests on load and on reset.

        *yaw*/*pitch* are radians; *look* is the look-at height as a fraction
        of the model (0.5 = its centre). Leave unset for the mesh-browser 3/4
        view - an ACTOR viewer wants the person facing the camera instead.
        """
        self._view.home_view = (yaw, pitch, look)

    def set_title(self, display_name: str, status: str = ""):
        """Retitle without loading - a browser showing what it is about to read."""
        self._header.setText(display_name)
        self._stats.setText(status)

    def set_nif_data(self, data: bytes, display_name: str,
                     resolver=None, archives=None, tex_override=None,
                     keep_view: bool = False, mesh_rel: str = "",
                     plugin_dirs=None, parts=None, skeleton=None,
                     skin_tint=None, hide_hair=False, selected_roots=None,
                     head_parts=None, eye_textures=None, face_morph=None,
                     face_skin_tint=None):
        """Preview in-memory bytes (a BSA/BA2 member); *keep_view* skips
        re-framing. *parts*/*skeleton* assemble a whole actor (see load)."""
        self._set_capture_ready(False)
        self._header.setText(display_name)
        self._stats.setText(self.tr("Loading…"))
        self._view.load(data, list(selected_roots or ()), None,
                        resolver, archives, tex_override,
                        mesh_rel=mesh_rel, plugin_dirs=plugin_dirs,
                        parts=parts, skeleton=skeleton, skin_tint=skin_tint,
                        hide_hair=hide_hair,
                        head_parts=head_parts, eye_textures=eye_textures,
                        face_morph=face_morph,
                        face_skin_tint=face_skin_tint,
                        keep_view=keep_view)

    def _on_loaded(self, meshes, bounds, gen, tex_paths=None,
                   head_bounds=None):
        if gen != self._view._generation:
            return          # a stale load must not retitle or repopulate the picker
        # Emitted even for a geometry-less mesh: the host's texture picker is
        # driven by which paths were REQUESTED, not by what resolved.
        safe_emit(self.textures_seen, list(tex_paths or ()))
        if not meshes:
            _log(self.log_fn, "displayed: no drawable geometry")
            self._stats.setText(self.tr("no drawable geometry"))
            self._set_capture_ready(False)
            return
        tris = sum(m.tri_count for m in meshes)
        textured = sum(1 for m in meshes if m.has_image)
        parts = [self.tr("{0} shapes").format(len(meshes)),
                 self.tr("{0} tris").format(f"{tris:,}")]
        if textured < len(meshes):
            parts.append(self.tr("{0}/{1} textured").format(textured, len(meshes)))
        self._stats.setText(" · ".join(parts))
        self._set_capture_ready(head_bounds is not None)
        if head_bounds is not None:
            # After the paint that uploads these meshes: grabbing now would
            # capture the previous NPC, or nothing at all on the first load.
            QTimer.singleShot(0, self._emit_portrait)

    def _emit_portrait(self):
        try:
            image = self._view.capture_portrait()
        except Exception:                                # noqa: BLE001
            image = None
        if image is not None:
            safe_emit(self.portrait_ready, image)

    def _set_capture_ready(self, ready: bool):
        ready = bool(ready)
        if ready == self._capture_ready:
            return
        self._capture_ready = ready
        safe_emit(self.scene_ready, ready)

    def can_capture(self) -> bool:
        return self._capture_ready

    def capture_turntable_sheet(self, background: str | None = None,
                                height: int = _SHEET_EXPORT_HEIGHT):
        """Capture the loaded actor as a multi-angle PNG-ready QImage.

        *background* is a BACKGROUNDS key, ``"transparent"``, or None to
        export on whatever backdrop the viewport is showing.
        """
        if not self._capture_ready:
            return None
        return self._view.capture_turntable_sheet(background, height)

    def capture_face_image(self, background: str | None = None,
                           size: int = _SHEET_EXPORT_HEIGHT):
        """Capture only the head close-up, at export resolution.

        *background* is a BACKGROUNDS key, ``"transparent"``, or None.
        """
        if not self._capture_ready:
            return None
        return self._view.capture_face_image(background, size)

    def background_key(self) -> str:
        """The backdrop preset the viewport is currently showing.

        Tracked here rather than read back off the viewport: _NoGLViewport has
        no colour to read, and it still has to answer.
        """
        return self._bg_key

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
            from Utils.ui.config import save_nif_cull_backfaces
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
            from Utils.ui.config import save_nif_invert_mouse
            save_nif_invert_mouse(bool(on))
        except Exception as exc:
            _log(self.log_fn, f"! could not save invert setting: {exc!r}")

    def _on_free_camera(self, on):
        _log(self.log_fn, f"option: free camera {'on' if on else 'off'}")
        self._view.set_free_camera(bool(on))
        try:
            from Utils.ui.config import save_nif_free_camera
            save_nif_free_camera(bool(on))
        except Exception as exc:
            _log(self.log_fn, f"! could not save camera setting: {exc!r}")

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
            from Utils.ui.config import save_nif_brightness
            save_nif_brightness(int(self._bright.value()))
        except Exception as exc:
            _log(self.log_fn, f"! could not save brightness: {exc!r}")

    def _on_background(self, act):
        key = act.data() or "light"
        _log(self.log_fn, f"option: background = {key}")
        self._bg_key = key
        self._view.set_background(key)
        try:
            from Utils.ui.config import save_nif_background
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
