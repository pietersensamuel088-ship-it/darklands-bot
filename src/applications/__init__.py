"""
applications package – Discord application bot replica for Darklands SMP.
"""
from applications.commands import ApplyCommands
from applications.session import ApplicationManager, application_manager
from applications.views import (
    ApplicationDenyModal,
    ApplicationLaunchView,
    ApplicationReviewView,
    build_application_embed,
    open_ticket_for_application,
)

__all__ = [
    "ApplyCommands",
    "ApplicationManager",
    "application_manager",
    "ApplicationLaunchView",
    "ApplicationReviewView",
    "ApplicationDenyModal",
    "build_application_embed",
    "open_ticket_for_application",
]
