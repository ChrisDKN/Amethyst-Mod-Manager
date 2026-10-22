from __future__ import annotations

import threading

from .installer_sessions import InstallerSessions
from .jobs import JobError
from .thunderstore_install import ThunderstorePlans


CONTRACTS = {
    "installers.sessions.open.start": ("archive_id game profile", "name", "Prepare an XML FOMOD wizard session without installing."),
    "installers.sessions.get": ("session_id", "", "Read visible installer steps, selections and validation errors."),
    "installers.sessions.update": ("group option revision session_id step", "", "Toggle a visible FOMOD option and recompute the wizard."),
    "installers.sessions.image.get": ("group option session_id step", "", "Read a bounded installer option image as base64 JPEG."),
    "installers.sessions.install.start": ("confirm revision session_id", "if_exists", "Revalidate and install the reviewed FOMOD selections."),
    "installers.sessions.close": ("session_id", "", "Close an installer session and remove its temporary extraction."),
    "nexus.download.manual.start": ("file_id game mod_id profile", "domain", "Wait for one browser-downloaded Nexus file and retain its exact identity."),
    "thunderstore.install.plan.start": ("game namespace package profile version", "", "Resolve a dependency-first Thunderstore installation plan."),
    "thunderstore.install.start": ("confirm game plan_id profile revision", "bain_selected bundle_selected dependencies fomod_selections if_exists installer_policy", "Apply a reviewed Thunderstore dependency plan."),
}

MUTATIONS = {"installers.sessions.open", "installers.sessions.update", "installers.sessions.install",
             "installers.sessions.close", "nexus.download.manual", "thunderstore.install"}
CANCELLABLE = ["installers.sessions.open", "nexus.download.manual", "thunderstore.install.plan", "thunderstore.install"]


class ClientWorkflows:
    def __init__(self, service):
        self.service = service
        self.installers = InstallerSessions()
        self.thunderstore = ThunderstorePlans()
        self._disconnect_controls = set()
        self._lock = threading.Lock()

    def cancel_pending(self):
        with self._lock:
            for control in self._disconnect_controls:
                control.set()
            self._disconnect_controls.clear()

    def close(self):
        self.installers.close_all()
        self.thunderstore.close()

    def _submit(self, kind, game, profile, options, runner, control=None, disconnect=False):
        service = self.service
        if disconnect:
            with self._lock:
                self._disconnect_controls = {value for value in self._disconnect_controls if not value.is_set()}
                self._disconnect_controls.add(control)
        def run(log, progress):
            try:
                return runner(log, progress)
            finally:
                with self._lock:
                    self._disconnect_controls.discard(control)
        try:
            return {"job": service._jobs.submit(kind, game, service._canonical_game(game), profile.name,
                                                 options, runner=run, cancel=control.set if control is not None else None)}
        except Exception:
            with self._lock:
                self._disconnect_controls.discard(control)
            raise

    def call(self, method, params):
        from .service import ServiceError

        service = self.service
        required, optional, _description = CONTRACTS[method]
        service._check_params(params, set((required + " " + optional).split()), set(required.split()))
        try:
            if method.startswith("installers.sessions."):
                return self._installer(method, params)
            game, profile = service._batch_profile(params)
            control = threading.Event()
            if method == "nexus.download.manual.start":
                from .nexus_manual import browser_url, runner
                _, domain = service._resolve_nexus_domain(params)
                mod_id = service._positive_integer(params, "mod_id")
                file_id = service._positive_integer(params, "file_id")
                options = {"domain": domain, "mod_id": mod_id, "file_id": file_id}
                result = self._submit("nexus.download.manual", game, profile, options,
                                      runner(service._nexus_session, game, profile, domain, mod_id, file_id, control),
                                      control, disconnect=True)
                return {**result, "browser_url": browser_url(domain, mod_id, file_id)}
            if method == "thunderstore.install.plan.start":
                from .thunderstore import package_link
                link = package_link(*(service._required_text(params, name) for name in ("namespace", "package", "version")),
                                    getattr(game, "thunderstore_community", ""))
                options = {name: params[name] for name in ("namespace", "package", "version")}
                return self._submit("thunderstore.install.plan", game, profile, options,
                                    lambda log, progress: self.thunderstore.plan(game, profile, link, log, progress, control), control)
            plan_id = service._required_text(params, "plan_id")
            revision = service._required_text(params, "revision")
            self.thunderstore.get(plan_id, revision, game, profile)
            self._confirm(params)
            options = self._install_options(params)
            dependencies = params.get("dependencies")
            if "dependencies" in params and (not isinstance(dependencies, list) or len(dependencies) > 256
                    or any(not isinstance(value, str) or not value or len(value) > 255 for value in dependencies)
                    or len(set(dependencies)) != len(dependencies)):
                raise ServiceError(-32602, "invalid_params", "'dependencies' must list up to 256 unique package IDs.")
            return self._submit("thunderstore.install", game, profile,
                                {"plan_id": plan_id, "revision": revision, "dependencies": dependencies, **options},
                                lambda log, progress: self.thunderstore.install(game, profile, plan_id, revision,
                                                                              dependencies, options, log, progress, control), control)
        except JobError as exc:
            raise ServiceError(-32012, exc.name, str(exc), exc.details) from exc

    @staticmethod
    def _confirm(params):
        from .service import ServiceError

        if params.get("confirm") is not True:
            raise ServiceError(-32602, "invalid_params", "'confirm' must be true to install the reviewed files.")

    def _install_options(self, params):
        from .service import ServiceError

        policy = params.get("installer_policy", "reject")
        if not isinstance(policy, str) or policy not in {"reject", "defaults"}:
            raise ServiceError(-32602, "invalid_params", "Choose the reject or defaults installer policy.")
        return {"if_exists": self._if_exists(params), "installer_policy": policy,
                "bain_selected": self.service._bain_selection(params, policy),
                "fomod_selections": self.service._fomod_selection(params, policy),
                "bundle_selected": self.service._bundle_selection(params, policy)}

    @staticmethod
    def _if_exists(params):
        from .service import ServiceError

        value = params.get("if_exists", "fail")
        if not isinstance(value, str) or value not in {"fail", "replace"}:
            raise ServiceError(-32602, "invalid_params", "'if_exists' must be fail or replace.")
        return value

    def _installer(self, method, params):
        service = self.service
        if method == "installers.sessions.open.start":
            game, profile = service._batch_profile(params)
            archive_id = service._archive_id(params["archive_id"])
            name = service._mod_name(params["name"], "name") if "name" in params else ""
            control = threading.Event()
            return self._submit("installers.sessions.open", game, profile, {"archive_id": archive_id, "name": name},
                                lambda log, progress: self.installers.open(game, profile, archive_id, name, log, progress, control),
                                control, disconnect=True)
        session_id = service._required_text(params, "session_id")
        if method == "installers.sessions.get":
            return self.installers.get(session_id)
        if method == "installers.sessions.close":
            return self.installers.close(session_id)
        if method in {"installers.sessions.update", "installers.sessions.image.get"}:
            step = service._integer_range(params["step"], "step", 0, 100000)
            group = service._integer_range(params["group"], "group", 0, 100000)
            option = service._required_text(params, "option")
            if method == "installers.sessions.image.get":
                return self.installers.image(session_id, step, group, option)
            return self.installers.update(session_id, service._required_text(params, "revision"), step, group, option)
        service._require_idle_state()
        revision = service._required_text(params, "revision")
        game, profile = self.installers.target(session_id, revision)
        service._require_no_managed_launch(game)
        self._confirm(params)
        if_exists = self._if_exists(params)
        return self._submit("installers.sessions.install", game, profile,
                            {"session_id": session_id, "revision": revision, "if_exists": if_exists},
                            lambda log, progress: self.installers.install(session_id, revision, if_exists, log, progress))
