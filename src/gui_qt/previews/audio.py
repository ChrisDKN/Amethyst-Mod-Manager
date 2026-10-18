"""Compact audio controls embedded in the Mod Files tab."""

from __future__ import annotations

import shutil
from pathlib import Path

from PySide6.QtCore import (
    QByteArray, QBuffer, QDir, QIODevice, QProcess, QTemporaryFile, Qt, QUrl,
)
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QPushButton, QSizePolicy, QSlider, QStyle,
    QToolButton, QWidget,
)

try:
    from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
except ImportError:
    QAudioOutput = QMediaPlayer = None

AUDIO_EXTS = {
    ".aac", ".ac3", ".aif", ".aifc", ".aiff", ".ape", ".au", ".caf",
    ".flac", ".fuz", ".m4a", ".mka", ".mp2", ".mp3", ".mpa", ".oga",
    ".ogg", ".opus", ".snd", ".wav", ".wave", ".wem", ".wma", ".xwm",
    ".xwma",
}


def _format_time(milliseconds: int) -> str:
    seconds = max(0, milliseconds) // 1000
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"


def _read_fuz_audio(path: Path) -> bytes:
    with path.open("rb") as stream:
        header = stream.read(12)
        if len(header) != 12 or header[:4] != b"FUZE":
            raise ValueError
        lip_size = int.from_bytes(header[8:12], "little")
        if lip_size > path.stat().st_size - 12:
            raise ValueError
        stream.seek(lip_size, 1)
        audio = stream.read()
    if not audio:
        raise ValueError
    return audio


def _is_xwma(data: bytes) -> bool:
    return len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"XWMA"


class AudioControls(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("BottomBar")
        self._path: Path | None = None
        self._display_name = ""
        self._duration = 0
        self._source_buffer: QBuffer | None = None
        self._prepare_process: QProcess | None = None
        self._prepare_input: QTemporaryFile | None = None
        self._prepared_file: QTemporaryFile | None = None
        self._player = None
        self._audio_output = None
        self._build()
        self.hide()

        if QMediaPlayer is None or QAudioOutput is None:
            self._set_unavailable(self.tr("Qt Multimedia is not installed."))
            self._mute_button.setEnabled(False)
            self._volume.setEnabled(False)

    def _ensure_player(self) -> bool:
        if self._player is not None:
            return True
        if QMediaPlayer is None or QAudioOutput is None:
            return False
        self._player = QMediaPlayer(self)
        self._audio_output = QAudioOutput(self)
        self._audio_output.setVolume(self._volume.value() / 100.0)
        self._player.setAudioOutput(self._audio_output)
        self._player.positionChanged.connect(self._on_position_changed)
        self._player.durationChanged.connect(self._on_duration_changed)
        self._player.playbackStateChanged.connect(self._on_state_changed)
        self._player.mediaStatusChanged.connect(self._on_media_status_changed)
        self._player.seekableChanged.connect(self._on_seekable_changed)
        self._player.errorOccurred.connect(self._on_error)
        self._audio_output.mutedChanged.connect(self._sync_mute_button)
        self._sync_mute_button()
        return True

    def _build(self) -> None:
        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(6)

        self._play_button = QPushButton(self.tr("Play"))
        self._play_button.setIcon(self.style().standardIcon(
            QStyle.StandardPixmap.SP_MediaPlay))
        self._play_button.setCursor(Qt.PointingHandCursor)
        self._play_button.clicked.connect(self._toggle_playback)
        self._play_button.setEnabled(False)
        layout.addWidget(self._play_button)

        self._stop_button = QPushButton(self.tr("Stop"))
        self._stop_button.setIcon(self.style().standardIcon(
            QStyle.StandardPixmap.SP_MediaStop))
        self._stop_button.setCursor(Qt.PointingHandCursor)
        self._stop_button.clicked.connect(self._stop)
        self._stop_button.setEnabled(False)
        layout.addWidget(self._stop_button)

        self._seek = QSlider(Qt.Horizontal)
        self._seek.setRange(0, 0)
        self._seek.setEnabled(False)
        self._seek.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._seek.sliderMoved.connect(self._show_seek_position)
        self._seek.sliderReleased.connect(self._seek_released)
        layout.addWidget(self._seek, 1)

        self._time = QLabel("0:00 / 0:00")
        self._time.setMinimumWidth(92)
        self._time.setAlignment(Qt.AlignCenter)
        layout.addWidget(self._time)

        self._mute_button = QToolButton()
        self._mute_button.setAutoRaise(True)
        self._mute_button.setCursor(Qt.PointingHandCursor)
        self._mute_button.clicked.connect(self._toggle_mute)
        layout.addWidget(self._mute_button)

        self._volume = QSlider(Qt.Horizontal)
        self._volume.setRange(0, 100)
        self._volume.setValue(80)
        self._volume.setFixedWidth(100)
        self._volume.setToolTip(self.tr("Volume"))
        self._volume.valueChanged.connect(self._set_volume)
        layout.addWidget(self._volume)
        self._sync_mute_button()

    def set_audio(self, path: Path, display_name: str = "") -> None:
        path = Path(path)
        self._release_source()
        self._path = path
        self._display_name = display_name or path.name
        self.setToolTip(self._display_name)
        self._reset_timeline()
        self.show()
        if not self._ensure_player():
            self._set_unavailable(self.tr("Qt Multimedia is not installed."))
            return

        if not path.is_file():
            self._set_unavailable(self.tr("Audio file not found."))
            return

        self._play_button.setEnabled(True)
        self._stop_button.setEnabled(True)
        if path.suffix.lower() == ".fuz":
            try:
                audio = _read_fuz_audio(path)
            except (OSError, ValueError):
                self._set_unavailable(
                    self.tr("The FUZ audio stream could not be read."))
                return
            source_buffer = QBuffer(self)
            source_buffer.setData(QByteArray(audio))
            if not source_buffer.open(QIODevice.OpenModeFlag.ReadOnly):
                source_buffer.deleteLater()
                self._set_unavailable(self.tr("The audio stream could not be opened."))
                return
            self._source_buffer = source_buffer
            if _is_xwma(audio) and self._start_xwma_predecode(source_buffer):
                return
            self._set_direct_source(source_buffer)
        else:
            if path.suffix.lower() in {".xwm", ".xwma"}:
                try:
                    with path.open("rb") as stream:
                        is_xwma = _is_xwma(stream.read(12))
                except OSError:
                    is_xwma = False
                if is_xwma and self._start_xwma_predecode():
                    return
            self._set_direct_source()

    def _set_direct_source(self, source_buffer: QBuffer | None = None) -> None:
        if self._path is None:
            return
        self._reset_timeline()
        self._play_button.setEnabled(True)
        self._stop_button.setEnabled(True)
        if source_buffer is not None:
            source_buffer.seek(0)
            self._player.setSourceDevice(
                source_buffer,
                QUrl.fromLocalFile(str(self._path.with_suffix(".xwm"))))
        else:
            self._player.setSource(QUrl.fromLocalFile(str(self._path.resolve())))

    def _start_xwma_predecode(
            self, source_buffer: QBuffer | None = None) -> bool:
        program = shutil.which("vgmstream-cli")
        if program is None or self._path is None:
            return False

        input_file = None
        input_path = str(self._path.resolve())
        if source_buffer is not None:
            input_file = QTemporaryFile(
                QDir.tempPath() + "/amethyst-audio-XXXXXX.xwm", self)
            data = source_buffer.data()
            if not input_file.open() or input_file.write(data) != data.size() or \
                    not input_file.flush():
                input_file.deleteLater()
                return False
            input_path = input_file.fileName()
            input_file.close()

        prepared_file = QTemporaryFile(
            QDir.tempPath() + "/amethyst-audio-XXXXXX.wav", self)
        if not prepared_file.open():
            if input_file is not None:
                input_file.deleteLater()
            prepared_file.deleteLater()
            return False
        output_path = prepared_file.fileName()
        prepared_file.close()

        process = QProcess(self)
        process.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        process.finished.connect(self._on_predecode_finished)
        process.errorOccurred.connect(self._on_predecode_error)
        self._prepare_process = process
        self._prepare_input = input_file
        self._prepared_file = prepared_file
        self._play_button.setEnabled(False)
        self._stop_button.setEnabled(False)
        self._time.setText(self.tr("Preparing…"))
        self._time.setToolTip(self.tr("Preparing XWM audio…"))
        process.start(program, ["-i", "-o", output_path, input_path])
        return True

    def _on_predecode_finished(self, exit_code: int, exit_status) -> None:
        if self._prepare_process is None or self._prepared_file is None:
            return
        output_path = Path(self._prepared_file.fileName())
        try:
            with output_path.open("rb") as stream:
                header = stream.read(12)
            valid = output_path.stat().st_size > 44 and \
                header[:4] == b"RIFF" and header[8:12] == b"WAVE"
        except OSError:
            valid = False
        if exit_code != 0 or exit_status != QProcess.ExitStatus.NormalExit or \
                not valid:
            self._predecode_failed()
            return

        self._discard_predecode()
        self._reset_timeline()
        self._play_button.setEnabled(True)
        self._stop_button.setEnabled(True)
        self._player.setSource(QUrl.fromLocalFile(str(output_path)))

    def _on_predecode_error(self, *_args) -> None:
        if self._prepare_process is not None:
            self._predecode_failed()

    def _predecode_failed(self) -> None:
        source_buffer = self._source_buffer
        self._discard_predecode(release_buffer=False)
        prepared_file = self._prepared_file
        self._prepared_file = None
        if prepared_file is not None:
            prepared_file.close()
            prepared_file.deleteLater()
        self._set_direct_source(source_buffer)

    def _discard_predecode(self, release_buffer: bool = True) -> None:
        process = self._prepare_process
        self._prepare_process = None
        if process is not None:
            if process.state() != QProcess.ProcessState.NotRunning:
                process.kill()
            process.deleteLater()
        input_file = self._prepare_input
        self._prepare_input = None
        if input_file is not None:
            input_file.close()
            input_file.deleteLater()
        if release_buffer:
            source_buffer = self._source_buffer
            self._source_buffer = None
            if source_buffer is not None:
                source_buffer.close()
                source_buffer.deleteLater()

    def _release_source(self) -> None:
        self._discard_predecode()
        if self._player is not None:
            self._player.stop()
            self._player.setSource(QUrl())
        source_buffer = self._source_buffer
        self._source_buffer = None
        if source_buffer is not None:
            source_buffer.close()
            source_buffer.deleteLater()
        prepared_file = self._prepared_file
        self._prepared_file = None
        if prepared_file is not None:
            prepared_file.close()
            prepared_file.deleteLater()

    def _reset_timeline(self) -> None:
        self._duration = 0
        self._seek.setRange(0, 0)
        self._seek.setEnabled(False)
        self._time.setText("0:00 / 0:00")
        self._time.setToolTip("")

    def _set_unavailable(self, message: str) -> None:
        self._time.setText(self.tr("Unavailable"))
        self._time.setToolTip(message)
        self._play_button.setEnabled(False)
        self._stop_button.setEnabled(False)
        self._seek.setEnabled(False)

    def clear_audio(self) -> None:
        self._release_source()
        self._path = None
        self._display_name = ""
        self.setToolTip("")
        self._reset_timeline()
        self._play_button.setEnabled(False)
        self._stop_button.setEnabled(False)
        self.hide()

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
            self._set_unavailable(self.tr("This audio format could not be played."))

    def _on_error(self, error, message: str) -> None:
        if error == QMediaPlayer.Error.NoError:
            return
        text = self.tr("Could not play this audio file.")
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

    def hideEvent(self, event) -> None:
        if self._player is not None and self._player.playbackState() == \
                QMediaPlayer.PlaybackState.PlayingState:
            self._player.pause()
        super().hideEvent(event)
