# -*- coding: utf-8 -*-
# mainwindow.py
# Copyright (C) 2013, 2014 LEAP
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
"""
Main window for Bitmask.
"""
import logging
import socket

from threading import Condition
from datetime import datetime

from PySide import QtCore, QtGui
from zope.proxy import ProxyBase, setProxiedObject
from twisted.internet import reactor, threads

from leap.bitmask import __version__ as VERSION
from leap.bitmask import __version_hash__ as VERSION_HASH
from leap.bitmask.config import flags
from leap.bitmask.config.leapsettings import LeapSettings
from leap.bitmask.config.providerconfig import ProviderConfig

from leap.bitmask.gui import statemachines
from leap.bitmask.gui.advanced_key_management import AdvancedKeyManagement
from leap.bitmask.gui.eip_preferenceswindow import EIPPreferencesWindow
from leap.bitmask.gui.eip_status import EIPStatusWidget
from leap.bitmask.gui.loggerwindow import LoggerWindow
from leap.bitmask.gui.login import LoginWidget
from leap.bitmask.gui.mail_status import MailStatusWidget
from leap.bitmask.gui.preferenceswindow import PreferencesWindow
from leap.bitmask.gui.systray import SysTray
from leap.bitmask.gui.wizard import Wizard

from leap.bitmask import provider
from leap.bitmask.platform_init import IS_WIN, IS_MAC, IS_LINUX
from leap.bitmask.platform_init.initializers import init_platform

from leap.bitmask import backend

from leap.bitmask.services import get_service_display_name

from leap.bitmask.services.mail import conductor as mail_conductor

from leap.bitmask.services import EIP_SERVICE, MX_SERVICE
from leap.bitmask.services.eip.connection import EIPConnection
from leap.bitmask.services.soledad.soledadbootstrapper import \
    SoledadBootstrapper

from leap.bitmask.util import make_address
from leap.bitmask.util.keyring_helpers import has_keyring
from leap.bitmask.util.leap_log_handler import LeapLogHandler

if IS_WIN:
    from leap.bitmask.platform_init.locks import WindowsLock
    from leap.bitmask.platform_init.locks import raise_window_ack

from leap.common.check import leap_assert
from leap.common.events import register
from leap.common.events import events_pb2 as proto

from leap.mail.imap.service.imap import IMAP_PORT

from ui_mainwindow import Ui_MainWindow

logger = logging.getLogger(__name__)


class MainWindow(QtGui.QMainWindow):
    """
    Main window for login and presenting status updates to the user
    """
    # Signals
    eip_needs_login = QtCore.Signal([])
    offline_mode_bypass_login = QtCore.Signal([])
    new_updates = QtCore.Signal(object)
    raise_window = QtCore.Signal([])
    soledad_ready = QtCore.Signal([])
    mail_client_logged_in = QtCore.Signal([])
    logout = QtCore.Signal([])

    # We use this flag to detect abnormal terminations
    user_stopped_eip = False

    # We give EIP some time to come up before starting soledad anyway
    EIP_TIMEOUT = 60000  # in milliseconds

    # We give each service some time to come to a halt before forcing quit
    SERVICE_STOP_TIMEOUT = 20

    def __init__(self, quit_callback, bypass_checks=False, start_hidden=False):
        """
        Constructor for the client main window

        :param quit_callback: Function to be called when closing
                              the application.
        :type quit_callback: callable

        :param bypass_checks: Set to true if the app should bypass first round
                              of checks for CA certificates at bootstrap
        :type bypass_checks: bool
        :param start_hidden: Set to true if the app should not show the window
                             but just the tray.
        :type start_hidden: bool
        """
        QtGui.QMainWindow.__init__(self)

        # register leap events ########################################
        register(signal=proto.UPDATER_NEW_UPDATES,
                 callback=self._new_updates_available,
                 reqcbk=lambda req, resp: None)  # make rpc call async
        register(signal=proto.RAISE_WINDOW,
                 callback=self._on_raise_window_event,
                 reqcbk=lambda req, resp: None)  # make rpc call async
        register(signal=proto.IMAP_CLIENT_LOGIN,
                 callback=self._on_mail_client_logged_in,
                 reqcbk=lambda req, resp: None)  # make rpc call async
        # end register leap events ####################################

        self._quit_callback = quit_callback
        self._updates_content = ""

        # setup UI
        self.ui = Ui_MainWindow()
        self.ui.setupUi(self)
        self.menuBar().setNativeMenuBar(not IS_LINUX)
        self._backend = backend.Backend(bypass_checks)
        self._backend.start()

        self._settings = LeapSettings()

        self._login_widget = LoginWidget(
            self._settings,
            self)
        self.ui.loginLayout.addWidget(self._login_widget)

        # Qt Signal Connections #####################################
        # TODO separate logic from ui signals.

        self._login_widget.login.connect(self._login)
        self._login_widget.cancel_login.connect(self._cancel_login)
        self._login_widget.show_wizard.connect(self._launch_wizard)
        self._login_widget.logout.connect(self._logout)

        self._eip_status = EIPStatusWidget(self)
        self.ui.eipLayout.addWidget(self._eip_status)
        self._login_widget.logged_in_signal.connect(
            self._eip_status.enable_eip_start)
        self._login_widget.logged_in_signal.connect(
            self._enable_eip_start_action)

        self._mail_status = MailStatusWidget(self)
        self.ui.mailLayout.addWidget(self._mail_status)

        self._eip_connection = EIPConnection()

        # XXX this should be handled by EIP Conductor
        self._eip_connection.qtsigs.connecting_signal.connect(
            self._start_EIP)
        self._eip_connection.qtsigs.disconnecting_signal.connect(
            self._stop_eip)

        self._eip_status.eip_connection_connected.connect(
            self._on_eip_connection_connected)
        self._eip_status.eip_connection_connected.connect(
            self._maybe_run_soledad_setup_checks)
        self.offline_mode_bypass_login.connect(
            self._maybe_run_soledad_setup_checks)

        self.eip_needs_login.connect(self._eip_status.disable_eip_start)
        self.eip_needs_login.connect(self._disable_eip_start_action)

        # This is loaded only once, there's a bug when doing that more
        # than once
        # XXX HACK!! But we need it as long as we are using
        # provider_config in here
        self._provider_config = self._backend.get_provider_config()

        # Used for automatic start of EIP
        self._provisional_provider_config = ProviderConfig()

        self._already_started_eip = False
        self._already_started_soledad = False

        # This is created once we have a valid provider config
        self._srp_auth = None
        self._logged_user = None
        self._logged_in_offline = False

        self._backend_connected_signals = {}
        self._backend_connect()

        self._soledad_bootstrapper = SoledadBootstrapper()
        self._soledad_bootstrapper.download_config.connect(
            self._soledad_intermediate_stage)
        self._soledad_bootstrapper.gen_key.connect(
            self._soledad_bootstrapped_stage)
        self._soledad_bootstrapper.local_only_ready.connect(
            self._soledad_bootstrapped_stage)
        self._soledad_bootstrapper.soledad_invalid_auth_token.connect(
            self._mail_status.set_soledad_invalid_auth_token)
        self._soledad_bootstrapper.soledad_failed.connect(
            self._mail_status.set_soledad_failed)

        self.ui.action_preferences.triggered.connect(self._show_preferences)
        self.ui.action_eip_preferences.triggered.connect(
            self._show_eip_preferences)
        self.ui.action_about_leap.triggered.connect(self._about)
        self.ui.action_quit.triggered.connect(self.quit)
        self.ui.action_wizard.triggered.connect(self._launch_wizard)
        self.ui.action_show_logs.triggered.connect(self._show_logger_window)
        self.ui.action_help.triggered.connect(self._help)
        self.ui.action_create_new_account.triggered.connect(
            self._launch_wizard)

        self.ui.action_advanced_key_management.triggered.connect(
            self._show_AKM)

        if IS_MAC:
            self.ui.menuFile.menuAction().setText(self.tr("File"))

        self.raise_window.connect(self._do_raise_mainwindow)

        # Used to differentiate between real quits and close to tray
        self._really_quit = False

        self._systray = None

        # XXX separate actions into a different
        # module.
        self._action_mail_status = QtGui.QAction(self.tr("Mail is OFF"), self)
        self._mail_status.set_action_mail_status(self._action_mail_status)

        self._action_eip_startstop = QtGui.QAction("", self)
        self._eip_status.set_action_eip_startstop(self._action_eip_startstop)

        self._action_visible = QtGui.QAction(self.tr("Hide Main Window"), self)
        self._action_visible.triggered.connect(self._toggle_visible)

        # disable buttons for now, may come back later.
        # self.ui.btnPreferences.clicked.connect(self._show_preferences)
        # self.ui.btnEIPPreferences.clicked.connect(self._show_eip_preferences)

        self._enabled_services = []
        self._ui_mx_visible = True
        self._ui_eip_visible = True

        # last minute UI manipulations

        self._center_window()
        self.ui.lblNewUpdates.setVisible(False)
        self.ui.btnMore.setVisible(False)
        #########################################
        # We hide this in height temporarily too
        self.ui.lblNewUpdates.resize(0, 0)
        self.ui.btnMore.resize(0, 0)
        #########################################
        self.ui.btnMore.clicked.connect(self._updates_details)
        if flags.OFFLINE is True:
            self._set_label_offline()

        # Services signals/slots connection
        self.new_updates.connect(self._react_to_new_updates)

        # XXX should connect to mail_conductor.start_mail_service instead
        self.soledad_ready.connect(self._start_smtp_bootstrapping)
        self.soledad_ready.connect(self._start_imap_service)
        self.mail_client_logged_in.connect(self._fetch_incoming_mail)
        self.logout.connect(self._stop_imap_service)
        self.logout.connect(self._stop_smtp_service)

        ################################# end Qt Signals connection ########

        init_platform()

        self._wizard = None
        self._wizard_firstrun = False

        self._logger_window = None

        self._bypass_checks = bypass_checks
        self._start_hidden = start_hidden

        # We initialize Soledad and Keymanager instances as
        # transparent proxies, so we can pass the reference freely
        # around.
        self._soledad = ProxyBase(None)
        self._keymanager = ProxyBase(None)

        self._soledad_defer = None

        self._mail_conductor = mail_conductor.MailConductor(
            self._soledad, self._keymanager)
        self._mail_conductor.connect_mail_signals(self._mail_status)

        # Eip machine is a public attribute where the state machine for
        # the eip connection will be available to the different components.
        # Remember that this will not live in the  +1600LOC mainwindow for
        # all the eternity, so at some point we will be moving this to
        # the EIPConductor or some other clever component that we will
        # instantiate from here.

        self.eip_machine = None
        # start event machines
        self.start_eip_machine()
        self._mail_conductor.start_mail_machine()

        self._eip_name = get_service_display_name(EIP_SERVICE)

        if self._first_run():
            self._wizard_firstrun = True
            self._disconnect_and_untrack()
            self._wizard = Wizard(backend=self._backend,
                                  bypass_checks=bypass_checks)
            # Give this window time to finish init and then show the wizard
            QtCore.QTimer.singleShot(1, self._launch_wizard)
            self._wizard.accepted.connect(self._finish_init)
            self._wizard.rejected.connect(self._rejected_wizard)
        else:
            # during finish_init, we disable the eip start button
            # so this has to be done after eip_machine is started
            self._finish_init()

    def _not_logged_in_error(self):
        """
        Handle the 'not logged in' backend error if we try to do an operation
        that requires to be logged in.
        """
        logger.critical("You are trying to do an operation that requires "
                        "log in first.")
        QtGui.QMessageBox.critical(
            self, self.tr("Application error"),
            self.tr("You are trying to do an operation "
                    "that requires logging in first."))

    def _connect_and_track(self, signal, method):
        """
        Helper to connect signals and keep track of them.

        :param signal: the signal to connect to.
        :type signal: QtCore.Signal
        :param method: the method to call when the signal is triggered.
        :type method: callable, Slot or Signal
        """
        self._backend_connected_signals[signal] = method
        signal.connect(method)

    def _backend_connect(self):
        """
        Helper to connect to backend signals
        """
        sig = self._backend.signaler
        self._connect_and_track(sig.prov_name_resolution,
                                self._intermediate_stage)
        self._connect_and_track(sig.prov_https_connection,
                                self._intermediate_stage)
        self._connect_and_track(sig.prov_download_ca_cert,
                                self._intermediate_stage)

        self._connect_and_track(sig.prov_download_provider_info,
                                self._load_provider_config)
        self._connect_and_track(sig.prov_check_api_certificate,
                                self._provider_config_loaded)

        self._connect_and_track(sig.prov_problem_with_provider,
                                self._login_problem_provider)

        self._connect_and_track(sig.prov_cancelled_setup,
                                self._set_login_cancelled)

        # Login signals
        self._connect_and_track(sig.srp_auth_ok, self._authentication_finished)

        auth_error = (
            lambda: self._authentication_error(self.tr("Unknown error.")))
        self._connect_and_track(sig.srp_auth_error, auth_error)

        auth_server_error = (
            lambda: self._authentication_error(
                self.tr("There was a server problem with authentication.")))
        self._connect_and_track(sig.srp_auth_server_error, auth_server_error)

        auth_connection_error = (
            lambda: self._authentication_error(
                self.tr("Could not establish a connection.")))
        self._connect_and_track(sig.srp_auth_connection_error,
                                auth_connection_error)

        auth_bad_user_or_password = (
            lambda: self._authentication_error(
                self.tr("Invalid username or password.")))
        self._connect_and_track(sig.srp_auth_bad_user_or_password,
                                auth_bad_user_or_password)

        # Logout signals
        self._connect_and_track(sig.srp_logout_ok, self._logout_ok)
        self._connect_and_track(sig.srp_logout_error, self._logout_error)

        self._connect_and_track(sig.srp_not_logged_in_error,
                                self._not_logged_in_error)

        # EIP bootstrap signals
        self._connect_and_track(sig.eip_config_ready,
                                self._eip_intermediate_stage)
        self._connect_and_track(sig.eip_client_certificate_ready,
                                self._finish_eip_bootstrap)

        # We don't want to disconnect some signals so don't track them:
        sig.prov_unsupported_client.connect(self._needs_update)
        sig.prov_unsupported_api.connect(self._incompatible_api)

        # EIP start signals
        sig.eip_openvpn_already_running.connect(
            self._on_eip_openvpn_already_running)
        sig.eip_alien_openvpn_already_running.connect(
            self._on_eip_alien_openvpn_already_running)
        sig.eip_openvpn_not_found_error.connect(
            self._on_eip_openvpn_not_found_error)
        sig.eip_vpn_launcher_exception.connect(
            self._on_eip_vpn_launcher_exception)
        sig.eip_no_polkit_agent_error.connect(
            self._on_eip_no_polkit_agent_error)
        sig.eip_no_pkexec_error.connect(self._on_eip_no_pkexec_error)
        sig.eip_no_tun_kext_error.connect(self._on_eip_no_tun_kext_error)

        sig.eip_state_changed.connect(self._eip_status.update_vpn_state)
        sig.eip_status_changed.connect(self._eip_status.update_vpn_status)
        sig.eip_process_finished.connect(self._eip_finished)
        sig.eip_network_unreachable.connect(self._on_eip_network_unreachable)
        sig.eip_process_restart_tls.connect(self._do_eip_restart)
        sig.eip_process_restart_ping.connect(self._do_eip_restart)

    def _disconnect_and_untrack(self):
        """
        Helper to disconnect the tracked signals.

        Some signals are emitted from the wizard, and we want to
        ignore those.
        """
        for signal, method in self._backend_connected_signals.items():
            try:
                signal.disconnect(method)
            except RuntimeError:
                pass  # Signal was not connected

        self._backend_connected_signals = {}

    @QtCore.Slot()
    def _rejected_wizard(self):
        """
        TRIGGERS:
            self._wizard.rejected

        Called if the wizard has been cancelled or closed before
        finishing.
        This is executed for the first run wizard only. Any other execution of
        the wizard won't reach this point.
        """
        providers = self._settings.get_configured_providers()
        has_provider_on_disk = len(providers) != 0
        if not has_provider_on_disk:
            # if we don't have any provider configured (included a pinned
            # one) we can't use the application, so quit.
            self.quit()
        else:
            # This happens if the user finishes the provider
            # setup but does not register
            self._wizard = None
            self._backend_connect()
            if self._wizard_firstrun:
                self._finish_init()

    @QtCore.Slot()
    def _launch_wizard(self):
        """
        TRIGGERS:
            self._login_widget.show_wizard
            self.ui.action_wizard.triggered

        Also called in first run.

        Launches the wizard, creating the object itself if not already
        there.
        """
        if self._wizard is None:
            self._disconnect_and_untrack()
            self._wizard = Wizard(backend=self._backend,
                                  bypass_checks=self._bypass_checks)
            self._wizard.accepted.connect(self._finish_init)
            self._wizard.rejected.connect(self._rejected_wizard)

        self.setVisible(False)
        # Do NOT use exec_, it will use a child event loop!
        # Refer to http://www.themacaque.com/?p=1067 for funny details.
        self._wizard.show()
        if IS_MAC:
            self._wizard.raise_()
        self._wizard.finished.connect(self._wizard_finished)
        self._settings.set_skip_first_run(True)

    @QtCore.Slot()
    def _wizard_finished(self):
        """
        TRIGGERS:
            self._wizard.finished

        Called when the wizard has finished.
        """
        self.setVisible(True)

    def _get_leap_logging_handler(self):
        """
        Gets the leap handler from the top level logger

        :return: a logging handler or None
        :rtype: LeapLogHandler or None
        """
        # TODO this can be a function, does not need
        # to be a method.
        leap_logger = logging.getLogger('leap')
        for h in leap_logger.handlers:
            if isinstance(h, LeapLogHandler):
                return h
        return None

    @QtCore.Slot()
    def _show_logger_window(self):
        """
        TRIGGERS:
            self.ui.action_show_logs.triggered

        Displays the window with the history of messages logged until now
        and displays the new ones on arrival.
        """
        if self._logger_window is None:
            leap_log_handler = self._get_leap_logging_handler()
            if leap_log_handler is None:
                logger.error('Leap logger handler not found')
                return
            else:
                self._logger_window = LoggerWindow(handler=leap_log_handler)
                self._logger_window.setVisible(
                    not self._logger_window.isVisible())
        else:
            self._logger_window.setVisible(not self._logger_window.isVisible())

    @QtCore.Slot()
    def _show_AKM(self):
        """
        TRIGGERS:
            self.ui.action_advanced_key_management.triggered

        Displays the Advanced Key Management dialog.
        """
        domain = self._login_widget.get_selected_provider()
        logged_user = "{0}@{1}".format(self._logged_user, domain)

        has_mx = True
        if self._logged_user is not None:
            provider_config = self._get_best_provider_config()
            has_mx = provider_config.provides_mx()

        akm = AdvancedKeyManagement(
            self, has_mx, logged_user, self._keymanager, self._soledad)
        akm.show()

    @QtCore.Slot()
    def _show_preferences(self):
        """
        TRIGGERS:
            self.ui.btnPreferences.clicked (disabled for now)
            self.ui.action_preferences

        Displays the preferences window.
        """
        user = self._login_widget.get_user()
        prov = self._login_widget.get_selected_provider()
        preferences = PreferencesWindow(
            self, self._backend, self._provider_config, self._soledad,
            user, prov)

        self.soledad_ready.connect(preferences.set_soledad_ready)
        preferences.show()
        preferences.preferences_saved.connect(self._update_eip_enabled_status)

    @QtCore.Slot()
    def _update_eip_enabled_status(self):
        """
        TRIGGER:
            PreferencesWindow.preferences_saved

        Enable or disable the EIP start/stop actions and stop EIP if the user
        disabled that service.

        :returns: if the eip actions were enabled or disabled
        :rtype: bool
        """
        settings = self._settings
        default_provider = settings.get_defaultprovider()
        enabled_services = []
        if default_provider is not None:
            enabled_services = settings.get_enabled_services(default_provider)

        eip_enabled = False
        if EIP_SERVICE in enabled_services:
            should_autostart = settings.get_autostart_eip()
            if should_autostart and default_provider is not None:
                self._eip_status.enable_eip_start()
                self._eip_status.set_eip_status("")
                eip_enabled = True
            else:
                # we don't have an usable provider
                # so the user needs to log in first
                self._eip_status.disable_eip_start()
        else:
            self._stop_eip()
            self._eip_status.disable_eip_start()
            self._eip_status.set_eip_status(self.tr("Disabled"))

        return eip_enabled

    @QtCore.Slot()
    def _show_eip_preferences(self):
        """
        TRIGGERS:
            self.ui.btnEIPPreferences.clicked
            self.ui.action_eip_preferences (disabled for now)

        Displays the EIP preferences window.
        """
        domain = self._login_widget.get_selected_provider()
        EIPPreferencesWindow(self, domain, self._backend).show()

    #
    # updates
    #

    def _new_updates_available(self, req):
        """
        Callback for the new updates event

        :param req: Request type
        :type req: leap.common.events.events_pb2.SignalRequest
        """
        self.new_updates.emit(req)

    @QtCore.Slot(object)
    def _react_to_new_updates(self, req):
        """
        TRIGGERS:
            self.new_updates

        Displays the new updates label and sets the updates_content

        :param req: Request type
        :type req: leap.common.events.events_pb2.SignalRequest
        """
        self.moveToThread(QtCore.QCoreApplication.instance().thread())
        self.ui.lblNewUpdates.setVisible(True)
        self.ui.btnMore.setVisible(True)
        self._updates_content = req.content

    @QtCore.Slot()
    def _updates_details(self):
        """
        TRIGGERS:
            self.ui.btnMore.clicked

        Parses and displays the updates details
        """
        msg = self.tr("The Bitmask app is ready to update, please"
                      " restart the application.")

        # We assume that if there is nothing in the contents, then
        # the Bitmask bundle is what needs updating.
        if len(self._updates_content) > 0:
            files = self._updates_content.split(", ")
            files_str = ""
            for f in files:
                final_name = f.replace("/data/", "")
                final_name = final_name.replace(".thp", "")
                files_str += final_name
                files_str += "\n"
            msg += self.tr(" The following components will be updated:\n%s") \
                % (files_str,)

        QtGui.QMessageBox.information(self,
                                      self.tr("Updates available"),
                                      msg)

    @QtCore.Slot()
    def _finish_init(self):
        """
        TRIGGERS:
            self._wizard.accepted

        Also called at the end of the constructor if not first run.

        Implements the behavior after either constructing the
        mainwindow object, loading the saved user/password, or after
        the wizard has been executed.
        """
        # XXX: May be this can be divided into two methods?

        providers = self._settings.get_configured_providers()
        self._login_widget.set_providers(providers)
        self._show_systray()

        if not self._start_hidden:
            self.show()
            if IS_MAC:
                self.raise_()

        self._show_hide_unsupported_services()

        if self._wizard:
            possible_username = self._wizard.get_username()
            possible_password = self._wizard.get_password()

            # select the configured provider in the combo box
            domain = self._wizard.get_domain()
            self._login_widget.select_provider_by_name(domain)

            self._login_widget.set_remember(self._wizard.get_remember())
            self._enabled_services = list(self._wizard.get_services())
            self._settings.set_enabled_services(
                self._login_widget.get_selected_provider(),
                self._enabled_services)
            if possible_username is not None:
                self._login_widget.set_user(possible_username)
            if possible_password is not None:
                self._login_widget.set_password(possible_password)
                self._login()
            else:
                self.eip_needs_login.emit()

            self._wizard = None
            self._backend_connect()
        else:
            self._try_autostart_eip()

            domain = self._settings.get_provider()
            if domain is not None:
                self._login_widget.select_provider_by_name(domain)

            if not self._settings.get_remember():
                # nothing to do here
                return

            saved_user = self._settings.get_user()

            if saved_user is not None and has_keyring():
                if self._login_widget.load_user_from_keyring(saved_user):
                    self._login()

    def _show_hide_unsupported_services(self):
        """
        Given a set of configured providers, it creates a set of
        available services among all of them and displays the service
        widgets of only those.

        This means, for example, that with just one provider with EIP
        only, the mail widget won't be displayed.
        """
        providers = self._settings.get_configured_providers()

        services = set()

        for prov in providers:
            provider_config = ProviderConfig()
            loaded = provider_config.load(
                provider.get_provider_path(prov))
            if loaded:
                for service in provider_config.get_services():
                    services.add(service)

        self._set_eip_visible(EIP_SERVICE in services)
        self._set_mx_visible(MX_SERVICE in services)

    def _set_mx_visible(self, visible):
        """
        Change the visibility of MX_SERVICE related UI components.

        :param visible: whether the components should be visible or not.
        :type visible: bool
        """
        # only update visibility if it is something to change
        if self._ui_mx_visible ^ visible:
            self.ui.mailWidget.setVisible(visible)
            self.ui.lineUnderEmail.setVisible(visible)
            self._action_mail_status.setVisible(visible)
            self._ui_mx_visible = visible

    def _set_eip_visible(self, visible):
        """
        Change the visibility of EIP_SERVICE related UI components.

        :param visible: whether the components should be visible or not.
        :type visible: bool
        """
        # NOTE: we use xor to avoid the code being run if the visibility hasn't
        # changed. This is meant to avoid the eip menu being displayed floating
        # around at start because the systray isn't rendered yet.
        if self._ui_eip_visible ^ visible:
            self.ui.eipWidget.setVisible(visible)
            self.ui.lineUnderEIP.setVisible(visible)
            self._eip_menu.setVisible(visible)
            self._ui_eip_visible = visible

    def _set_label_offline(self):
        """
        Set the login label to reflect offline status.
        """
        if self._logged_in_offline:
            provider = ""
        else:
            provider = self.ui.lblLoginProvider.text()

        self.ui.lblLoginProvider.setText(
            provider +
            self.tr(" (offline mode)"))

    #
    # systray
    #

    def _show_systray(self):
        """
        Sets up the systray icon
        """
        if self._systray is not None:
            self._systray.setVisible(True)
            return

        # Placeholder action
        # It is temporary to display the tray as designed
        help_action = QtGui.QAction(self.tr("Help"), self)
        help_action.setEnabled(False)

        systrayMenu = QtGui.QMenu(self)
        systrayMenu.addAction(self._action_visible)
        systrayMenu.addSeparator()

        eip_status_label = "{0}: {1}".format(self._eip_name, self.tr("OFF"))
        self._eip_menu = eip_menu = systrayMenu.addMenu(eip_status_label)
        eip_menu.addAction(self._action_eip_startstop)
        self._eip_status.set_eip_status_menu(eip_menu)
        systrayMenu.addSeparator()
        systrayMenu.addAction(self._action_mail_status)
        systrayMenu.addSeparator()
        systrayMenu.addAction(self.ui.action_quit)
        self._systray = SysTray(self)
        self._systray.setContextMenu(systrayMenu)
        self._systray.setIcon(self._eip_status.ERROR_ICON_TRAY)
        self._systray.setVisible(True)
        self._systray.activated.connect(self._tray_activated)

        self._mail_status.set_systray(self._systray)
        self._eip_status.set_systray(self._systray)

        if self._start_hidden:
            hello = lambda: self._systray.showMessage(
                self.tr('Hello!'),
                self.tr('Bitmask has started in the tray.'))
            # we wait for the systray to be ready
            reactor.callLater(1, hello)

    @QtCore.Slot(int)
    def _tray_activated(self, reason=None):
        """
        TRIGGERS:
            self._systray.activated

        :param reason: the reason why the tray got activated.
        :type reason: int

        Displays the context menu from the tray icon
        """
        self._update_hideshow_menu()

        context_menu = self._systray.contextMenu()
        if not IS_MAC:
            # for some reason, context_menu.show()
            # is failing in a way beyond my understanding.
            # (not working the first time it's clicked).
            # this works however.
            context_menu.exec_(self._systray.geometry().center())

    def _update_hideshow_menu(self):
        """
        Updates the Hide/Show main window menu text based on the
        visibility of the window.
        """
        get_action = lambda visible: (
            self.tr("Show Main Window"),
            self.tr("Hide Main Window"))[int(visible)]

        # set labels
        visible = self.isVisible() and self.isActiveWindow()
        self._action_visible.setText(get_action(visible))

    @QtCore.Slot()
    def _toggle_visible(self):
        """
        TRIGGERS:
            self._action_visible.triggered

        Toggles the window visibility
        """
        visible = self.isVisible() and self.isActiveWindow()

        if not visible:
            QtGui.QApplication.setQuitOnLastWindowClosed(True)
            self.show()
            self.activateWindow()
            self.raise_()
        else:
            # We set this in order to avoid dialogs shutting down the
            # app on close, as they will be the only visible window.
            # e.g.: PreferencesWindow, LoggerWindow
            QtGui.QApplication.setQuitOnLastWindowClosed(False)
            self.hide()

        # Wait a bit until the window visibility has changed so
        # the menu is set with the correct value.
        QtCore.QTimer.singleShot(500, self._update_hideshow_menu)

    def _center_window(self):
        """
        Centers the mainwindow based on the desktop geometry
        """
        geometry = self._settings.get_geometry()
        state = self._settings.get_windowstate()

        if geometry is None:
            app = QtGui.QApplication.instance()
            width = app.desktop().width()
            height = app.desktop().height()
            window_width = self.size().width()
            window_height = self.size().height()
            x = (width / 2.0) - (window_width / 2.0)
            y = (height / 2.0) - (window_height / 2.0)
            self.move(x, y)
        else:
            self.restoreGeometry(geometry)

        if state is not None:
            self.restoreState(state)

    @QtCore.Slot()
    def _about(self):
        """
        TRIGGERS:
            self.ui.action_about_leap.triggered

        Display the About Bitmask dialog
        """
        today = datetime.now().date()
        greet = ("Happy New 1984!... or not ;)<br><br>"
                 if today.month == 1 and today.day < 15 else "")
        QtGui.QMessageBox.about(
            self, self.tr("About Bitmask - %s") % (VERSION,),
            self.tr("Version: <b>%s</b> (%s)<br>"
                    "<br>%s"
                    "Bitmask is the Desktop client application for "
                    "the LEAP platform, supporting encrypted internet "
                    "proxy, secure email, and secure chat (coming soon).<br>"
                    "<br>"
                    "LEAP is a non-profit dedicated to giving "
                    "all internet users access to secure "
                    "communication. Our focus is on adapting "
                    "encryption technology to make it easy to use "
                    "and widely available. <br>"
                    "<br>"
                    "<a href='https://leap.se'>More about LEAP"
                    "</a>") % (VERSION, VERSION_HASH[:10], greet))

    @QtCore.Slot()
    def _help(self):
        """
        TRIGGERS:
            self.ui.action_help.triggered

        Display the Bitmask help dialog.
        """
        # TODO: don't hardcode!
        smtp_port = 2013

        url = ("<a href='https://addons.mozilla.org/es/thunderbird/"
               "addon/bitmask/'>bitmask addon</a>")

        msg = self.tr(
            "<strong>Instructions to use mail:</strong><br>"
            "If you use Thunderbird you can use the Bitmask extension helper. "
            "Search for 'Bitmask' in the add-on manager or download it "
            "from: {0}.<br><br>"
            "You can configure Bitmask manually with these options:<br>"
            "<em>"
            "   Incoming -> IMAP, port: {1}<br>"
            "   Outgoing -> SMTP, port: {2}<br>"
            "   Username -> your bitmask username.<br>"
            "   Password -> does not matter, use any text. "
            " Just don't leave it empty and don't use your account's password."
            "</em>").format(url, IMAP_PORT, smtp_port)
        QtGui.QMessageBox.about(self, self.tr("Bitmask Help"), msg)

    def _needs_update(self):
        """
        Display a warning dialog to inform the user that the app needs update.
        """
        url = "https://dl.bitmask.net/"
        msg = self.tr(
            "The current client version is not supported "
            "by this provider.<br>"
            "Please update to latest version.<br><br>"
            "You can get the latest version from "
            "<a href='{0}'>{1}</a>").format(url, url)
        QtGui.QMessageBox.warning(self, self.tr("Update Needed"), msg)

    def _incompatible_api(self):
        """
        Display a warning dialog to inform the user that the provider has an
        incompatible API.
        """
        msg = self.tr(
            "This provider is not compatible with the client.<br><br>"
            "Error: API version incompatible.")
        QtGui.QMessageBox.warning(self, self.tr("Incompatible Provider"), msg)

    def changeEvent(self, e):
        """
        Reimplements the changeEvent method to minimize to tray
        """
        if not IS_MAC and \
           QtGui.QSystemTrayIcon.isSystemTrayAvailable() and \
           e.type() == QtCore.QEvent.WindowStateChange and \
           self.isMinimized():
            self._toggle_visible()
            e.accept()
            return
        QtGui.QMainWindow.changeEvent(self, e)

    def closeEvent(self, e):
        """
        Reimplementation of closeEvent to close to tray
        """
        if QtGui.QSystemTrayIcon.isSystemTrayAvailable() and \
                not self._really_quit:
            self._toggle_visible()
            e.ignore()
            return

        self._settings.set_geometry(self.saveGeometry())
        self._settings.set_windowstate(self.saveState())

        QtGui.QMainWindow.closeEvent(self, e)

    def _first_run(self):
        """
        Returns True if there are no configured providers. False otherwise

        :rtype: bool
        """
        providers = self._settings.get_configured_providers()
        has_provider_on_disk = len(providers) != 0
        skip_first_run = self._settings.get_skip_first_run()
        return not (has_provider_on_disk and skip_first_run)

    def _download_provider_config(self):
        """
        Starts the bootstrapping sequence. It will download the
        provider configuration if it's not present, otherwise will
        emit the corresponding signals inmediately
        """
        # XXX should rename this provider, name clash.
        provider = self._login_widget.get_selected_provider()
        self._backend.setup_provider(provider)

    @QtCore.Slot(dict)
    def _load_provider_config(self, data):
        """
        TRIGGERS:
            self._backend.signaler.prov_download_provider_info

        Once the provider config has been downloaded, this loads the
        self._provider_config instance with it and starts the second
        part of the bootstrapping sequence

        :param data: result from the last stage of the
                     run_provider_select_checks
        :type data: dict
        """
        if data[self._backend.PASSED_KEY]:
            selected_provider = self._login_widget.get_selected_provider()
            self._backend.provider_bootstrap(selected_provider)
        else:
            logger.error(data[self._backend.ERROR_KEY])
            self._login_problem_provider()

    @QtCore.Slot()
    def _login_problem_provider(self):
        """
        Warns the user about a problem with the provider during login.
        """
        self._login_widget.set_status(
            self.tr("Unable to login: Problem with provider"))
        self._login_widget.set_enabled(True)

    @QtCore.Slot()
    def _login(self):
        """
        TRIGGERS:
            self._login_widget.login

        Starts the login sequence. Which involves bootstrapping the
        selected provider if the selection is valid (not empty), then
        start the SRP authentication, and as the last step
        bootstrapping the EIP service
        """
        # TODO most of this could ve handled by the login widget,
        # but we'd have to move lblLoginProvider into the widget itself,
        # instead of having it as a top-level attribute.
        if flags.OFFLINE is True:
            logger.debug("OFFLINE mode! bypassing remote login")
            # TODO reminder, we're not handling logout for offline
            # mode.
            self._login_widget.logged_in()
            self._logged_in_offline = True
            self._set_label_offline()
            self.offline_mode_bypass_login.emit()
        else:
            leap_assert(self._provider_config, "We need a provider config")
            self.ui.action_create_new_account.setEnabled(False)
            if self._login_widget.start_login():
                self._download_provider_config()

    @QtCore.Slot(unicode)
    def _authentication_error(self, msg):
        """
        TRIGGERS:
            Signaler.srp_auth_error
            Signaler.srp_auth_server_error
            Signaler.srp_auth_connection_error
            Signaler.srp_auth_bad_user_or_password

        Handle the authentication errors.

        :param msg: the message to show to the user.
        :type msg: unicode
        """
        self._login_widget.set_status(msg)
        self._login_widget.set_enabled(True)
        self.ui.action_create_new_account.setEnabled(True)

    @QtCore.Slot()
    def _cancel_login(self):
        """
        TRIGGERS:
            self._login_widget.cancel_login

        Stops the login sequence.
        """
        logger.debug("Cancelling log in.")
        self._cancel_ongoing_defers()

    def _cancel_ongoing_defers(self):
        """
        Cancel the running defers to avoid app blocking.
        """
        # XXX: Should we stop all the backend defers?
        self._backend.cancel_setup_provider()
        self._backend.cancel_login()

        if self._soledad_defer is not None:
            logger.debug("Cancelling soledad defer.")
            self._soledad_defer.cancel()
            self._soledad_defer = None

    @QtCore.Slot()
    def _set_login_cancelled(self):
        """
        TRIGGERS:
            Signaler.prov_cancelled_setup fired by
            self._backend.cancel_setup_provider()

        This method re-enables the login widget and display a message for
        the cancelled operation.
        """
        self._login_widget.set_status(self.tr("Log in cancelled by the user."))
        self._login_widget.set_enabled(True)

    @QtCore.Slot(dict)
    def _provider_config_loaded(self, data):
        """
        TRIGGERS:
            self._backend.signaler.prov_check_api_certificate

        Once the provider configuration is loaded, this starts the SRP
        authentication
        """
        leap_assert(self._provider_config, "We need a provider config!")

        if data[self._backend.PASSED_KEY]:
            username = self._login_widget.get_user()
            password = self._login_widget.get_password()

            self._show_hide_unsupported_services()

            domain = self._provider_config.get_domain()
            self._backend.login(domain, username, password)
        else:
            logger.error(data[self._backend.ERROR_KEY])
            self._login_problem_provider()

    @QtCore.Slot()
    def _authentication_finished(self):
        """
        TRIGGERS:
            self._srp_auth.authentication_finished

        Once the user is properly authenticated, try starting the EIP
        service
        """
        self._login_widget.set_status(self.tr("Succeeded"), error=False)

        self._logged_user = self._login_widget.get_user()
        user = self._logged_user
        domain = self._provider_config.get_domain()
        full_user_id = make_address(user, domain)
        self._mail_conductor.userid = full_user_id
        self._start_eip_bootstrap()
        self.ui.action_create_new_account.setEnabled(True)

        # if soledad/mail is enabled:
        if MX_SERVICE in self._enabled_services:
            btn_enabled = self._login_widget.set_logout_btn_enabled
            btn_enabled(False)
            self.soledad_ready.connect(lambda: btn_enabled(True))
            self._soledad_bootstrapper.soledad_failed.connect(
                lambda: btn_enabled(True))

        if not self._get_best_provider_config().provides_mx():
            self._set_mx_visible(False)

    def _start_eip_bootstrap(self):
        """
        Changes the stackedWidget index to the EIP status one and
        triggers the eip bootstrapping.
        """

        self._login_widget.logged_in()
        provider = self._provider_config.get_domain()
        self.ui.lblLoginProvider.setText(provider)

        self._enabled_services = self._settings.get_enabled_services(
            self._provider_config.get_domain())

        # TODO separate UI from logic.
        if self._provides_mx_and_enabled():
            self._mail_status.about_to_start()
        else:
            self._mail_status.set_disabled()

        self._maybe_start_eip()

    def _provides_mx_and_enabled(self):
        """
        Defines if the current provider provides mx and if we have it enabled.

        :returns: True if provides and is enabled, False otherwise
        :rtype: bool
        """
        provider_config = self._get_best_provider_config()
        return (provider_config.provides_mx() and
                MX_SERVICE in self._enabled_services)

    def _provides_eip_and_enabled(self):
        """
        Defines if the current provider provides eip and if we have it enabled.

        :returns: True if provides and is enabled, False otherwise
        :rtype: bool
        """
        provider_config = self._get_best_provider_config()
        return (provider_config.provides_eip() and
                EIP_SERVICE in self._enabled_services)

    def _maybe_run_soledad_setup_checks(self):
        """
        Conditionally start Soledad.
        """
        # TODO split.
        if self._already_started_soledad is True:
            return

        if not self._provides_mx_and_enabled():
            return

        username = self._login_widget.get_user()
        password = unicode(self._login_widget.get_password())
        provider_domain = self._login_widget.get_selected_provider()

        sb = self._soledad_bootstrapper
        if flags.OFFLINE is True:
            provider_domain = self._login_widget.get_selected_provider()
            sb._password = password

            self._provisional_provider_config.load(
                provider.get_provider_path(provider_domain))

            full_user_id = make_address(username, provider_domain)
            uuid = self._settings.get_uuid(full_user_id)
            self._mail_conductor.userid = full_user_id

            if uuid is None:
                # We don't need more visibility at the moment,
                # this is mostly for internal use/debug for now.
                logger.warning("Sorry! Log-in at least one time.")
                return
            fun = sb.load_offline_soledad
            fun(full_user_id, password, uuid)
        else:
            provider_config = self._provider_config

            if self._logged_user is not None:
                self._soledad_defer = sb.run_soledad_setup_checks(
                    provider_config, username, password,
                    download_if_needed=True)

    ###################################################################
    # Service control methods: soledad

    @QtCore.Slot(dict)
    def _soledad_intermediate_stage(self, data):
        # TODO missing param docstring
        """
        TRIGGERS:
            self._soledad_bootstrapper.download_config

        If there was a problem, displays it, otherwise it does nothing.
        This is used for intermediate bootstrapping stages, in case
        they fail.
        """
        passed = data[self._soledad_bootstrapper.PASSED_KEY]
        if not passed:
            # TODO display in the GUI:
            # should pass signal to a slot in status_panel
            # that sets the global status
            logger.error("Soledad failed to start: %s" %
                         (data[self._soledad_bootstrapper.ERROR_KEY],))

    @QtCore.Slot(dict)
    def _soledad_bootstrapped_stage(self, data):
        """
        TRIGGERS:
            self._soledad_bootstrapper.gen_key
            self._soledad_bootstrapper.local_only_ready

        If there was a problem, displays it, otherwise it does nothing.
        This is used for intermediate bootstrapping stages, in case
        they fail.

        :param data: result from the bootstrapping stage for Soledad
        :type data: dict
        """
        passed = data[self._soledad_bootstrapper.PASSED_KEY]
        if not passed:
            # TODO should actually *display* on the panel.
            logger.debug("ERROR on soledad bootstrapping:")
            logger.error("%r" % data[self._soledad_bootstrapper.ERROR_KEY])
            return

        logger.debug("Done bootstrapping Soledad")

        # Update the proxy objects to point to
        # the initialized instances.
        setProxiedObject(self._soledad,
                         self._soledad_bootstrapper.soledad)
        setProxiedObject(self._keymanager,
                         self._soledad_bootstrapper.keymanager)

        # Ok, now soledad is ready, so we can allow other things that
        # depend on soledad to start.
        self._soledad_defer = None

        # this will trigger start_imap_service
        # and start_smtp_boostrapping
        self.soledad_ready.emit()

    ###################################################################
    # Service control methods: smtp

    @QtCore.Slot()
    def _start_smtp_bootstrapping(self):
        """
        TRIGGERS:
            self.soledad_ready
        """
        if flags.OFFLINE is True:
            logger.debug("not starting smtp in offline mode")
            return

        if self._provides_mx_and_enabled():
            self._mail_conductor.start_smtp_service(self._provider_config,
                                                    download_if_needed=True)

    # XXX --- should remove from here, and connecte directly to the state
    # machine.
    @QtCore.Slot()
    def _stop_smtp_service(self):
        """
        TRIGGERS:
            self.logout
        """
        # TODO call stop_mail_service
        self._mail_conductor.stop_smtp_service()

    ###################################################################
    # Service control methods: imap

    @QtCore.Slot()
    def _start_imap_service(self):
        """
        TRIGGERS:
            self.soledad_ready
        """
        # TODO in the OFFLINE mode we should also modify the  rules
        # in the mail state machine so it shows that imap is active
        # (but not smtp since it's not yet ready for offline use)
        start_fun = self._mail_conductor.start_imap_service
        if flags.OFFLINE is True:
            provider_domain = self._login_widget.get_selected_provider()
            self._provider_config.load(
                provider.get_provider_path(provider_domain))
        provides_mx = self._provider_config.provides_mx()

        if flags.OFFLINE is True and provides_mx:
            start_fun()
            return

        if self._provides_mx_and_enabled():
            start_fun()

    def _on_mail_client_logged_in(self, req):
        """
        Triggers qt signal when client login event is received.
        """
        self.mail_client_logged_in.emit()

    @QtCore.Slot()
    def _fetch_incoming_mail(self):
        """
        TRIGGERS:
            self.mail_client_logged_in
        """
        # TODO connect signal directly!!!
        self._mail_conductor.fetch_incoming_mail()

    @QtCore.Slot()
    def _stop_imap_service(self):
        """
        TRIGGERS:
            self.logout
        """
        cv = Condition()
        cv.acquire()
        # TODO call stop_mail_service
        threads.deferToThread(self._mail_conductor.stop_imap_service, cv)
        # and wait for it to be stopped
        logger.debug('Waiting for imap service to stop.')
        cv.wait(self.SERVICE_STOP_TIMEOUT)

    # end service control methods (imap)

    ###################################################################
    # Service control methods: eip

    def start_eip_machine(self):
        """
        Initializes and starts the EIP state machine
        """
        button = self._eip_status.eip_button
        action = self._action_eip_startstop
        label = self._eip_status.eip_label
        builder = statemachines.ConnectionMachineBuilder(self._eip_connection)
        eip_machine = builder.make_machine(button=button,
                                           action=action,
                                           label=label)
        self.eip_machine = eip_machine
        self.eip_machine.start()
        logger.debug('eip machine started')

    @QtCore.Slot()
    def _disable_eip_start_action(self):
        """
        Disables the EIP start action in the systray menu.
        """
        self._action_eip_startstop.setEnabled(False)

    @QtCore.Slot()
    def _enable_eip_start_action(self):
        """
        Enables the EIP start action in the systray menu.
        """
        self._action_eip_startstop.setEnabled(True)

    @QtCore.Slot()
    def _on_eip_connection_connected(self):
        """
        TRIGGERS:
            self._eip_status.eip_connection_connected

        Emits the EIPConnection.qtsigs.connected_signal

        This is a little workaround for connecting the vpn-connected
        signal that currently is beeing processed under status_panel.
        After the refactor to EIPConductor this should not be necessary.
        """
        self._eip_connection.qtsigs.connected_signal.emit()

        provider_config = self._get_best_provider_config()
        domain = provider_config.get_domain()

        self._eip_status.set_provider(domain)
        self._settings.set_defaultprovider(domain)
        self._already_started_eip = True

        # check for connectivity
        self._check_name_resolution(domain)

    def _check_name_resolution(self, domain):
        """
        Check if we can resolve the given domain name.

        :param domain: the domain to check.
        :type domain: str
        """
        def do_check():
            """
            Try to resolve the domain name.
            """
            socket.gethostbyname(domain.encode('idna'))

        def check_err(failure):
            """
            Errback handler for `do_check`.

            :param failure: the failure that triggered the errback.
            :type failure: twisted.python.failure.Failure
            """
            logger.error(repr(failure))
            logger.error("Can't resolve hostname.")

            msg = self.tr(
                "The server at {0} can't be found, because the DNS lookup "
                "failed. DNS is the network service that translates a "
                "website's name to its Internet address. Either your computer "
                "is having trouble connecting to the network, or you are "
                "missing some helper files that are needed to securely use "
                "DNS while {1} is active. To install these helper files, quit "
                "this application and start it again."
            ).format(domain, self._eip_name)

            show_err = lambda: QtGui.QMessageBox.critical(
                self, self.tr("Connection Error"), msg)
            reactor.callLater(0, show_err)

            # python 2.7.4 raises socket.error
            # python 2.7.5 raises socket.gaierror
            failure.trap(socket.gaierror, socket.error)

        d = threads.deferToThread(do_check)
        d.addErrback(check_err)

    def _try_autostart_eip(self):
        """
        Tries to autostart EIP
        """
        settings = self._settings

        if not self._update_eip_enabled_status():
            return

        default_provider = settings.get_defaultprovider()
        self._enabled_services = settings.get_enabled_services(
            default_provider)

        loaded = self._provisional_provider_config.load(
            provider.get_provider_path(default_provider))
        if loaded:
            # XXX I think we should not try to re-download config every time,
            # it adds some delay.
            # Maybe if it's the first run in a session,
            # or we can try only if it fails.
            self._maybe_start_eip()
        else:
            # XXX: Display a proper message to the user
            self.eip_needs_login.emit()
            logger.error("Unable to load %s config, cannot autostart." %
                         (default_provider,))

    @QtCore.Slot()
    def _start_EIP(self):
        """
        Starts EIP
        """
        self._eip_status.eip_pre_up()
        self.user_stopped_eip = False

        # Until we set an option in the preferences window, we'll assume that
        # by default we try to autostart. If we switch it off manually, it
        # won't try the next time.
        self._settings.set_autostart_eip(True)

        self._backend.start_eip()

    @QtCore.Slot()
    def _on_eip_connection_aborted(self):
        """
        TRIGGERS:
            Signaler.eip_connection_aborted
        """
        logger.error("Tried to start EIP but cannot find any "
                     "available provider!")

        eip_status_label = self.tr("Could not load {0} configuration.")
        eip_status_label = eip_status_label.format(self._eip_name)
        self._eip_status.set_eip_status(eip_status_label, error=True)

        # signal connection_aborted to state machine:
        qtsigs = self._eip_connection.qtsigs
        qtsigs.connection_aborted_signal.emit()

    def _on_eip_openvpn_already_running(self):
        self._eip_status.set_eip_status(
            self.tr("Another openvpn instance is already running, and "
                    "could not be stopped."),
            error=True)
        self._set_eipstatus_off()

    def _on_eip_alien_openvpn_already_running(self):
        self._eip_status.set_eip_status(
            self.tr("Another openvpn instance is already running, and "
                    "could not be stopped because it was not launched by "
                    "Bitmask. Please stop it and try again."),
            error=True)
        self._set_eipstatus_off()

    def _on_eip_openvpn_not_found_error(self):
        self._eip_status.set_eip_status(
            self.tr("We could not find openvpn binary."),
            error=True)
        self._set_eipstatus_off()

    def _on_eip_vpn_launcher_exception(self):
        # XXX We should implement again translatable exceptions so
        # we can pass a translatable string to the panel (usermessage attr)
        self._eip_status.set_eip_status("VPN Launcher error.", error=True)
        self._set_eipstatus_off()

    def _on_eip_no_polkit_agent_error(self):
        self._eip_status.set_eip_status(
            # XXX this should change to polkit-kde where
            # applicable.
            self.tr("We could not find any authentication agent in your "
                    "system.<br/>Make sure you have"
                    "<b>polkit-gnome-authentication-agent-1</b> running and"
                    "try again."),
            error=True)
        self._set_eipstatus_off()

    def _on_eip_no_pkexec_error(self):
        self._eip_status.set_eip_status(
            self.tr("We could not find <b>pkexec</b> in your system."),
            error=True)
        self._set_eipstatus_off()

    def _on_eip_no_tun_kext_error(self):
        self._eip_status.set_eip_status(
            self.tr("{0} cannot be started because the tuntap extension is "
                    "not installed properly in your "
                    "system.").format(self._eip_name))
        self._set_eipstatus_off()

    @QtCore.Slot()
    def _stop_eip(self):
        """
        TRIGGERS:
          self._eip_connection.qtsigs.do_disconnect_signal (via state machine)

        Stops vpn process and makes gui adjustments to reflect
        the change of state.

        :param abnormal: whether this was an abnormal termination.
        :type abnormal: bool
        """
        self.user_stopped_eip = True
        self._backend.stop_eip()

        self._set_eipstatus_off(False)
        self._already_started_eip = False

        logger.debug('Setting autostart to: False')
        self._settings.set_autostart_eip(False)

        if self._logged_user:
            self._eip_status.set_provider(
                make_address(
                    self._logged_user,
                    self._get_best_provider_config().get_domain()))
        self._eip_status.eip_stopped()

    @QtCore.Slot()
    def _on_eip_network_unreachable(self):
        # XXX Should move to EIP Conductor
        """
        TRIGGERS:
            self._eip_connection.qtsigs.network_unreachable

        Displays a "network unreachable" error in the EIP status panel.
        """
        self._eip_status.set_eip_status(self.tr("Network is unreachable"),
                                        error=True)
        self._eip_status.set_eip_status_icon("error")

    @QtCore.Slot()
    def _do_eip_restart(self):
        # XXX Should move to EIP Conductor
        """
        TRIGGERS:
            self._eip_connection.qtsigs.process_restart

        Restart the connection.
        """
        # for some reason, emitting the do_disconnect/do_connect
        # signals hangs the UI.
        self._stop_eip()
        QtCore.QTimer.singleShot(2000, self._start_EIP)

    def _set_eipstatus_off(self, error=True):
        """
        Sets eip status to off
        """
        # XXX this should be handled by the state machine.
        self._eip_status.set_eip_status("", error=error)
        self._eip_status.set_eip_status_icon("error")

    @QtCore.Slot(int)
    def _eip_finished(self, exitCode):
        """
        TRIGGERS:
            Signaler.eip_process_finished

        Triggered when the EIP/VPN process finishes to set the UI
        accordingly.

        Ideally we would have the right exit code here,
        but the use of different wrappers (pkexec, cocoasudo) swallows
        the openvpn exit code so we get zero exit in some cases  where we
        shouldn't. As a workaround we just use a flag to indicate
        a purposeful switch off, and mark everything else as unexpected.

        In the near future we should trigger a native notification from here,
        since the user really really wants to know she is unprotected asap.
        And the right thing to do will be to fail-close.

        :param exitCode: the exit code of the eip process.
        :type exitCode: int
        """
        # TODO move to EIPConductor.
        # TODO Add error catching to the openvpn log observer
        # so we can have a more precise idea of which type
        # of error did we have (server side, local problem, etc)

        logger.info("VPN process finished with exitCode %s..."
                    % (exitCode,))

        qtsigs = self._eip_connection.qtsigs
        signal = qtsigs.disconnected_signal

        # XXX check if these exitCodes are pkexec/cocoasudo specific
        if exitCode in (126, 127):
            eip_status_label = self.tr(
                "{0} could not be launched "
                "because you did not authenticate properly.")
            eip_status_label = eip_status_label.format(self._eip_name)
            self._eip_status.set_eip_status(eip_status_label, error=True)
            signal = qtsigs.connection_aborted_signal
            self._backend.terminate_eip()

        elif exitCode != 0 or not self.user_stopped_eip:
            eip_status_label = self.tr("{0} finished in an unexpected manner!")
            eip_status_label = eip_status_label.format(self._eip_name)
            self._eip_status.eip_stopped()
            self._eip_status.set_eip_status_icon("error")
            self._eip_status.set_eip_status(eip_status_label, error=True)
            signal = qtsigs.connection_died_signal

        if exitCode == 0 and IS_MAC:
            # XXX remove this warning after I fix cocoasudo.
            logger.warning("The above exit code MIGHT BE WRONG.")

        # We emit signals to trigger transitions in the state machine:
        signal.emit()

    # eip boostrapping, config etc...

    def _maybe_start_eip(self):
        """
        Start the EIP bootstrapping sequence if the client is configured to
        do so.
        """
        if self._provides_eip_and_enabled() and not self._already_started_eip:
            # XXX this should be handled by the state machine.
            self._eip_status.set_eip_status(
                self.tr("Starting..."))

            domain = self._login_widget.get_selected_provider()
            self._backend.setup_eip(domain)

            self._already_started_eip = True
            # we want to start soledad anyway after a certain timeout if eip
            # fails to come up
            QtCore.QTimer.singleShot(
                self.EIP_TIMEOUT,
                self._maybe_run_soledad_setup_checks)
        else:
            if not self._already_started_eip:
                if EIP_SERVICE in self._enabled_services:
                    self._eip_status.set_eip_status(
                        self.tr("Not supported"),
                        error=True)
                else:
                    self._eip_status.disable_eip_start()
                    self._eip_status.set_eip_status(self.tr("Disabled"))
            # eip will not start, so we start soledad anyway
            self._maybe_run_soledad_setup_checks()

    @QtCore.Slot(dict)
    def _finish_eip_bootstrap(self, data):
        """
        TRIGGERS:
            self._backend.signaler.eip_client_certificate_ready

        Starts the VPN thread if the eip configuration is properly
        loaded
        """
        passed = data[self._backend.PASSED_KEY]

        if not passed:
            error_msg = self.tr("There was a problem with the provider")
            self._eip_status.set_eip_status(error_msg, error=True)
            logger.error(data[self._backend.ERROR_KEY])
            self._already_started_eip = False
            return

        # DO START EIP Connection!
        self._eip_connection.qtsigs.do_connect_signal.emit()

    @QtCore.Slot(dict)
    def _eip_intermediate_stage(self, data):
        # TODO missing param
        """
        TRIGGERS:
            self._backend.signaler.eip_config_ready

        If there was a problem, displays it, otherwise it does nothing.
        This is used for intermediate bootstrapping stages, in case
        they fail.
        """
        passed = data[self._backend.PASSED_KEY]
        if not passed:
            self._login_widget.set_status(
                self.tr("Unable to connect: Problem with provider"))
            logger.error(data[self._backend.ERROR_KEY])
            self._already_started_eip = False

    # end of EIP methods ---------------------------------------------

    def _get_best_provider_config(self):
        """
        Returns the best ProviderConfig to use at a moment. We may
        have to use self._provider_config or
        self._provisional_provider_config depending on the start
        status.

        :rtype: ProviderConfig
        """
        # TODO move this out of gui.
        leap_assert(self._provider_config is not None or
                    self._provisional_provider_config is not None,
                    "We need a provider config")

        provider_config = None
        if self._provider_config.loaded():
            provider_config = self._provider_config
        elif self._provisional_provider_config.loaded():
            provider_config = self._provisional_provider_config
        else:
            leap_assert(False, "We could not find any usable ProviderConfig.")

        return provider_config

    @QtCore.Slot()
    def _logout(self):
        """
        TRIGGERS:
            self._login_widget.logout

        Starts the logout sequence
        """
        setProxiedObject(self._soledad, None)

        self._cancel_ongoing_defers()

        # reset soledad status flag
        self._already_started_soledad = False

        # XXX: If other defers are doing authenticated stuff, this
        # might conflict with those. CHECK!
        self._backend.logout()
        self.logout.emit()

    @QtCore.Slot()
    def _logout_error(self):
        """
        TRIGGER:
            self._srp_auth.logout_error

        Inform the user about a logout error.
        """
        self._login_widget.done_logout()
        self.ui.lblLoginProvider.setText(self.tr("Login"))
        self._login_widget.set_status(
            self.tr("Something went wrong with the logout."))

    @QtCore.Slot()
    def _logout_ok(self):
        """
        TRIGGER:
            self._srp_auth.logout_ok

        Switches the stackedWidget back to the login stage after
        logging out
        """
        self._login_widget.done_logout()
        self.ui.lblLoginProvider.setText(self.tr("Login"))

        self._logged_user = None
        self._login_widget.logged_out()
        self._mail_status.mail_state_disabled()

        self._show_hide_unsupported_services()

    @QtCore.Slot(dict)
    def _intermediate_stage(self, data):
        # TODO this method name is confusing as hell.
        """
        TRIGGERS:
            self._backend.signaler.prov_name_resolution
            self._backend.signaler.prov_https_connection
            self._backend.signaler.prov_download_ca_cert

        If there was a problem, displays it, otherwise it does nothing.
        This is used for intermediate bootstrapping stages, in case
        they fail.
        """
        passed = data[self._backend.PASSED_KEY]
        if not passed:
            logger.error(data[self._backend.ERROR_KEY])
            self._login_problem_provider()

    #
    # window handling methods
    #

    def _on_raise_window_event(self, req):
        """
        Callback for the raise window event
        """
        if IS_WIN:
            raise_window_ack()
        self.raise_window.emit()

    @QtCore.Slot()
    def _do_raise_mainwindow(self):
        """
        TRIGGERS:
            self._on_raise_window_event

        Triggered when we receive a RAISE_WINDOW event.
        """
        TOPFLAG = QtCore.Qt.WindowStaysOnTopHint
        self.setWindowFlags(self.windowFlags() | TOPFLAG)
        self.show()
        self.setWindowFlags(self.windowFlags() & ~TOPFLAG)
        self.show()
        if IS_MAC:
            self.raise_()

    #
    # cleanup and quit methods
    #

    def _cleanup_pidfiles(self):
        """
        Removes lockfiles on a clean shutdown.

        Triggered after aboutToQuit signal.
        """
        if IS_WIN:
            WindowsLock.release_all_locks()

    def _cleanup_and_quit(self):
        """
        Call all the cleanup actions in a serialized way.
        Should be called from the quit function.
        """
        logger.debug('About to quit, doing cleanup...')

        self._stop_imap_service()

        if self._logged_user is not None:
            self._backend.logout()

        if self._soledad_bootstrapper.soledad is not None:
            logger.debug("Closing soledad...")
            self._soledad_bootstrapper.soledad.close()
        else:
            logger.error("No instance of soledad was found.")

        logger.debug('Terminating vpn')
        self._backend.stop_eip(shutdown=True)

        self._cancel_ongoing_defers()

        # TODO missing any more cancels?

        logger.debug('Cleaning pidfiles')
        self._cleanup_pidfiles()

    def quit(self):
        """
        Cleanup and tidely close the main window before quitting.
        """
        # TODO separate the shutting down of services from the
        # UI stuff.

        # first thing to do quitting, hide the mainwindow and show tooltip.
        self.hide()
        if self._systray is not None:
            self._systray.showMessage(
                self.tr('Quitting...'),
                self.tr('The app is quitting, please wait.'))

        # explicitly process events to display tooltip immediately
        QtCore.QCoreApplication.processEvents()

        # Set this in case that the app is hidden
        QtGui.QApplication.setQuitOnLastWindowClosed(True)

        self._cleanup_and_quit()

        # We queue the call to stop since we need to wait until EIP is stopped.
        # Otherwise we may exit leaving an unmanaged openvpn process.
        reactor.callLater(0, self._backend.stop)
        self._really_quit = True

        if self._wizard:
            self._wizard.close()

        if self._logger_window:
            self._logger_window.close()

        self.close()

        if self._quit_callback:
            self._quit_callback()

        logger.debug('Bye.')
