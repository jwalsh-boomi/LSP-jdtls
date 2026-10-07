from __future__ import annotations

import datetime
import json
import os
import re
import shutil
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Set
from urllib.parse import urlparse

import sublime
import sublime_plugin
from LSP.plugin import (
    AbstractPlugin,
    Request,
    WorkspaceFolder,
    register_plugin,
    text_document_identifier,
    unregister_plugin,
)
from LSP.protocol import TextDocumentIdentifier
from typing_extensions import override

from . import installer
from .constants import (
    DATA_DIR,
    JDTLS_CONFIG_TO_SUBLIME_SETTING,
    SESSION_NAME,
    SETTING_JAVA_HOME,
    SETTING_JAVA_HOME_DEPRECATED,
    SETTING_LOMBOK_ENABLED,
    SETTING_PROGRESS_REPORT_ENABLED,
    VSCODE_PLUGINS,
)
from .protocol_extensions_handler import (
    language_actionableNotification,
    language_progressReport,
    language_status,
)
from .utils import (
    add_notification_handler,
    add_request_handler,
    get_settings,
    view_for_uri_async,
)
from .workspace_execute_client_command_handler import workspace_executeClientCommand
from .workspace_execute_command_handler import handle_client_command

if TYPE_CHECKING:
    from LSP.plugin import ClientConfig
    from LSP.protocol import ConfigurationItem, DocumentUri, ExecuteCommandParams


_launched_data_paths: dict[int, str] = {}
"""Data directory each window's server was launched with, keyed by window id."""


def _set_data_argument(command: List[str], data_path: str) -> None:
    """
    Points the server's -data argument at data_path.

    The argument is appended when the configured command does not carry one, so
    that a customized command cannot silently fall back to a data directory
    shared with the other open windows.
    """
    try:
        index = command.index("-data")
    except ValueError:
        command.extend(["-data", data_path])
        return
    if index + 1 < len(command):
        command[index + 1] = data_path
    else:
        command.append(data_path)


@add_request_handler("workspace/executeClientCommand", workspace_executeClientCommand)
@add_notification_handler("language/status", language_status)
@add_notification_handler("language/progressReport", language_progressReport)
@add_notification_handler(
    "language/actionableNotification", language_actionableNotification
)
class EclipseJavaDevelopmentTools(AbstractPlugin):
    @classmethod
    @override
    def name(cls) -> str:
        return SESSION_NAME

    # Installation and Updates
    ##########################

    @classmethod
    @override
    def needs_update_or_installation(cls) -> bool:
        return installer.needs_update_or_installation()

    @classmethod
    @override
    def install_or_update(cls) -> None:
        installer.install_or_update()

    # Server configuration
    ######################

    @classmethod
    @override
    def additional_variables(cls) -> dict[str, str] | None:
        settings = get_settings()

        java_home = settings.get("settings").get(SETTING_JAVA_HOME)
        if not java_home:
            java_home = settings.get("settings").get(SETTING_JAVA_HOME_DEPRECATED)
        if not java_home:
            java_home = os.environ.get("JAVA_HOME")

        if java_home:
            java_executable = os.path.join(java_home, "bin", "java")
        else:
            java_executable = "java"

        launcher_version = ""
        for file in os.listdir(os.path.join(installer.jdtls_path(), "plugins")):
            match = re.search("org.eclipse.equinox.launcher_(.*).jar", file)
            if match:
                launcher_version = match.group(1)

        def _jdtls_platform() -> str:
            p = sublime.platform()
            if p == "windows":
                return "win"
            elif p == "osx":
                return "mac"
            elif p == "linux":
                return "linux"
            else:
                raise ValueError(f"unknown platform: {p}")

        return {
            "java_executable": java_executable,
            "watch_parent_process": "false"
            if sublime.platform() == "windows"
            else "true",
            "jdtls_platform": _jdtls_platform(),
            "serverdir": installer.jdtls_path(),
            "datadir": installer.jdtls_data_path(),
            "launcher_version": launcher_version,
        }

    @classmethod
    def _enable_lombok(cls, configuration: ClientConfig):
        """
        Edits the command to enable/disable lombok.
        """
        javaagent_arg = "-javaagent:" + installer.lombok_jar_path()

        # Prevent adding the argument multiple times
        if (
            configuration.settings.get(SETTING_LOMBOK_ENABLED)
            and javaagent_arg not in configuration.command
        ):
            jar_index = configuration.command.index("-jar")
            configuration.command.insert(jar_index, javaagent_arg)
        elif (
            not configuration.settings.get(SETTING_LOMBOK_ENABLED)
            and javaagent_arg in configuration.command
        ):
            configuration.command.remove(javaagent_arg)

    @classmethod
    def _insert_bundles(cls, configuration: ClientConfig):
        bundles = configuration.init_options.get("bundles") or []
        # Skip jars that jdtls already ships (e.g. ASM) to avoid duplicate bundle errors
        server_jars = set(os.listdir(os.path.join(installer.jdtls_path(), "plugins")))
        for plugin in VSCODE_PLUGINS:
            ext_path = installer.vscode_plugin_extension_path(plugin)
            with open(os.path.join(ext_path, "package.json"), "r") as package_json:
                jars = (
                    json.load(package_json)
                    .get("contributes", {})
                    .get("javaExtensions", [])
                )
                for jar in jars:
                    if os.path.basename(jar) in server_jars:
                        continue
                    abspath = os.path.abspath(
                        os.path.normpath(os.path.join(ext_path, jar))
                    )
                    if abspath not in bundles:
                        bundles.append(abspath)
        configuration.init_options.set("bundles", bundles)

    @classmethod
    @override
    def on_pre_start(
        cls,
        window: sublime.Window,
        initiating_view: sublime.View,
        workspace_folders: list[WorkspaceFolder],
        configuration: ClientConfig,
    ) -> str | None:
        cls._enable_lombok(configuration)
        cls._insert_bundles(configuration)

        # Use per-project data directory to prevent workspace locking conflicts
        # when multiple Sublime Text windows run jdtls simultaneously.
        data_path = installer.jdtls_data_path_for_folders(
            [wf.path for wf in workspace_folders]
        )
        _set_data_argument(configuration.command, data_path)
        _launched_data_paths[window.id()] = data_path

        configuration.init_options.set(
            "workspaceFolders", [x.uri() for x in workspace_folders]
        )
        configuration.init_options.set("settings", configuration.settings.copy())
        configuration.init_options.set(
            "extendedClientCapabilities",
            {
                "progressReportProvider": configuration.settings.get(
                    SETTING_PROGRESS_REPORT_ENABLED
                ),
                "classFileContentsSupport": True,
                "overrideMethodsPromptSupport": False,
                "hashCodeEqualsPromptSupport": False,
                "advancedOrganizeImportsSupport": False,
                "generateToStringPromptSupport": False,
                "advancedGenerateAccessorsSupport": False,
                "generateConstructorsPromptSupport": False,
                "generateDelegateMethodsPromptSupport": False,
                "advancedExtractRefactoringSupport": False,
                "inferSelectionSupport": [],
                "moveRefactoringSupport": False,
                "clientHoverProvider": False,
                "clientDocumentSymbolProvider": False,
                "gradleChecksumWrapperPromptSupport": False,
                "resolveAdditionalTextEditsSupport": False,
                "advancedIntroduceParameterRefactoringSupport": False,
                "actionableRuntimeNotificationSupport": True,
                "shouldLanguageServerExitOnShutdown": True,
                "onCompletionItemSelectedCommand": "editor.action.triggerParameterHints",
            },
        )

        # configuration.init_options.set("triggerFiles", configuration.settings)

        return None

    @override
    def on_open_uri_async(
        self, uri: DocumentUri, callback: Callable[[str, str, str], None]
    ) -> bool:
        if not uri.startswith("jdt:"):
            return False
        session = self.weaksession()
        if not session:
            return False
        # https://github.com/redhat-developer/vscode-java/blob/9f32875a67352487f5c414bb7fef04c9b00af89d/src/protocol.ts#L105-L107
        # https://github.com/redhat-developer/vscode-java/blob/9f32875a67352487f5c414bb7fef04c9b00af89d/src/providerDispatcher.ts#L61-L76
        # https://github.com/redhat-developer/vscode-java/blob/9f32875a67352487f5c414bb7fef04c9b00af89d/src/providerDispatcher.ts#L27-L28
        session.send_request_async(
            Request[TextDocumentIdentifier, str](
                "java/classFileContents", text_document_identifier(uri), progress=True
            ),
            lambda resp: callback(urlparse(uri).path, resp, "Packages/Java/Java.sublime-syntax"),
            lambda err: callback("ERROR", str(err), "Packages/Text/Plain text.tmLanguage"),
        )
        return True

    # Custom command handling
    #########################

    @override
    def on_pre_server_command(
        self, command: ExecuteCommandParams, done_callback: Callable[[], None]
    ) -> bool:
        session = self.weaksession()
        if not session:
            return False
        return handle_client_command(
            session,
            done_callback,
            command["command"],
            command["arguments"] if "arguments" in command else [],
        )

    @override
    def on_workspace_configuration(self, params: ConfigurationItem, configuration: Any) -> Any:
        if (
            "section" in params
            and "scopeUri" in params
            and params["section"] in JDTLS_CONFIG_TO_SUBLIME_SETTING
        ):
            session = self.weaksession()
            if session:
                view = view_for_uri_async(session, params["scopeUri"])
                if view:
                    return view.settings().get(
                        JDTLS_CONFIG_TO_SUBLIME_SETTING[params["section"]], None
                    )
        return configuration


def _active_data_paths() -> Set[str]:
    """
    Data directories that an open window is using or would use.

    Both sources are needed: the recorded launch path covers a session whose
    project folders changed after the server started, and the folder derived
    path covers a window whose server has not started yet.
    """
    open_ids = {window.id() for window in sublime.windows()}
    for window_id in list(_launched_data_paths):
        if window_id not in open_ids:
            del _launched_data_paths[window_id]
    paths = set(_launched_data_paths.values())
    for window in sublime.windows():
        paths.add(installer.jdtls_data_path_for_folders(window.folders()))
    return paths


def _dir_size_mb(path: str) -> float:
    """Size of the directory tree, skipping files that vanish while walking it."""
    total = 0
    for dirpath, _, filenames in os.walk(path):
        for filename in filenames:
            try:
                total += os.path.getsize(os.path.join(dirpath, filename))
            except OSError:
                pass
    return total / (1024 * 1024)


def _get_data_dirs() -> List[Dict[str, Any]]:
    """List all jdtls data directories with metadata."""
    base = installer.storage_subpath()
    dirs = []
    if not os.path.isdir(base):
        return dirs
    active_paths = _active_data_paths()
    for name in os.listdir(base):
        if name != DATA_DIR and not name.startswith(DATA_DIR + "_"):
            continue
        path = os.path.join(base, name)
        if not os.path.isdir(path):
            continue
        dirs.append({
            "name": name,
            "path": path,
            "size_mb": _dir_size_mb(path),
            "mtime": os.path.getmtime(path),
            "active": path in active_paths,
        })
    dirs.sort(key=lambda d: d["mtime"], reverse=True)
    return dirs


class LspJdtlsCleanDataDirectoriesCommand(sublime_plugin.WindowCommand):
    def run(self) -> None:
        self._dirs = _get_data_dirs()
        if not self._dirs:
            sublime.message_dialog("No per-project jdtls data directories found.")
            return

        items = []
        for d in self._dirs:
            modified = datetime.datetime.fromtimestamp(d["mtime"]).strftime("%Y-%m-%d")
            status = "IN USE" if d["active"] else "stale"
            items.append("{} — {:.0f} MB — {} — {}".format(d["name"], d["size_mb"], modified, status))

        items.insert(0, "Delete all stale directories")
        self.window.show_quick_panel(items, self._on_select)

    def _on_select(self, index: int) -> None:
        if index == -1:
            return
        if index == 0:
            stale = [d for d in self._dirs if not d["active"]]
            if not stale:
                sublime.message_dialog("No stale directories to delete.")
                return
            total_mb = sum(d["size_mb"] for d in stale)
            if sublime.ok_cancel_dialog(
                "Delete {} stale director{} ({:.0f} MB)?".format(
                    len(stale), "y" if len(stale) == 1 else "ies", total_mb
                )
            ):
                for d in stale:
                    shutil.rmtree(d["path"], ignore_errors=True)
                sublime.status_message("LSP-jdtls: Deleted {} stale data director{}.".format(
                    len(stale), "y" if len(stale) == 1 else "ies"
                ))
        else:
            d = self._dirs[index - 1]
            if d["active"]:
                sublime.message_dialog(
                    "Cannot delete — this data directory is in use by an open window.\n\n"
                    "To clear the workspace of the project in this window, run "
                    "\"LSP-jdtls: Clear data\"."
                )
                return
            if sublime.ok_cancel_dialog("Delete {} ({:.0f} MB)?".format(d["name"], d["size_mb"])):
                shutil.rmtree(d["path"], ignore_errors=True)
                sublime.status_message("LSP-jdtls: Deleted {}.".format(d["name"]))


def plugin_loaded() -> None:
    register_plugin(EclipseJavaDevelopmentTools)


def plugin_unloaded() -> None:
    unregister_plugin(EclipseJavaDevelopmentTools)
