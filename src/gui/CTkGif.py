import customtkinter as ctk
from PIL import Image


class CTkGif(ctk.CTkLabel):

    def __init__(self, master: any, path, loop=True, acceleration=1, repeat=1, width=40, height=40, **kwargs):
        super().__init__(master, **kwargs)
        if acceleration <= 0:
            raise ValueError('Acceleration must be strictly positive')
        self.master = master
        self.repeat = repeat
        self.configure(text='')
        self.path = path
        self.count = 0
        self.loop = loop
        self.acceleration = acceleration
        self.index = 0
        self.is_playing = False
        self.gif = Image.open(path)
        self.n_frame = self.gif.n_frames
        self.frame_duration = self.gif.info['duration'] * 1 / self.acceleration
        self.force_stop = False

        self.width = width
        self.height = height

    def update(self):
        if self.index < self.n_frame:
            if not self.force_stop:
                self.gif.seek(self.index)
                self.configure(image=ctk.CTkImage(self.gif, size=(self.width, self.height)))
                self.index += 1
                self.after(int(self.frame_duration), self.update)
            else:
                self.force_stop = False
        else:
            self.index = 0
            self.count += 1
            if self.is_playing and (self.count < self.repeat or self.loop):
                self.after(int(self.frame_duration), self.update)
            else:
                self.is_playing = False

    def start(self):
        if not self.is_playing:
            self.count = 0
            self.is_playing = True
            self.after(int(self.frame_duration), self.update)

    def stop(self, forced=False):
        if self.is_playing:
            self.is_playing = False
            self.force_stop = forced

    def toggle(self, forced=False):
        if self.is_playing:
            self.stop(forced=forced)
        else:
            self.start()
