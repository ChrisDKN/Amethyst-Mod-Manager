from dataclasses import dataclass, field

from Utils.mods.modlist import ModEntry


@dataclass
class ExternalModEntry(ModEntry):
    label: str = ""
    plugin_id: str = ""
    section_id: str = ""
    item_id: str = ""
    tooltip: str = ""
    values: dict = field(default_factory=dict)

    @property
    def is_external(self):
        return True

    @property
    def display_name(self):
        return self.label


def external(entry):
    return isinstance(entry, ExternalModEntry)
