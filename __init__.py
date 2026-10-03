# SPDX-License-Identifier: GPL-3.0-or-later
"""Render with Blender, then encode an animated AVIF using bundled libavif."""
import os
from pathlib import Path
import platform
import shutil
import subprocess
import tempfile
from fractions import Fraction

import bpy
from bpy.app.handlers import persistent
from bpy.props import BoolProperty, EnumProperty, IntProperty, PointerProperty, StringProperty
from bpy.types import AddonPreferences, Operator, Panel, PropertyGroup

bl_info = {
    "name": "AVIF Animation", "author": "AVIF Animation contributors",
    "version": (1, 0, 1), "blender": (4, 2, 0),
    "location": "Output Properties > AVIF Animation",
    "description": "Render and export a single animated AVIF inside Blender",
    "category": "Render",
}

_job = None
_last_folder = ""
_status = "Ready"


def _hidden_process():
    return {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}


def encoder_path(context):
    addon = context.preferences.addons.get(__package__)
    custom = addon.preferences.encoder if addon else ""
    if custom:
        path = Path(bpy.path.abspath(custom)).expanduser()
        if not path.is_file():
            raise RuntimeError("The selected avifenc executable does not exist")
        return str(path.resolve())
    bundled = Path(__file__).parent / "bin" / "windows-x64" / "avifenc.exe"
    if os.name == "nt" and platform.machine().lower() in {"amd64", "x86_64"} and bundled.is_file():
        return str(bundled)
    found = shutil.which("avifenc")
    if found:
        return found
    raise RuntimeError("Select avifenc in the add-on preferences; the bundled encoder requires Windows x64")


def compact_name(index):
    alphabet = "0123456789abcdefghijklmnopqrstuvwxyz"
    result = ""
    while True:
        index, digit = divmod(index, 36)
        result = alphabet[digit] + result
        if not index:
            return result + ".png"


def make_command(encoder, names, target, fps, fps_base, step, quality, speed, chroma, loop, repeats, alpha):
    # Blender stores fps_base as float32; recover values such as 1.001 rather
    # than turning its binary rounding noise into a huge AVIF timescale.
    rate = (Fraction(fps) / Fraction(str(round(fps_base, 6)))).limit_denominator(1000000)
    command = [encoder, "--no-overwrite", "-j", str(min(os.cpu_count() or 1, 16)),
               "-s", str(speed), "-q", str(quality), "--qalpha", "100", "-y", chroma,
               "--timescale", str(rate.numerator), "--duration", str(rate.denominator * step),
               "--repetition-count", "infinite" if loop else str(repeats),
               "--cicp", "1/13/6", "-r", "full"]
    command += ["--"] + names + [str(target)]
    if os.name == "nt" and len(subprocess.list2cmdline(command).encode("utf-16-le")) // 2 >= 32000:
        raise RuntimeError("This animation exceeds Windows' encoder command limit. Export a shorter frame range (about 4,000 frames)")
    return command


def redraw():
    for manager in bpy.data.window_managers:
        for window in manager.windows:
            for area in window.screen.areas:
                if area.type == "PROPERTIES":
                    area.tag_redraw()


class ExportJob:
    def __init__(self, context):
        self.scene = context.scene
        settings = self.scene.avif_animation
        self.encoder = encoder_path(context)
        probe = subprocess.run([self.encoder, "--version"], capture_output=True, text=True,
                               timeout=10, **_hidden_process())
        if probe.returncode:
            raise RuntimeError("avifenc could not start: " + probe.stderr.strip()[-300:])
        raw = settings.filepath.strip()
        if not raw or raw.endswith(("/", "\\")):
            raise RuntimeError("Choose an output filename ending in .avif")
        if raw.startswith("//") and not bpy.data.filepath:
            raise RuntimeError("Save the .blend first or choose an absolute output path")
        self.output = Path(bpy.path.abspath(raw)).expanduser().resolve()
        if self.output.suffix.lower() != ".avif":
            self.output = self.output.with_suffix(".avif")
        self.overwrite = settings.overwrite
        if self.output.exists() and not self.overwrite:
            raise RuntimeError("Output already exists. Choose another name or enable Overwrite")
        if self.output.is_dir():
            raise RuntimeError("The output path is a directory")
        self.frames = list(range(self.scene.frame_start, self.scene.frame_end + 1, self.scene.frame_step))
        if not self.frames:
            raise RuntimeError("The scene frame range is empty")
        if self.scene.render.use_multiview:
            raise RuntimeError("Disable Multiview before exporting AVIF")
        if self.scene.display_settings.display_device != "sRGB":
            raise RuntimeError("Set Color Management > Display Device to sRGB for this SDR AVIF exporter")
        self.output.parent.mkdir(parents=True, exist_ok=True)
        # Reserve only a unique staging filename; the existing output stays intact.
        fd, partial = tempfile.mkstemp(prefix=".avif-", suffix=".avif", dir=self.output.parent)
        os.close(fd)
        Path(partial).unlink()
        self.partial = Path(partial)
        self.names = [compact_name(index) for index in range(len(self.frames))]
        self.command = make_command(self.encoder, self.names, self.partial,
                                    self.scene.render.fps, self.scene.render.fps_base,
                                    self.scene.frame_step, settings.quality, settings.speed,
                                    settings.chroma, settings.loop, settings.repeats, settings.alpha)
        self.temp = Path(tempfile.mkdtemp(prefix="blender-avif-"))
        self.keep = settings.keep_frames
        self.alpha = settings.alpha
        self.render_finished = False
        self.render_cancelled = False
        self.process = None
        self.log = None
        self.saved = None
        self.phase = "RENDER"
        self.written = 0
        self.cancel_requested = False

    def prepare_render(self):
        render = self.scene.render
        image = render.image_settings
        self.saved = {
            "render": {name: getattr(render, name) for name in
                       ("filepath", "use_file_extension", "use_overwrite", "use_placeholder", "film_transparent")},
            "image": {name: getattr(image, name) for name in
                      ("file_format", "color_mode", "color_depth", "compression", "color_management")},
            "frame": self.scene.frame_current, "subframe": self.scene.frame_subframe,
        }
        if hasattr(image, "media_type"):
            self.saved["image"]["media_type"] = image.media_type
            image.media_type = "IMAGE"
        image.file_format = "PNG"
        image.color_mode = "RGBA" if self.alpha else "RGB"
        image.color_depth = "8"
        image.compression = 15
        image.color_management = "FOLLOW_SCENE"
        render.filepath = str(self.temp / "frame_")
        render.use_file_extension = True
        render.use_overwrite = True
        render.use_placeholder = False
        render.film_transparent = self.alpha
        self.sources = [Path(render.frame_path(frame=frame)) for frame in self.frames]

    def restore(self):
        if self.saved is None:
            return
        saved, self.saved = self.saved, None
        render = self.scene.render
        image = render.image_settings
        # Restore media type first so the original video format is valid again.
        if "media_type" in saved["image"]:
            image.media_type = saved["image"]["media_type"]
        for name, value in saved["image"].items():
            if name != "media_type":
                setattr(image, name, value)
        for name, value in saved["render"].items():
            setattr(render, name, value)
        self.scene.frame_set(saved["frame"], subframe=saved["subframe"])

    def start_encoder(self):
        for source in self.sources:
            if not source.is_file() or source.stat().st_size == 0:
                raise RuntimeError("A rendered frame is missing: " + source.name)
        for source, name in zip(self.sources, self.names):
            source.rename(self.temp / name)
        self.restore()
        self.log = (self.temp / "encode.log").open("wb")
        self.process = subprocess.Popen(self.command, cwd=self.temp, stdin=subprocess.DEVNULL,
                                        stdout=self.log, stderr=subprocess.STDOUT, **_hidden_process())
        self.phase = "ENCODE"

    def publish(self):
        if not self.partial.is_file() or self.partial.stat().st_size < 32:
            raise RuntimeError("The encoder produced no valid output")
        if self.overwrite:
            os.replace(self.partial, self.output)
        else:
            # Hard-link commit is atomic and cannot replace an existing file.
            try:
                os.link(self.partial, self.output)
            except FileExistsError:
                raise RuntimeError("Output appeared during the render; choose a new filename")
            except OSError as error:
                raise RuntimeError("Could not commit AVIF on this filesystem; enable Overwrite or export to a local NTFS disk: " + str(error))
            self.partial.unlink()

    def close(self, success=False):
        global _last_folder
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=3)
        if self.log:
            self.log.close()
            self.log = None
        self.restore()
        self.partial.unlink(missing_ok=True)
        if success and not self.keep:
            shutil.rmtree(self.temp)
            _last_folder = ""
        else:
            _last_folder = str(self.temp)


@persistent
def on_render_complete(scene, *args):
    if _job and scene == _job.scene and _job.phase == "RENDER":
        _job.render_finished = True


@persistent
def on_render_cancel(scene, *args):
    if _job and scene == _job.scene and _job.phase == "RENDER":
        _job.render_cancelled = True


@persistent
def on_render_write(scene, *args):
    global _status
    if _job and scene == _job.scene and _job.phase == "RENDER":
        _job.written += 1
        _status = "Rendering %d / %d" % (_job.written, len(_job.frames))


@persistent
def on_load_pre(*args):
    global _job, _status
    if _job:
        _job.close()
        _job = None
        _status = "Export stopped when the file changed; frames kept"


class AVIFPreferences(AddonPreferences):
    bl_idname = __package__
    encoder: StringProperty(name="Custom avifenc", subtype="FILE_PATH",
                            description="Optional override; Windows x64 encoder is included")

    def draw(self, context):
        self.layout.label(text="Windows x64: encoder included. No downloads needed.")
        self.layout.prop(self, "encoder")


class AVIFSettings(PropertyGroup):
    filepath: StringProperty(
        name="Output", subtype="FILE_PATH", default="//animation.avif",
        options={"PATH_SUPPORTS_BLEND_RELATIVE"} if bpy.app.version >= (4, 5, 0) else set(),
    )
    quality: IntProperty(name="Quality", default=75, min=0, max=100,
                         description="Higher gives better color quality and larger files")
    speed: IntProperty(name="Encoding Speed", default=6, min=0, max=10,
                       description="0 is slowest; 10 is fastest, with less efficient compression")
    chroma: EnumProperty(name="Color Detail", items=[
        ("444", "Full (4:4:4)", "Best for text, graphics, and sharp colored edges"),
        ("420", "Compact (4:2:0)", "Smaller files with less color detail")], default="444")
    alpha: BoolProperty(name="Transparent Background", default=False,
                        description="Render with Film Transparent and preserve alpha losslessly")
    loop: BoolProperty(name="Loop Forever", default=True)
    repeats: IntProperty(name="Additional Repeats", default=0, min=0, max=10000,
                         description="0 plays once; 1 plays twice; ignored for Loop Forever")
    overwrite: BoolProperty(name="Overwrite Existing AVIF", default=False)
    keep_frames: BoolProperty(name="Keep Rendered Frames", default=False,
                              description="Keep temporary PNGs after success; failures always keep frames")


class AVIF_OT_render(Operator):
    bl_idname = "render.avif_animation"
    bl_label = "Render & Export AVIF"
    bl_description = "Render the scene frame range and encode one animated AVIF"
    _timer = None

    @classmethod
    def poll(cls, context):
        return context.scene is not None and _job is None and not bpy.app.is_job_running("RENDER")

    def finish(self, context, success, message):
        global _job, _status
        job, _job = _job, None
        cleanup_error = None
        if job:
            try:
                job.close(success)
            except Exception as error:
                cleanup_error = str(error)
        if self._timer:
            context.window_manager.event_timer_remove(self._timer)
            self._timer = None
        _status = message
        if cleanup_error:
            _status += "; cleanup: " + cleanup_error
        self.report({"INFO"} if success else {"WARNING"}, _status)
        redraw()
        return {"FINISHED"} if success else {"CANCELLED"}

    def execute(self, context):
        global _job, _status
        try:
            _job = ExportJob(context)
            _job.prepare_render()
            _status = "Rendering 0 / %d" % len(_job.frames)
            if bpy.app.background:
                result = bpy.ops.render.render("EXEC_DEFAULT", animation=True, scene=_job.scene.name)
                if "CANCELLED" in result or _job.render_cancelled:
                    return self.finish(context, False, "Render cancelled; frames kept")
                _job.start_encoder()
                if _job.process.wait() != 0:
                    return self.finish(context, False, "Encoding failed; open frames/log folder")
                _job.publish()
                return self.finish(context, True, "Exported " + str(_job.output))
            self._timer = context.window_manager.event_timer_add(0.25, window=context.window)
            context.window_manager.modal_handler_add(self)
            result = bpy.ops.render.render("INVOKE_DEFAULT", animation=True, scene=_job.scene.name)
            if "CANCELLED" in result:
                return self.finish(context, False, "Render could not start; frames kept")
            return {"RUNNING_MODAL"}
        except Exception as error:
            return self.finish(context, False, str(error))

    def modal(self, context, event):
        global _status
        if _job is None:
            if self._timer:
                context.window_manager.event_timer_remove(self._timer)
                self._timer = None
            return {"CANCELLED"}
        if event.type == "ESC" and _job.phase == "ENCODE":
            return self.finish(context, False, "Encoding cancelled; frames kept")
        if event.type != "TIMER":
            return {"PASS_THROUGH"}
        try:
            if _job.phase == "RENDER":
                if bpy.app.is_job_running("RENDER"):
                    redraw()
                    return {"PASS_THROUGH"}
                if _job.render_cancelled:
                    return self.finish(context, False, "Render cancelled; frames kept")
                if _job.render_finished:
                    _job.start_encoder()
                    _status = "Encoding AVIF... (Esc to cancel)"
                    redraw()
            elif _job.process.poll() is not None:
                if _job.cancel_requested:
                    return self.finish(context, False, "Encoding cancelled; frames kept")
                if _job.process.returncode:
                    return self.finish(context, False, "Encoding failed; open frames/log folder")
                _job.publish()
                return self.finish(context, True, "Exported " + str(_job.output))
        except Exception as error:
            return self.finish(context, False, str(error))
        return {"PASS_THROUGH"}


class AVIF_OT_cancel(Operator):
    bl_idname = "render.avif_cancel"
    bl_label = "Cancel Encoding"

    @classmethod
    def poll(cls, context):
        return _job is not None and _job.phase == "ENCODE"

    def execute(self, context):
        global _status
        _job.cancel_requested = True
        if _job.process.poll() is None:
            _job.process.terminate()
        _status = "Cancelling encoding..."
        return {"FINISHED"}


class AVIF_OT_open_folder(Operator):
    bl_idname = "render.avif_open_folder"
    bl_label = "Open Frames / Log Folder"

    def execute(self, context):
        folder = _last_folder
        if not folder or not Path(folder).is_dir():
            self.report({"WARNING"}, "No retained frames folder")
            return {"CANCELLED"}
        bpy.ops.wm.path_open(filepath=folder)
        return {"FINISHED"}


class AVIF_PT_output(Panel):
    bl_label = "AVIF Animation"
    bl_idname = "AVIF_PT_output"
    bl_space_type = "PROPERTIES"
    bl_region_type = "WINDOW"
    bl_context = "output"

    def draw(self, context):
        layout = self.layout
        settings = context.scene.avif_animation
        column = layout.column()
        column.enabled = _job is None
        for name in ("filepath", "quality", "speed", "chroma", "alpha", "loop"):
            column.prop(settings, name)
        if not settings.loop:
            column.prop(settings, "repeats")
        column.prop(settings, "overwrite")
        column.prop(settings, "keep_frames")
        frames = range(context.scene.frame_start, context.scene.frame_end + 1, context.scene.frame_step)
        layout.label(text="%d frames | %.3f fps | scene frame range" %
                     (len(frames), context.scene.render.fps / context.scene.render.fps_base))
        layout.operator(AVIF_OT_render.bl_idname, icon="RENDER_ANIMATION")
        if _job:
            if _job.phase == "ENCODE":
                layout.operator(AVIF_OT_cancel.bl_idname, icon="CANCEL")
            else:
                layout.label(text="Press Esc in the render window to cancel")
        layout.label(text=_status[:100])
        if _last_folder:
            layout.operator(AVIF_OT_open_folder.bl_idname, icon="FILE_FOLDER")


def render_menu(self, context):
    self.layout.operator(AVIF_OT_render.bl_idname)


_classes = (AVIFPreferences, AVIFSettings, AVIF_OT_render, AVIF_OT_cancel, AVIF_OT_open_folder, AVIF_PT_output)
_handlers = ((bpy.app.handlers.render_complete, on_render_complete),
             (bpy.app.handlers.render_cancel, on_render_cancel),
             (bpy.app.handlers.render_write, on_render_write),
             (bpy.app.handlers.load_pre, on_load_pre))


def register():
    for cls in _classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.avif_animation = PointerProperty(type=AVIFSettings)
    bpy.types.TOPBAR_MT_render.append(render_menu)
    for handlers, callback in _handlers:
        if callback not in handlers:
            handlers.append(callback)


def _deferred_close():
    global _job
    if bpy.app.is_job_running("RENDER"):
        return 0.25
    if _job:
        _job.close()
        _job = None
    return None


def unregister():
    if _job:
        if bpy.app.is_job_running("RENDER"):
            bpy.app.timers.register(_deferred_close, first_interval=0.25)
        else:
            _deferred_close()
    for handlers, callback in _handlers:
        if callback in handlers:
            handlers.remove(callback)
    bpy.types.TOPBAR_MT_render.remove(render_menu)
    del bpy.types.Scene.avif_animation
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
