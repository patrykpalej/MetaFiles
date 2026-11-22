"""Main window: places sidebar on the left, tabs of the active project, project sidebar on the right."""

import os

from gi.repository import Adw, Gdk, Gio, GLib, Gtk

from metafiles import APP_ID, APP_NAME, dialogs, fileutil, menus
from metafiles.fileactions import FileActionsMixin
from metafiles.i18n import _
from metafiles.pathbar import PathBar
from metafiles.places import PlacesSidebar
from metafiles.project import ProjectView
from metafiles.sidebar import SIDEBAR_KEYS, ProjectSidebar
from metafiles.storage import ProjectState


def app_brand():
    """Icon and name of the application, shown at the left of the header bar."""
    box = Gtk.Box(spacing=8, margin_start=4, margin_end=6, valign=Gtk.Align.CENTER)
    box.append(Gtk.Image(icon_name=APP_ID, pixel_size=20))
    label = Gtk.Label(label=APP_NAME)
    label.add_css_class("heading")
    box.append(label)
    return box


class MainWindow(FileActionsMixin, Adw.ApplicationWindow):
    def __init__(self, app, session):
        super().__init__(application=app, title=APP_NAME)
        self.app = app
        self.session = session
        self._closing = False
        self._tab_handlers = []
        self._project_handler = None
        self._current_tab = None
        self._pane_handlers = []
        self._active_pane = None

        self.set_default_size(session.window_width, session.window_height)
        self.set_size_request(480, 360)
        self.set_icon_name(APP_ID)
        if session.maximized:
            self.maximize()
        self.connect("close-request", self._on_close_request)
        self.connect("notify::focus-widget", self._on_focus_changed)

        self._build()
        self._build_actions()
        for project in session.projects:
            self._add_view(project)
        self.sidebar.sync_empty_groups()
        views = self.sidebar.views
        if views:
            self.sidebar.select_index(min(session.active_project, len(views) - 1))
        self.places.set_mode(session.places_mode)
        self.refresh_actions()
        self._update_undo()
        app.ops.connect("undo-changed", lambda *_: self._update_undo())
        app.ops.connect("progress-changed", lambda *_: self._update_progress())
        app.ops.connect("changed", self._on_ops_changed)

    # --- layout ----------------------------------------------------------------

    def _build(self):
        self.toasts = Adw.ToastOverlay()
        self.outer = Adw.OverlaySplitView(sidebar_position=Gtk.PackType.END, min_sidebar_width=220,
                                          max_sidebar_width=420, sidebar_width_fraction=0.22,
                                          enable_show_gesture=False, enable_hide_gesture=False)
        self.inner = Adw.OverlaySplitView(min_sidebar_width=180, max_sidebar_width=300, sidebar_width_fraction=0.2,
                                          enable_show_gesture=False, enable_hide_gesture=False)
        self.outer.set_content(self.inner)
        self.toasts.set_child(self.outer)
        self.set_content(self.toasts)

        # Places sidebar (left), with the window title like Nautilus.
        places_view = Adw.ToolbarView()
        places_header = Adw.HeaderBar(show_end_title_buttons=False)
        self.places_title = Adw.WindowTitle(title=APP_NAME)
        places_header.pack_start(app_brand())
        self.progress_button = Gtk.MenuButton(icon_name="emblem-synchronizing-symbolic", visible=False,
                                              tooltip_text=_("Operations"))
        places_header.pack_end(self.progress_button)
        places_view.add_top_bar(places_header)
        self.places = PlacesSidebar(self.app)
        self.places.connect("open", lambda _p, uri, new: self.open_location(uri, new_tab=new))
        self.places.connect("menu", self._on_places_menu)
        self.places.connect("drop-files", lambda _p, uri, uris, move: self.drop_files(uris, uri, move))
        places_view.set_content(self.places)
        places_view.add_css_class("mf-places-view")
        self.inner.set_sidebar(places_view)
        self.inner.set_show_sidebar(self.session.places_visible)
        self.inner.connect("notify::show-sidebar", lambda *_: self._sync_header_buttons())

        # Content: header bar (Nautilus toolbar), tab bar and the projects' tab views.
        content = Adw.ToolbarView(top_bar_style=Adw.ToolbarStyle.RAISED)
        self.header = Adw.HeaderBar(show_title=True)
        self.places_toggle = Gtk.ToggleButton(icon_name="sidebar-show-symbolic", tooltip_text=_("Show Sidebar"))
        self.places_toggle.set_active(self.session.places_visible)
        self.places_toggle.connect("toggled", lambda b: self.set_places_visible(b.get_active()))
        self.brand_hidden_sidebar = app_brand()
        self.header.pack_start(self.brand_hidden_sidebar)
        self.header.pack_start(self.places_toggle)
        history = Gtk.Box()
        history.add_css_class("linked")
        self.back_button = Gtk.Button(icon_name="go-previous-symbolic", tooltip_text=_("Back"), action_name="win.back")
        self.forward_button = Gtk.Button(icon_name="go-next-symbolic", tooltip_text=_("Forward"),
                                         action_name="win.forward")
        for button in (self.back_button, self.forward_button):
            button.add_css_class("flat")
        history.append(self.back_button)
        history.append(self.forward_button)
        self.header.pack_start(history)

        self.pathbar = PathBar()
        self.pathbar.connect("navigate", lambda _b, uri: self.active_pane and self.active_pane.navigate(uri))
        self.pathbar.connect("current-menu", lambda _b, button: self.show_context_menu(
            self.active_pane, None, None, background=True, anchor=button))
        self.pathbar.connect("drop-files", lambda _b, uri, uris, move: self.drop_files(uris, uri, move))
        self.pathbar.connect("search", lambda _b, text: self._on_search_text(text))
        self.pathbar.connect("search-closed", lambda *_: self._on_search_closed())
        self.pathbar.connect("location-closed", lambda *_: self.active_pane and self.active_pane.focus_view())
        self.header.set_title_widget(self.pathbar)

        self.menu_button = Gtk.MenuButton(icon_name="open-menu-symbolic", tooltip_text=_("Main Menu"),
                                          primary=True)
        self.menu_button.set_menu_model(self._main_menu())
        self.header.pack_end(self.menu_button)
        self.view_button = Adw.SplitButton(icon_name="view-grid-symbolic", tooltip_text=_("View Options"),
                                           action_name="win.toggle-view-mode")
        self.view_button.connect("notify::active", lambda b, _p: None)
        self.view_button.set_popover(Gtk.PopoverMenu())
        self.view_button.get_popover().connect("show", lambda *_: self._fill_view_menu())
        self.header.pack_end(self.view_button)
        self.search_button = Gtk.ToggleButton(icon_name="edit-find-symbolic", tooltip_text=_("Search Current Folder"))
        self.search_button.connect("toggled", self._on_search_toggled)
        self.header.pack_end(self.search_button)
        self.split_button = Gtk.ToggleButton(icon_name="view-dual-symbolic", tooltip_text=_("Split View (F3)"))
        self.split_button.connect("clicked", lambda *_: self.activate_action("win.toggle-split", None))
        self.header.pack_end(self.split_button)
        self.projects_toggle = Gtk.ToggleButton(icon_name="view-right-pane-symbolic",
                                                tooltip_text=_("Show/Hide Projects"))
        self.projects_toggle.set_active(self.session.sidebar_visible)
        self.projects_toggle.connect("toggled", lambda b: self.set_projects_visible(b.get_active()))
        self.header.pack_end(self.projects_toggle)
        content.add_top_bar(self.header)

        self.tab_bar = Adw.TabBar(autohide=True, expand_tabs=True)
        dbl = Gtk.GestureClick(button=Gdk.BUTTON_PRIMARY)
        dbl.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        dbl.connect("pressed", self._on_tab_bar_click)
        self.tab_bar.add_controller(dbl)
        # Dropping files on a tab puts them into that tab's folder.
        self.tab_bar.setup_extra_drop_target(Gdk.DragAction.COPY | Gdk.DragAction.MOVE, [Gdk.FileList])
        self.tab_bar.connect("extra-drag-drop", self._on_tab_drop)
        content.add_top_bar(self.tab_bar)

        self.stack = Gtk.Stack(vexpand=True, hexpand=True)
        self.empty = Adw.StatusPage(icon_name="folder-symbolic", title=_("No Projects"))
        self.stack.add_named(self.empty, "__empty__")
        content.set_content(self.stack)
        content.add_css_class("view")
        self.inner.set_content(content)

        # Projects sidebar (right).
        projects_view = Adw.ToolbarView()
        projects_header = Adw.HeaderBar(show_start_title_buttons=False, show_title=False)
        buttons = Gtk.Box(spacing=6, homogeneous=True, hexpand=True)
        buttons.add_css_class("mf-sidebar-actions")
        for icon, text, action, tip in (
            ("folder-new-symbolic", _("Add project"), "add-project", _("Add a directory as a project")),
            ("view-list-symbolic", _("New group"), "new-group", _("New project group")),
        ):
            inner = Gtk.Box(spacing=6, halign=Gtk.Align.CENTER)
            inner.append(Gtk.Image.new_from_icon_name(icon))
            inner.append(Gtk.Label(label=f"{text} ({SIDEBAR_KEYS[action]})"))
            button = Gtk.Button(action_name=f"win.{action}", tooltip_text=tip, child=inner)
            button.add_css_class("flat")
            buttons.append(button)
        projects_header.set_title_widget(buttons)
        projects_header.set_show_title(True)
        projects_view.add_top_bar(projects_header)
        self.sidebar = ProjectSidebar(self.session, lambda group: self.choose_project(group))
        self.sidebar.connect("project-selected", lambda _s, view: self._on_project_selected(view))
        self.sidebar.connect("project-activated", lambda *_: self.focus_files())
        self.sidebar.connect("row-menu", lambda _s, row, x, y: self._show_project_menu(row, x, y))
        self.sidebar.connect("group-menu", lambda _s, name, widget, x, y: self._show_group_menu(name, widget, x, y))
        self.sidebar.connect("escape", lambda *_: self.focus_files())
        self.sidebar.connect("project-moved", lambda _s, view, group, before: self.move_project_to(view, group, before))
        self.sidebar.connect("drop-files", lambda _s, view, uris, move: self.drop_files(uris, view.root_uri, move))
        projects_view.set_content(self.sidebar)
        projects_view.add_css_class("mf-projects-view")
        self.outer.set_sidebar(projects_view)
        self.outer.set_show_sidebar(self.session.sidebar_visible)
        self.outer.connect("notify::show-sidebar", lambda *_: self._sync_header_buttons())
        self._sync_header_buttons()

    def _sync_header_buttons(self):
        # Window buttons go to the right-most header bar.
        projects = self.outer.get_show_sidebar()
        self.header.set_show_end_title_buttons(not projects)
        self.places_toggle.set_active(self.inner.get_show_sidebar())
        # Like Nautilus: the sidebar button shows only while the sidebar is hidden.
        self.places_toggle.set_visible(not self.inner.get_show_sidebar())
        self.brand_hidden_sidebar.set_visible(not self.inner.get_show_sidebar())
        self.projects_toggle.set_active(projects)
        self.header.set_show_start_title_buttons(not self.inner.get_show_sidebar())

    def _main_menu(self):
        menu = Gio.Menu()
        section = Gio.Menu()
        section.append(_("New _Tab"), "win.new-tab")
        section.append(_("_Add Project…"), "win.add-project")
        section.append(_("New Project _Group…"), "win.new-group")
        menu.append_section(None, section)
        undo = Gio.Menu()
        ops = self.app.ops
        undo.append(ops.undo_label() if ops.can_undo() else _("_Undo"), "win.undo")
        undo.append(_("_Redo"), "win.redo")
        menu.append_section(None, undo)
        panels = Gio.Menu()
        panels.append(_("_Quick Open…"), "win.quick-open")
        panels.append(_("Split _View"), "win.toggle-split")
        panels.append(_("_Preview Panel"), "win.toggle-preview")
        panels.append(_("_Terminal Panel"), "win.toggle-terminal")
        panels.append(_("Status _Bar"), "win.toggle-statusbar")
        menu.append_section(None, panels)
        app = Gio.Menu()
        app.append(_("_Preferences"), "win.preferences")
        app.append(_("_Keyboard Shortcuts"), "win.show-shortcuts")
        app.append(_("_About MetaFiles"), "win.about")
        menu.append_section(None, app)
        return menu

    # --- actions -------------------------------------------------------------------

    def _build_actions(self):
        pane = lambda fn: lambda: self.active_pane is not None and fn(self.active_pane)  # noqa: E731
        project = lambda fn: lambda: self.current_view is not None and fn(self.current_view)  # noqa: E731
        tab = lambda fn: lambda: self.current_tab is not None and fn(self.current_tab)  # noqa: E731
        simple = {
            # files
            "open": self.open_selection, "open-new-tab": lambda: self.open_selection(new_tab=True),
            "open-as-project": self.open_selection_as_project, "open-item-location": self.open_item_location,
            "open-terminal": lambda: self.open_terminal(), "context-menu": self.context_menu_from_keyboard,
            "new-folder": self.new_folder, "new-file": self.new_file, "cut": lambda: self.copy_selection(cut=True),
            "copy": self.copy_selection, "paste": self.paste, "rename": self.rename_selection,
            "duplicate": self.duplicate_selection, "trash": self.trash_selection,
            "delete-permanently": self.delete_selection, "link-to-copied": self.link_to_copied,
            "link-to-selected": self.link_to_selected, "properties": self.show_properties,
            "undo": self.app.ops.undo, "redo": self.app.ops.redo, "bookmark": self.bookmark_current,
            "copy-full-path": lambda: self.copy_paths("full"), "copy-relative-path": lambda: self.copy_paths("relative"),
            # view
            "zoom-in": pane(lambda p: p.zoom(+1)), "zoom-out": pane(lambda p: p.zoom(-1)),
            "zoom-reset": pane(lambda p: p.zoom(0)),
            "view-list": pane(lambda p: p.set_view_mode("tree" if self.app.nautilus.get("list-view", "use-tree-view")
                                                          else "list")),
            "view-grid": pane(lambda p: p.set_view_mode("grid")),
            "toggle-view-mode": pane(lambda p: p.set_view_mode("list" if p.view_mode == "grid" else "grid")),
            "reload": pane(lambda p: p.reload()), "show-hidden": self.toggle_hidden,
            "toggle-places": lambda: self.set_places_visible(not self.inner.get_show_sidebar()),
            "toggle-statusbar": self.toggle_statusbar,
            # selection
            "select-all": self.select_all, "invert-selection": pane(lambda p: p.invert_selection()),
            "select-matching": lambda: dialogs.select_matching(self, lambda text: self.active_pane.select_matching(text)),
            # navigation
            "back": pane(lambda p: p.go_back()), "forward": pane(lambda p: p.go_forward()),
            "up": pane(lambda p: p.go_up()), "home": pane(lambda p: p.go_home()),
            "location": lambda: self.pathbar.show_location(),
            "location-root": lambda: self.pathbar.show_location("/" if not self._locked() else "~/"),
            "location-home": lambda: self.pathbar.show_location("~/"),
            "search": lambda: self.start_search(), "search-project": lambda: self.start_search(whole_project=True),
            # tabs
            "new-tab": self.new_tab, "close-tab": tab(lambda t: self.current_view.close_tab(t)),
            "restore-tab": project(lambda v: v.restore_closed_tab()),
            "prev-tab": project(lambda v: v.cycle(-1)), "next-tab": project(lambda v: v.cycle(+1)),
            "tab-move-left": project(lambda v: v.move_current(-1)),
            "tab-move-right": project(lambda v: v.move_current(+1)), "rename-tab": self.rename_tab,
            # projects
            "add-project": lambda: self.choose_project(),
            "new-group": lambda: self.create_group([self.current_view] if self.current_view and not self.current_view.loose else []),
            "prev-project": lambda: self.sidebar.cycle(-1), "next-project": lambda: self.sidebar.cycle(+1),
            "switch-to-loose": lambda: self.sidebar.select_index(0),
            "toggle-projects": lambda: self.set_projects_visible(not self.outer.get_show_sidebar()),
            "toggle-focus": self.toggle_focus,
            # extra features
            "quick-open": self.quick_open, "toggle-preview": tab(lambda t: t.toggle_preview()),
            "toggle-split": tab(lambda t: t.toggle_split()), "switch-pane": tab(lambda t: t.switch_pane()),
            "copy-to-other-pane": lambda: self.transfer_to_other_pane(move=False),
            "move-to-other-pane": lambda: self.transfer_to_other_pane(move=True),
            "toggle-terminal": self.toggle_terminal, "toggle-git-ignored": self.toggle_git_ignored,
            "quick-preview": self.quick_preview,
            # application
            "main-menu": lambda: self.menu_button.popup(), "preferences": self.show_preferences,
            "show-shortcuts": self.show_shortcuts, "about": self.show_about, "quit": self.request_quit,
        }
        for i in range(1, 10):
            simple[f"tab-{i}"] = (lambda i: lambda: self.current_view and self.current_view.select_index(i - 1))(i)
            # Project numbers skip the general entry at row 0.
            simple[f"switch-to-project-{i}"] = (lambda i: lambda: self.sidebar.select_index(i))(i)
        self.actions = {}
        for name, callback in simple.items():
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", (lambda cb: lambda *_: cb())(callback))
            self.add_action(action)
            self.actions[name] = action
        self._custom_action_names = []

    def refresh_actions(self):
        """(Re)register custom actions so their shortcuts work."""
        for name in self._custom_action_names:
            self.remove_action(name)
        self._custom_action_names = []
        for custom in self.app.custom_actions:
            name = f"custom-{custom.id}"
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", lambda *_a, c=custom: self.run_custom_action(c, from_shortcut=True))
            self.add_action(action)
            self._custom_action_names.append(name)
        self.app.apply_accels()
        self._update_empty_hint()

    def _locked(self):
        view = self.current_view
        return view is not None and view.lock

    def accel_for(self, action):
        accels = self.app.get_accels_for_action(f"win.{action}")
        return accels[0] if accels else None

    # --- projects ------------------------------------------------------------------

    @property
    def views(self):
        return self.sidebar.views

    @property
    def current_view(self):
        return self.sidebar.selected_view()

    @property
    def current_tab(self):
        view = self.current_view
        return view.current if view is not None else None

    @property
    def active_pane(self):
        tab = self.current_tab
        return tab.active if tab is not None else None

    def _add_view(self, state):
        view = ProjectView(self.app, state)
        view.connect("changed", self._on_view_changed)
        view.connect("tab-changed", lambda v: v is self.current_view and self._on_tab_switched())
        view.connect("selection-changed", lambda v: v is self.current_view and self._on_selection_changed())
        self.stack.add_child(view)
        self.sidebar.add_view(view)
        return view

    def add_project(self, path, group="", select=None):
        root = fileutil.real(path)
        if not os.path.isdir(root):
            self.show_error(_("Not a directory: {path}").format(path=root))
            return None
        for view in self.views:
            if not view.loose and view.root == root:
                self.sidebar.select_view(view)
                if select and view.current:
                    view.current.active.select_uris(select)
                return view
        state = ProjectState(path=root, name=os.path.basename(root) or root, group=group)
        if select:
            from metafiles.storage import new_tab_state

            state.tabs = [new_tab_state(Gio.File.new_for_path(root).get_uri())]
        self.session.projects.append(state)
        self.session.ensure_loose()
        view = self._add_view(state)
        self.sidebar.resort()
        self.sidebar.select_view(view)
        if select and view.current:
            GLib.timeout_add(300, lambda: view.current.active.select_uris(select) and False)
        self.app.save_session()
        return view

    def choose_project(self, group=""):
        dialog = Gtk.FileDialog(title=_("Choose Project Directory"), accept_label=_("_Add"))
        view = self.current_view
        start = os.path.dirname(view.root) if view and not view.loose else fileutil.HOME
        dialog.set_initial_folder(Gio.File.new_for_path(start))

        def done(d, result):
            try:
                folder = d.select_folder_finish(result)
            except GLib.Error:
                return
            if folder is not None and folder.get_path():
                self.add_project(folder.get_path(), group)

        dialog.select_folder(self, None, done)

    def open_unassigned(self, uri):
        """Open a location outside the project in the general entry."""
        loose = self.views[0]
        self.sidebar.select_view(loose)
        loose.ensure_started()
        loose.new_tab(location=uri)

    def _on_project_selected(self, view):
        self.stack.set_visible_child(view)
        view.ensure_started()
        row = self.sidebar.row_for(view)
        if row is not None:
            self._refresh_row(row)
        self.tab_bar.set_view(view.tab_view)
        self.places.set_project(view)
        self.session.active_project = self.views.index(view)
        self._on_tab_switched()
        if not self.sidebar.has_focus_inside():
            GLib.idle_add(lambda: self.focus_files() and False)

    def _refresh_row(self, row):
        repo = None if row.view.loose else self.app.git.get(row.view.root)
        branch = None
        if repo is not None and repo.status is not None:
            branch = ("● " if repo.status.dirty else "") + repo.status.branch_label()
        if repo is not None and not getattr(row, "git_connected", False):
            repo.connect("changed", lambda *_: self._refresh_row(row))
            row.git_connected = True
        row.refresh(branch)

    def _on_view_changed(self, view):
        row = self.sidebar.row_for(view)
        if row is not None:
            self._refresh_row(row)
        if view is self.current_view:
            self._update_title()

    def _on_tab_switched(self):
        tab = self.current_tab
        for obj, handler in self._tab_handlers:
            obj.disconnect(handler)
        self._tab_handlers = []
        self._current_tab = tab
        if tab is not None:
            self._tab_handlers.append((tab, tab.connect("changed", lambda *_: self._on_active_pane_changed())))
        self._on_active_pane_changed()

    def _on_active_pane_changed(self):
        pane = self.active_pane
        for obj, handler in self._pane_handlers:
            obj.disconnect(handler)
        self._pane_handlers = []
        self._active_pane = pane
        view = self.current_view
        if view is None:
            self.stack.set_visible_child_name("__empty__")
        if pane is not None:
            for signal in ("location-changed", "search-changed", "view-changed"):
                self._pane_handlers.append((pane, pane.connect(signal, lambda *_: self._update_header())))
            self._pane_handlers.append((pane, pane.connect("status-changed", lambda *_: self._update_header_light())))
        self._update_header()
        self._on_selection_changed()

    def _update_header_light(self):
        pane = self.active_pane
        if pane is None:
            return
        self.actions["back"].set_enabled(pane.can_go_back())
        self.actions["forward"].set_enabled(pane.can_go_forward())
        self.actions["up"].set_enabled(pane.can_go_up())

    def _update_header(self):
        pane = self.active_pane
        tab = self.current_tab
        self._update_title()
        if pane is None:
            self.pathbar.set_sensitive(False)
            return
        self.pathbar.set_sensitive(True)
        self.pathbar.update(pane)
        if pane.search_text:
            if not self.pathbar.searching:
                self.pathbar.show_search(pane.search_text)
            self.search_button.set_active(True)
        elif self.pathbar.searching:
            self.pathbar.set_visible_child_name("path")
            self.search_button.set_active(False)
        self.view_button.set_icon_name("view-list-symbolic" if pane.view_mode == "grid" else "view-grid-symbolic")
        self.view_button.set_tooltip_text(_("List View") if pane.view_mode == "grid" else _("Grid View"))
        self.split_button.set_active(tab is not None and tab.is_split)
        self._update_header_light()
        self.places.highlight(pane.location)
        pane.status.set_visible(self.session.statusbar_visible)
        if tab is not None and tab.is_split:
            for other in tab.panes:
                other.status.set_visible(self.session.statusbar_visible)

    def _update_title(self):
        view = self.current_view
        pane = self.active_pane
        if view is None:
            self.set_title(APP_NAME)
            self.places_title.set_title(APP_NAME)
            self.places_title.set_subtitle("")
            return
        title = pane.title() if pane is not None else view.name
        self.set_title(f"{title} — {view.name}" if not view.loose else title)
        self.places_title.set_title(view.name)
        self.places_title.set_subtitle(fileutil.short_path(view.root) if not view.loose else "")

    def _on_selection_changed(self):
        pane = self.active_pane
        has = bool(pane and pane.selected_positions())
        for name in ("cut", "copy", "rename", "trash", "delete-permanently", "duplicate", "properties",
                     "link-to-selected", "copy-full-path", "copy-relative-path", "open", "open-new-tab"):
            self.actions[name].set_enabled(has or name == "properties")

    # --- tabs --------------------------------------------------------------------------

    def new_tab(self):
        view = self.current_view
        if view is None:
            self.choose_project()
            return
        view.ensure_started()
        view.new_tab(after_current=True)

    def open_in_new_tab(self, uri, background=False):
        view = self.current_view
        if view is None:
            return
        if not view.allows(uri):
            view.refuse_navigation(uri)
            return
        view.new_tab(location=uri, background=background, after_current=True)

    def open_location(self, uri, new_tab=False):
        if new_tab:
            self.open_in_new_tab(uri)
        elif self.active_pane is not None:
            self.active_pane.navigate(uri)
            self.active_pane.focus_view()
        elif self.current_view is not None:
            self.current_view.new_tab(location=uri)

    def _on_tab_drop(self, _bar, page, value):
        from metafiles.fileactions import drop_mode

        tab = page.get_child()
        if tab.active.path() is None:
            return False
        uris = [f.get_uri() for f in value.get_files()]
        state = self.get_display().get_default_seat().get_keyboard().get_modifier_state()
        self.drop_files(uris, tab.active.location, drop_mode(state))
        return True

    def _on_tab_bar_click(self, gesture, n_press, x, y):
        if n_press == 2:
            GLib.idle_add(lambda: self.rename_tab() and False)

    def rename_tab(self):
        tab = self.current_tab
        if tab is None:
            return

        def done(text):
            if text is None:
                return
            tab.custom_name = text.strip()
            self.current_view.refresh_labels()
            self._update_title()

        dialogs.ask_text(self, _("Rename Tab"), tab.custom_name, _("_Rename"), done,
                         body=_("An empty name restores the folder name."))
        # Allow empty names: they restore the default label.

    # --- project menus (ported from MetaTerminal) ---------------------------------------

    def _show_project_menu(self, row, x, y):
        view = row.view
        if x < 0:
            x = y = None
        entries = [
            ("item", _("New Tab"), lambda: (self.sidebar.select_view(view), view.new_tab()), view.available),
            ("item", _("Rename…"), lambda: self._rename_project(view)),
        ]
        if not view.loose:
            entries += [
                ("check", _("Lock navigation to the project directory"), view.state.lock,
                 lambda: self._set_lock(view, not view.state.lock)),
                ("section", [
                    ("item", _("Open in MetaTerminal"), lambda: self.open_in_metaterminal(view.root),
                     self._has("metaterminal")),
                    ("item", _("Open in Nautilus"), lambda: self.open_in_nautilus([view.root_uri], folders=True)),
                ] + self._custom_entries("project", [], view.root, view)),
                ("section", [
                    ("submenu", _("Group"), [
                        ("radio", label, view.state.group == value, lambda value=value: self._set_group(view, value))
                        for label, value in [(_("No group"), "")] + [(g, g) for g in self.session.groups]
                    ] + [("section", [("item", _("New group…"), lambda: self.create_group([view]))])]),
                    ("item", _("Move up"), lambda: self._move_project(view, -1), self._can_move(view, -1)),
                    ("item", _("Move down"), lambda: self._move_project(view, +1), self._can_move(view, +1)),
                ]),
                ("section", [("item", _("Close project"), lambda: self._close_project(view))]),
            ]
        menus.popup(row, entries, x, y)

    def _rename_project(self, view):
        def done(name):
            if name and name.strip():
                view.state.name = name.strip()
                self._on_view_changed(view)
                self.app.save_session()
        dialogs.ask_text(self, _("Rename Project"), view.state.name, _("_Rename"), done)

    def _set_lock(self, view, enabled):
        view.set_lock(enabled)
        self._on_view_changed(view)
        self.places.rebuild()
        self._update_header()
        self.app.save_session()

    def _resort(self):
        self.sidebar.resort()
        self.app.save_session()

    def _set_group(self, view, group):
        if view.state.group == group:
            return
        view.state.group = group
        if group and group not in self.session.groups:
            self.session.groups.append(group)
        self.session.projects.remove(view.state)
        self.session.projects.append(view.state)
        self._resort()

    def move_project_to(self, view, group, before=None):
        """Put a project into `group`, just before the ProjectState `before` (None: at the end)."""
        if view.loose:
            return
        projects = self.session.projects
        if before is view.state:
            index = projects.index(before) + 1
            before = projects[index] if index < len(projects) else None
        if group and group not in self.session.groups:
            self.session.groups.append(group)
        view.state.group = group
        projects.remove(view.state)
        projects.insert(projects.index(before) if before in projects else len(projects), view.state)
        self._resort()

    def create_group(self, preselected=()):
        dialog = Adw.AlertDialog(heading=_("New Project Group"))
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        entry = Gtk.Entry(activates_default=True, width_chars=32, placeholder_text=_("Group name"))
        content.append(entry)
        projects = [v for v in self.views if not v.loose]
        checks = []
        if projects:
            content.append(Gtk.Label(label=_("Move into the group (optional):"), xalign=0, margin_top=6))
            column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            for view in projects:
                suffix = f"  ({view.state.group})" if view.state.group else ""
                check = Gtk.CheckButton(label=view.state.name + suffix, active=view in preselected)
                checks.append((check, view))
                column.append(check)
            content.append(Gtk.ScrolledWindow(child=column, hscrollbar_policy=Gtk.PolicyType.NEVER,
                                              propagate_natural_height=True, max_content_height=320))
        hint = Gtk.Label(label=_("A group may be empty. Add projects later with the button in the group or from the project menu."),
                         xalign=0, wrap=True, max_width_chars=44)
        hint.add_css_class("dim-label")
        content.append(hint)
        dialog.set_extra_child(content)
        dialog.add_response("cancel", _("_Cancel"))
        dialog.add_response("create", _("_Create"))
        dialog.set_response_appearance("create", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("create")
        dialog.set_close_response("cancel")
        dialog.set_response_enabled("create", False)
        entry.connect("changed", lambda e: dialog.set_response_enabled("create", bool(e.get_text().strip())))

        def respond(_d, response):
            name = entry.get_text().strip()
            if response != "create" or not name:
                return
            if name not in self.session.groups:
                self.session.groups.append(name)
            for check, view in checks:
                if check.get_active():
                    self._set_group(view, name)
            self._resort()

        dialog.connect("response", respond)
        dialog.present(self)
        GLib.idle_add(lambda: entry.grab_focus() and False)

    def _show_group_menu(self, name, widget, x, y):
        groups = self.session.groups
        index = groups.index(name) if name in groups else -1
        menus.popup(widget, [
            ("item", _("Add project to group…"), lambda: self.choose_project(name)),
            ("item", _("Rename group…"), lambda: self._rename_group(name)),
            ("section", [
                ("item", _("Move group up"), lambda: self._move_group(name, -1), index > 0),
                ("item", _("Move group down"), lambda: self._move_group(name, +1), 0 <= index < len(groups) - 1),
            ]),
            ("section", [("item", _("Dissolve group (projects stay without a group)"),
                          lambda: self._dissolve_group(name))]),
        ], x, y)

    def _rename_group(self, old):
        def done(new):
            new = (new or "").strip()
            if not new or new == old:
                return
            groups = self.session.groups
            if new in groups:
                groups.remove(old)
            else:
                groups[groups.index(old)] = new
            for project in self.session.projects:
                if project.group == old:
                    project.group = new
            self._resort()
        dialogs.ask_text(self, _("Rename Group"), old, _("_Rename"), done)

    def _move_group(self, name, step):
        groups = self.session.groups
        i = groups.index(name)
        j = i + step
        if 0 <= j < len(groups):
            groups[i], groups[j] = groups[j], groups[i]
            self._resort()

    def _dissolve_group(self, name):
        for project in self.session.projects:
            if project.group == name:
                project.group = ""
        if name in self.session.groups:
            self.session.groups.remove(name)
        self._resort()

    def _can_move(self, view, step):
        projects = self.session.projects
        j = projects.index(view.state) + step
        return not view.loose and 0 <= j < len(projects) and not projects[j].loose and projects[j].group == view.state.group

    def _move_project(self, view, step):
        if not self._can_move(view, step):
            return
        projects = self.session.projects
        i = projects.index(view.state)
        projects[i], projects[i + step] = projects[i + step], projects[i]
        self._resort()

    def _close_project(self, view):
        if view.loose:
            return
        running = view.running_process_count() + len(self.app.ops.running)

        def close(ok=True):
            if not ok:
                return
            index = self.views.index(view)
            view.close_all()
            self.sidebar.remove_view(view)
            self.stack.remove(view)
            self.session.projects.remove(view.state)
            self.session.ensure_loose()
            self.sidebar.resort()
            views = self.views
            if views:
                self.sidebar.select_index(min(index, len(views) - 1))
            self.app.save_session()

        if running:
            self.confirm_async(_("Close project “{name}”?").format(name=view.state.name),
                               _("Something is still running (a file operation or a terminal process). "
                                 "The project leaves the list; its directory on disk stays untouched."),
                               _("_Close"), close)
        else:
            close()

    # --- places menu ----------------------------------------------------------------------

    def _on_places_menu(self, _places, row, x, y):
        entries = []
        if row.uri and row.kind != "device" or (row.kind == "device" and row.uri):
            entries.append(("item", _("Open in New Tab"), lambda: self.open_location(row.uri, new_tab=True)))
        if row.kind == "bookmark":
            entries.append(("section", [
                ("item", _("Rename…"), lambda: dialogs.ask_text(
                    self, _("Rename Bookmark"), row.label_text, _("_Rename"),
                    lambda text: text is not None and self.app.bookmarks.rename(row.uri, text.strip()))),
                ("item", _("Remove from Bookmarks"), lambda: self.app.bookmarks.remove(row.uri)),
            ]))
        if row.kind == "project-bookmark":
            path = Gio.File.new_for_uri(row.uri).get_path()
            entries.append(("section", [("item", _("Remove from Project Bookmarks"),
                                         lambda: self._remove_project_bookmark(path))]))
        if row.kind == "trash":
            entries.append(("section", [("item", _("Empty Trash…"), self.empty_trash)]))
        if row.kind == "device":
            if row.mount is not None:
                entries.append(("section", [
                    ("item", _("Unmount"), lambda: self.places.eject(row.mount), row.mount.can_unmount()),
                    ("item", _("Eject"), lambda: self.places.eject(row.mount), row.mount.can_eject()),
                ]))
            elif row.volume is not None:
                entries.append(("item", _("Mount"), lambda: self.places.mount_volume(row.volume)))
        if row.uri and row.uri.startswith("file://"):
            entries.append(("section", [("item", _("Properties"),
                                         lambda: self.show_properties_for([row.uri]))]))
        if entries:
            menus.popup(row, entries, x, y)

    def _remove_project_bookmark(self, path):
        view = self.current_view
        if view is not None and path in view.state.bookmarks:
            view.state.bookmarks.remove(path)
            self.places.rebuild()
            self.app.save_session()

    # --- sidebars and focus -------------------------------------------------------------------

    def set_places_visible(self, visible):
        self.inner.set_show_sidebar(visible)
        self.session.places_visible = visible

    def set_projects_visible(self, visible):
        self.outer.set_show_sidebar(visible)
        self.session.sidebar_visible = visible
        if not visible:
            self.focus_files()

    def focus_files(self):
        tab = self.current_tab
        if tab is not None:
            tab.focus()
        return False

    def toggle_focus(self):
        """Ctrl+Tab: move keyboard focus between the files and the project list."""
        if self.sidebar.has_focus_inside():
            self.focus_files()
            return
        if not self.outer.get_show_sidebar():
            self.set_projects_visible(True)
        self.sidebar.focus_selected()

    def _on_focus_changed(self, *_):
        if self.sidebar.has_focus_inside():
            self.add_css_class("mf-sidebar-focused")
        else:
            self.remove_css_class("mf-sidebar-focused")

    def toggle_statusbar(self):
        self.session.statusbar_visible = not self.session.statusbar_visible
        for view in self.views:
            for tab in view.tabs:
                for pane in tab.panes:
                    pane.status.set_visible(self.session.statusbar_visible)

    def toggle_hidden(self):
        self.app.set_show_hidden(not self.app.show_hidden)

    def refilter_all(self):
        for view in self.views:
            for tab in view.tabs:
                for pane in tab.panes:
                    pane.refilter()
        self.places.tree.refresh() if self.places.mode == "tree" else None

    def toggle_git_ignored(self):
        view = self.current_view
        if view is None:
            return
        view.state.hide_git_ignored = not view.state.hide_git_ignored
        for tab in view.tabs:
            for pane in tab.panes:
                pane.refilter()
        self.toast(_("Files ignored by Git are hidden") if view.state.hide_git_ignored
                   else _("Files ignored by Git are shown"))

    # --- toasts, dialogs -------------------------------------------------------------------------

    def toast(self, text, button=None, callback=None):
        toast = Adw.Toast(title=GLib.markup_escape_text(text), timeout=4)
        if button and callback:
            toast.set_button_label(button)
            toast.connect("button-clicked", lambda *_: callback())
        self.toasts.add_toast(toast)

    def show_error(self, title, detail=""):
        dialogs.error(self, title, detail)

    def confirm_async(self, title, body, accept, callback):
        dialogs.confirm(self, title, body, accept, callback)

    def _update_undo(self):
        ops = self.app.ops
        self.actions["undo"].set_enabled(ops.can_undo())
        self.actions["redo"].set_enabled(ops.can_redo())
        # The label says what will be undone, and when the history is Nautilus' shared one.
        self.menu_button.set_menu_model(self._main_menu())

    def _update_progress(self):
        jobs = self.app.ops.running
        self.progress_button.set_visible(bool(jobs))
        if jobs:
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin_start=12, margin_end=12,
                          margin_top=12, margin_bottom=12)
            for job in jobs:
                row = Gtk.Box(spacing=8)
                column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, hexpand=True)
                column.append(Gtk.Label(label=job.label, xalign=0))
                bar = Gtk.ProgressBar(fraction=job.fraction())
                if not job.total:
                    bar.pulse()
                column.append(bar)
                row.append(column)
                cancel = Gtk.Button(icon_name="process-stop-symbolic", tooltip_text=_("Cancel"))
                cancel.add_css_class("flat")
                cancel.connect("clicked", lambda *_a, j=job: j.cancel())
                row.append(cancel)
                box.append(row)
            popover = Gtk.Popover(child=box)
            self.progress_button.set_popover(popover)

    def _on_ops_changed(self, _ops, uris):
        for uri in uris or []:
            gf = Gio.File.new_for_uri(uri)
            path = gf.get_path()
            if path:
                repo = self.app.git.get(path)
                if repo is not None:
                    repo.refresh()
        if self.active_pane is not None and self.active_pane.location in ("trash:///",):
            self.active_pane.reload()

    def _update_empty_hint(self):
        accel = self.app.accel_label("add-project")
        self.empty.set_description(_("Add a project directory with the button in the right panel")
                                   + (_(" or with {accel}.").format(accel=accel) if accel else "."))

    # --- session -----------------------------------------------------------------------

    def snapshot(self):
        s = self.session
        s.maximized = self.is_maximized()
        if not s.maximized and not self.is_fullscreen():
            s.window_width, s.window_height = self.get_width(), self.get_height()
        s.projects = [view.snapshot() for view in self.views]
        view = self.current_view
        s.active_project = self.views.index(view) if view in self.views else 0
        s.places_visible = self.inner.get_show_sidebar()
        s.sidebar_visible = self.outer.get_show_sidebar()
        s.places_mode = self.places.mode
        return s

    def _on_close_request(self, *_):
        return not self.request_quit()

    def request_quit(self):
        if self._closing:
            return True
        running = sum(view.running_process_count() for view in self.views)
        jobs = len(self.app.ops.running)
        if running or jobs:
            def answer(ok):
                if ok:
                    self._quit_now()
            self.confirm_async(_("Quit {app}?").format(app=APP_NAME),
                               _("File operations or terminal processes are still running. Quitting will stop them."),
                               _("_Quit"), answer)
            return False
        self._quit_now()
        return True

    def _quit_now(self):
        self._closing = True
        self.app.save_session()
        for view in self.views:
            view.close_all()
        self.destroy()

    def show_about(self):
        from metafiles import VERSION

        about = Adw.AboutDialog(application_name=APP_NAME, application_icon=APP_ID, version=VERSION,
                                comments=_("Files organized into projects, in the style of GNOME Files."))
        about.present(self)

    def _has(self, feature):
        from metafiles import features

        return features.has(feature)

