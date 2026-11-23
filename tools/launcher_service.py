"""GPL-3.0-or-later. Local JSON-lines adapter for the Tauri launcher.

Only this process owns the portable lock and mutable launcher state. Long jobs
use the existing Backend; the frontend can navigate without interrupting them.
No TCP server, arbitrary command execution or remote page access is exposed.
"""
from __future__ import annotations
import argparse
import contextlib
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

sys.path.insert(0,str(Path(__file__).resolve().parent))

from portable_launcher import Backend, instance_lock
from portable_support import Layout, write_json
from portable_update import latest_release, stage_release, start_updater
from runtime_paths import repair_portable_saves
from modkit import ModError
from modkit.package import ModStore, runtime_build
from modkit.soundtrack import SoundtrackProject
from launcher_presentation import (KEYBOARD, read_options, save_option, read_launcher_preferences,
    set_accent, read_release_cache, release_list, fetch_release_history, progress_summary, BUILD_STAGES)


class Service:
    def __init__(self, layout, send, launcher_pid=0, testing=False):
        self.layout, self.send, self.launcher_pid = layout, send, launcher_pid
        self.mutex = threading.RLock()
        self.backend = Backend(layout, self.notify)
        self.busy = False
        self.phase = 'idle'
        self.title = 'Ready to play' if layout.state().get('disc') else 'Choose your game disc'
        self.detail = 'Your game, settings and saves stay in this folder.'
        self.percent = None
        self.error = None
        self.update = None
        self.staged = None
        self.recovery = (layout.local/'update-state.json').exists()
        self.releases = release_list(layout, read_release_cache(layout.local))
        self.revision = 0
        self.last_progress = 0
        self.started_at = None
        self.stage = ''
        self.workers = ''
        self.mods = ModStore(layout.local/'mods')
        self.mods_revision = 0
        self.soundtrack = SoundtrackProject(layout.local/'soundtrack', layout.installation)
        self.soundtrack_revision = 0
        self.preview_process = None
        if self.recovery:
            self.title = 'Restore previous version'
            self.detail = 'The previous update did not finish. Your game and saves are kept.'
        result = layout.local/'last-update-result.json'
        if result.is_file():
            try:
                value = json.loads(result.read_text())
                if not value.get('success'): self.error = value.get('error', 'Previous version restored')
                result.unlink()
            except (OSError, ValueError): pass
        if not testing:
            threading.Thread(target=self.history, daemon=True).start()

    def snapshot(self):
        with self.mutex:
            state = self.layout.state()
            return dict(revision=self.revision, version=self.layout.version,
                platform=self.layout.display_name, busy=self.busy, phase=self.phase,
                title=self.title, detail=self.detail, percent=self.percent, error=self.error,
                elapsed=max(0,time.monotonic()-self.started_at) if self.started_at is not None else 0,
                workers=self.workers,
                recovery=self.recovery, disc=state.get('disc'), prepared=bool(state.get('prepared_version')),
                imported=(self.layout.installation/'installation.json').is_file(),
                extracted=(self.layout.local/'game').is_dir(), options=read_options(self.layout.local),
                accent=read_launcher_preferences(self.layout.local)['accent'], keyboard=KEYBOARD,
                mods_revision=self.mods_revision, soundtrack_revision=self.soundtrack_revision,
                releases=self.releases, update=({'version':self.update['version']} if self.update else None))

    def publish(self):
        with self.mutex:
            self.revision += 1
            self.send(dict(event='state', payload=self.snapshot()))

    def history(self):
        try:
            releases = fetch_release_history(self.layout.local)
            with self.mutex:
                self.releases = release_list(self.layout, releases)
                self.publish()
        except (OSError, ValueError): pass  # Bundled/cached notes work offline.

    def notify(self, kind, text):
        with self.mutex:
            if kind == 'progress':
                if text.startswith('Build workers: '):
                    self.workers=text[len('Build workers: '):];self.publish();return
                if text.startswith('Stage: '):self.stage=text[7:].split(' | ',1)[0]
                summary = progress_summary(text,self.stage)
                if not summary: return
                previous=self.phase,self.title
                self.title, self.detail, self.percent = summary
                if self.title == 'Game running': self.phase = 'running'
                elif self.title == 'Starting game': self.phase = 'starting'
                elif self.title in BUILD_STAGES-{'Verifying your disc'} or self.title in ('Compiling','Setting up','Downloading tools','Checking SDL',
                        'Checking framework','Downloading SDL','Downloading framework',
                        'Downloading setup components'): self.phase = 'setup'
                if self.title in ('Building tools','Building game'):self.phase='build'
                # Throttle repeated percentages, never lifecycle/stage changes.
                # A reused build can verify and launch inside the same 80 ms.
                if previous==(self.phase,self.title) and time.monotonic()-self.last_progress < .08: return
                self.last_progress = time.monotonic()
            elif kind in ('done','error'):
                cancelled = self.backend.cancel.is_set()
                self.busy = False
                self.started_at = None
                self.phase = 'idle'
                self.percent = None
                self.title = text if kind == 'done' else 'Setup stopped' if cancelled else 'Something went wrong'
                self.detail = '' if kind == 'done' else text
                self.error = text if kind == 'error' and not cancelled else None
            self.publish()

    def idle(self):
        if self.busy: raise ValueError('Wait for the current task or close the game first.')
        if self.recovery: raise ValueError('Restore the previous launcher version first.')

    def begin(self, title, job, phase='setup'):
        with self.mutex:
            self.idle()
            self.busy, self.phase, self.title = True, phase, title
            self.percent, self.error = None, None
            self.detail, self.stage, self.workers = '', '', ''
            self.started_at = time.monotonic()
            self.backend.cancel.clear()
            self.publish()
        def work():
            try: job()
            except Exception as e: self.notify('error',str(e))
        threading.Thread(target=work, daemon=False).start()

    def open_path(self, path):
        if os.name == 'nt': os.startfile(str(path))
        else: subprocess.Popen(['xdg-open',str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def mod_listing(self):
        # A running or reused game build is checked; one being rebuilt is not.
        build=None if self.busy and self.phase in ('setup','build') else runtime_build(self.layout.work)
        try: packages,problem=self.mods.status(build),None
        except ModError as e: packages,problem=self.mods.status(None),str(e)
        return dict(packages=packages,checked=bool(build) and problem is None,problem=problem,
                    store=str(self.mods.root),revision=self.mods_revision)

    def mod_change(self, change):
        with self.mutex:
            self.idle();result=change(runtime_build(self.layout.work));self.mods_revision+=1;self.publish()
        return result

    def soundtrack_change(self, change):
        with self.mutex:
            self.idle();result=change();self.soundtrack_revision+=1;self.publish()
        return result

    def stop_preview(self):
        if os.name == 'nt':
            import winsound
            winsound.PlaySound(None, 0)
        elif self.preview_process and self.preview_process.poll() is None:
            self.preview_process.terminate()
        self.preview_process = None

    def play_preview(self, path):
        """Play a preview WAVE through the operating system; no game launch."""
        self.stop_preview()
        if os.name == 'nt':
            import winsound
            winsound.PlaySound(str(path), winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT)
            return
        import shutil
        player = next((shutil.which(n) for n in ('paplay', 'pw-play', 'aplay') if shutil.which(n)), None)
        if not player: raise ValueError('Install PulseAudio, PipeWire or ALSA tools to preview audio.')
        self.preview_process = subprocess.Popen([player, str(path)], stdin=subprocess.DEVNULL,
                                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def soundtrack_slot(self, args):
        slot = args.get('slot')
        if type(slot) is not int: raise ValueError('Choose a soundtrack slot')
        return slot

    def handle(self, command, args):
        args = args or {}
        if not isinstance(args,dict): raise ValueError('Invalid launcher arguments')
        if command == 'state': return self.snapshot()
        if command == 'play':
            if not self.layout.state().get('disc'): raise ValueError('Choose your supported USA CUE sheet first.')
            self.begin('Starting game…', self.backend.action,phase='starting')
        elif command == 'choose':
            selected = args.get('path')
            if not isinstance(selected,str) or not selected: raise ValueError('No disc selected')
            self.begin('Verifying your disc…', lambda:self.backend.action(Path(selected),play=False,import_only=True))
        elif command == 'assets':
            with self.mutex: self.idle()
            path=self.layout.local/'game'
            if path.is_dir(): self.open_path(path)
            else:
                if not (self.layout.installation/'installation.json').is_file(): raise ValueError('Choose and verify your disc first.')
                self.begin('Extracting asset folders…',lambda:self.backend.action(assets=True))
        elif command == 'mods': return self.mod_listing()
        elif command == 'mod_install':
            selected=args.get('path')
            if not isinstance(selected,str) or not selected.lower().endswith('.psxmod'): raise ValueError('Choose a .psxmod package')
            def install():
                record=self.mods.install(Path(selected),runtime_build(self.layout.work))
                with self.mutex: self.mods_revision+=1
                self.notify('done',f"Installed {record['name']} {record['version']}. Enable it in Mods.")
            self.begin('Installing mod…',install,phase='mods')
        elif command == 'mod_enable':
            package,enabled=args.get('id'),args.get('enabled')
            if not isinstance(package,str) or type(enabled) is not bool: raise ValueError('Invalid mod selection')
            self.mod_change(lambda build:self.mods.set_enabled(package,enabled,build))
        elif command == 'mod_remove':
            package=args.get('id')
            if not isinstance(package,str): raise ValueError('Invalid mod package')
            self.mod_change(lambda build:self.mods.remove(package))
        elif command == 'soundtrack':
            result = self.soundtrack.status()
            result['revision'] = self.soundtrack_revision
            result['exports'] = sorted(p.name for p in self.soundtrack.exports.glob('*.psxmod')) if self.soundtrack.exports.is_dir() else []
            return result
        elif command == 'soundtrack_import':
            slot, selected = self.soundtrack_slot(args), args.get('path')
            if not isinstance(selected, str) or not selected.lower().endswith('.wav'): raise ValueError('Choose a .wav file')
            self.soundtrack_change(lambda: self.soundtrack.import_wave(slot, Path(selected)))
        elif command == 'soundtrack_restore':
            slot = self.soundtrack_slot(args)
            self.soundtrack_change(lambda: self.soundtrack.restore(slot))
        elif command == 'soundtrack_move':
            slot, offset = self.soundtrack_slot(args), args.get('offset')
            if offset not in (-1, 1) or type(offset) is not int: raise ValueError('Invalid move')
            self.soundtrack_change(lambda: self.soundtrack.move(slot, offset))
        elif command == 'soundtrack_reset':
            self.soundtrack_change(self.soundtrack.reset)
        elif command == 'soundtrack_details':
            details = args.get('details')
            if not isinstance(details, dict): raise ValueError('Invalid package details')
            self.soundtrack_change(lambda: self.soundtrack.set_details(details))
        elif command == 'soundtrack_preview':
            slot = args.get('slot')
            if slot is not None and type(slot) is not int: raise ValueError('Choose a soundtrack slot')
            self.play_preview(self.soundtrack.preview(slot))
        elif command == 'soundtrack_stop':
            self.stop_preview()
        elif command == 'soundtrack_export':
            install = args.get('install') is True
            def export():
                build = runtime_build(self.layout.work)
                result = self.soundtrack.export(build)
                message = f"Exported {Path(result['path']).name}."
                if install:
                    record = self.mods.install(Path(result['path']), build)
                    with self.mutex: self.mods_revision += 1
                    message = f"Exported and installed {record['name']} {record['version']}. Enable it in Mods."
                with self.mutex: self.soundtrack_revision += 1
                self.notify('done', message)
            self.begin('Exporting soundtrack…', export, phase='mods')
        elif command == 'stop':
            with self.mutex:
                if not self.busy or self.phase == 'running': raise ValueError('Close the game window to return here.')
                self.title='Stopping…';self.publish()
            threading.Thread(target=self.backend.stop,daemon=True).start()
        elif command == 'save_option':
            with self.mutex:
                self.idle();save_option(self.layout.local,args.get('key'),args.get('value'));self.publish()
        elif command == 'accent':
            with self.mutex:
                self.idle();set_accent(self.layout.local,args.get('value'));self.publish()
        elif command == 'check_update':
            def check():
                release=latest_release(self.layout.version,self.layout.asset_name)
                if self.backend.cancel.is_set(): raise ValueError('Update check stopped.')
                with self.mutex: self.update=release
                self.notify('done','Update available' if release else 'You have the latest release.')
            self.begin('Checking for updates…',check,phase='update')
        elif command == 'install_update':
            if not self.update: raise ValueError('Check for an available update first.')
            def stage():
                staged=stage_release(self.layout,self.update,self.backend)
                if self.backend.cancel.is_set(): raise ValueError('Update stopped. Downloaded files were kept.')
                with self.mutex:
                    self.staged=staged;self.phase='restart';self.title='Restarting launcher…';self.publish()
                # The native host asks finalize_update, then closes both old processes.
                self.send(dict(event='update-ready',payload={}))
            self.begin('Downloading update…',stage,phase='update')
        elif command == 'finalize_update':
            with self.mutex:
                if self.phase!='restart' or not self.staged: raise ValueError('No staged update')
                start_updater(self.layout,self.staged,self.launcher_pid)
        elif command == 'restore':
            with self.mutex:
                if self.busy or not self.recovery: raise ValueError('No interrupted update to restore')
                start_updater(self.layout,None,self.launcher_pid)
        elif command == 'open':
            target=args.get('target')
            if target == 'soundtrack-exports':
                path=self.soundtrack.exports;path.mkdir(parents=True,exist_ok=True)
            elif target in ('saves','game','mods'):
                path=self.layout.local/target;path.mkdir(parents=True,exist_ok=True)
            elif target in ('setup.log','update.log'):
                path=self.layout.local/target
                if not path.is_file(): raise ValueError('No log yet. Run setup or an update first.')
            elif target == 'keybinds.ini':
                try:
                    record=json.loads((self.layout.work/'runtime-build.json').read_text())
                    path=Path(record['executable']).resolve().parent/target
                    if not path.is_relative_to(self.layout.work.resolve()) or not path.is_file(): raise ValueError()
                except (OSError,ValueError,KeyError): raise ValueError('Launch the game once to create its keyboard bindings file.')
            elif target in ('project','releases','recompia'):
                import webbrowser
                webbrowser.open({'project':'https://github.com/TheEelYT/Nitrogen-Squared',
                    'releases':'https://github.com/TheEelYT/Nitrogen-Squared/releases',
                    'recompia':'https://github.com/Dittobyte/Recompia'}[target]);return None
            else: raise ValueError('Unsupported launcher destination')
            self.open_path(path)
        elif command == 'close':
            if self.busy: raise ValueError('Stop the current task, or close the game, before closing the launcher.')
        else: raise ValueError('Unsupported launcher command')
        return self.snapshot()


def serve(layout, launcher_pid=0, testing=False, report=None):
    lock=instance_lock(layout)
    repair_portable_saves(layout)
    write_lock=threading.Lock()
    output=sys.stdout
    def send(value):
        with write_lock:
            output.write(json.dumps(value,ensure_ascii=True)+'\n');output.flush()
    # Tool output must never corrupt the protocol. Backend progress uses notify.
    with contextlib.redirect_stdout(sys.stderr):
        service=Service(layout,send,launcher_pid,testing)
        for line in sys.stdin:
            request={}
            try:
                if len(line)>1024*1024: raise ValueError('Launcher request too large')
                request=json.loads(line)
                if not isinstance(request,dict) or type(request.get('id')) is not int: raise ValueError('Invalid launcher request')
                command=request.get('command');args=request.get('args') or {}
                if command=='self_test':
                    if not testing or report is None: raise ValueError('Self-test mode required')
                    ui=args.get('ui')
                    if not isinstance(ui,dict) or not all(ui.get(k) for k in ('artwork','layout','navigation','progress')):
                        raise ValueError('Launcher frontend smoke check failed')
                    report.resolve().relative_to(layout.local.resolve())
                    write_json(report,dict(version=layout.version,python=sys.executable,storage=str(layout.local),
                        frontend='tauri',asset_tools=True,first_run=not bool(layout.state().get('disc')),launcher_ui=ui))
                    result=True
                else: result=service.handle(command,args)
                send(dict(id=request['id'],result=result))
                if command in ('close','finalize_update','restore','self_test'): break
            except Exception as e:
                send(dict(id=request.get('id'),error=str(e)))
    if service.busy and service.phase not in ('restart','running'): service.backend.stop()
    lock.close()


def main():
    for stream in (sys.stdout,sys.stderr):
        if stream and hasattr(stream,'reconfigure'): stream.reconfigure(encoding='utf-8',errors='replace')
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root',type=Path,required=True);p.add_argument('--launcher-pid',type=int,default=0)
    p.add_argument('--self-test',action='store_true');p.add_argument('--report',type=Path)
    args=p.parse_args()
    if os.name=='nt': layout=Layout(args.root)
    else:
        from linux_support import LinuxLayout
        layout=LinuxLayout(args.root)
    serve(layout,args.launcher_pid,args.self_test,args.report)

if __name__=='__main__': main()
