#!/usr/bin/env python3
#
# Electron Cash - a lightweight Bitcoin Cash client
# CashFusion - an advanced coin anonymizer
#
# Copyright (C) 2020 Mark B. Lundeberg
# Copyright (C) 2020 Calin A. Culianu
#
# Permission is hereby granted, free of charge, to any person
# obtaining a copy of this software and associated documentation files
# (the "Software"), to deal in the Software without restriction,
# including without limitation the rights to use, copy, modify, merge,
# publish, distribute, sublicense, and/or sell copies of the Software,
# and to permit persons to whom the Software is furnished to do so,
# subject to the following conditions:
#
# The above copyright notice and this permission notice shall be
# included in all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
# EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
# MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
# NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS
# BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN
# ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN
# CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
import threading
import time
import weakref

from functools import partial

from PyQt5.QtCore import *
from PyQt5.QtGui import *
from PyQt5.QtWidgets import *

from electroncash.i18n import _, ngettext, pgettext
from electroncash.plugins import hook, run_hook
from electroncash.util import (
    do_in_main_thread, finalization_print_error, format_satoshis_plain,
    InvalidPassword, print_error, PrintError, profiler)
from electroncash.wallet import Abstract_Wallet
from electroncash_gui.qt.amountedit import BTCAmountEdit
from electroncash_gui.qt.main_window import ElectrumWindow, StatusBarButton
from electroncash_gui.qt.popup_widget import ShowPopupLabel, KillPopupLabel
from electroncash_gui.qt.util import (
    Buttons, CancelButton, CloseButton, ColorScheme, EnterButton, HelpLabel,
    OkButton, WaitingDialog, WindowModalDialog)
from electroncash_gui.qt.utils import PortValidator, UserPortValidator

from .fusion import can_fuse_from, can_fuse_to, DEFAULT_SELF_FUSE
from .server import FusionServer, Params
from .plugin import (
    FusionPlugin, TOR_PORTS, is_tor_port, server_list, get_upnp,
    DEFAULT_SELECTOR, COIN_FRACTION_FUDGE_FACTOR, select_coins,
    DEFAULT_QUEUED_AUTOFUSE, DEFAULT_AUTOFUSE_CONFIRMED_ONLY)

from pathlib import Path
heredir = Path(__file__).parent
icon_fusion_logo = QIcon(str(heredir / 'Cash Fusion Logo - No Text.svg'))
icon_fusion_logo_gray = QIcon(str(heredir / 'Cash Fusion Logo - No Text Gray.svg'))
image_red_exclamation = QImage(str(heredir / 'red_exclamation.png'))

class Plugin(FusionPlugin, QObject):
    server_status_changed_signal = pyqtSignal(bool, tuple)

    fusions_win = None
    weak_settings_tab = None
    gui = None
    initted = False
    last_server_status = (True, ("Ok", ''))

    def __init__(self, *args, **kwargs):
        QObject.__init__(self)  # parentless top-level QObject. We need this type for the signal.
        FusionPlugin.__init__(self, *args, **kwargs) # gives us self.config
        self.widgets = weakref.WeakSet() # widgets we made, that need to be hidden & deleted when plugin is disabled

    def on_close(self):
        super().on_close()
        # Shut down plugin.
        # This can be triggered from one wallet's window while
        # other wallets' windows have plugin-related modals open.
        # TODO: disable auto-fusing
        for window in self.gui.windows:
            # this could be slow since it touches windows one by one... could optimize this by dispatching simultaneously.
            self.on_close_window(window)
        # Clean up
        for w in self.widgets:
            try:
                w.setParent(None)
                w.close()
                w.hide()
                w.deleteLater()
            except Exception:
                # could be <RuntimeError: wrapped C/C++ object of type SettingsDialog has been deleted> but really we just want to suppress all exceptions
                pass
        # clean up member attributes to be tidy
        self.fusions_win = None  # should trigger a deletion of object if not already dead
        self.weak_settings_tab = None
        self.gui = None
        self.initted = False

    @hook
    def init_qt(self, gui):
        # This gets called when this plugin is initialized, but also when
        # any other plugin is initialized after us.
        if self.initted:
            return
        self.initted = self.active = True  # self.active is declared in super
        self.gui = gui
        if self.gui.nd:
            # since a network dialog already exists, let's create the settings
            # tab now.
            self.on_network_dialog(self.gui.nd)

        # We also have to find which windows are already open, and make
        # them work with fusion.
        for window in self.gui.windows:
            self.on_new_window(window)

    @hook
    def address_list_context_menu_setup(self, address_list, menu, addrs):
        if not self.active:
            return
        wallet = address_list.wallet
        window = address_list.parent
        network = wallet.network
        if not (can_fuse_from(wallet) and can_fuse_to(wallet) and network):
            return
        if not hasattr(wallet, '_fusions'):
            # that's a bug... all wallets should have this
            return

        coins = wallet.get_utxos(addrs, exclude_frozen=True, mature=True, confirmed_only=True, exclude_slp=True)

        def start_fusion():
            def do_it(password):
                try:
                    with wallet.lock:
                        if not hasattr(wallet, '_fusions'):
                            return
                        fusion = self.start_fusion(wallet, password, coins)
                except RuntimeError as e:
                    window.show_error(_('CashFusion failed: {error_message}').format(error_message=str(e)))
                    return
                window.show_message(ngettext("One coin has been sent to CashFusion for fusing.",
                                             "{count} coins have been sent to CashFusion for fusing.",
                                             len(coins)).format(count=len(coins)))

            has_pw, password = Plugin.get_cached_pw(wallet)
            if has_pw and password is None:
                d = PasswordDialog(wallet, _("Enter your password to fuse these coins"), do_it)
                d.show()
                self.widgets.add(d)
            else:
                do_it(password)

        if coins:
            menu.addAction(ngettext("Input one coin to CashFusion", "Input {count} coins to CashFusion", len(coins)).format(count = len(coins)),
                           start_fusion)

    @hook
    def on_new_window(self, window):
        # Called on initial plugin load (if enabled) and every new window; only once per window.
        wallet = window.wallet

        if not (can_fuse_from(wallet) and can_fuse_to(wallet)):
            # don't do anything with non-fusable wallets
            # (if inter-wallet fusing is added, this should change.)
            return

        want_autofuse = wallet.storage.get('cashfusion_autofuse', False)
        self.add_wallet(wallet, window.gui_object.get_cached_password(wallet))

        # bit of a dirty hack, to insert our status bar icon (always using index 4, should put us just after the password-changer icon)
        sb = window.statusBar()
        sbbtn = FusionButton(self, wallet)
        self.server_status_changed_signal.connect(sbbtn.update_server_error)
        sb.insertPermanentWidget(4, sbbtn)
        self.widgets.add(sbbtn)
        window._cashfusion_button = weakref.ref(sbbtn)

        # prompt for password if auto-fuse was enabled
        if want_autofuse and not self.is_autofusing(wallet):
            def callback(password):
                self.enable_autofusing(wallet, password)
                button = window._cashfusion_button()
                if button: button.update_state()
            d = PasswordDialog(wallet, _("Previously you had auto-fusion enabled on this wallet. If you would like to keep auto-fusing in the background, enter your password."),
                               callback_ok = callback)
            d.show()
            self.widgets.add(d)

        # this runs at most once and only if absolutely no Tor ports are found
        self._maybe_prompt_user_if_they_want_integrated_tor_if_no_tor_found()

    @hook
    def on_close_window(self, window):
        # Invoked when closing wallet or entire application
        # Also called by on_close, above.
        wallet = window.wallet

        fusions = self.remove_wallet(wallet)
        if not fusions:
            return

        for f in fusions:
            f.stop('Closing wallet')

        # Soft-stop background fuse if running.
        # We avoid doing a hard disconnect in the middle of a fusion round.
        def task():
            for f in fusions:
                f.join()
        d = WaitingDialog(window.top_level_window(), _('Shutting down active CashFusions (may take a minute to finish)'), task)
        d.exec_()

    @hook
    def on_new_password(self, window, old, new):
        wallet = window.wallet
        if self.is_autofusing(wallet):
            try:
                self.enable_autofusing(wallet, new)
                self.print_error(wallet, "updated autofusion password")
            except InvalidPassword:
                self.disable_autofusing(wallet)
                self.print_error(wallet, "disabled autofusion due to incorrect password - BUG")

    def show_util_window(self, ):
        if self.fusions_win is None:
            # keep a singleton around
            self.fusions_win = FusionsWindow(self)
            self.widgets.add(self.fusions_win)
        self.fusions_win.show()
        self.fusions_win.raise_()

    def requires_settings(self):
        # called from main_window.py internal_plugins_dialog
        return True
    def settings_widget(self, window):
        # called from main_window.py internal_plugins_dialog
        btn = QPushButton(_('Settings'))
        btn.clicked.connect(self.show_settings_dialog)
        return btn

    def show_settings_dialog(self):
        self.gui.show_network_dialog(None, jumpto='fusion')

    @hook
    def on_network_dialog(self, network_dialog):
        if self.weak_settings_tab and self.weak_settings_tab():
            return  # already exists
        settings_tab = SettingsWidget(self, self.config)
        self.server_status_changed_signal.connect(settings_tab.update_server_error)
        tabs = network_dialog.nlayout.tabs
        tabs.addTab(settings_tab, icon_fusion_logo, _('CashFusion'))
        self.widgets.add(settings_tab)
        self.weak_settings_tab = weakref.ref(settings_tab)

    @hook
    def on_network_dialog_jumpto(self, nlayout, location):
        settings_tab = self.weak_settings_tab and self.weak_settings_tab()
        if settings_tab and location in ('fusion', 'cashfusion'):
            nlayout.tabs.setCurrentWidget(settings_tab)
            return True

    def update_coins_ui(self, wallet):
        ''' Overrides super, the Fusion thread calls this in its thread context
        to indicate it froze/unfroze some coins. We must update the coins tab,
        but only in the main thread.'''
        def update_coins_tab(wallet):
            strong_window = wallet and wallet.weak_window and wallet.weak_window()
            if strong_window:
                strong_window.utxo_list.update()  # this is rate_limited so it's ok to call it many times in rapid succession.

        do_in_main_thread(update_coins_tab, wallet)

    def notify_server_status(self, b, tup):
        ''' Reimplemented from super '''
        super().notify_server_status(b, tup)
        status_tup = (b, tup)
        if self.last_server_status != status_tup:
            self.last_server_status = status_tup
            self.server_status_changed_signal.emit(b, tup)

    def get_server_error(self) -> tuple:
        ''' Returns a 2-tuple of strings for the last server error, or None
        if there is no extant server error. '''
        if not self.last_server_status[0]:
            return self.last_server_status[1]

    @classmethod
    def window_for_wallet(cls, wallet):
        ''' Convenience: Given a wallet instance, derefernces the weak_window
        attribute of the wallet and returns a strong reference to the window.
        May return None if the window is gone (deallocated).  '''
        assert isinstance(wallet, Abstract_Wallet)
        return (wallet.weak_window and wallet.weak_window()) or None

    @classmethod
    def get_suitable_dialog_window_parent(cls, wallet_or_window):
        ''' Convenience: Given a wallet or a window instance, return a suitable
        'top level window' parent to use for dialog boxes. '''
        if isinstance(wallet_or_window, Abstract_Wallet):
            wallet = wallet_or_window
            window = cls.window_for_wallet(wallet)
            return (window and window.top_level_window()) or None
        elif isinstance(wallet_or_window, ElectrumWindow):
            window = wallet_or_window
            return window.top_level_window()
        else:
            raise TypeError(f"Expected a wallet or a window instance, instead got {type(wallet_or_window)}")

    @classmethod
    def get_cached_pw(cls, wallet):
        ''' Will return a tuple: (bool, password) for the given wallet.  The
        boolean is whether the wallet is password protected and the second
        item is the cached password, if it's known, otherwise None if it is not
        known.  If the wallet has no password protection the tuple is always
        (False, None). '''
        if not wallet.has_password():
            return False, None
        window = cls.window_for_wallet(wallet)
        if not window:
            raise RuntimeError(f'Wallet {wallet.diagnostic_name()} lacks a valid ElectrumWindow instance!')
        pw = window.gui_object.get_cached_password(wallet)
        if pw is not None:
            try:
                wallet.check_password(pw)
            except InvalidPassword:
                pw = None
        return True, pw

    @classmethod
    def cache_pw(cls, wallet, password):
        window = cls.window_for_wallet(wallet)
        if window:
            window.gui_object.cache_password(wallet, password)

    _integrated_tor_asked = [False, None]
    def _maybe_prompt_user_if_they_want_integrated_tor_if_no_tor_found(self):
        if any(self._integrated_tor_asked):
            # timer already active or already prompted user
            return
        weak_self = weakref.ref(self)
        def chk_tor_ok():
            self = weak_self()
            if not self:
                return
            self._integrated_tor_asked[1] = None  # kill QTimer reference
            if self.active and self.gui and self.gui.windows and self.tor_port_good is None and not self._integrated_tor_asked[0]:
                # prompt user to enable automatic Tor if not enabled and no auto-detected Tor ports were found
                window = self.gui.windows[-1]
                network = self.gui.daemon.network
                if network and not network.tor_controller.is_enabled():
                    self._integrated_tor_asked[0] = True
                    icon_pm = icon_fusion_logo.pixmap(32)
                    answer = window.question(
                        _('CashFusion requires Tor to operate anonymously. Would'
                          ' you like to enable the integrated Tor client now?'),
                        icon = icon_pm,
                        title = _("Tor Required"),
                        parent = None,
                        app_modal = True,
                        rich_text = True,
                        defaultButton = QMessageBox.Yes
                    )
                    if answer:
                        def on_status(controller):
                            try: network.tor_controller.status_changed.remove(on_status)  # remove the callback immediately
                            except ValueError: pass
                            if controller.status == controller.Status.STARTED:
                                buttons = [ _('Settings...'), _('Ok') ]
                                index = window.show_message(
                                    _('The integrated Tor client has been successfully started.'),
                                    detail_text = (
                                        _("The integrated Tor client can be stopped at any time from the Network Settings -> Proxy Tab"
                                          ", however CashFusion does require Tor in order to operate correctly.")
                                    ),
                                    icon = icon_pm,
                                    rich_text = True,
                                    buttons = buttons,
                                    defaultButton = buttons[1],
                                    escapeButton = buttons[1]
                                )
                                if index == 0:
                                    # They want to go to "Settings..." so send
                                    # them to the Tor settings (in Proxy tab)
                                    self.gui.show_network_dialog(window, jumpto='tor')
                            else:
                                controller.set_enabled(False)  # latch it back to False so we may prompt them again in the future
                                window.show_error(_('There was an error starting the integrated Tor client'))
                        network.tor_controller.status_changed.append(on_status)
                        network.tor_controller.set_enabled(True)
        self._integrated_tor_asked[1] = t = QTimer()
        # if in 5 seconds no tor port, ask user if they want to enable the integrated Tor
        t.timeout.connect(chk_tor_ok)
        t.setSingleShot(True)
        t.start(5000)


class PasswordDialog(WindowModalDialog):
    """ Slightly fancier password dialog -- can be used non-modal (asynchronous) and has internal password checking.
    To run non-modally, use .show with the callbacks; to run modally, use .run. """
    def __init__(self, wallet, message, callback_ok = None, callback_cancel = None):
        parent = Plugin.get_suitable_dialog_window_parent(wallet)
        super().__init__(parent=parent, title=_("Enter Password"))
        self.setWindowIcon(icon_fusion_logo)
        self.wallet = wallet
        self.callback_ok = callback_ok
        self.callback_cancel = callback_cancel
        self.password = None

        vbox = QVBoxLayout()
        self.setLayout(vbox)
        self.msglabel = QLabel(message)
        self.msglabel.setWordWrap(True)
        self.msglabel.setMinimumWidth(250)
        self.msglabel.setSizePolicy(QSizePolicy.MinimumExpanding, QSizePolicy.Expanding)
        hbox = QHBoxLayout()
        iconlabel = QLabel(); iconlabel.setPixmap(icon_fusion_logo.pixmap(32))
        hbox.addWidget(iconlabel)
        hbox.addWidget(self.msglabel, 1, Qt.AlignLeft|Qt.AlignVCenter)
        cmargins = hbox.contentsMargins(); cmargins.setBottom(10); hbox.setContentsMargins(cmargins)  # pad the bottom a bit
        vbox.addLayout(hbox, 1)
        self.pwle = QLineEdit()
        self.pwle.setEchoMode(2)
        grid_for_hook_api = QGridLayout()
        grid_for_hook_api.setContentsMargins(0,0,0,0)
        grid_for_hook_api.addWidget(self.pwle, 0, 0)
        run_hook('password_dialog', self.pwle, grid_for_hook_api, 0)  # this is for the virtual keyboard plugin
        vbox.addLayout(grid_for_hook_api)
        self.badpass_msg = "<i>" + _("Incorrect password entered. Please try again.") + "</i>"

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        buttons.addWidget(CancelButton(self))
        okbutton = OkButton(self)
        okbutton.clicked.disconnect()
        okbutton.clicked.connect(self.pw_entered)
        buttons.addWidget(okbutton)
        vbox.addLayout(buttons)

    def _on_pw_ok(self, password):
        self.password = password
        Plugin.cache_pw(self.wallet, password)  # to remember it for a time so as to not keep bugging the user
        self.accept()
        if self.callback_ok:
            self.callback_ok(password)

    def _chk_pass(self, password):
        pw_ok = not self.wallet.has_password()
        if not pw_ok:
            try:
                self.wallet.check_password(password)
                pw_ok = True
            except InvalidPassword:
                pass
        return pw_ok

    def pw_entered(self, ):
        password = self.pwle.text()
        if self._chk_pass(password):
            self._on_pw_ok(password)
        else:
            self.msglabel.setText(self.badpass_msg)
            self.pwle.clear()
            self.pwle.setFocus()

    def closeEvent(self, event):
        ''' This happens if .run() is called, then dialog is closed. '''
        super().closeEvent(event)
        if event.isAccepted():
            self._close_hide_common()

    def hideEvent(self, event):
        ''' This happens if .show() is called, then dialog is closed. '''
        super().hideEvent(event)
        if event.isAccepted():
            self._close_hide_common()

    def _close_hide_common(self):
        if not self.result() and self.callback_cancel:
            self.callback_cancel(self)
        self.setParent(None)
        self.deleteLater()

    def run(self):
        self.exec_()
        return self.password


class FusionButton(StatusBarButton):
    def __init__(self, plugin, wallet):
        super().__init__(QIcon(), 'Fusion', self.toggle_autofuse)

        self.plugin = plugin
        self.wallet = wallet

        self.server_error : tuple = None

        self.icon_autofusing_on = icon_fusion_logo
        self.icon_autofusing_off = icon_fusion_logo_gray
        self.icon_fusing_problem = self.style().standardIcon(QStyle.SP_MessageBoxWarning)

#        title = QWidgetAction(self)
#        title.setDefaultWidget(QLabel("<i>" + _("CashFusion") + "</i>"))
        self.action_toggle = QAction(_("Auto-fuse in background"))
        self.action_toggle.setCheckable(True)
        self.action_toggle.triggered.connect(self.toggle_autofuse)
        action_separator1 = QAction(self); action_separator1.setSeparator(True)
        action_wsettings = QAction(_("Wallet settings..."), self)
        action_wsettings.triggered.connect(self.show_wallet_settings)
        action_settings = QAction(_("CashFusion settings..."), self)
        action_settings.triggered.connect(self.plugin.show_settings_dialog)
        action_separator2 = QAction(self); action_separator2.setSeparator(True)
        action_util = QAction(_("Fusions..."), self)
        action_util.triggered.connect(self.plugin.show_util_window)

        self.addActions([self.action_toggle, action_separator1,
                         action_wsettings, action_settings,
                         action_separator2, action_util])

        self.setContextMenuPolicy(Qt.ActionsContextMenu)

        self.update_state()

    def update_state(self):
        autofuse = self.plugin.is_autofusing(self.wallet)
        self.action_toggle.setChecked(autofuse)
        if autofuse:
            self.setIcon(self.icon_autofusing_on)
            self.setToolTip(_('CashFusion is fusing in the background for this wallet'))
            self.setStatusTip(_('CashFusion Auto-fusion - Enabled'))
        else:
            self.setIcon(self.icon_autofusing_off)
            self.setToolTip(_('Auto-fusion is paused for this wallet (click to enable)'))
            self.setStatusTip(_('CashFusion Auto-fusion - Disabled (click to enable)'))
        if self.server_error:
            self.setToolTip(_('CashFusion') + ": " + ', '.join(self.server_error))
            self.setStatusTip(_('CashFusion') + ": " + ', '.join(self.server_error))

    def paintEvent(self, event):
        super().paintEvent(event)
        if event.isAccepted() and self.server_error:
            # draw error overlay if we are in an error state
            p = QPainter(self)
            try:
                p.setClipRegion(event.region())
                r = self.rect()
                r -= QMargins(4,6,4,6)
                r.moveCenter(r.center() + QPoint(4,4))
                p.drawImage(r, image_red_exclamation)
            finally:
                # paranoia. The above never raises but.. if it does.. PyQt will
                # crash hard if we don't end the QPainter properly before
                # returning.
                p.end()
                del p

    def toggle_autofuse(self):
        plugin = self.plugin
        autofuse = plugin.is_autofusing(self.wallet)
        if not autofuse:
            has_pw, password = Plugin.get_cached_pw(self.wallet)
            if has_pw and password is None:
                # Fixme: See if we can not use a blocking password dialog here.
                pd = PasswordDialog(self.wallet, _("To perform auto-fusion in the background, please enter your password."))
                self.plugin.widgets.add(pd)  # just in case this plugin is unloaded while this dialog is up
                password = pd.run()
                del pd
                if password is None or not plugin.active:  # must check plugin.active because user can theoretically kill plugin from another window while the above password dialog is up
                    return
            try:
                plugin.enable_autofusing(self.wallet, password)
            except InvalidPassword:
                ''' Somehow the password changed from underneath us. Silenty ignore. '''
        else:
            running = plugin.disable_autofusing(self.wallet)
            if running:
                res = QMessageBox.question(Plugin.get_suitable_dialog_window_parent(self.wallet),
                                           _("Disabling automatic Cash Fusions"),
                                           _("New automatic fusions will not be started, but you have {num} currently in progress."
                                             " Would you like to signal them to stop?").format(num=len(running)) )
                if res == QMessageBox.Yes:
                    for f in running:
                        f.stop('Stop requested by user')
        self.update_state()

    def show_wallet_settings(self):
        win = getattr(self.wallet, '_cashfusion_settings_window', None)
        if not win:
            win = WalletSettingsDialog(Plugin.get_suitable_dialog_window_parent(self.wallet),
                                       self.plugin, self.wallet)
            self.plugin.widgets.add(win)  # ensures if plugin is unloaded while dialog is up, that the dialog will be killed.
        win.show()
        win.raise_()

    def update_server_error(self):
        tup = self.plugin.get_server_error()
        changed = tup != self.server_error
        if not changed:
            return
        self.server_error = tup
        name = "CashFusionError;" + str(id(self))  # make sure name is unique per FusionButton widget
        if self.server_error:
            weak_plugin = weakref.ref(self.plugin)
            def onClick():
                KillPopupLabel(name)
                plugin = weak_plugin()
                if plugin:
                    plugin.show_settings_dialog()
            ShowPopupLabel(name = name,
                           text="<center><b>{}</b><br><small>{}</small></center>".format(_("Server Error"),_("Click here to resolve")),
                           target=self,
                           timeout=20000, onClick=onClick, onRightClick=onClick,
                           dark_mode = ColorScheme.dark_scheme)
        else:
            KillPopupLabel(name)

        self.update()  # causes a repaint

        window = self.wallet.weak_window and self.wallet.weak_window()
        if window:
            window.print_error("CashFusion server_error is now {}".format(self.server_error))
            oldTip = self.statusTip()
            self.update_state()
            newTip = self.statusTip()
            if newTip != oldTip:
                window.statusBar().showMessage(newTip, 7500)


class SettingsWidget(QWidget):
    torscanthread = None
    torscanthread_update = pyqtSignal(object)

    def __init__(self, plugin, config, parent=None):
        super().__init__(parent)
        self.plugin = plugin
        self.config = config
        self.torscanthread_ping = threading.Event()
        self.torscanthread_update.connect(self.torport_update)

        main_layout = QVBoxLayout()
        self.setLayout(main_layout)

        box = QGroupBox(_("Network"))
        main_layout.addWidget(box, 0, Qt.AlignTop | Qt.AlignHCenter)
        slayout = QVBoxLayout() ; box.setLayout(slayout)

        grid = QGridLayout() ; slayout.addLayout(grid)

        grid.addWidget(QLabel(_("Server")), 0, 0)
        hbox = QHBoxLayout(); grid.addLayout(hbox, 0, 1)
        self.combo_server_host = QComboBox()
        self.combo_server_host.setEditable(True)
        self.combo_server_host.setInsertPolicy(QComboBox.NoInsert)
        self.combo_server_host.setCompleter(None)
        self.combo_server_host.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.combo_server_host.activated.connect(self.combo_server_activated)
        self.combo_server_host.lineEdit().textEdited.connect(self.user_changed_server)
        self.combo_server_host.addItems([f'{s[0]} ({s[1]}{" - ssl" if s[2] else ""})' for s in server_list])
        hbox.addWidget(self.combo_server_host)
        hbox.addWidget(QLabel(_("P:")))
        self.le_server_port = QLineEdit()
        self.le_server_port.setMaximumWidth(50)
        self.le_server_port.setValidator(PortValidator(self.le_server_port))
        self.le_server_port.textEdited.connect(self.user_changed_server)

        hbox.addWidget(self.le_server_port)
        self.cb_server_ssl = QCheckBox(_('SSL'))
        self.cb_server_ssl.clicked.connect(self.user_changed_server)
        hbox.addWidget(self.cb_server_ssl)

        self.server_error_label = QLabel()
        self.server_error_label.setAlignment(Qt.AlignTop|Qt.AlignJustify)
        grid.addWidget(self.server_error_label, 1, 0, 1, -1)

        grid.addWidget(QLabel(_("Tor")), 2, 0)
        hbox = QHBoxLayout(); grid.addLayout(hbox, 2, 1)
        self.le_tor_host = QLineEdit('localhost')
        self.le_tor_host.textEdited.connect(self.user_edit_torhost)
        hbox.addWidget(self.le_tor_host)
        hbox.addWidget(QLabel(_("P:")))
        self.le_tor_port = QLineEdit()
        self.le_tor_port.setMaximumWidth(50)
        self.le_tor_port.setValidator(UserPortValidator(self.le_tor_port))
        self.le_tor_port.textEdited.connect(self.user_edit_torport)
        hbox.addWidget(self.le_tor_port)
        self.l_tor_status = QLabel()
        hbox.addWidget(self.l_tor_status)
        self.b_tor_refresh = QPushButton()
        self.b_tor_refresh.clicked.connect(self.torscanthread_ping.set)
        self.b_tor_refresh.setIcon(self.style().standardIcon(QStyle.SP_BrowserReload))
        self.b_tor_refresh.setDefault(False); self.b_tor_refresh.setAutoDefault(False)
        hbox.addWidget(self.b_tor_refresh)
        self.cb_tor_auto = QCheckBox(_('autodetect'))
        self.cb_tor_auto.clicked.connect(self.cb_tor_auto_clicked)
        hbox.addWidget(self.cb_tor_auto)

        btn = QPushButton(_("Fusions...")); btn.setDefault(False); btn.setAutoDefault(False)
        btn.clicked.connect(self.plugin.show_util_window)
        buts = Buttons(btn)
        buts.setAlignment(Qt.AlignRight | Qt.AlignTop)
        main_layout.addLayout(buts)

        main_layout.addStretch(1)
        self.stretch_item_index = main_layout.count()-1


        self.server_widget = ServerWidget(self.plugin)
        self.server_widget.layout().setContentsMargins(0,0,0,0)
        main_layout.addWidget(self.server_widget)
        self.timer_server_widget_visibility = QTimer(self.server_widget)
        self.timer_server_widget_visibility.setSingleShot(False)
        self.timer_server_widget_visibility.timeout.connect(self.update_server_widget_visibility)

        self.server_widget_index = main_layout.count()-1

        self.pm_good_proxy = QIcon(":icons/status_connected_proxy.svg").pixmap(24)
        self.pm_bad_proxy = QIcon(":icons/status_disconnected.svg").pixmap(24)

    def update_server(self):
        # called initially / when config changes
        host, port, ssl = self.plugin.get_server()
        try: # see if it's in default list, if so we can set it ...
            index = server_list.index((host,port,ssl))
        except ValueError: # not in list
            index = -1
        self.combo_server_host.setCurrentIndex(index)
        self.combo_server_host.setEditText(host)
        self.le_server_port.setText(str(port))
        self.cb_server_ssl.setChecked(ssl)

    def update_server_error(self):
        errtup = self.plugin.get_server_error()
        self.server_error_label.setHidden(errtup is None)
        if errtup:
            color = ColorScheme.RED.get_html()
            self.server_error_label.setText(f'<b>{errtup[0]}:</b> <font color="{color}"><i>{errtup[1]}</i></font>')


    def combo_server_activated(self, index):
        # only triggered when user selects a combo item
        self.plugin.set_server(*server_list[index])
        self.update_server()

    def user_changed_server(self, *args):
        # user edited the host / port / ssl
        host = self.combo_server_host.currentText()
        try:
            port = int(self.le_server_port.text())
        except ValueError:
            port = 0
        ssl = self.cb_server_ssl.isChecked()
        self.plugin.set_server(host, port, ssl)

    def update_tor(self,):
        # called on init an switch of auto
        autoport = self.plugin.has_auto_torport()
        host = self.plugin.get_torhost()
        port = self.plugin.get_torport()
        self.l_tor_status.clear()
        self.torport_update(port)
        self.cb_tor_auto.setChecked(autoport)
        self.le_tor_host.setEnabled(not autoport)
        self.le_tor_host.setText(str(host))
        self.le_tor_port.setEnabled(not autoport)
        if not autoport:
            self.le_tor_port.setText(str(port))

    def torport_update(self, goodport):
        # signalled from the tor checker thread
        autoport = self.plugin.has_auto_torport()
        port = self.plugin.get_torport()
        if autoport:
            sport = '?' if port is None else str(port)
            self.le_tor_port.setText(sport)
        if goodport is None:
            self.l_tor_status.setPixmap(self.pm_bad_proxy)
            if autoport:
                self.l_tor_status.setToolTip(_('Cannot find a Tor proxy on ports %(ports)s.')%dict(ports=TOR_PORTS))
            else:
                self.l_tor_status.setToolTip(_('Cannot find a Tor proxy on port %(port)d.')%dict(port=port))
        else:
            self.l_tor_status.setToolTip(_('Found a valid Tor proxy on this port.'))
            self.l_tor_status.setPixmap(self.pm_good_proxy)

    def user_edit_torhost(self, host):
        self.plugin.set_torhost(host)
        self.torscanthread_ping.set()

    def user_edit_torport(self, sport):
        try:
            port = int(sport)
        except ValueError:
            return
        self.plugin.set_torport(port)
        self.torscanthread_ping.set()

    def cb_tor_auto_clicked(self, state):
        self.plugin.set_torport('auto' if state else 'manual')
        port = self.plugin.get_torport()
        if port is not None:
            self.le_tor_port.setText(str(port))
        self.torscanthread_ping.set()
        self.update_tor()

    def refresh(self):
        self.update_server()
        self.update_tor()
        self.update_server_widget_visibility()
        self.update_server_error()

    def update_server_widget_visibility(self):
        if not self.server_widget.is_server_running():
            self.server_widget.setHidden(True)
            self.layout().setStretch(self.stretch_item_index, 1)
            self.layout().setStretch(self.server_widget_index, 0)
        else:
            self.server_widget.setHidden(False)
            self.layout().setStretch(self.stretch_item_index, 0)
            self.layout().setStretch(self.server_widget_index, 1)

    def showEvent(self, event):
        super().showEvent(event)
        if not event.isAccepted():
            return
        self.refresh()
        self.timer_server_widget_visibility.start(2000)
        if self.torscanthread is None:
            self.torscanthread = threading.Thread(name='Fusion-scan_torport_settings', target=self.scan_torport_loop)
            self.torscanthread.daemon = True
            self.torscanthread_stopping = False
            self.torscanthread.start()

    def _hide_close_common(self):
        self.timer_server_widget_visibility.stop()
        self.torscanthread_stopping = True
        self.torscanthread_ping.set()
        self.torscanthread = None

    def closeEvent(self, event):
        super().closeEvent(event)
        if not event.isAccepted():
            return
        self._hide_close_common()

    def hideEvent(self, event):
        super().hideEvent(event)
        if not event.isAccepted():
            return
        self._hide_close_common()

    def scan_torport_loop(self, ):
        while not self.torscanthread_stopping:
            goodport = self.plugin.scan_torport()
            self.torscanthread_update.emit(goodport)
            self.torscanthread_ping.wait(10)
            self.torscanthread_ping.clear()


class WalletSettingsDialog(WindowModalDialog):
    def __init__(self, parent, plugin, wallet):
        super().__init__(parent=parent, title=_("CashFusion - Wallet Settings"))
        self.setWindowIcon(icon_fusion_logo)
        self.plugin = plugin
        self.wallet = wallet

        assert not hasattr(self.wallet, '_cashfusion_settings_window')
        main_window = self.wallet.weak_window()
        assert main_window
        self.wallet._cashfusion_settings_window = self

        main_layout = QVBoxLayout()
        self.setLayout(main_layout)

        box = QGroupBox(_("Auto-fusion coin selection")) ; main_layout.addWidget(box)
        slayout = QVBoxLayout() ; box.setLayout(slayout)

        grid = QGridLayout() ; slayout.addLayout(grid)

        self.radio_select_size = QRadioButton(_("Target typical output amount"))
        grid.addWidget(self.radio_select_size, 0, 0)
        self.radio_select_fraction = QRadioButton(_("Per-coin random chance"))
        grid.addWidget(self.radio_select_fraction, 1, 0)
        self.radio_select_count = QRadioButton(_("Target number of coins in wallet"))
        grid.addWidget(self.radio_select_count, 2, 0)

        self.radio_select_size.clicked.connect(self.edited_size)
        self.radio_select_fraction.clicked.connect(self.edited_fraction)
        self.radio_select_count.clicked.connect(self.edited_count)

        self.amt_selector_size = BTCAmountEdit(main_window.get_decimal_point)
        grid.addWidget(self.amt_selector_size, 0, 1)
        self.sb_selector_fraction = QDoubleSpinBox()
        self.sb_selector_fraction.setRange(0.1, 100.)
        self.sb_selector_fraction.setSuffix("%")
        self.sb_selector_fraction.setDecimals(1)
        grid.addWidget(self.sb_selector_fraction, 1, 1)
        self.sb_selector_count = QSpinBox()
        self.sb_selector_count.setRange(COIN_FRACTION_FUDGE_FACTOR, 9999)  # Somewhat hardcoded limit of 9999 is arbitrary, have this come from constants?
        grid.addWidget(self.sb_selector_count, 2, 1)

        self.amt_selector_size.editingFinished.connect(self.edited_size)
        self.sb_selector_fraction.valueChanged.connect(self.edited_fraction)
        self.sb_selector_count.valueChanged.connect(self.edited_count)

        # Clicking the radio button should bring its corresponding widget buddy into focus
        self.radio_select_size.clicked.connect(self.amt_selector_size.setFocus)
        self.radio_select_fraction.clicked.connect(self.sb_selector_fraction.setFocus)
        self.radio_select_count.clicked.connect(self.sb_selector_count.setFocus)

        self.l_warn_selection = QLabel(_("Your target number of coins is low. In order to achieve the best consolidation, make sure that you have only 1 queued auto-fusion, and have 'self-fusing' set to 'No', and enable fusing only when all coins are confirmed."))
        self.l_warn_selection.setWordWrap(True)
        self.l_warn_selection.setAlignment(Qt.AlignJustify|Qt.AlignVCenter)
        qs = QSizePolicy()
        qs.setRetainSizeWhenHidden(True)
        self.l_warn_selection.setSizePolicy(qs)
        slayout.addWidget(self.l_warn_selection)
        slayout.setAlignment(self.l_warn_selection, Qt.AlignCenter)

        box = QGroupBox(_("Auto-fusion limits")) ; main_layout.addWidget(box)
        slayout = QVBoxLayout() ; box.setLayout(slayout)
        grid = QGridLayout() ; slayout.addLayout(grid)
        grid.addWidget(QLabel(_("Number of queued fusions")), 0, 0)
        self.sb_queued_autofuse = QSpinBox()
        self.sb_queued_autofuse.setRange(1, 10)  # hard-coded rande 1-10, maybe have this come from some constants?
        self.sb_queued_autofuse.setMinimumWidth(50)  # just so it doesn't end up too tiny
        grid.addWidget(self.sb_queued_autofuse, 0, 1)
        self.cb_autofuse_only_all_confirmed = QCheckBox(_("Only autofuse when all coins are confirmed"))
        slayout.addWidget(self.cb_autofuse_only_all_confirmed)
        grid.addWidget(QWidget(), 0, 2); grid.setColumnStretch(2, 1) # spacer

        self.sb_queued_autofuse.valueChanged.connect(self.edited_queued_autofuse)
        self.cb_autofuse_only_all_confirmed.clicked.connect(self.clicked_confirmed_only)

        box = QGroupBox(_("Self-fusing")) ; main_layout.addWidget(box)
        slayout = QVBoxLayout() ; box.setLayout(slayout)

        lbl = QLabel(_("Allow this wallet to participate multiply in the same fusion round?"))
        lbl.setWordWrap(True)
        slayout.addWidget(lbl)
        box = QHBoxLayout(); box.setContentsMargins(0,0,0,0)
        self.combo_self_fuse = QComboBox()
        self.combo_self_fuse.addItem(_('No'), 1)
        self.combo_self_fuse.addItem(_('Yes - as up to two players'), 2)
        box.addStretch(1)
        box.addWidget(self.combo_self_fuse)
        slayout.addLayout(box) ; del box

        self.combo_self_fuse.activated.connect(self.chose_self_fuse)

        cbut = CloseButton(self)
        main_layout.addLayout(Buttons(cbut))
        cbut.setDefault(False)
        cbut.setAutoDefault(False)

    def update(self):
        eligible, ineligible, sum_value, has_unconfirmed = select_coins(self.wallet)
        select_type, select_amount = self.wallet.storage.get('cashfusion_selector', DEFAULT_SELECTOR)

        edit_widgets = [self.amt_selector_size, self.sb_selector_fraction, self.sb_selector_count, self.sb_queued_autofuse]
        try:
            for w in edit_widgets:
                # Block spurious editingFinished signals and valueChanged signals as
                # we modify the state and focus of widgets programatically below.
                # On macOS not doing this led to a very strange/spazzy UI.
                w.blockSignals(True)
            self.amt_selector_size.setEnabled(select_type == 'size')
            self.sb_selector_count.setEnabled(select_type == 'count')
            self.sb_selector_fraction.setEnabled(select_type == 'fraction')
            if select_type == 'size':
                self.radio_select_size.setChecked(True)
                sel_size = select_amount
                if sum_value > 0:
                    sel_fraction = min(COIN_FRACTION_FUDGE_FACTOR * select_amount / sum_value, 1)
                else:
                    sel_fraction = 1.
            elif select_type == 'count':
                self.radio_select_count.setChecked(True)
                sel_size = max(sum_value / select_amount, 10000)
                sel_fraction = COIN_FRACTION_FUDGE_FACTOR / max(select_amount, 1)
            elif select_type == 'fraction':
                self.radio_select_fraction.setChecked(True)
                sel_size = max(sum_value * select_amount / COIN_FRACTION_FUDGE_FACTOR, 10000)
                sel_fraction = select_amount
            else:
                self.wallet.storage.put('cashfusion_selector', None)
                return self.update()
            sel_count = COIN_FRACTION_FUDGE_FACTOR / sel_fraction
            self.amt_selector_size.setAmount(round(sel_size))
            self.sb_selector_fraction.setValue(sel_fraction * 100.)
            self.sb_selector_count.setValue(sel_count)
            try: self.sb_queued_autofuse.setValue(int(self.wallet.storage.get('cashfusion_queued_autofuse', DEFAULT_QUEUED_AUTOFUSE)))
            except (TypeError, ValueError): pass  # should never happen but paranoia pays off in the long-term
        finally:
            # re-enable signals
            for w in edit_widgets: w.blockSignals(False)
        self.l_warn_selection.setVisible(sel_fraction > 0.2)

        self.cb_autofuse_only_all_confirmed.setChecked(self.wallet.storage.get('cashfusion_autofuse_only_when_all_confirmed', DEFAULT_AUTOFUSE_CONFIRMED_ONLY))

        self.combo_self_fuse.setCurrentIndex(self.wallet.storage.get('cashfusion_self_fuse_players', DEFAULT_SELF_FUSE) - 1)

    def edited_size(self,):
        size = self.amt_selector_size.get_amount()
        if size is None or size < 10000:
            size = 10000
        self.wallet.storage.put('cashfusion_selector', ('size', size))
        self.update()

    def edited_fraction(self,):
        fraction = self.sb_selector_fraction.value() / 100.
        fraction = max(0., min(fraction, 1.))
        self.wallet.storage.put('cashfusion_selector', ('fraction', round(fraction, 3)))
        self.update()

    def edited_count(self,):
        count = self.sb_selector_count.value()
        self.wallet.storage.put('cashfusion_selector', ('count', count))
        self.update()

    def edited_queued_autofuse(self,):
        prevval = self.wallet.storage.get('cashfusion_queued_autofuse', DEFAULT_QUEUED_AUTOFUSE)
        numfuse = self.sb_queued_autofuse.value()
        self.wallet.storage.put('cashfusion_queued_autofuse', numfuse)
        if prevval > numfuse:
            for f in self.wallet._fusions_auto:
                f.stop('User decreased queued-fuse limit', not_if_running = True)
        self.update()

    def clicked_confirmed_only(self, checked):
        self.wallet.storage.put('cashfusion_autofuse_only_when_all_confirmed', checked)
        self.update()

    def chose_self_fuse(self,):
        sel = self.combo_self_fuse.currentData()
        oldsel = self.wallet.storage.get('cashfusion_self_fuse_players', DEFAULT_SELF_FUSE)
        self.wallet.storage.put('cashfusion_self_fuse_players', sel)
        if oldsel != sel:
            for f in self.wallet._fusions:
                # we have to stop waiting fusions since the tags won't overlap.
                # otherwise, the user will end up self fusing way too much.
                f.stop('User changed self-fuse limit', not_if_running = True)
        self.update()

    def closeEvent(self, event):
        super().closeEvent(event)
        if event.isAccepted():
            self.setParent(None)
            del self.wallet._cashfusion_settings_window

    def showEvent(self, event):
        super().showEvent(event)
        if event.isAccepted():
            self.update()


class ServerFusionsBaseMixin:
    def __init__(self, plugin, refresh_interval=2000):
        assert isinstance(self, QWidget)
        self.plugin = plugin
        self.refresh_interval = refresh_interval

        self.timer_refresh = QTimer(self)
        self.timer_refresh.setSingleShot(False)
        self.timer_refresh.timeout.connect(self.refresh)

    def _on_show(self):
        self.timer_refresh.start(self.refresh_interval)
        self.refresh()

    def _on_hide(self):
        self.timer_refresh.stop()

    def showEvent(self, event):
        super().showEvent(event)
        if event.isAccepted():
            self._on_show()

    def hideEvent(self, event):
        super().hideEvent(event)
        if event.isAccepted():
            self._on_hide()

    def closeEvent(self, event):
        super().closeEvent(event)
        if event.isAccepted():
            self._on_hide()

    def refresh(self):
        raise NotImplementedError('ServerFusionsBaseMixin refresh() needs an implementation')


class ServerWidget(ServerFusionsBaseMixin, QWidget):
    def __init__(self, plugin, parent=None):
        QWidget.__init__(self, parent)
        ServerFusionsBaseMixin.__init__(self, plugin)

        main_layout = QVBoxLayout()
        self.setLayout(main_layout)

        self.serverbox = QGroupBox(_("Server"))
        main_layout.addWidget(self.serverbox)
        #self.serverbox.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Preferred)

        slayout = QVBoxLayout()
        self.serverbox.setLayout(slayout)

        self.l_server_status = QLabel()
        slayout.addWidget(self.l_server_status)

        self.t_server_waiting = QTableWidget()
        self.t_server_waiting.setColumnCount(3)
        self.t_server_waiting.setRowCount(len(Params.tiers))
        self.t_server_waiting.setHorizontalHeaderLabels([_('Tier (sats)'), _('Num players'), ''])
        for i, t in enumerate(Params.tiers):
            button = QPushButton(_("Start"))
            button.setDefault(False); button.setAutoDefault(False)  # on some platforms if we don't do this, one of the buttons traps "Enter" key
            button.clicked.connect(partial(self.clicked_start_fuse, t))
            self.t_server_waiting.setCellWidget(i, 2, button)
        slayout.addWidget(self.t_server_waiting)

    def sizeHint(self):
        return QSize(300, 150)

    def refresh(self):
        if self.is_server_running():
            self.t_server_waiting.setEnabled(True)
            self.l_server_status.setText(_('Server status: ACTIVE') + f' {self.plugin.fusion_server.host}:{self.plugin.fusion_server.port}')
            table = self.t_server_waiting
            table.setRowCount(len(self.plugin.fusion_server.waiting_pools))
            for i,(t,pool) in enumerate(self.plugin.fusion_server.waiting_pools.items()):
                table.setItem(i,0,QTableWidgetItem(str(t)))
                table.setItem(i,1,QTableWidgetItem(str(len(pool.pool))))
        else:
            self.t_server_waiting.setEnabled(False)
            self.l_server_status.setText(_('Server status: NOT RUNNING'))

    def is_server_running(self):
        return bool(self.plugin.fusion_server)

    def clicked_start_fuse(self, tier, event):
        if self.plugin.fusion_server is None:
            return
        self.plugin.fusion_server.start_fuse(tier)


class FusionsWindow(ServerFusionsBaseMixin, QDialog):
    def __init__(self, plugin):
        QDialog.__init__(self, parent=None)
        ServerFusionsBaseMixin.__init__(self, plugin, refresh_interval=1000)

        self.setWindowTitle(_("CashFusion - Fusions"))
        self.setWindowIcon(icon_fusion_logo)

        main_layout = QVBoxLayout()
        self.setLayout(main_layout)

        clientbox = QGroupBox(_("Fusions"))
        main_layout.addWidget(clientbox)

        clayout = QVBoxLayout()
        clientbox.setLayout(clayout)

        self.t_active_fusions = QTreeWidget()
        self.t_active_fusions.setHeaderLabels([_('Wallet'), _('Status'), _('Status Extra')])
        self.t_active_fusions.setContextMenuPolicy(Qt.CustomContextMenu)
        self.t_active_fusions.customContextMenuRequested.connect(self.create_menu_active_fusions)
        self.t_active_fusions.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.t_active_fusions.itemDoubleClicked.connect(self.on_double_clicked)
        clayout.addWidget(self.t_active_fusions)

        self.resize(520, 240)  # TODO: Have this somehow not be hard-coded

    def refresh(self):
        tree = self.t_active_fusions
        reselect_fusions = set(i.data(0, Qt.UserRole)() for i in tree.selectedItems())
        reselect_fusions.discard(None)
        reselect_items = []
        tree.clear()
        fusions_and_times = sorted(self.plugin.fusions.items(), key=lambda x:x[1], reverse=True)
        for fusion,t in fusions_and_times:
            wname = fusion.target_wallet.diagnostic_name()
            status, status_ext = fusion.status
            item = QTreeWidgetItem( [ wname, status, status_ext] )
            item.setToolTip(0, wname)  # this doesn't always fit in the column
            item.setToolTip(2, status_ext or '')  # neither does this
            item.setData(0, Qt.UserRole, weakref.ref(fusion))
            if fusion in reselect_fusions:
                reselect_items.append(item)
            tree.addTopLevelItem(item)
        for item in reselect_items:
            item.setSelected(True)

    def create_menu_active_fusions(self, position):
        selected = self.t_active_fusions.selectedItems()
        if not selected:
            return

        fusions = set(i.data(0, Qt.UserRole)() for i in selected)
        fusions.discard(None)
        statuses = set(f.status[0] for f in fusions)
        selection_of_1_fusion = list(fusions)[0] if len(fusions) == 1 else None
        has_live = 'running' in statuses or 'waiting' in statuses

        menu = QMenu()
        def cancel():
            for fusion in fusions:
                fusion.stop(_('Stop requested by user'))
        if has_live:
            if 'running' in statuses:
                msg = _('Cancel (at end of round)')
            else:
                msg = _('Cancel')
            menu.addAction(msg, cancel)
        if selection_of_1_fusion and selection_of_1_fusion.txid:
            menu.addAction(_("View tx"), lambda: self._open_tx_for_fusion(selection_of_1_fusion))
        if not menu.isEmpty():
            menu.exec_(self.t_active_fusions.viewport().mapToGlobal(position))

    def on_double_clicked(self, item, column):
        self._open_tx_for_fusion( item.data(0, Qt.UserRole)() )

    def _open_tx_for_fusion(self, fusion):
        if not fusion or not fusion.txid or not fusion.target_wallet:
            return
        wallet = fusion.target_wallet
        window = wallet.weak_window and wallet.weak_window()
        txid = fusion.txid
        if window:
            tx = window.wallet.transactions.get(txid)
            if tx:
                window.show_transaction(tx, wallet.get_label(txid))
            else:
                window.show_error(_("Transaction not yet in wallet"))