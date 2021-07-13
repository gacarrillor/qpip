import os
import subprocess
import sys
from collections import namedtuple
from importlib import metadata
from subprocess import PIPE, STDOUT, Popen

import pkg_resources
from pkg_resources import DistributionNotFound, VersionConflict
from PyQt5 import uic
from qgis import utils
from qgis.core import Qgis, QgsApplication, QgsMessageLog, QgsSettings
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import (
    QAction,
    QDialog,
    QMessageBox,
    QProgressDialog,
    QTableWidgetItem,
)

MissingDep = namedtuple("MissingDep", ["package", "requirement", "state"])


def log(message):
    QgsMessageLog.logMessage(message, "QPIP", level=Qgis.MessageLevel.Info)


def warn(message):
    QgsMessageLog.logMessage(message, "QPIP", level=Qgis.MessageLevel.Warning)


class Plugin:
    """QGIS Plugin Implementation."""

    def __init__(self, iface):
        self.iface = iface
        self.plugin_dir = os.path.dirname(__file__)
        self._defered_packages = []
        self._init_complete = False
        self.settings = QgsSettings()
        self.settings.beginGroup("QPIP")

        self.plugins_path = os.path.join(
            QgsApplication.qgisSettingsDirPath(), "python", "plugins"
        )
        self.prefix_path = os.path.join(
            QgsApplication.qgisSettingsDirPath().replace("/", os.path.sep),
            "python",
            "dependencies",
        )
        self.site_packages_path = os.path.join(self.prefix_path, "Lib", "site-packages")
        self.bin_path = os.path.join(self.prefix_path, "Scripts")

        if self.site_packages_path not in sys.path:
            log(f"Adding {self.site_packages_path} to PYTHONPATH")
            sys.path.insert(0, self.site_packages_path)
            os.environ["PYTHONPATH"] = (
                self.site_packages_path + ";" + os.environ.get("PYTHONPATH", "")
            )

        if self.bin_path not in os.environ["PATH"]:
            log(f"Adding {self.bin_path} to PATH")
            os.environ["PATH"] = self.bin_path + ";" + os.environ["PATH"]

        sys.path_importer_cache.clear()

        # Monkey patch qgis.utils
        log("Applying monkey patch to qgis.utils")
        self._original_loadPlugin = utils.loadPlugin
        utils.loadPlugin = self.patched_load_plugin

        self.iface.initializationCompleted.connect(self.initComplete)

    def initGui(self):
        self.show_action = QAction(
            QIcon(os.path.join(self.plugin_dir, "icon.svg")), "Show installed"
        )
        self.show_action.triggered.connect(self.show)
        self.iface.addPluginToMenu("Python dependencies (QPIP)", self.show_action)

        self.skip_action = QAction(
            QIcon(os.path.join(self.plugin_dir, "icon.svg")), "Show skips"
        )
        self.skip_action.triggered.connect(self.skip)
        self.iface.addPluginToMenu("Python dependencies (QPIP)", self.skip_action)

    def initComplete(self):
        self._init_complete = True
        if self._defered_packages:
            log(f"Initialization complete. Loading deferred packages")
            self.install_deps_and_start(self._defered_packages)
        self._defered_packages = []

    def unload(self):
        self.iface.removePluginMenu("Python dependencies (QPIP)", self.show_action)

        # Remove monkey patch
        log("Unapplying monkey patch to qgis.utils")
        utils.loadPlugin = self._original_loadPlugin

        # Remove path alterations
        if self.site_packages_path in sys.path:
            sys.path.remove(self.site_packages_path)
            os.environ["PYTHONPATH"] = os.environ["PYTHONPATH"].replace(
                self.bin_path + ";", ""
            )
            os.environ["PATH"] = os.environ["PATH"].replace(self.bin_path + ";", "")

    def patched_load_plugin(self, packageName):
        """
        This replaces qgis.utils.loadPlugin
        """
        missing_deps = self.list_missing_deps(packageName)
        if not missing_deps:
            # We simply load the plugin right away
            return self._original_loadPlugin(packageName)
        else:
            # We miss some dependencies
            log(f"{packageName} has missing dependencies.")
            if not self._init_complete:
                # If gui not initialized yet, we defer loading
                log(f"Initialisation not ready. Deferring loading of {packageName}.")
                self._defered_packages.append(packageName)
                return False
            else:
                # Otherwise (probably a plugin that was just installed), we install deps and load right away
                self.install_deps_and_start([packageName])
                return True

    def install_deps_and_start(self, packageNames):
        """
        This collects all missing deps for given packages, then shows a GUI to install them,
        and the loads and starts all packages. It tries to match implementation of
        QgsPluginRegistry::loadPythonPlugin (including watchdog).
        """

        assert self._init_complete

        log(f"Installing deps for {packageNames} before starting them.")

        missing_deps = []
        for packageName in packageNames:
            missing_deps.extend(self.list_missing_deps(packageName))

        deps_to_install = []
        if len(missing_deps):
            log(f"{len(missing_deps)} missing dependencies.")

            dialog = InstallMissingDialog(missing_deps)
            if dialog.exec_():
                deps_to_install = dialog.deps_to_install()
                deps_to_dontask = dialog.deps_to_dontask()

                for dep in deps_to_dontask:
                    self.settings.setValue(
                        f"skips/{dep.package}/{dep.requirement}", True
                    )

        if deps_to_install:
            log(f"Will install selected dependencies : {deps_to_install}")
            self.install_deps(deps_to_install)

            sys.path_importer_cache.clear()

        for packageName in packageNames:
            log(f"Proceeding to load {packageName}")
            could_load = self._original_loadPlugin(packageName)

            if could_load:
                # When called deferred, we also need to start the plugin. This matches implementation
                # of QgsPluginRegistry::loadPythonPlugin
                could_start = utils.startPlugin(packageName)
                if could_start:
                    QgsSettings().setValue("/PythonPlugins/" + packageName, True)
                    QgsSettings().remove("/PythonPlugins/watchDog/" + packageName)

    def list_missing_deps(self, packageName):
        missing_deps = []
        # If requirements.txt is present, we see if we can load it
        requirements_path = os.path.join(
            self.plugins_path, packageName, "requirements.txt"
        )
        if os.path.isfile(requirements_path):
            log(f"Loading requirements for {packageName}")

            with open(requirements_path, "r") as f:
                requirements = pkg_resources.parse_requirements(f)
                working_set = pkg_resources.WorkingSet()
                for requirement in requirements:
                    req = str(requirement)
                    if self.settings.value(f"skips/{packageName}/{req}", False):
                        log(f"Skipping {req} required by {packageName}.")
                        continue

                    try:
                        working_set.require(req)
                    except (DistributionNotFound, VersionConflict) as e:
                        if isinstance(e, DistributionNotFound):
                            error = "missing"
                        else:  # if isinstance(e, VersionConflict):
                            error = f"conflict ({e.dist})"
                        missing_deps.append(MissingDep(packageName, req, error))
        return missing_deps

    def install_deps(self, deps_to_install, extra_args=[]):
        os.makedirs(self.prefix_path, exist_ok=True)
        reqs = [dep.requirement for dep in deps_to_install]
        log(f"Will pip install {reqs}")
        pip_args = [
            "python",
            "-um",
            "pip",
            "install",
            *reqs,
            "--prefix",
            self.prefix_path,
            *extra_args,
        ]

        progress_dlg = QProgressDialog(
            "Installing dependencies", "Abort", 0, 0, parent=self.iface.mainWindow()
        )
        progress_dlg.setWindowModality(Qt.WindowModal)
        progress_dlg.show()

        process = Popen(pip_args, shell=True, stdout=PIPE, stderr=STDOUT)

        full_output = ""
        while True:
            QgsApplication.processEvents()
            try:
                # FIXME : this doesn't seem to timeout
                out, _ = process.communicate(timeout=0.1)
                output = out.decode(errors="replace").strip()
                full_output += output
                if output:
                    progress_dlg.setLabelText(output)
                    log(output)
            except subprocess.TimeoutExpired:
                pass

            if progress_dlg.wasCanceled():
                process.kill()
            if process.poll() is not None:
                break

        progress_dlg.close()

        if process.returncode != 0:
            warn(f"Installation failed.")
            message = QMessageBox(
                QMessageBox.Warning,
                "Installation failed",
                f"Installation of dependecies failed (code {process.returncode}).\nSee logs for more information.",
                parent=self.iface.mainWindow(),
            )
            message.setDetailedText(full_output)
            message.exec_()
        else:
            self.iface.messageBar().pushMessage(
                "Success",
                f"Installed {len(deps_to_install)} requirements",
                level=Qgis.Success,
            )

    def show(self):
        dialog = ShowDialog()
        dialog.exec_()

    def skip(self):
        dialog = SkipDialog()
        dialog.exec_()


class InstallMissingDialog(QDialog):
    def __init__(self, missing_deps):
        super().__init__()
        uic.loadUi(os.path.join(os.path.dirname(__file__), "ui_install.ui"), self)

        self.checkboxes = {}
        self.tableWidget.setRowCount(len(missing_deps))
        for i, dep in enumerate(missing_deps):
            self.checkboxes[dep] = QTableWidgetItem()
            self.checkboxes[dep].setCheckState(Qt.Checked)
            self.tableWidget.setItem(i, 0, QTableWidgetItem(dep.package))
            self.tableWidget.setItem(i, 1, QTableWidgetItem(dep.requirement))
            self.tableWidget.setItem(i, 2, QTableWidgetItem(dep.state))
            self.tableWidget.setItem(i, 3, self.checkboxes[dep])

        if QgsApplication.primaryScreen().logicalDotsPerInch() > 110:
            self.setMinimumSize(self.minimumWidth() * 2, self.minimumHeight() * 2)

    def deps_to_install(self):
        deps = []
        for dep, checkbox in self.checkboxes.items():
            if checkbox.checkState() == Qt.Checked:
                deps.append(dep)
        return deps

    def deps_to_dontask(self):
        deps = []
        if self.dontAskAgainCheckbox.isChecked():
            for dep, checkbox in self.checkboxes.items():
                if checkbox.checkState() == Qt.Unchecked:
                    deps.append(dep)
        return deps


class ShowDialog(QDialog):
    def __init__(self):
        super().__init__()
        uic.loadUi(os.path.join(os.path.dirname(__file__), "ui_show.ui"), self)

        distributions = list(metadata.distributions())

        self.checkboxes = {}
        self.tableWidget.setRowCount(len(distributions))
        for i, dist in enumerate(distributions):
            self.tableWidget.setItem(i, 0, QTableWidgetItem(dist.metadata["Name"]))
            self.tableWidget.setItem(i, 1, QTableWidgetItem(dist.metadata["Version"]))
            self.tableWidget.setItem(
                i, 2, QTableWidgetItem(os.path.dirname(dist._path))
            )

        if QgsApplication.primaryScreen().logicalDotsPerInch() > 110:
            self.setMinimumSize(self.minimumWidth() * 2, self.minimumHeight() * 2)


class SkipDialog(QDialog):
    def __init__(self):
        super().__init__()
        uic.loadUi(os.path.join(os.path.dirname(__file__), "ui_skips.ui"), self)

        self.settings = QgsSettings()
        self.settings.beginGroup("QPIP")

        self.pushButton.pressed.connect(self.clear_skips)

        self.repopulate()

        if QgsApplication.primaryScreen().logicalDotsPerInch() > 110:
            self.setMinimumSize(self.minimumWidth() * 2, self.minimumHeight() * 2)

    def repopulate(self):

        keys = self.settings.allKeys()

        self.tableWidget.setRowCount(len(keys))
        for i, key in enumerate(keys):
            _, package, req = key.split("/")
            self.tableWidget.setItem(i, 0, QTableWidgetItem(package))
            self.tableWidget.setItem(i, 1, QTableWidgetItem(req))

    def clear_skips(self):
        self.settings.remove("skips")
        self.repopulate()
