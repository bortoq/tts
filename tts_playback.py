"""Interactive mpv playback with a local Lua control bridge."""
import json
import os
import subprocess
import time
from tts_config import executable

def player_command():
    return [executable("mpv"), "--no-video", "--idle=yes",
            "--keep-open=no", "--terminal=yes", "--input-terminal=yes", "--resume-playback=no",
            "--msg-level=cplayer=warn,cplayer/statusline=status,file=warn",
            "--input-default-bindings=yes", "--cache=yes",
            "--demuxer-max-bytes=32MiB", "--"]


class PlaybackStopped(Exception):
    """The user closed mpv (for example with q)."""


class MpvPlayer:
    """One interactive mpv receives continuous stdin; Lua reports completion."""
    def __init__(self, directory, source=None, raw=False, speed=1.0, state=None):
        self.directory = directory
        self.source = source
        self.raw = raw
        self.speed = speed
        self.state = state or {}
        self.commands_path = directory / "mpv-commands.jsonl"
        self.events_path = directory / "mpv-events.jsonl"
        self.process = None
        self.events = None

    def __enter__(self):
        (self.directory / "mpv-exit.json").unlink(missing_ok=True)
        self.commands_path.touch()
        self.events_path.touch()
        script = self.directory / "tts-mpv.lua"
        script.write_text('''
local mp = require('mp')
local utils = require('mp.utils')
local directory = os.getenv('TTS_MPV_CONTROL_DIR')
local output = assert(io.open(directory .. '/mpv-events.jsonl', 'a'))
local offset = 0
local restored = utils.parse_json(os.getenv('TTS_MPV_STATE') or '{}') or {}
local stats_visible = false
local function toggle_stats()
    stats_visible = not stats_visible
    mp.command('script-binding stats/display-stats-toggle')
end
mp.add_key_binding('I', 'tts-stats-toggle', toggle_stats)
mp.add_key_binding('Ctrl+i', 'tts-stats-ctrl', toggle_stats)
-- Traditional terminals send the same byte for Ctrl+I and Tab.
mp.add_key_binding('TAB', 'tts-stats-tab', toggle_stats)
mp.add_key_binding('Ctrl+o', 'tts-osd-ctrl', function()
    mp.command('no-osd cycle-values osd-level 3 1')
end)
local function emit(event)
    output:write(utils.format_json(event), '\\n')
    output:flush()
end
mp.register_event('start-file', function(event)
    event.event = 'start-file'
    emit(event)
end)
local last_position = nil
local last_state = nil
local closing = false
mp.observe_property('time-pos', 'number', function(_, position)
    if position then last_position = position end
end)
local function save_position()
    local position = mp.get_property_number('time-pos')
    if position and not closing then
        last_state = {seconds=position, speed=mp.get_property_number('speed', 1),
            volume=mp.get_property_number('volume', 100), mute=mp.get_property_bool('mute', false),
            osd_level=mp.get_property_number('osd-level', 1), stats_visible=stats_visible}
    end
    position = position or last_position
    if position then
        local path = directory .. '/mpv-position.json'
        local f = assert(io.open(path .. '.part', 'w'))
        local encoded = utils.format_json(last_state or {seconds=position})
        f:write(encoded)
        f:close()
        os.rename(path .. '.part', path)
    end
end
local function quiet_exit()
    if closing then return end
    closing = true
    -- Keep live mpv output; silence only teardown redraws after saving state.
    mp.set_property('msg-level', 'cplayer=warn,cplayer/statusline=no,file=warn')
    mp.set_property('term-osd', 'no')
    mp.set_property('term-status-msg', '')
    if stats_visible then mp.command('script-binding stats/display-stats-toggle') end
    mp.osd_message('', 0)
end
mp.register_event('end-file', function(event)
    save_position()
    quiet_exit()
    event.event = 'end-file'
    emit(event)
end)
mp.register_event('playback-restart', function()
    if restored.stats_visible then
        restored.stats_visible = false
        toggle_stats()
    end
end)
local function quit_user(remember)
    save_position()
    local path = directory .. '/mpv-exit.json'
    local f = assert(io.open(path .. '.part', 'w'))
    local encoded = utils.format_json({remember_position=remember})
    f:write(encoded)
    f:close()
    os.rename(path .. '.part', path)
    quiet_exit()
    mp.command('quit')
end
mp.add_key_binding('Q', 'tts-quit-save', function() quit_user(true) end)
mp.add_key_binding('q', 'tts-quit', function() quit_user(false) end)
mp.register_event('shutdown', function()
    save_position()
    quiet_exit()
    emit({event='shutdown'})
end)
mp.add_periodic_timer(0.02, function()
    local input = assert(io.open(directory .. '/mpv-commands.jsonl', 'r'))
    input:seek('set', offset)
    for line in input:lines() do
        local request = utils.parse_json(line)
        if request then
            local result, error
            if request.command[1] == 'get_property' then
                result, error = mp.get_property_native(request.command[2])
            else
                if request.command[1] == 'quit' then
                    save_position()
                    quiet_exit()
                end
                result, error = mp.command_native(request.command)
            end
            emit({request_id=request.request_id, data=result, error=error or 'success'})
        end
    end
    offset = input:seek()
    input:close()
end)
mp.add_periodic_timer(0.25, save_position)
emit({event='ready'})
''', encoding="utf-8")
        self.events = self.events_path.open(encoding="utf-8")
        command = player_command()
        # Keep stdout/stderr attached to the terminal. stdin carries audio;
        # mpv reads terminal controls from /dev/tty while stdin carries audio.
        try:
            options = [f"--script={script}", f"--speed={self.speed}"]
            if 'volume' in self.state:
                options.append(f"--volume={self.state['volume']}")
            if 'mute' in self.state:
                options.append('--mute=' + ('yes' if self.state['mute'] else 'no'))
            if 'osd_level' in self.state:
                options.append(f"--osd-level={self.state['osd_level']}")
            if self.raw:
                options += ["--demuxer=rawaudio", "--demuxer-rawaudio-format=s16le",
                            "--demuxer-rawaudio-rate=24000", "--demuxer-rawaudio-channels=mono"]
            self.process = subprocess.Popen(
                command[:-1] + options + ["--"] + (["-"] if self.source is not None else []),
                stdin=self.source,
                env={**os.environ, "TTS_MPV_CONTROL_DIR": str(self.directory),
                     "TTS_MPV_STATE": json.dumps(self.state)},
            )
            deadline = time.monotonic() + 5
            while self._message(deadline).get("event") != "ready":
                pass
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def ensure_running(self):
        status = self.process.poll()
        if status is not None:
            if status == 0:
                raise PlaybackStopped()
            raise RuntimeError(f"mpv exited with code {status}")

    def _message(self, deadline=None):
        while True:
            line = self.events.readline()
            if line:
                return json.loads(line)
            self.ensure_running()
            if deadline is not None and time.monotonic() >= deadline:
                raise RuntimeError("mpv did not respond at startup")
            time.sleep(0.02)

    def _send(self, command):
        with self.commands_path.open("a", encoding="utf-8") as commands:
            commands.write(json.dumps(command) + "\n")

    def feed(self, pcm):
        self.ensure_running()
        try:
            self.process.stdin.write(pcm)
            self.process.stdin.flush()
        except BrokenPipeError:
            raise PlaybackStopped()

    def finish(self):
        try:
            self.process.stdin.close()
        except BrokenPipeError:
            raise PlaybackStopped()

    def wait(self):
        while True:
            message = self._message()
            if message.get("event") == "shutdown":
                raise PlaybackStopped()
            if message.get("event") == "end-file":
                reason = message.get("reason")
                if reason == "eof":
                    return
                if reason in ("quit", "stop"):
                    raise PlaybackStopped()
                raise RuntimeError(f"mpv: cannot read audio ({message.get('file_error', reason)})")

    def __exit__(self, exc_type, exc, traceback):
        if self.process is not None and self.process.poll() is None:
            self._send({"command": ["quit"]})
        if self.process is not None:
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.terminate()
                try:
                    self.process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait()
        if self.events is not None:
            self.events.close()
        if self.process is not None and self.process.stdin is not None:
            try:
                self.process.stdin.close()
            except BrokenPipeError:
                pass


