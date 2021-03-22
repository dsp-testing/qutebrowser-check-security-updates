# vim: ft=python fileencoding=utf-8 sts=4 sw=4 et:

# Copyright 2015-2021 Florian Bruhin (The Compiler) <mail@qutebrowser.org>
#
# This file is part of qutebrowser.
#
# qutebrowser is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# qutebrowser is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with qutebrowser.  If not, see <https://www.gnu.org/licenses/>.

"""A QProcess which shows notifications in the GUI."""

import dataclasses
import locale
import shlex
from typing import Mapping, Sequence, Dict, Optional

from PyQt5.QtCore import (pyqtSlot, pyqtSignal, QObject, QProcess,
                          QProcessEnvironment, QByteArray, QUrl)

from qutebrowser.utils import message, log, utils
from qutebrowser.api import cmdutils, apitypes
from qutebrowser.completion.models import miscmodels


all_processes: Dict[int, 'GUIProcess'] = {}
last_pid: Optional[int] = None


@cmdutils.register()
@cmdutils.argument('tab', value=cmdutils.Value.cur_tab)
@cmdutils.argument('pid', completion=miscmodels.process)
@cmdutils.argument('action', choices=['show', 'terminate', 'kill'])
def process(tab: apitypes.Tab, pid: int = None, action: str = 'show') -> None:
    """Manage processes spawned by qutebrowser.

    Args:
        pid: The process ID of the process to manage.
        action: What to do with the given process:

            - show: Show information about the process.
            - terminate: Try to gracefully terminate the process (SIGTERM).
            - kill: Kill the process forcefully (SIGKILL).
    """
    if pid is None:
        if last_pid is None:
            raise cmdutils.CommandError("No process executed yet!")
        pid = last_pid

    try:
        proc = all_processes[pid]
    except KeyError:
        raise cmdutils.CommandError(f"No process found with pid {pid}")

    if action == 'show':
        tab.load_url(QUrl(f'qute://process/{pid}'))
    elif action == 'terminate':
        proc.terminate()
    elif action == 'kill':
        proc.terminate(kill=True)


@dataclasses.dataclass
class ProcessOutcome:

    """The outcome of a finished process."""

    what: str
    running: bool = False
    status: Optional[QProcess.ExitStatus] = None
    code: Optional[int] = None

    def was_successful(self) -> bool:
        return self.status == QProcess.NormalExit and self.code == 0

    def __str__(self) -> str:
        if self.running:
            return f"{self.what.capitalize()} is running."
        elif self.status is None:
            return f"{self.what.capitalize()} did not start."

        assert self.status is not None
        assert self.code is not None

        if self.status == QProcess.CrashExit:
            return f"{self.what.capitalize()} crashed."
        elif self.was_successful():
            return f"{self.what.capitalize()} exited successfully."

        assert self.status == QProcess.NormalExit
        # We call this 'status' here as it makes more sense to the user -
        # it's actually 'code'.
        return f"{self.what.capitalize()} exited with status {self.code}."

    def state_str(self) -> str:
        """Get a short string describing the state of the process.

        This is used in the :process completion.
        """
        if self.running:
            return 'running'
        elif self.status is None:
            return 'not started'
        elif self.status == QProcess.CrashExit:
            return 'crashed'
        elif self.was_successful():
            return 'successful'
        else:
            return 'unsuccessful'


class GUIProcess(QObject):

    """An external process which shows notifications in the GUI.

    Args:
        cmd: The command which was started.
        args: A list of arguments which gets passed.
        verbose: Whether to show more messages.
        running: Whether the underlying process is started.
        what: What kind of thing is spawned (process/editor/userscript/...).
              Used in messages.
        _output_messages: Show output as messages.
        _proc: The underlying QProcess.

    Signals:
        error/finished/started signals proxied from QProcess.
    """

    error = pyqtSignal(QProcess.ProcessError)
    finished = pyqtSignal(int, QProcess.ExitStatus)
    started = pyqtSignal()

    def __init__(
            self,
            what: str,
            *,
            verbose: bool = False,
            additional_env: Mapping[str, str] = None,
            output_messages: bool = False,
            parent: QObject = None,
    ):
        super().__init__(parent)
        self.what = what
        self.verbose = verbose
        self._output_messages = output_messages
        self.outcome = ProcessOutcome(what=what)
        self.cmd = None
        self.args = None
        self.pid = None

        self.stdout: str = ""
        self.stderr: str = ""

        self._proc = QProcess(self)
        self._proc.setReadChannel(QProcess.StandardOutput)
        self._proc.errorOccurred.connect(self._on_error)
        self._proc.errorOccurred.connect(self.error)
        self._proc.finished.connect(self._on_finished)
        self._proc.finished.connect(self.finished)
        self._proc.started.connect(self._on_started)
        self._proc.started.connect(self.started)
        self._proc.readyRead.connect(self._on_ready_read)  # type: ignore[attr-defined]

        if additional_env is not None:
            procenv = QProcessEnvironment.systemEnvironment()
            for k, v in additional_env.items():
                procenv.insert(k, v)
            self._proc.setProcessEnvironment(procenv)

    def __str__(self) -> str:
        if self.cmd is None or self.args is None:
            return '<unknown command>'
        return ' '.join(shlex.quote(e) for e in [self.cmd] + list(self.args))

    def _decode_data(self, qba: QByteArray) -> str:
        """Decode data coming from a process."""
        encoding = locale.getpreferredencoding(do_setlocale=False)
        return qba.data().decode(encoding, 'replace')

    @pyqtSlot()
    def _on_ready_read(self) -> None:
        if not self._output_messages:
            return

        while True:
            text = self._decode_data(self._proc.readLine())  # type: ignore[arg-type]
            if not text:
                break

            if '\r' in text:
                # Crude handling of CR for e.g. progress output.
                # Discard everything before the last \r in the new input, then discard
                # everything after the last \n in self.stdout.
                text = text.rsplit('\r', maxsplit=1)[-1]
                self.stdout = self.stdout.rsplit('\n', maxsplit=1)[0] + '\n'

            self.stdout += text

        message.info(self.stdout.strip(), replace=f"stdout-{self.pid}")

    @pyqtSlot(QProcess.ProcessError)
    def _on_error(self, error: QProcess.ProcessError) -> None:
        """Show a message if there was an error while spawning."""
        if error == QProcess.Crashed and not utils.is_windows:
            # Already handled via ExitStatus in _on_finished
            return

        what = f"{self.what} {self.cmd!r}"
        error_descriptions = {
            QProcess.FailedToStart: f"{what.capitalize()} failed to start",
            QProcess.Crashed: f"{what.capitalize()} crashed",
            QProcess.Timedout: f"{what.capitalize()} timed out",
            QProcess.WriteError: f"Write error for {what}",
            QProcess.WriteError: f"Read error for {what}",
        }
        error_string = self._proc.errorString()
        msg = ': '.join([error_descriptions[error], error_string])

        # We can't get some kind of error code from Qt...
        # https://bugreports.qt.io/browse/QTBUG-44769
        # However, it looks like those strings aren't actually translated?
        known_errors = ['No such file or directory', 'Permission denied']
        if (': ' in error_string and  # pragma: no branch
                error_string.split(': ', maxsplit=1)[1] in known_errors):
            msg += f'\n(Hint: Make sure {self.cmd!r} exists and is executable)'

        message.error(msg)

    @pyqtSlot(int, QProcess.ExitStatus)
    def _on_finished(self, code: int, status: QProcess.ExitStatus) -> None:
        """Show a message when the process finished."""
        log.procs.debug("Process finished with code {}, status {}.".format(
            code, status))

        self.outcome.running = False
        self.outcome.code = code
        self.outcome.status = status
        self.stderr += self._decode_data(self._proc.readAllStandardError())
        self.stdout += self._decode_data(self._proc.readAllStandardOutput())

        if self._output_messages:
            if self.stdout:
                message.info(self.stdout.strip(), replace=f"stdout-{self.pid}")
            if self.stderr:
                message.error(self.stderr.strip())

        if not self.outcome.was_successful():
            if self.stdout:
                log.procs.error("Process stdout:\n" + self.stdout.strip())
            if self.stderr:
                log.procs.error("Process stderr:\n" + self.stderr.strip())
            message.error(str(self.outcome) + " See :process for details.")
        elif self.verbose:
            message.info(str(self.outcome))

    @pyqtSlot()
    def _on_started(self) -> None:
        """Called when the process started successfully."""
        log.procs.debug("Process started.")
        assert not self.outcome.running
        self.outcome.running = True

    def _pre_start(self, cmd: str, args: Sequence[str]) -> None:
        """Prepare starting of a QProcess."""
        if self.outcome.running:
            raise ValueError("Trying to start a running QProcess!")
        self.cmd = cmd
        self.args = args
        log.procs.debug(f"Executing: {self}")
        if self.verbose:
            message.info(f'Executing: {self}')

    def start(self, cmd: str, args: Sequence[str]) -> None:
        """Convenience wrapper around QProcess::start."""
        log.procs.debug("Starting process.")
        self._pre_start(cmd, args)
        self._proc.start(cmd, args)
        self._post_start()
        self._proc.closeWriteChannel()

    def start_detached(self, cmd: str, args: Sequence[str]) -> bool:
        """Convenience wrapper around QProcess::startDetached."""
        log.procs.debug("Starting detached.")
        self._pre_start(cmd, args)
        ok, self.pid = self._proc.startDetached(
            cmd, args, None)  # type: ignore[call-arg]

        if not ok:
            message.error("Error while spawning {}".format(self.what))
            return False

        log.procs.debug("Process started.")
        self.outcome.running = True
        self._post_start()
        return True

    def _post_start(self) -> None:
        """Register this process and remember the process ID after starting."""
        self.pid = self._proc.processId()
        all_processes[self.pid] = self  # FIXME cleanup?
        global last_pid
        last_pid = self.pid

    def terminate(self, kill: bool = False) -> None:
        """Terminate or kill the process."""
        if kill:
            self._proc.kill()
        else:
            self._proc.terminate()
