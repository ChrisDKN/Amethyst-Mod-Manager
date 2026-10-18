"""Child-process check of the OpenGL widget path."""

import json
import sys
import traceback

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QOpenGLContext, QSurfaceFormat
from PySide6.QtOpenGL import (
    QOpenGLShader, QOpenGLShaderProgram, QOpenGLVertexArrayObject,
)
from PySide6.QtOpenGLWidgets import QOpenGLWidget
from PySide6.QtWidgets import QApplication, QLabel, QWidget
from shiboken6 import delete

from gui_qt.runtime.gl_support import _foreign_gl_libs, preview_surface_format


def main() -> int:
    foreign = _foreign_gl_libs()
    if foreign:
        print(", ".join(foreign), file=sys.stderr)
        return 9

    QSurfaceFormat.setDefaultFormat(preview_surface_format())
    app = QApplication(sys.argv)
    result = None
    stage = 0
    check_pending = False

    def finish(code, detail=""):
        nonlocal result
        if result is not None:
            return
        result = code
        if detail:
            print(detail, file=sys.stderr, flush=True)
        QTimer.singleShot(0, lambda: app.exit(code))

    def exception_hook(kind, value, tb):
        traceback.print_exception(kind, value, tb)
        finish(7)

    sys.excepthook = exception_hook

    class Viewport(QOpenGLWidget):
        def __init__(self, parent):
            super().__init__(parent)
            self.setFormat(preview_surface_format())
            self.program = None
            self.vao = None
            self.info = {}

        def initializeGL(self):
            ctx = self.context()
            if ctx is None or not ctx.isValid():
                finish(2)
                return
            if QOpenGLContext.currentContext() != ctx:
                finish(4)
                return
            fmt = ctx.format()
            if ctx.isOpenGLES() or fmt.version() < (3, 3):
                finish(6, f"Received {'OpenGL ES' if ctx.isOpenGLES() else 'OpenGL'} "
                          f"{fmt.majorVersion()}.{fmt.minorVersion()}")
                return
            functions = ctx.functions()

            def gl_string(token):
                value = functions.glGetString(token)
                if isinstance(value, (bytes, bytearray)):
                    return value.decode("utf-8", "replace")
                return str(value or "")

            self.info = {
                "renderer": gl_string(0x1F01),
                "vendor": gl_string(0x1F00),
                "version": gl_string(0x1F02),
                "glsl": gl_string(0x8B8C),
                "surface": f"{fmt.majorVersion()}.{fmt.minorVersion()}",
                "gles": ctx.isOpenGLES(),
            }
            self.program = QOpenGLShaderProgram(self)
            vertex = """#version 330 core
const vec2 vertices[3] = vec2[3](vec2(-1, -1), vec2(3, -1), vec2(-1, 3));
void main() { gl_Position = vec4(vertices[gl_VertexID], 0, 1); }
"""
            fragment = """#version 330 core
out vec4 color;
void main() { color = vec4(0.25, 0.5, 0.75, 1); }
"""
            if not (self.program.addShaderFromSourceCode(QOpenGLShader.Vertex, vertex)
                    and self.program.addShaderFromSourceCode(QOpenGLShader.Fragment, fragment)
                    and self.program.link()):
                finish(7, self.program.log())
                return
            self.vao = QOpenGLVertexArrayObject(self)
            if not self.vao.create():
                finish(7, "Vertex array creation failed")

        def paintGL(self):
            if result is not None or self.program is None or self.vao is None:
                return
            functions = self.context().functions()
            functions.glClearColor(0, 0, 0, 1)
            functions.glClear(0x00004000)
            if not self.program.bind():
                finish(7, "Shader program could not be bound")
                return
            self.vao.bind()
            functions.glDrawArrays(0x0004, 0, 3)
            self.vao.release()
            self.program.release()
            error = functions.glGetError()
            if error:
                finish(7, f"OpenGL drawing error: {error:#x}")

    window = QWidget(None, Qt.Tool | Qt.FramelessWindowHint
                     | Qt.WindowDoesNotAcceptFocus | Qt.WindowTransparentForInput)
    window.setAttribute(Qt.WA_ShowWithoutActivating)
    window.setWindowTitle("Amethyst OpenGL probe")
    window.setWindowOpacity(0.0)
    window.resize(32, 32)
    viewport = Viewport(window)
    viewport.setGeometry(window.rect())
    overlay = QLabel("GL", window)
    overlay.setStyleSheet("background: #112233; color: white;")
    overlay.setGeometry(0, 0, 16, 16)
    overlay.hide()

    def check_frame():
        nonlocal stage, check_pending
        check_pending = False
        if result is not None:
            return
        if not viewport.isValid():
            finish(2)
            return
        image = viewport.grabFramebuffer()
        if image.isNull():
            finish(7, "Framebuffer readback failed")
            return
        color = image.pixelColor(image.width() // 2, image.height() // 2)
        if any(abs(actual - expected) > 3 for actual, expected in zip(
                (color.red(), color.green(), color.blue()), (64, 128, 191))):
            finish(7, f"Unexpected framebuffer color: {color.name()}")
            return
        if stage == 0:
            stage = 1
            window.resize(40, 40)
            viewport.setGeometry(window.rect())
            overlay.show()
            overlay.raise_()
            viewport.update()
            window.update()
        else:
            finish(0)

    def frame_swapped():
        nonlocal check_pending
        if result is None and not check_pending:
            check_pending = True
            QTimer.singleShot(0, check_frame)

    viewport.frameSwapped.connect(frame_swapped)
    QTimer.singleShot(10000, lambda: finish(8))
    window.show()
    app.exec()
    info = viewport.info
    if result == 0 and not viewport.isValid():
        result = 2
    if viewport.isValid():
        viewport.makeCurrent()
        if QOpenGLContext.currentContext() == viewport.context():
            if viewport.vao is not None:
                viewport.vao.destroy()
            if viewport.program is not None:
                delete(viewport.program)
                viewport.program = None
            viewport.doneCurrent()
        elif result == 0:
            result = 4
    window.close()
    delete(window)
    if result == 0:
        print("__AMM_GL_INFO__" + json.dumps({**info, "validated": True}), flush=True)
    return result if result is not None else 8
