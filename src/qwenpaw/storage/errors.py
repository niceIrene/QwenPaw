# -*- coding: utf-8 -*-
"""Failures that must never silently select another storage backend."""


class StorageError(RuntimeError):
    """Base storage failure."""


class StorageIdentityError(StorageError):
    """The namespace belongs to another deployment or dataset."""


class StorageMaintenanceError(StorageError):
    """The stored dataset is inactive or a handle has stale identity."""


class StorageConflictError(StorageError):
    """A revision or unique identity conflicts with stored state."""
