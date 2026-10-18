"""Panel-scoped video player for the Mod Files tab."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QUrl
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QPushButton, QSizePolicy, QSlider, QStackedWidget,
    QStyle, QToolButton, QVBoxLayout, QWidget,
)

try:
    from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
    from PySide6.QtMultimediaWidgets import QVideoWidget
except ImportError:
    QAudioOutput = QMediaPlayer = QVideoWidget = None


VIDEO_EXTS = {
    ".3g2", ".3gp", ".asf", ".avi", ".bik", ".bk2", ".divx", ".f4v",
    ".flv", ".ivf", ".m1v", ".m2ts", ".m2v", ".m4v", ".mjpeg", ".mjpg",
    ".mkv", ".mov", ".mp4", ".mpeg", ".mpg", ".mpv", ".mts", ".mve",
    ".mxf", ".nut", ".ogv", ".rm", ".rmvb", ".roq", ".rpl", ".sfd",
    ".smk", ".ts", ".usm", ".vob", ".vp6", ".webm", ".wmv", ".y4m",
}


def _format_time(milliseconds: int) -> str:
    seconds = max(0, milliseconds) // 1000
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"


class VideoPreview(QWidget):
    def __init__(self, path: Path | None = None, display_name: str = "",
                 parent=None):
        super().__init__(parent)
        self._path: Path | None = None
        self._display_name = ""
        self._duration = 0
        self._player = None
        self._audio_output = None
        self._build()

        if not self._ensure_player():
            self._set_unavailable(self.tr("Qt Multimedia is not installed."))
            self._mute_button.setEnabled(False)
            self._volume.setEnabled(False)
        if path is not None:
            self.set_video(path, display_name)

    def _build(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._display = QStackedWidget(self)
        self._video = QVideoWidget(self._display) if QVideoWidget else None
        if self._video is not None:
            self._video.setAspectRatioMode(Qt.KeepAspectRatio)
            self._video.setSizePolicy(QSizePolicy.Expanding,
                                      QSizePolicy.Expanding)
            self._video.setStyleSheet("background: black;")
            self._display.addWidget(self._video)

        self._message = QLabel(self.tr("No video selected"), self._display)
        self._message.setAlignment(Qt.AlignCenter)
        self._message.setWordWrap(True)
        self._message.setStyleSheet("background: black; color: #dddddd;")
        self._display.addWidget(self._message)
        self._display.setCurrentWidget(self._message)
        layout.addWidget(self._display, 1)

        controls = QWidget(self)
        controls.setObjectName("BottomBar")
        bar = QHBoxLayout(controls)
        bar.setContentsMargins(10, 8, 10, 8)
        bar.setSpacing(6)

        self._play_button = QPushButton(self.tr("Play"))
        self._play_button.setIcon(self.style().standardIcon(
            QStyle.StandardPixmap.SP_MediaPlay))
        self._play_button.setCursor(Qt.PointingHandCursor)
        self._play_button.setEnabled(False)
        self._play_button.clicked.connect(self._toggle_playback)
        bar.addWidget(self._play_button)

        self._stop_button = QPushButton(self.tr("Stop"))
        self._stop_button.setIcon(self.style().standardIcon(
            QStyle.StandardPixmap.SP_MediaStop))
        self._stop_button.setCursor(Qt.PointingHandCursor)
        self._stop_button.setEnabled(False)
        self._stop_button.clicked.connect(self._stop)
        bar.addWidget(self._stop_button)

        self._seek = QSlider(Qt.Horizontal)
        self._seek.setRange(0, 0)
        self._seek.setEnabled(False)
        self._seek.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._seek.sliderMoved.connect(self._show_seek_position)
        self._seek.sliderReleased.connect(self._seek_released)
        bar.addWidget(self._seek, 1)

        self._time = QLabel("0:00 / 0:00")
        self._time.setMinimumWidth(92)
        self._time.setAlignment(Qt.AlignCenter)
        bar.addWidget(self._time)

        self._mute_button = QToolButton()
        self._mute_button.setAutoRaise(True)
        self._mute_button.setCursor(Qt.PointingHandCursor)
        self._mute_button.clicked.connect(self._toggle_mute)
        bar.addWidget(self._mute_button)

        self._volume = QSlider(Qt.Horizontal)
        self._volume.setRange(0, 100)
        self._volume.setValue(80)
        self._volume.setFixedWidth(100)
        self._volume.setToolTip(self.tr("Volume"))
        self._volume.valueChanged.connect(self._set_volume)
        bar.addWidget(self._volume)
        self._sync_mute_button()
        layout.addWidget(controls)

    def _ensure_player(self) -> bool:
        if self._player is not None:
            return True
        if QMediaPlayer is None or QAudioOutput is None or self._video is None:
            return False
        self._player = QMediaPlayer(self)
        self._audio_output = QAudioOutput(self)
        self._audio_output.setVolume(self._volume.value() / 100.0)
        self._player.setAudioOutput(self._audio_output)
        self._player.setVideoOutput(self._video)
        self._player.positionChanged.connect(self._on_position_changed)
        self._player.durationChanged.connect(self._on_duration_changed)
        self._player.playbackStateChanged.connect(self._on_state_changed)
        self._player.mediaStatusChanged.connect(self._on_media_status_changed)
        self._player.seekableChanged.connect(self._on_seekable_changed)
        self._player.errorOccurred.connect(self._on_error)
        self._audio_output.mutedChanged.connect(self._sync_mute_button)
        self._sync_mute_button()
        return True

    def set_video(self, path: Path, display_name: str = "") -> None:
        path = Path(path)
        self._release_source()
        self._path = path
        self._display_name = display_name or path.name
        self.setToolTip(self._display_name)
        self._reset_timeline()

        if not self._ensure_player():
            self._set_unavailable(self.tr("Qt Multimedia is not installed."))
            return
        if not path.is_file():
            self._set_unavailable(self.tr("Video file not found."))
            return

        self._display.setCurrentWidget(self._video)
        self._play_button.setEnabled(True)
        self._stop_button.setEnabled(True)
        self._player.setSource(QUrl.fromLocalFile(str(path.resolve())))

    def _release_source(self) -> None:
        if self._player is not None:
            self._player.stop()
            self._player.setSource(QUrl())

    def _reset_timeline(self) -> None:
        self._duration = 0
        self._seek.setRange(0, 0)
        self._seek.setEnabled(False)
        self._time.setText("0:00 / 0:00")
        self._time.setToolTip("")

    def _set_unavailable(self, message: str) -> None:
        self._message.setText(message)
        self._display.setCurrentWidget(self._message)
        self._time.setText(self.tr("Unavailable"))
        self._time.setToolTip(message)
        self._play_button.setEnabled(False)
        self._stop_button.setEnabled(False)
        self._seek.setEnabled(False)

    def clear_video(self) -> None:
        self._release_source()
        self._path = None
        self._display_name = ""
        self.setToolTip("")
        self._reset_timeline()
        self._message.setText(self.tr("No video selected"))
        self._display.setCurrentWidget(self._message)
        self._play_button.setEnabled(False)
        self._stop_button.setEnabled(False)

    def _toggle_playback(self) -> None:
        if self._player is None:
            return
        if self._player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self._player.pause()
            return
        if self._player.mediaStatus() == QMediaPlayer.MediaStatus.EndOfMedia:
            self._player.setPosition(0)
        self._player.play()

    def _stop(self) -> None:
        if self._player is not None:
            self._player.stop()

    def _seek_released(self) -> None:
        if self._player is not None and self._seek.isEnabled():
            self._player.setPosition(self._seek.value())

    def _show_seek_position(self, position: int) -> None:
        self._update_time(position)

    def _on_position_changed(self, position: int) -> None:
        if not self._seek.isSliderDown():
            self._seek.setValue(position)
            self._update_time(position)

    def _on_duration_changed(self, duration: int) -> None:
        self._duration = max(0, duration)
        self._seek.setRange(0, self._duration)
        self._on_seekable_changed(self._player.isSeekable())
        self._update_time(self._player.position())

    def _on_seekable_changed(self, seekable: bool) -> None:
        self._seek.setEnabled(bool(seekable and self._duration > 0))

    def _update_time(self, position: int) -> None:
        self._time.setText(
            f"{_format_time(position)} / {_format_time(self._duration)}")

    def _on_state_changed(self, state) -> None:
        playing = state == QMediaPlayer.PlaybackState.PlayingState
        self._play_button.setText(self.tr("Pause") if playing else self.tr("Play"))
        self._play_button.setIcon(self.style().standardIcon(
            QStyle.StandardPixmap.SP_MediaPause if playing
            else QStyle.StandardPixmap.SP_MediaPlay))

    def _on_media_status_changed(self, status) -> None:
        if status == QMediaPlayer.MediaStatus.InvalidMedia:
            self._set_unavailable(self.tr("This video format could not be played."))
        elif status == QMediaPlayer.MediaStatus.LoadedMedia and \
                self._video is not None:
            self._display.setCurrentWidget(self._video)

    def _on_error(self, error, message: str) -> None:
        if error == QMediaPlayer.Error.NoError:
            return
        text = self.tr("Could not play this video file.")
        if message:
            text += f" {message}"
        self._set_unavailable(text)

    def _set_volume(self, value: int) -> None:
        if self._audio_output is not None:
            self._audio_output.setVolume(value / 100.0)
        self._sync_mute_button()

    def _toggle_mute(self) -> None:
        if self._audio_output is not None:
            if self._volume.value() == 0:
                self._volume.setValue(80)
                self._audio_output.setMuted(False)
                return
            self._audio_output.setMuted(not self._audio_output.isMuted())

    def _sync_mute_button(self, *_args) -> None:
        muted = self._volume.value() == 0
        if self._audio_output is not None:
            muted = muted or self._audio_output.isMuted()
        pixmap = (QStyle.StandardPixmap.SP_MediaVolumeMuted if muted
                  else QStyle.StandardPixmap.SP_MediaVolume)
        self._mute_button.setIcon(self.style().standardIcon(pixmap))
        self._mute_button.setToolTip(self.tr("Unmute") if muted else self.tr("Mute"))

    def tab_closing(self) -> None:
        self.clear_video()

    def hideEvent(self, event) -> None:
        if self._player is not None and self._player.playbackState() == \
                QMediaPlayer.PlaybackState.PlayingState:
            self._player.pause()
        super().hideEvent(event)
