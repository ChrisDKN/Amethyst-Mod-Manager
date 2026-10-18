from PySide6.QtWidgets import QMenu


class _StayOpenMenu(QMenu):
    """A QMenu that stays open when a checkable action is toggled, so the user
    can flip several column/filter options without re-opening it each time.
    Non-checkable actions (and submenu navigation) behave normally."""

    def mouseReleaseEvent(self, event):
        act = self.activeAction()
        if act is not None and act.isEnabled() and act.isCheckable():
            act.trigger()          # flips the check + fires toggled
            return                 # ...but DON'T let the menu close
        super().mouseReleaseEvent(event)
