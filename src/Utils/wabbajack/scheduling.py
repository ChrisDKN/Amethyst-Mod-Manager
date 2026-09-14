from Utils.downloads.resources import InstallResources as SharedInstallResources
from .diagnostics import emit


class InstallResources(SharedInstallResources):
    def __init__(self, workers, request, acquisition, priorities, *, log=None):
        super().__init__(
            workers, request.downloads, request.directory, acquisition.resource_snapshot,
            acquisition.report.download_bytes,
            {acquisition.ids[key]: -priority[1] for key, priority in priorities.items()},
            blocked=lambda: acquisition.budget is not None and acquisition.budget.waiting,
            on_event=lambda event, **fields: emit(log, event, **fields))
