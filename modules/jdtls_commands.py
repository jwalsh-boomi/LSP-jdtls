from __future__ import annotations

import os
import shutil
from typing import TYPE_CHECKING

import sublime
import sublime_plugin
from LSP.plugin import Request, Session

from . import installer
from .constants import SESSION_NAME
from .utils import LspJdtlsTextCommand

if TYPE_CHECKING:
    from LSP.plugin.core.protocol import ResponseError


class LspJdtlsBuildWorkspace(LspJdtlsTextCommand):
    def run_jdtls_command(self, edit: sublime.Edit, session: Session) -> None:
        params = True
        session.send_request(
            Request[bool, int]("java/buildWorkspace", params),
            self.on_response_async,
            self.on_error_async,
        )

    def on_response_async(self, response: int) -> None:
        window = self.view.window()
        if window is None:
            return
        if response == 0:
            window.status_message("LSP-jdtls: Build failed")
        elif response == 1:
            window.status_message("LSP-jdtls: Build succeeded")
        elif response == 2:
            window.status_message("LSP-jdtls: Build ended with error")
        elif response == 3:
            window.status_message("LSP-jdtls: Build cancelled")

    def on_error_async(self, error: ResponseError) -> None:
        pass


class JdtlsClearData(sublime_plugin.TextCommand):
    """Deletes the Eclipse workspace of this window's project and restarts the server."""

    def run(self, edit: sublime.Edit) -> None:
        window = self.view.window()
        if not window:
            return
        data_path = installer.jdtls_data_path_for_folders(window.folders())
        if not os.path.exists(data_path):
            sublime.message_dialog("LSP-jdtls: nothing to clear at " + data_path)
            return
        if not sublime.ok_cancel_dialog("Are you sure you want to clear " + data_path):
            return
        try:
            shutil.rmtree(data_path)
        except OSError as error:
            sublime.error_message(
                "LSP-jdtls: could not clear {}\n\n{}".format(data_path, error)
            )
            return
        self.view.run_command("lsp_restart_server", {"config_name": SESSION_NAME})
