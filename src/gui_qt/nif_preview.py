"""
nif_preview.py
Panel-scoped 3D preview for .nif meshes (QOpenGLWidget, no new deps).

Parses off-thread via Utils.nif_reader, bakes world transforms into vertices,
and resolves textures through Utils.asset_resolver (what the game would load)
with archive/loose fallbacks. Starfield geometry is fetched from external
.mesh files. Meshes are Z-up; the camera orbits +Z.
"""

from __future__ import annotations

import array
import math
import threading
from pathlib import Path
from shiboken6 import VoidPtr

from PySide6.QtCore import QEvent, Qt, Signal
from PySide6.QtGui import QColor, QMatrix4x4, QSurfaceFormat, QVector3D
from PySide6.QtOpenGL import (
    QOpenGLBuffer, QOpenGLShader, QOpenGLShaderProgram, QOpenGLTexture,
    QOpenGLVersionFunctionsFactory, QOpenGLVersionProfile,
    QOpenGLVertexArrayObject,
)
from PySide6.QtOpenGLWidgets import QOpenGLWidget
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QLabel, QVBoxLayout, QWidget,
)

from Utils.asset_resolver import DirCache as _DirCache
from gui_qt.eliding_label import ElidingLabel
from gui_qt.flow_layout import FlowLayout, enable_height_for_width
from gui_qt.safe_emit import safe_emit
from gui_qt.theme_qt import active_palette, _c

PREVIEW_EXTS = {".nif"}

# Cap decoded textures: a 4K RGBA QImage is 67 MB, and a dozen of those
# exhausts memory on a handheld.
TEXTURE_MAX_DIM = 1024

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

# PySide6 exposes no GL constant module; these are the standard values.
_GL_TRIANGLES = 0x0004
_GL_DEPTH_BUFFER_BIT = 0x0100
_GL_FRONT_AND_BACK = 0x0408
_GL_DEPTH_TEST = 0x0B71
_GL_UNSIGNED_INT = 0x1405
_GL_FLOAT = 0x1406
_GL_LINE = 0x1B01
_GL_FILL = 0x1B02
_GL_COLOR_BUFFER_BIT = 0x4000

# PySide6 binds glDrawElements' `indices` as a real pointer, so an integer 0 is
# rejected; with an element buffer bound it must be a null VoidPtr offset.
_NULL_OFFSET = VoidPtr(0)

_VERT_SRC = """#version 330 core
layout(location = 0) in vec3 aPos;
layout(location = 1) in vec3 aNormal;
layout(location = 2) in vec2 aUV;
uniform mat4 uMVP;
out vec3 vNormal;
out vec2 vUV;
void main() {
    vNormal = aNormal;
    // NO V flip: QOpenGLTexture(QImage) already mirrors on upload; flipping
    // again samples the wrong atlas island.
    vUV = aUV;
    gl_Position = uMVP * vec4(aPos, 1.0);
}
"""

_FRAG_SRC = """#version 330 core
in vec3 vNormal;
in vec2 vUV;
// uHasTex is a float: PySide6 setUniformValue silently misses int uniforms.
uniform sampler2D uTex;
uniform float uHasTex;
uniform vec3 uBaseColor;
out vec4 FragColor;
void main() {
    vec3 n = normalize(vNormal);
    vec3 key = normalize(vec3(0.35, 0.55, 0.75));
    // Two-sided: unlit backfaces read as holes on mod meshes.
    float d = abs(dot(n, key));
    float fill = 0.25 * abs(dot(n, normalize(vec3(-0.6, -0.3, 0.2))));
    vec3 base = uHasTex > 0.5 ? texture(uTex, vUV).rgb : uBaseColor;
    // Generous ambient: readability over physical fidelity.
    FragColor = vec4(base * (0.55 + 0.60 * d + fill), 1.0);
}
"""


class _Mesh:
    """One shape's CPU-side buffers, built off-thread and uploaded on demand."""

    __slots__ = ("name", "verts", "indices", "image", "has_image", "tri_count",
                 "vao", "vbo", "ibo", "texture")

    def __init__(self, name, verts, indices, image, tri_count):
        self.name = name
        self.verts = verts
        self.indices = indices
        self.image = image
        # Kept because `image` is dropped once the texture is on the GPU.
        self.has_image = image is not None
        self.tri_count = tri_count
        self.vao = self.vbo = self.ibo = self.texture = None


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


def _qimage_from_bytes(data: bytes):
    """Decode texture bytes pulled from an archive (DDS goes via Pillow)."""
    from PySide6.QtGui import QImage
    img = QImage()
    if img.loadFromData(data) and not img.isNull():
        return img
    try:
        import io
        from PIL import Image as PilImage
        from Utils.dds_compat import sanitise_dds
        with PilImage.open(io.BytesIO(sanitise_dds(data))) as im:
            # Reduce BEFORE convert so the full-size RGBA never hits the heap.
            big = max(im.width, im.height)
            if big > TEXTURE_MAX_DIM:
                im = im.reduce(max(1, big // TEXTURE_MAX_DIM))
            im = im.convert("RGBA")
            raw = im.tobytes("raw", "RGBA")
            return QImage(raw, im.width, im.height,
                          QImage.Format_RGBA8888).copy()
    except Exception:                                    # noqa: BLE001
        return None


def _make_texture_loader(texture_roots: list[Path], archives=None, resolver=None,
                         override=None):
    """Return ``shape -> QImage|None``; resolver first, then roots/archives.

    FO4/Starfield shapes name a material file whose textures override the
    mesh's own (usually empty) texture set. *override* (``rel -> bytes|None``)
    is consulted before everything else; requested paths are recorded on
    ``load.requested``.
    """
    cache = _DirCache()
    seen: dict[str, object] = {}
    materials: dict[str, object] = {}
    requested: list[str] = []

    def fetch(rel: str):
        """Raw bytes for a data-relative path; retries with textures/ prefix."""
        if not rel:
            return None
        blob = _fetch_exact(rel)
        if blob is not None:
            return blob
        low = rel.replace("\\", "/").lower()
        if not low.startswith(("textures/", "materials/", "data/")):
            return _fetch_exact("textures/" + rel)
        return None

    def _fetch_exact(rel: str):
        if override is not None:
            blob = override(rel)
            if blob:
                return blob
        if resolver is not None:
            blob = resolver.read(rel)
            if blob:
                return blob
        else:
            # Loose files win over archives, matching what the engine loads.
            for root in texture_roots:
                found = cache.resolve(root, rel)
                if found is not None:
                    try:
                        return found.read_bytes()
                    except OSError:
                        return None
        if archives is not None:
            # Only source for a mesh previewed out of an uninstalled mod's archive.
            return archives.read(rel)
        return None

    def material_diffuse(rel: str) -> str:
        key = rel.lower()
        if key not in materials:
            from Utils.bgsm_reader import read_material
            blob = fetch(rel)
            materials[key] = read_material(blob) if blob else None
        mat = materials[key]
        return mat.diffuse if mat is not None else ""

    def load(shape):
        rel = material_diffuse(shape.material) if shape.material else ""
        if not rel:
            rel = shape.diffuse
        if not rel:
            return None
        key = rel.replace("\\", "/").lower()
        if not key.startswith(("textures/", "materials/", "data/")):
            key = "textures/" + key          # what fetch() ends up asking for
        if key not in seen:
            requested.append(key)
        else:
            return seen[key]
        blob = fetch(rel)
        image = _qimage_from_bytes(blob) if blob else None
        if image is not None and image.isNull():
            image = None
        image = _fit_texture(image)
        seen[key] = image
        return image

    load.fetch = fetch
    load.requested = requested
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


def _build_meshes(model, load_texture):
    """Bake world transforms and interleave into GL-ready buffers."""
    meshes: list[_Mesh] = []
    lo = [float("inf")] * 3
    hi = [float("-inf")] * 3

    for shape in model.shapes:
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

        tx, ty, tz = shape.translation
        r = shape.rotation
        s = shape.scale
        flat = array.array("f")
        push = flat.extend
        for (x, y, z), (nx, ny, nz), (u, v) in zip(verts, normals, uvs):
            wx = tx + s * (r[0] * x + r[1] * y + r[2] * z)
            wy = ty + s * (r[3] * x + r[4] * y + r[5] * z)
            wz = tz + s * (r[6] * x + r[7] * y + r[8] * z)
            push((wx, wy, wz,
                  r[0] * nx + r[1] * ny + r[2] * nz,
                  r[3] * nx + r[4] * ny + r[5] * nz,
                  r[6] * nx + r[7] * ny + r[8] * nz,
                  u, v))
            if wx < lo[0]:
                lo[0] = wx
            if wy < lo[1]:
                lo[1] = wy
            if wz < lo[2]:
                lo[2] = wz
            if wx > hi[0]:
                hi[0] = wx
            if wy > hi[1]:
                hi[1] = wy
            if wz > hi[2]:
                hi[2] = wz

        nv = len(verts)
        idx = array.array("I")
        for a, b, c in tris:
            if a < nv and b < nv and c < nv:
                idx.extend((a, b, c))
        if not idx:
            continue

        image = load_texture(shape)
        meshes.append(_Mesh(shape.name, flat, idx, image, len(idx) // 3))

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
        for obj in (m.vao, m.vbo, m.ibo, m.texture):
            if obj is not None:
                try:
                    shiboken6.invalidate(obj)
                except Exception:                        # noqa: BLE001
                    pass
        m.vao = m.vbo = m.ibo = m.texture = None


def _neutralise_view(view) -> None:
    """Last-resort orphan cleanup when a context dies (Python attrs only —
    the widget's C++ half may already be mid-destruction)."""
    _neutralise_meshes(list(view._meshes) + list(view._pending or ()))
    view._meshes = []
    view._pending = None


class _Viewport(QOpenGLWidget):
    """The GL canvas: orbit/pan/zoom camera over the parsed shapes."""

    loaded = Signal(object, object, int, object)  # meshes, bounds, gen, tex paths
    failed = Signal(str, int)

    def __init__(self, parent=None):
        super().__init__(parent)
        fmt = QSurfaceFormat()
        fmt.setVersion(3, 3)
        fmt.setProfile(QSurfaceFormat.CoreProfile)
        fmt.setDepthBufferSize(24)
        fmt.setSamples(4)
        self.setFormat(fmt)

        self._program: QOpenGLShaderProgram | None = None
        self._core = None
        self._u_mvp = self._u_hastex = self._u_base = -1
        self._meshes: list[_Mesh] = []
        self._pending: list[_Mesh] | None = None
        self._uploaded = False
        self._gl_error = ""
        self._generation = 0
        self._reload_args = None
        self._needs_reload = False
        self._keep_view = False

        self._yaw = math.radians(-60.0)
        self._pitch = math.radians(22.0)
        self._distance = 100.0
        self._target = QVector3D(0, 0, 0)
        self._home = (self._yaw, self._pitch, self._distance, QVector3D(0, 0, 0))
        self._last_pos = None
        self._last_buttons = Qt.NoButton
        self.wireframe = False
        self.textured = True
        self.invert_mouse = True
        self._bg = QColor(BACKGROUNDS["light"])
        self._base = (0.40, 0.39, 0.37)

        self.setMinimumSize(1, 1)
        self.setFocusPolicy(Qt.StrongFocus)
        self.loaded.connect(self._on_loaded)

    # -- loading ------------------------------------------------------------
    def load(self, source, texture_roots: list[Path], archive_roots=None,
             resolver=None, archives=None, tex_override=None,
             keep_view: bool = False):
        """Parse and build *source* (path or raw bytes) off-thread, then swap.

        *archives* lets a mesh read from inside a BSA find its own textures.
        """
        self._generation += 1
        gen = self._generation
        # keep_view: same mesh, new textures — don't snap the camera back.
        self._keep_view = bool(keep_view)
        # Kept so the mesh can be rebuilt after a context loss (tab detach).
        self._reload_args = (source, texture_roots, archive_roots,
                             resolver, archives, tex_override)
        self._discard_pending()

        def work():
            from Utils.nif_reader import read_nif
            try:
                extra = archives
                if extra is None and resolver is None and archive_roots:
                    from Utils.archive_lookup import ArchiveLookup, find_archives
                    extra = ArchiveLookup(find_archives(archive_roots),
                                          keep_prefix=ASSET_PREFIXES)
                loader = _make_texture_loader(texture_roots, extra, resolver,
                                              tex_override)
                model = read_nif(source)
                # Starfield keeps geometry in external .mesh files.
                if any(s.mesh_path for s in model.shapes):
                    _load_external_geometry(model, loader.fetch)
                meshes, bounds = _build_meshes(model, loader)
            except Exception as e:                       # noqa: BLE001
                safe_emit(self.failed, str(e), gen)
                return
            safe_emit(self.loaded, meshes, bounds, gen,
                      list(dict.fromkeys(loader.requested)))

        threading.Thread(target=work, daemon=True).start()

    def _discard_pending(self):
        self._pending = None

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
        self._target = QVector3D(cx, cy, cz)
        self._distance = radius * 3.0
        self._yaw = math.radians(-60.0)
        self._pitch = math.radians(22.0)
        self._home = (self._yaw, self._pitch, self._distance,
                      QVector3D(cx, cy, cz))

    # -- GL -----------------------------------------------------------------
    def initializeGL(self):
        prog = QOpenGLShaderProgram(self)
        ok = prog.addShaderFromSourceCode(QOpenGLShader.Vertex, _VERT_SRC)
        ok = prog.addShaderFromSourceCode(QOpenGLShader.Fragment, _FRAG_SRC) and ok
        ok = prog.link() and ok
        if not ok:
            self._gl_error = prog.log() or "shader compilation failed"
            self._program = None
            return
        self._program = prog
        self._u_mvp = prog.uniformLocation("uMVP")
        self._u_hastex = prog.uniformLocation("uHasTex")
        self._u_base = prog.uniformLocation("uBaseColor")
        ctx = self.context()
        ctx.functions().glEnable(_GL_DEPTH_TEST)
        # glPolygonMode (wireframe) needs the 3.3 core functions object.
        profile = QOpenGLVersionProfile()
        profile.setVersion(3, 3)
        profile.setProfile(QSurfaceFormat.CoreProfile)
        try:
            self._core = QOpenGLVersionFunctionsFactory.get(profile, ctx)
        except Exception:                                # noqa: BLE001
            self._core = None
        # Reparenting (tab detach/re-pin) destroys the context and everything
        # uploaded to it. Free while it is still current, then rebuild from
        # the kept load args when the new context initialises.
        ctx.aboutToBeDestroyed.connect(self._on_context_lost)
        # Safety net for hosts that delete this widget as a CHILD: their
        # DeferredDelete never reaches us and the bound connection above is
        # dropped mid-destruction — but a receiver-less lambda still fires,
        # and it touches only Python-side state.
        ctx.aboutToBeDestroyed.connect(lambda v=self: _neutralise_view(v))
        if self._needs_reload:
            self._needs_reload = False
            if self._reload_args is not None:
                # A context-loss rebuild is a recovery: keep the camera.
                self.load(*self._reload_args, keep_view=True)

    def _on_context_lost(self):
        from PySide6.QtGui import QOpenGLContext
        try:
            self.makeCurrent()
        except Exception:                                # noqa: BLE001
            pass
        if QOpenGLContext.currentContext() is not None:
            self._release_gpu()
            self.doneCurrent()
        else:
            # No current context to free under — and once the creation context
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
        aboutToBeDestroyed fires too late — Qt has dropped our connections)."""
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
            for obj in (m.vao, m.vbo, m.ibo, m.texture):
                if obj is not None:
                    try:
                        obj.destroy()
                    except RuntimeError:
                        pass
            m.vao = m.vbo = m.ibo = m.texture = None
        self._meshes = []

    def _upload(self):
        prog = self._program
        for m in self._pending or []:
            m.vao = QOpenGLVertexArrayObject()
            m.vao.create()
            m.vao.bind()

            m.vbo = QOpenGLBuffer(QOpenGLBuffer.VertexBuffer)
            m.vbo.create()
            m.vbo.bind()
            data = m.verts.tobytes()
            m.vbo.allocate(data, len(data))

            stride = 8 * 4
            prog.enableAttributeArray(0)
            prog.setAttributeBuffer(0, _GL_FLOAT, 0, 3, stride)
            prog.enableAttributeArray(1)
            prog.setAttributeBuffer(1, _GL_FLOAT, 3 * 4, 3, stride)
            prog.enableAttributeArray(2)
            prog.setAttributeBuffer(2, _GL_FLOAT, 6 * 4, 2, stride)

            m.ibo = QOpenGLBuffer(QOpenGLBuffer.IndexBuffer)
            m.ibo.create()
            m.ibo.bind()
            idata = m.indices.tobytes()
            m.ibo.allocate(idata, len(idata))

            m.vao.release()
            m.vbo.release()
            m.ibo.release()

            if m.image is not None:
                m.texture = QOpenGLTexture(m.image)
                m.texture.setMinificationFilter(QOpenGLTexture.LinearMipMapLinear)
                m.texture.setMagnificationFilter(QOpenGLTexture.Linear)
                m.texture.setWrapMode(QOpenGLTexture.Repeat)
            # Free both CPU copies now the GPU owns the data.
            m.verts = array.array("f")
            m.image = None
        self._meshes = self._pending or []
        self._pending = None
        self._uploaded = True

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
        wire = self.wireframe and self._core is not None
        if wire:
            self._core.glPolygonMode(_GL_FRONT_AND_BACK, _GL_LINE)

        prog = self._program
        prog.bind()
        prog.setUniformValue(self._u_mvp, self._mvp())
        # glUniform* directly: setUniformValue silently drops plain scalars.
        f.glUniform3f(self._u_base, *self._base)

        for m in self._meshes:
            m.vao.bind()
            m.ibo.bind()
            use_tex = self.textured and m.texture is not None and not wire
            if use_tex:
                m.texture.bind(0)
            f.glUniform1f(self._u_hastex, 1.0 if use_tex else 0.0)
            f.glDrawElements(_GL_TRIANGLES, len(m.indices),
                             _GL_UNSIGNED_INT, _NULL_OFFSET)
            if use_tex:
                m.texture.release(0)
            m.ibo.release()
            m.vao.release()
        prog.release()
        if wire:
            self._core.glPolygonMode(_GL_FRONT_AND_BACK, _GL_FILL)

    def resizeGL(self, w, h):
        self.context().functions().glViewport(0, 0, max(1, w), max(1, h))

    def _mvp(self) -> QMatrix4x4:
        w = max(1, self.width())
        h = max(1, self.height())
        d = max(self._distance, 1e-3)
        eye = QVector3D(
            self._target.x() + d * math.cos(self._pitch) * math.cos(self._yaw),
            self._target.y() + d * math.cos(self._pitch) * math.sin(self._yaw),
            self._target.z() + d * math.sin(self._pitch),
        )
        proj = QMatrix4x4()
        proj.perspective(45.0, w / h, max(d * 0.001, 1e-3), d * 50.0)
        view = QMatrix4x4()
        view.lookAt(eye, self._target, QVector3D(0, 0, 1))
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
        # Orbit and pan are deliberately mirrored; the toggle flips both.
        orbit_sign = -1.0 if self.invert_mouse else 1.0
        pan_sign = -orbit_sign
        if e.buttons() & Qt.LeftButton:
            self._yaw += orbit_sign * delta.x() * 0.01
            self._pitch = max(-1.5533, min(
                1.5533, self._pitch - orbit_sign * delta.y() * 0.01))
        elif e.buttons() & (Qt.RightButton | Qt.MiddleButton):
            # Pan across the view plane, scaled so the drag tracks the cursor.
            scale = self._distance * 0.0022 * pan_sign
            right = QVector3D(math.sin(self._yaw), -math.cos(self._yaw), 0.0)
            up = QVector3D(
                -math.sin(self._pitch) * math.cos(self._yaw),
                -math.sin(self._pitch) * math.sin(self._yaw),
                math.cos(self._pitch),
            )
            self._target += right * (delta.x() * scale)
            self._target += up * (delta.y() * scale)
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

    def set_background(self, key: str):
        """Swap the backdrop preset; clay colour flips to keep the silhouette."""
        self._bg = QColor(BACKGROUNDS.get(key, BACKGROUNDS["light"]))
        lum = (0.299 * self._bg.redF() + 0.587 * self._bg.greenF()
               + 0.114 * self._bg.blueF())
        self._base = ((0.40, 0.39, 0.37) if lum > 0.5
                      else (0.72, 0.71, 0.68))
        self.update()

    def mouseDoubleClickEvent(self, e):
        self._yaw, self._pitch, self._distance, target = self._home
        self._target = QVector3D(target)
        self.update()


class NifPreview(QWidget):
    """A panel-scoped .nif viewer: header + stats, view toggles, GL viewport."""

    # Texture paths the last-loaded mesh asked for (drives the source picker).
    textures_seen = Signal(object)
    # A texture source was picked: its opaque data, None = as the game loads.
    texture_source_changed = Signal(object)

    def __init__(self, path: "Path | None", display_name: str = "",
                 texture_roots: list[Path] | None = None,
                 archive_roots: list[Path] | None = None,
                 resolver=None, parent=None):
        # path None = caller feeds bytes via set_nif_data() (archive member).
        super().__init__(parent)
        self.setObjectName("NifPreview")
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)

        pal = active_palette()
        bar = QWidget()
        bar.setStyleSheet(f"background:{_c(pal, 'BG_HEADER')};")
        # FlowLayout, not QHBoxLayout: an un-wrappable title+controls row set a
        # minimum width under the pane and jammed the host splitter.
        row = FlowLayout(bar, spacing=12)
        row.setContentsMargins(10, 6, 10, 6)
        enable_height_for_width(bar)

        # ElidingLabel tooltips the full title; the drag hint moved to the
        # viewport, where the dragging happens.
        self._header = ElidingLabel(display_name or path.name)
        self._header.setStyleSheet(
            f"color:{_c(pal, 'TEXT_MAIN')}; font-weight:600;")
        row.addWidget(self._header)

        self._stats = QLabel("")
        self._stats.setStyleSheet(f"color:{_c(pal, 'TEXT_DIM')};")
        row.addWidget(self._stats)

        self._cb_tex = QCheckBox(self.tr("Textures"))
        self._cb_tex.setChecked(True)
        self._cb_tex.setStyleSheet(f"color:{_c(pal, 'TEXT_DIM')};")
        self._cb_tex.toggled.connect(self._on_textured)
        row.addWidget(self._cb_tex)

        self._cb_wire = QCheckBox(self.tr("Wireframe"))
        self._cb_wire.setStyleSheet(f"color:{_c(pal, 'TEXT_DIM')};")
        self._cb_wire.toggled.connect(self._on_wireframe)
        row.addWidget(self._cb_wire)

        self._cb_invert = QCheckBox(self.tr("Invert mouse"))
        self._cb_invert.setToolTip(self.tr(
            "Reverse the drag direction for orbiting and panning"))
        self._cb_invert.setStyleSheet(f"color:{_c(pal, 'TEXT_DIM')};")
        row.addWidget(self._cb_invert)

        self._tex_box = QComboBox()
        self._tex_box.setToolTip(self.tr(
            "Preview this mesh with another mod's copy of its textures"))
        self._tex_box.hide()          # shown once a host offers alternatives
        self._tex_box.activated.connect(self._on_texture_source)
        row.addWidget(self._tex_box)

        self._bg_box = QComboBox()
        self._bg_box.setToolTip(self.tr("Viewport background"))
        for key, label in (("light", self.tr("Light")),
                           ("grey", self.tr("Grey")),
                           ("dark", self.tr("Dark")),
                           ("black", self.tr("Black"))):
            self._bg_box.addItem(label, key)
        row.addWidget(self._bg_box)

        v.addWidget(bar)

        self._view = _Viewport()
        self._view.setToolTip(self.tr(
            "Drag to orbit · right-drag to pan · scroll to zoom · "
            "double-click to reframe"))
        self._view.loaded.connect(self._on_loaded)
        self._view.failed.connect(self._on_failed)
        v.addWidget(self._view, 1)

        # Restore prefs, then connect user-only signals (clicked/activated)
        # so restoring state never rewrites the config.
        try:
            from Utils.ui_config import load_nif_invert_mouse
            inverted = load_nif_invert_mouse()
        except Exception:
            inverted = True
        self._view.invert_mouse = bool(inverted)
        self._cb_invert.setChecked(bool(inverted))
        self._cb_invert.clicked.connect(self._on_invert_mouse)

        try:
            from Utils.ui_config import load_nif_background
            saved = load_nif_background()
        except Exception:
            saved = "light"
        if saved not in BACKGROUNDS:
            saved = "light"
        self._view.set_background(saved)
        idx = self._bg_box.findData(saved)
        self._bg_box.setCurrentIndex(idx if idx >= 0 else 0)
        self._bg_box.activated.connect(self._on_background)

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
        self._view.load(path,
                        texture_roots or default_texture_roots(path),
                        archive_roots or default_archive_roots(path),
                        resolver, None, tex_override, keep_view)

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

    def texture_source(self):
        """Data of the picked source, or None for 'as the game loads'."""
        return self._tex_box.currentData() if self._tex_box.isVisible() else None

    def _on_texture_source(self, _index):
        self.texture_source_changed.emit(self._tex_box.currentData())

    def set_title(self, display_name: str, status: str = ""):
        """Retitle without loading — a browser showing what it is about to read."""
        self._header.setText(display_name)
        self._stats.setText(status)

    def set_nif_data(self, data: bytes, display_name: str,
                     resolver=None, archives=None, tex_override=None,
                     keep_view: bool = False):
        """Preview in-memory bytes (a BSA/BA2 member); *keep_view* skips
        re-framing."""
        self._header.setText(display_name)
        self._stats.setText(self.tr("Loading…"))
        self._view.load(data, [], None, resolver, archives, tex_override,
                        keep_view)

    def _on_loaded(self, meshes, bounds, gen, tex_paths=None):
        if gen != self._view._generation:
            return          # a stale load must not retitle or repopulate the picker
        # Emitted even for a geometry-less mesh: the host's texture picker is
        # driven by which paths were REQUESTED, not by what resolved.
        safe_emit(self.textures_seen, list(tex_paths or ()))
        if not meshes:
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
        self._stats.setText(self.tr("failed: {0}").format(message))

    def _on_textured(self, on):
        self._view.textured = bool(on)
        self._view.update()

    def _on_wireframe(self, on):
        self._view.wireframe = bool(on)
        self._view.update()

    def _on_invert_mouse(self, on):
        self._view.invert_mouse = bool(on)
        try:
            from Utils.ui_config import save_nif_invert_mouse
            save_nif_invert_mouse(bool(on))
        except Exception:
            pass

    def _on_background(self, _index):
        key = self._bg_box.currentData() or "light"
        self._view.set_background(key)
        try:
            from Utils.ui_config import save_nif_background
            save_nif_background(key)
        except Exception:
            pass

    def event(self, e):
        # Scoped tabs close via deleteLater(); closeEvent never fires.
        if e.type() == QEvent.DeferredDelete:
            self._view.release_gl()
        return super().event(e)


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
